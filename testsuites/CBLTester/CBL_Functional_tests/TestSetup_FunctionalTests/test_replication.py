import pytest
import time
import os
import random

from keywords.MobileRestClient import MobileRestClient
from keywords.ClusterKeywords import ClusterKeywords
from keywords import couchbaseserver
from keywords.utils import log_info
from CBLClient.Database import Database
from CBLClient.Replication import Replication
from CBLClient.Document import Document
from CBLClient.Authenticator import Authenticator
from concurrent.futures import ThreadPoolExecutor
from libraries.testkit.prometheus import verify_stat_on_prometheus
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords import document, attachment
from libraries.testkit import cluster
from utilities.cluster_config_utils import persist_cluster_config_environment_prop, copy_to_temp_conf
from keywords.attachment import generate_2_png_100_100
from keywords.SyncGateway import SyncGateway
from keywords.constants import RBAC_FULL_ADMIN


@pytest.fixture(scope="function")
def setup_teardown_test(params_from_base_test_setup):
    cbl_db_name = "cbl_db"
    base_url = params_from_base_test_setup["base_url"]
    db = Database(base_url)
    db_config = db.configure()
    log_info("Creating db")
    cbl_db = db.create(cbl_db_name, db_config)

    yield{
        "db": db,
        "cbl_db": cbl_db,
        "cbl_db_name": cbl_db_name
    }

    log_info("Deleting the db")
    db.deleteDB(cbl_db)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, continuous, x509_cert_auth", [
    pytest.param(10, True, True, marks=pytest.mark.sanity)
])
def test_replication_configuration_valid_values(params_from_base_test_setup, num_of_docs, continuous, x509_cert_auth):
    """
        @summary:
        1. Create CBL DB and create bulk doc in CBL
        2. Configure replication with valid values of valid cbl Db, valid target url
        3. Start replication with push and pull
        4. Verify replication is successful and verify docs exist
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')
    channels_sg = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 2

    # Create CBL database
    sg_client = MobileRestClient()

    # Reset cluster to ensure no data in system
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_config, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_config = temp_cluster_config
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels_sg)

    # Configure replication with push_pull
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels_sg, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        base_url, sg_admin_url, sg_db, username, password, channels_sg, sg_client, cbl_db, sg_blip_url, continuous=continuous, replication_type="push_pull", auth=auth)

    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs["rows"], number_updates=number_of_updates, auth=session)
    replicator.wait_until_replicator_idle(repl)
    total = replicator.getTotal(repl)
    completed = replicator.getCompleted(repl)
    assert total == completed, "total is not equal to completed"
    time.sleep(2)  # wait until replication is done
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)
    sg_docs = sg_docs["rows"]

    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    assert len(sg_docs) == cbl_doc_count, "Expected number of docs does not exist in sync-gateway after replication"

    time.sleep(2)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    count = 0
    for doc in cbl_doc_ids:
        if continuous:
            while count < 30:
                time.sleep(0.5)
                log_info("Checking {} for updates".format(doc))
                if cbl_db_docs[doc]["updates"] == number_of_updates:
                    break
                else:
                    log_info("{} is missing updates, Retrying...".format(doc))
                    count += 1
                    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)
            assert cbl_db_docs[doc]["updates"] == number_of_updates, "updates did not get updated"
        else:
            assert cbl_db_docs[doc]["updates"] == 0, "sync-gateway updates got pushed to CBL for one shot replication"
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("authenticator_type, attachments_generator", [
    ('session', attachment.generate_2_png_10_10),
    ('basic', None)
])
def test_replication_configuration_with_pull_replication(params_from_base_test_setup, authenticator_type, attachments_generator):
    """
        @summary:
        1. Create CBL DB and create bulk doc in CBL
        2. Configure replication.
        3. Create docs in SG.
        4. pull docs to CBL.
        5. Verify all docs replicated and pulled to CBL.

    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    sg_mode = params_from_base_test_setup["mode"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    prometheus_enable = params_from_base_test_setup["prometheus_enable"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    channels = ["ABC"]
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    sg_client = MobileRestClient()

    # Add 5 docs to CBL
    # Add 10 docs to SG
    # One shot replication
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_added_doc_ids, cbl_added_doc_ids, session = setup_sg_cbl_docs(params_from_base_test_setup, sg_db=sg_db, base_url=base_url, db=db,
                                                                     cbl_db=cbl_db, sg_url=sg_url, sg_admin_url=sg_admin_url, sg_blip_url=sg_blip_url,
                                                                     replication_type="pull", channels=channels, replicator_authenticator_type=authenticator_type,
                                                                     attachments_generator=attachments_generator, auth=auth)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, auth=auth)
    if sg_mode == "di":
        cookie, session = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
        authenticator = Authenticator(base_url)
        replicator_authenticator = authenticator.authentication(session, cookie, authentication_type="session")
        replicator = Replication(base_url)
        replicator.configure_and_replicate(cbl_db, replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                           channels=channels)

    cbl_doc_count = db.getCount(cbl_db)
    cbl_doc_ids = db.getDocIds(cbl_db)

    assert len(sg_docs["rows"]) == 10, "Number of sg docs is not equal to total number of cbl docs and sg docs"
    assert cbl_doc_count == 15, "Did not get expected number of cbl docs"

    # Check that CBL docs are not pushed to SG as it is just a pull
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc in cbl_added_doc_ids:
        assert doc not in sg_ids

    # Verify SG docs are pulled to CBL
    for id in sg_added_doc_ids:
        assert id in cbl_doc_ids

    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["request_changes_count"] == 1, "request_changes_count did not get incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["request_changes_time"] > 0, "request_changes_time did not get incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["num_pull_repl_since_zero"] == 1, "num_pull_repl_since_zero did not get incremented"
        if attachments_generator is not None:
            assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["attachment_pull_count"] == 20, "attachment_pull_count did not get incremented"
            assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["attachment_pull_bytes"] > 0, "attachment_pull_bytes did not get incremented"
            if prometheus_enable and sync_gateway_version >= "2.8.0":
                assert verify_stat_on_prometheus("sgw_replication_pull_attachment_pull_count"), expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["attachment_pull_count"]


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("authenticator_type, attachments_generator", [
    ('session', attachment.generate_2_png_10_10),
    ('basic', None)
])
def test_replication_configuration_with_push_replication(params_from_base_test_setup, authenticator_type, attachments_generator):
    """
        @summary:
        1. Create docs in SG
        2. Create docs in CBL
        3. Do push replication with session/basic authenticated user
        4. Verify CBL docs got replicated to SG
        5. Verify sg docs not replicated to CBL

    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    mode = params_from_base_test_setup["mode"]
    prometheus_enable = params_from_base_test_setup["prometheus_enable"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    c = cluster.Cluster(config=cluster_config)
    sg_config = sync_gateway_config_path_for_mode("custom_sync/grant_access_one", mode)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_added_doc_ids, cbl_added_doc_ids, session = setup_sg_cbl_docs(params_from_base_test_setup, sg_db=sg_db, base_url=base_url, db=db,
                                                                     cbl_db=cbl_db, sg_url=sg_url, sg_admin_url=sg_admin_url, sg_blip_url=sg_blip_url,
                                                                     replication_type="push", channels=channels, replicator_authenticator_type=authenticator_type,
                                                                     attachments_generator=attachments_generator, auth=auth)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, auth=auth)

    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    cbl_doc_ids = db.getDocIds(cbl_db)

    assert len(sg_docs["rows"]) == 15, "Number of sg docs is not equal to total number of cbl docs and sg docs"
    assert cbl_doc_count == 5, "Did not get expected number of cbl docs"

    # Check that all doc ids in SG are also present in CBL
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc in cbl_doc_ids:
        assert doc in sg_ids

    # Verify sg docs does not exist in CBL as it is just a push replication
    for doc_id in sg_added_doc_ids:
        assert doc_id not in cbl_doc_ids

    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_push"]["doc_push_count"] == 5, "doc_push_count did not get incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["database"]["sync_function_time"] > 0, "sync_function_time is not incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["database"]["sync_function_count"] > 0, "sync_function_count is not incremented"

        if attachments_generator is not None:
            assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_push"]["attachment_push_count"] == 30, "attachment_push_count did not get incremented"
            assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_push"]["attachment_push_bytes"] > 0, "attachment_push_bytes did not get incremented"
            if prometheus_enable and sync_gateway_version >= "2.8.0":
                assert verify_stat_on_prometheus("sgw_replication_push_attachment_push_count"), expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_push"]["attachment_push_count"]
        if prometheus_enable and sync_gateway_version >= "2.8.0":
            assert verify_stat_on_prometheus("sgw_database_num_doc_writes"), expvars["syncgateway"]["per_db"][sg_db]["database"]["num_doc_writes"]


@pytest.mark.listener
@pytest.mark.replication
def test_replication_push_replication_without_authentication(params_from_base_test_setup):
    """
        @summary:
        1. Create docs in CBL
        2. Create docs in SG
        3. Do push replication without authentication.
        4. Verify docs are not replicated without authentication

    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]
    sg_client = MobileRestClient()

    db.create_bulk_docs(5, "cbl", db=cbl_db, channels=channels)
    # Add docs in SG
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    session = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)

    sg_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=10, id_prefix="sg_doc", channels=channels, auth=session)
    sg_ids = [row["id"] for row in sg_docs]

    replicator = Replication(base_url)
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True, replication_type="push", replicator_authenticator=None)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    # Removed replicator wait call and adding the sleep to allow the replicator to try few times to get the error
    time.sleep(7)
    error = replicator.getError(repl)

    assert "401" in error, "expected error did not occurred"

    replicator.stop(repl)

    cbl_doc_ids = db.getDocIds(cbl_db)
    # Check that all doc ids in CBL are not replicated to SG
    for doc in cbl_doc_ids:
        assert doc not in sg_ids


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize(
    'replicator_authenticator, invalid_username, invalid_password, invalid_session, invalid_cookie',
    [
        ('basic', 'invalid_user', 'password', None, None),
        ('session', None, None, 'invalid_session', 'invalid_cookie'),
    ]
)
def test_replication_push_replication_invalid_authentication(params_from_base_test_setup, replicator_authenticator,
                                                             invalid_username, invalid_password, invalid_session, invalid_cookie):
    """
        @summary:
        1. Create docs in CBL
        2. Create docs in SG
        3. Do push replication with invalid authentication.
        4. Verify replication configuration fails.

    """
    sg_db = "db"

    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    prometheus_enable = params_from_base_test_setup["prometheus_enable"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]
    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    db.create_bulk_docs(5, "cbl", db=cbl_db, channels=channels)
    # Add docs in SG
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)

    replicator = Replication(base_url)
    if replicator_authenticator == "session":
        replicator_authenticator = authenticator.authentication(invalid_session, invalid_cookie, authentication_type="session")
    elif replicator_authenticator == "basic":
        replicator_authenticator = authenticator.authentication(username=invalid_username, password=invalid_password, authentication_type="basic")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True, replication_type="push", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    # Adding sleep to replicator to retry
    time.sleep(5)
    error = replicator.getError(repl)

    assert "401" in error, "expected error did not occurred"
    replicator.stop(repl)
    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["security"]["auth_failed_count"] > 0, "auth failed count is not incremented"
        assert expvars["syncgateway"]["per_db"][sg_db]["security"]["total_auth_time"] > 0, "total_auth_time is not incremented"
    if prometheus_enable and sync_gateway_version >= "2.8.0":
        assert verify_stat_on_prometheus("sgw_security_auth_failed_count"), expvars["syncgateway"]["per_db"][sg_db]["security"]["auth_failed_count"]


@pytest.mark.listener
@pytest.mark.replication
def test_replication_configuration_with_filtered_doc_ids(params_from_base_test_setup):
    """
        @summary:
        1. Create docs in SG
        2. Create docs in CBL
        3. PushPull Replicate one shot from CBL -> SG with doc id filters set
        4. Verify SG only has the doc ids set in the replication from CBL
        5. Add new docs to SG
        6. PushPull Replicate one shot from SG -> CBL with doc id filters set
        7. Verify CBL only has the doc ids set in the replication from SG
        NOTE: Only works with one shot replication for filtered doc ids
    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    mode = params_from_base_test_setup["mode"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    prometheus_enable = params_from_base_test_setup["prometheus_enable"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip('Filter doc ids does not work with di modes')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]
    sg_client = MobileRestClient()
    replicator = Replication(base_url)

    db.create_bulk_docs(number=10, id_prefix="cbl_filter", db=cbl_db, channels=channels)
    cbl_added_doc_ids = db.getDocIds(cbl_db)
    num_of_filtered_ids = 5
    list_of_filtered_ids = random.sample(cbl_added_doc_ids, num_of_filtered_ids)

    cbl_doc_ids = db.getDocIds(cbl_db)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_added_doc_ids, cbl_added_doc_ids, session = setup_sg_cbl_docs(params_from_base_test_setup, sg_db=sg_db, base_url=base_url, db=db,
                                                                     cbl_db=cbl_db, sg_url=sg_url, sg_admin_url=sg_admin_url, sg_blip_url=sg_blip_url, document_ids=list_of_filtered_ids,
                                                                     replicator_authenticator_type="basic", channels=channels, auth=auth)

    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    # Verify sg docs count
    sg_added_docs = len(sg_added_doc_ids)
    total_sg_docs = sg_added_docs + num_of_filtered_ids

    assert len(sg_docs["rows"]) == total_sg_docs, "Number of sg docs is not expected"

    list_of_non_filtered_ids = set(cbl_added_doc_ids) - set(list_of_filtered_ids)

    # Verify only filtered cbl doc ids are replicated to sg
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for sg_id in list_of_filtered_ids:
        assert sg_id in sg_ids

    # Verify non filtered docs ids are not replicated in sg
    for doc_id in list_of_non_filtered_ids:
        assert doc_id not in sg_ids

    cbl_doc_ids = db.getDocIds(cbl_db)

    # Now filter doc ids
    authenticator = Authenticator(base_url)
    cookie, session_id = session
    log_info("Authentication cookie: {}".format(cookie))
    log_info("Authentication session id: {}".format(session_id))
    replicator_authenticator = authenticator.authentication(session_id, cookie,
                                                            authentication_type="session")
    sg_new_added_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=10, id_prefix="sg_doc_filter",
                                           channels=channels, auth=session)
    sg_new_added_ids = [row["id"] for row in sg_new_added_docs]
    list_of_sg_filtered_ids = random.sample(sg_new_added_ids, num_of_filtered_ids)
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=False,
                                       documentIDs=list_of_sg_filtered_ids, channels=channels,
                                       replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    log_info("Starting replicator")
    replicator.start(repl)
    log_info("Waiting for replicator to go idle")
    replicator.wait_until_replicator_idle(repl)
    # Verify only filtered sg ids are replicated to cbl
    cbl_doc_ids = db.getDocIds(cbl_db)
    list_of_non_sg_filtered_ids = set(sg_new_added_ids) - set(list_of_sg_filtered_ids)
    for sg_id in list_of_sg_filtered_ids:
        assert sg_id in cbl_doc_ids

    # Verify non filtered docs ids are not replicated in cbl
    for doc_id in list_of_non_sg_filtered_ids:
        assert doc_id not in cbl_doc_ids

    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["num_pull_repl_total_one_shot"] == 2, "num_pull_repl_total_one_shot did not get incremented"
    if prometheus_enable and sync_gateway_version >= "2.8.0":
        assert verify_stat_on_prometheus("sgw_replication_pull_num_pull_repl_total_one_shot"), expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["num_pull_repl_total_one_shot"]


@pytest.mark.listener
@pytest.mark.replication
def test_replication_configuration_with_headers(params_from_base_test_setup):
    """
        @summary:
        1. Create docs in CBL
        2. Make replication configuration by authenticating through headers
        4. Verify CBL docs with doc ids sent in configuration got replicated to SG

    """
    sg_db = "db"
    num_of_docs = 10

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]
    sg_client = MobileRestClient()

    db.create_bulk_docs(num_of_docs, "cbll", db=cbl_db, channels=channels)

    # Add docs in SG
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    auth_session = cookie, session
    sync_cookie = "{}={}".format(cookie, session)

    session_header = {"Cookie": sync_cookie}

    replicator = Replication(base_url)
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True, headers=session_header)
    repl = replicator.create(repl_config)
    repl_change_listener = replicator.addChangeListener(repl)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    changes_count = replicator.getChangesCount(repl_change_listener)
    replicator.stop(repl)
    assert changes_count > 0, "did not get any changes"
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=auth_session)

    # Verify database doc counts
    cbl_doc_ids = db.getDocIds(cbl_db)

    assert len(sg_docs["rows"]) == num_of_docs, "Number of sg docs should be equal to cbl docs"
    assert len(cbl_doc_ids) == num_of_docs, "Did not get expected number of cbl docs"

    # Check that all doc ids in CBL are replicated to SG
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc in cbl_doc_ids:
        assert doc in sg_ids


@pytest.mark.listener
@pytest.mark.noconflicts
@pytest.mark.parametrize("num_of_docs, x509_cert_auth", [
    (10, False),
    (100, True),
    (1000, False)
])
def test_CBL_tombstone_doc(params_from_base_test_setup, num_of_docs, x509_cert_auth):
    """
        @summary:
        1. Create docs in SG.
        2. pull replication to CBL with continuous
        3. tombstone doc in sG.
        4. wait for replication to finish
        5. Verify that tombstone doc is deleted in CBL too

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0":
        pytest.skip('--no-conflicts is enabled and does not work with sg < 2.0 , so skipping the test')

    channels = ["Replication"]
    sg_client = MobileRestClient()

    # Modify sync-gateway config to use no-conflicts config
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_config, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_config = temp_cluster_config
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if sync_gateway_version >= "2.5.0":
        sg_client = MobileRestClient()
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        chan_cache_tombstone_revs = expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_tombstone_revs"]
        chan_cache_removal_revs = expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_removal_revs"]

    # 1. Add docs to SG.
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs,
                                   attachments_generator=attachment.generate_2_png_10_10, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)
    assert len(sg_docs) == num_of_docs

    # 2. Pull replication to CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channels, replication_type="pull", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 2.5. call POST _changes admin API to apply changes to all channels.
    sg_client.stream_continuous_changes(
        url=sg_admin_url,
        db=sg_db,
        since=0,
        auth=auth,
        filter_type=None,
        filter_channels=None)

    # 3. tombstone doc in SG.
    doc_id = "sg_docs_6"
    doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=doc_id, auth=session)
    sg_client.delete_doc(url=sg_url, db=sg_db, doc_id=doc_id, rev=doc['_rev'], auth=session)

    # 4. wait for replication to finish
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert doc_id not in cbl_doc_ids, "doc is expected to be deleted in CBL ,but not deleted"

    if sync_gateway_version >= "2.5.0":
        expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
        assert chan_cache_tombstone_revs < expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_tombstone_revs"], "chan cache tombstone revs did not get incremented"
        assert chan_cache_removal_revs < expvars["syncgateway"]["per_db"][sg_db]["cache"]["chan_cache_removal_revs"], "chan_cache_removal_revs did not get incremented"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("sg_conf_name, delete_doc_type", [
    ('listener_tests/listener_tests_no_conflicts', "purge"),
    ('listener_tests/listener_tests_no_conflicts', "expire")
])
def test_CBL_for_purged_doc(params_from_base_test_setup, sg_conf_name, delete_doc_type):
    """
        @summary:
        1. Create docs in SG.
        2. pull replication to CBL with continuous
        3. Purge doc or expire doc in SG.
        4. wait for replication to finish.
        5. Stop replication
        6. Verify that purged doc or expired doc is not deleted in CBL

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    num_of_docs = 10

    if sync_gateway_version < "2.0":
        pytest.skip('--no-conflicts is enabled and does not work with sg < 2.0 , so skipping the test')

    channels = ["Replication"]
    sg_client = MobileRestClient()

    # Modify sync-gateway config to use no-conflicts config
    if no_conflicts_enabled:
        sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    cl = cluster.Cluster(config=cluster_config)
    cl.reset(sg_config_path=sg_config)

    # 1. Add docs to SG.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs,
                                   attachments_generator=attachment.generate_2_png_10_10, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)
    assert len(sg_docs) == num_of_docs

    # Create an expiry doc
    if delete_doc_type == "expire":
        doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=3, channels=channels)
        sg_client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=session)
        doc_id = "exp_3"

    # 2. Pull replication to CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channels, replication_type="pull", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    # 3. Purge doc in SG.
    if delete_doc_type == "purge":
        doc_id = "sg_docs_7"
        doc = sg_client.get_doc(url=sg_url, db=sg_db, doc_id=doc_id, auth=session)
        sg_client.purge_doc(url=sg_admin_url, db=sg_db, doc=doc, auth=auth)

    # expire doc
    if delete_doc_type == "expire":
        time.sleep(5)

    # 4. wait for replication to finish
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert doc_id in cbl_doc_ids, "{} document does not exist in CBL after replication".format(delete_doc_type)


@pytest.mark.listener
@pytest.mark.noconflicts
@pytest.mark.replication
@pytest.mark.parametrize("sg_conf_name, delete_doc_type", [
    ('listener_tests/listener_tests_no_conflicts', "purge"),
    # ('listener_tests/listener_tests_no_conflicts', "expire") # not supported yet
])
def test_replication_purge_in_CBL(params_from_base_test_setup, sg_conf_name, delete_doc_type):
    """
        @summary:
        1. Create docs in CBL
        2. Push replication to SG.
        3. Purge or expire doc in CBL
        4. Continue  replication to SG
        5. Stop replication .
        6. Verify docs did not get removed in SG

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    doc_obj = Document(base_url)
    exp_doc_id = "exp_doc"

    if sync_gateway_version < "2.0":
        pytest.skip('It does not work with sg < 2.0 , so skipping the test')

    channels = ["Replication"]
    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    # Modify sync-gateway config to use no-conflicts config

    if no_conflicts_enabled:
        sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Create CBL database
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels)

    # Create an expiry doc
    if delete_doc_type == "expire":
        doc_exp_3_body = document.create_doc(doc_id=exp_doc_id, expiry=3, channels=channels)
        mutable_doc = doc_obj.create(exp_doc_id, doc_exp_3_body)
        db.saveDocument(cbl_db, mutable_doc)

    # 2. push replication to SG.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    session = cookie, session_id
    replicator = Replication(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channels, replication_type="push", replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    # 3. Purge doc in CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    removed_cbl_id = random.choice(cbl_doc_ids)
    if delete_doc_type == "purge":
        random_cbl_doc = db.getDocument(cbl_db, doc_id=removed_cbl_id)
        mutable_doc = doc_obj.toMutable(random_cbl_doc)
        db.purge(cbl_db, mutable_doc)

    # 3. Expire doc in CBL
    if delete_doc_type == "expire":
        time.sleep(10)
        removed_cbl_id = exp_doc_id

    cbl_doc_ids = db.getDocIds(cbl_db)
    assert removed_cbl_id not in cbl_doc_ids

    # 4. Continue  replication to SG
    replicator.wait_until_replicator_idle(repl)
    # 5. Stop replication
    replicator.stop(repl)

    # 6. Verify docs did not  purged in SG
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_doc_ids = [doc['id'] for doc in sg_docs["rows"]]
    assert removed_cbl_id in sg_doc_ids, "{} document got {}ed in SG".format(delete_doc_type, delete_doc_type)


@pytest.mark.listener
@pytest.mark.noconflicts
@pytest.mark.replication
@pytest.mark.parametrize("sg_conf_name", [
    ('listener_tests/listener_tests_no_conflicts')
])
def test_replication_delete_in_CBL(params_from_base_test_setup, sg_conf_name):
    """
        @summary:
        1. Create docs in CBL
        2. Push replication to SG.
        3. Delete doc in CBL
        4. Continue replication to SG
        5. Stop replication .
        6. Verify deleted doc in CBL got removed in SG too

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    doc_obj = Document(base_url)

    if sync_gateway_version < "2.0":
        pytest.skip('It does not work with sg < 2.0 , so skipping the test')

    channels = ["Replication"]
    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    # Modify sync-gateway config to use no-conflicts config
    if no_conflicts_enabled:
        sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Create CBL database
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels)

    # 2. push replication to SG.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    session = cookie, session_id
    replicator = Replication(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channels, replication_type="push", replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    # 3. Delete doc in CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    random_cbl_id = random.choice(cbl_doc_ids)
    random_cbl_doc = db.getDocument(cbl_db, doc_id=random_cbl_id)
    mutable_doc = doc_obj.toMutable(random_cbl_doc)
    log_info("Deleting doc: {}".format(random_cbl_id))
    db.delete(database=cbl_db, document=mutable_doc)
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert random_cbl_id not in cbl_doc_ids

    # 4. Continue  replication to SG
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 6. Verify deleted doc in CBL got removed in SG too
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_doc_ids = [doc['id'] for doc in sg_docs["rows"]]
    assert random_cbl_id not in sg_doc_ids, "deleted doc in CBL did not removed in SG"


@pytest.mark.listener
@pytest.mark.noconflicts
@pytest.mark.replication
@pytest.mark.parametrize("sg_conf_name, num_of_docs, number_of_updates", [
    ('listener_tests/listener_tests_no_conflicts', 10, 4),
    ('listener_tests/listener_tests_no_conflicts', 100, 10),
    ('listener_tests/listener_tests_no_conflicts', 1000, 10)
])
def test_CBL_push_pull_with_sgAccel_down(params_from_base_test_setup, sg_conf_name, num_of_docs, number_of_updates):
    """
        @summary:
        1. Have SG and SG accel up
        2. Create docs in CBL.
        3. push replication to SG
        4. update docs in SG.
        5. Bring down sg Accel
        6. Now Get pull replication to SG
        7. update docs in CBL
        8. Verify CBL can update docs successfully
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"

    if sync_gateway_version < "2.0" or sg_mode.lower() != "di":
        pytest.skip('sg < 2.0 or mode is not in di , so skipping the test')

    channels = ["Replication"]
    sg_client = MobileRestClient()

    # 1. Have SG and SG accel up
    # Modify sync-gateway config to use no-conflicts config
    if no_conflicts_enabled:
        sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 2. Create docs in CBL.
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels)

    # 3. push replication to SG
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    replication_type = "push"
    replicator = Replication(base_url)
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        base_url, sg_admin_url, sg_db, username, password, channels, sg_client, cbl_db, sg_blip_url, replication_type, auth=auth)
    replicator.stop(repl)  # todo : trying removing this

    # 4. update docs in SG.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs["rows"], number_updates=number_of_updates, auth=session)

    # 5. Bring down sg Accel
    c.sg_accels[0].stop()

    # 6. Now Get pull replication to SG
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=False,
                                       replication_type="pull", channels=channels, replicator_authenticator=replicator_authenticator)

    repl1 = replicator.create(repl_config)
    replicator.start(repl1)
    replicator.wait_until_replicator_idle(repl1)
    replicator.stop(repl1)

    # update docs in CBL
    db.update_bulk_docs(database=cbl_db, number_of_updates=number_of_updates)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    for doc in cbl_doc_ids:
        assert cbl_db_docs[doc]["updates-cbl"] == number_of_updates, "updates-cbl did not get updated"


@pytest.mark.listener
@pytest.mark.noconflicts
@pytest.mark.parametrize("sg_conf_name, num_of_docs", [
    ('listener_tests/listener_tests_no_conflicts', 10)
])
def CBL_offline_test(params_from_base_test_setup, sg_conf_name, num_of_docs):
    """
        @summary:
        This test is meant to be run locally only, not on jenkins.
        1. Create docs in CBL1.
        2. push replication to SG.
        3. CBL goes offline(block outbound requests
        to SG through IPtables)
        4. Do updates on CBL
        5. Continue push replication to SG from CBL
        6. CBL comes online( unblock ports)
        7. push replication and do pull replication
        8. Verify conflicts resolved on CBL.
    """
    sg_db = "db"
    cbl_db_name = "cbl_db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    sg_config = params_from_base_test_setup["sg_config"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels = ["Replication"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    sg_client = MobileRestClient()
    db = Database(base_url)
    replicator = Replication(base_url)

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    if no_conflicts_enabled:
        sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in CBL.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    cbl_db = db.create(cbl_db_name)
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels)
    # 2. push replication to SG
    replication_type = "push"
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        base_url, sg_admin_url, sg_db, username, password, channels, sg_client, cbl_db, sg_blip_url, replication_type, auth=auth)

    # 3. CBL goes offline(Block incomming requests of CBL to Sg)
    command = "mode=\"100% Loss\" osascript run_scripts/network_link_conditioner.applescript"
    return_val = os.system(command)
    if return_val != 0:
        raise Exception("{0} failed".format(command))

    # 4. Do updates on CBL
    db.update_bulk_docs(cbl_db, number_of_updates=number_of_updates)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 6. CBL comes online( unblock ports)
    command = "mode=\"Wi-Fi\" osascript run_scripts/network_link_conditioner.applescript"
    return_val = os.system(command)
    if return_val != 0:
        raise Exception("{0} failed".format(command))

    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)
    # 8. Verify replication happened in sync_gateway
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    for doc in sg_docs["rows"]:
        assert doc["updates-cbl"] == number_of_updates, "sync gateway is not replicated after CBL is back online"

    # 7. Do pull replication
    replication_type = "pull"
    repl = replicator.configure_and_replicate(cbl_db, replicator_authenticator, target_url=sg_blip_url, replication_type=replication_type, continuous=True,
                                              channels=channels)
    replicator.stop(repl)

    # 8. Get Documents from CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)

    db.update_bulk_docs(cbl_db, number_of_updates=1)

    # 9 Verify CBL updated successfully
    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    for doc in cbl_doc_ids:
        assert cbl_db_docs[doc]["updates-cbl"] == number_of_updates + 1, "updates-cbl did not get updated"


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.backgroundapp
@pytest.mark.parametrize("num_docs, need_attachments, replication_after_backgroundApp", [
    (1000, True, False),
    (1000, False, False),
    # (10000, False, True), # TODO : Not yet supported by Test server app
    # (1000, True, True) # TODO: Not yet supported by Test server app
])
def test_initial_pull_replication_background_apprun(params_from_base_test_setup, num_docs, need_attachments,
                                                    replication_after_backgroundApp):
    """
    @summary
    1. Add specified number of documents to sync-gateway.
    2. Start continous pull replication to pull the docs from a sync_gateway database.
    3. While docs are getting replicated , push the app to the background
    4. Verify if all of the docs got pulled and replication completed when app goes background
    """

    sg_db = "db"

    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    device_enabled = params_from_base_test_setup["device_enabled"]
    cbl_db = params_from_base_test_setup["source_db"]
    base_url = params_from_base_test_setup["base_url"]
    testserver = params_from_base_test_setup["testserver"]
    sg_config = params_from_base_test_setup["sg_config"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # No command to push the app to background on device, so avoid test to run on ios device and no app for .net
    if not ((liteserv_platform.lower() == "ios" or liteserv_platform.lower() == "xamarin-ios") and not device_enabled):
        pytest.skip('This test cannot run either it is .Net or ios with device enabled ')

    client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client.create_user(sg_admin_url, sg_db, "testuser", password="password", channels=["ABC", "NBC"], auth=auth)
    cookie, session_id = client.create_session(sg_admin_url, sg_db, "testuser", auth=auth)
    # session = cookie, session_id
    # Add 'number_of_sg_docs' to Sync Gateway
    bulk_docs_resp = []
    if need_attachments:
        sg_doc_bodies = document.create_docs(
            doc_id_prefix="seeded_doc",
            number=num_docs,
            attachments_generator=attachment.generate_2_png_10_10,
            channels=["ABC"]
        )
    else:
        sg_doc_bodies = document.create_docs(doc_id_prefix='seeded_doc', number=num_docs, channels=["ABC"])
    # if adding bulk docs with huge attachment more than 5000 fails
    for x in range(0, len(sg_doc_bodies), 100000):
        chunk_docs = sg_doc_bodies[x:x + 100000]
        ch_bulk_docs_resp = client.add_bulk_docs(url=sg_admin_url, db=sg_db, docs=chunk_docs, auth=auth)
        log_info("length of bulk docs resp{}".format(len(ch_bulk_docs_resp)))
        bulk_docs_resp += ch_bulk_docs_resp
    # docs = client.add_bulk_docs(url=sg_one_public, db=sg_db, docs=sg_doc_bodies, auth=session)
    assert len(bulk_docs_resp) == num_docs

    # Add a poll to make sure all of the docs have propagated to sync_gateway's _changes before initiating
    # the one shot pull replication to ensure that the client is aware of all of the docs to pull
    client.verify_docs_in_changes(url=sg_admin_url, db=sg_db, expected_docs=bulk_docs_resp, auth=auth,
                                  polling_interval=10)

    db = Database(base_url)
    # Replicate to all CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True,
                                       replication_type="pull", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    time.sleep(3)  # let replication go for few seconds and then make app go background
    testserver.close_app()
    time.sleep(10)  # wait until all replication is done
    testserver.open_app()
    replicator.wait_until_replicator_idle(repl)
    # Verify docs replicated to client
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert len(cbl_doc_ids) == len(bulk_docs_resp)
    sg_docs = client.get_all_docs(url=sg_admin_url, db=sg_db, auth=auth)
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc in cbl_doc_ids:
        assert doc in sg_ids

    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.backgroundapp
@pytest.mark.parametrize("num_docs, need_attachments, replication_after_backgroundApp", [
    (100, True, False),
    (10000, False, False),
    # (1000000, False, False)  you can run this locally if needed, jenkins cannot run more than 15 mins
])
def test_push_replication_with_backgroundApp(params_from_base_test_setup, num_docs, need_attachments,
                                             replication_after_backgroundApp):
    """
    @summary
    1. Prepare Testserver to have specified number of documents.
    2. Start continous push replication to push the docs into a sync_gateway database.
    3. While docs are getting replecated , push the app to the background
    4. Verify if all of the docs get pushed and replication continous when app goes background
    """

    sg_db = "db"

    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    device_enabled = params_from_base_test_setup["device_enabled"]
    cbl_db = params_from_base_test_setup["source_db"]
    base_url = params_from_base_test_setup["base_url"]
    testserver = params_from_base_test_setup["testserver"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    sg_url = params_from_base_test_setup["sg_url"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    channels = ["ABC"]

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # No command to push the app to background on device, so avoid test to run on ios device and no app for .net
    if liteserv_platform.lower() == "net-msft" or liteserv_platform.lower() == "net-uwp" or ((liteserv_platform.lower() != "ios" or liteserv_platform.lower() != "xamarin-ios") and device_enabled):
        pytest.skip('This test cannot run either it is .Net or ios with device enabled ')

    if liteserv_platform in ["java-macosx", "java-msft", "java-ubuntu", "java-centos", "javaws-macosx", "javaws-msft", "javaws-ubuntu", "javaws-centos"] or 'c' in liteserv_platform:
        pytest.skip('This test cannot run as a Java application')

    client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client.create_user(sg_admin_url, sg_db, "testuser", password="password", channels=channels, auth=auth)
    cookie, session_id = client.create_session(sg_admin_url, sg_db, "testuser", auth=auth)
    session = cookie, session_id

    # liteserv cannot handle bulk docs more than 100000, if you run more than 100000, it will chunk the
    # docs into set of 100000 and call add bulk docs
    if need_attachments:
        for x in range(0, num_docs, 100000):
            cbl_prefix = "cbl" + str(x)
            db.create_bulk_docs(num_docs, cbl_prefix, db=cbl_db, generator="simple_user",
                                attachments_generator=attachment.generate_png_100_100, channels=channels)
    else:
        for x in range(0, num_docs, 100000):
            cbl_prefix = "cbl" + str(x)
            db.create_bulk_docs(num_docs, cbl_prefix, db=cbl_db, channels=channels)

    # wait until cbl got expected docs as there could be delay due to bulk docs
    cbl_doc_ids = db.getDocIds(cbl_db, limit=num_docs)
    assert len(cbl_doc_ids) == num_docs

    # Start replication after app goes background. So close app first and start replication
    db = Database(base_url)
    # Replicate to all CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True,
                                       replication_type="push", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    time.sleep(3)  # let replication go for few seconds and then make app go background
    testserver.close_app()
    time.sleep(10)  # wait until all replication is done
    testserver.open_app()
    replicator.wait_until_replicator_idle(repl)
    # Verify docs replicated to sync_gateway
    sg_docs = client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc_id in cbl_doc_ids:
        assert doc_id in sg_ids
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_replication_wrong_blip(params_from_base_test_setup):
    """
        @summary:
        1. Create docs in CBL
        2. Push replication to SG with wrong blip
        3. Verify it fails .
    """
    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    sg_blip_url = sg_blip_url.replace("ws", "ht2tp")
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    username = "autotest"
    password = "password"

    channels = ["Replication"]
    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    # Modify sync-gateway config to use no-conflicts config
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in CBL.
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels)

    # 2. Push replication to SG with wrong blip
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    with pytest.raises(Exception) as ex:
        replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channels, replicator_authenticator=replicator_authenticator)
    ex_data = str(ex.value)

    if liteserv_platform == "ios" or liteserv_platform.startswith("javaws-"):
        assert "Invalid scheme for URLEndpoint url" in ex_data
        assert "ht2tp" in ex_data
        assert "must be either 'ws:' or 'wss:'" in ex_data
    else:
        assert ex_data.startswith('400 Client Error: Bad Request for url:')
        assert "unsupported" in ex_data or "Invalid" in ex_data
    assert "ws" in ex_data and "wss" in ex_data


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("delete_source, attachments, number_of_updates", [
    ('sg', True, 1),
    ('cbl', True, 1),
    ('sg', False, 1),
    ('cbl', False, 1),
    ('sg', False, 5),
    ('cbl', False, 5),
])
def test_default_conflict_scenario_delete_wins(params_from_base_test_setup, delete_source, attachments, number_of_updates):
    """
        @summary:
        1. Create docs in CBL.
        2. Replicate docs to SG with push_pull and continous False
        3. Wait until replication is done and stop replication
        4. update doc in Sg and delete doc in CBL/ delete doc in Sg and update doc in CBL
        5. Start the replication with same configuration as step 2
        6. Verify delete wins
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    sg_mode = params_from_base_test_setup["mode"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    channels = ["replication-channel"]
    num_of_docs = 10
    username = "autotest"
    password = "password"

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Create bulk doc json
    if attachments:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels, attachments_generator=attachment.generate_2_png_10_10)
    else:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels)
    sg_client = MobileRestClient()

    # Start and stop continuous replication
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    replicator = Replication(base_url)
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(baseUrl=base_url, sg_admin_url=sg_admin_url, sg_db=sg_db, username=username, password=password,
                                                                                            channels=channels, sg_client=sg_client, cbl_db=cbl_db, sg_blip_url=sg_blip_url, replication_type="push_pull", continuous=False, auth=auth)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]

    if delete_source == 'cbl':
        with ThreadPoolExecutor(max_workers=4) as tpe:
            sg_updateDocs_task = tpe.submit(
                sg_client.update_docs, url=sg_url, db=sg_db, docs=sg_docs,
                number_updates=number_of_updates, auth=session
            )
            cbl_delete_task = tpe.submit(
                db.cbl_delete_bulk_docs, cbl_db=cbl_db
            )
            sg_updateDocs_task.result()
            cbl_delete_task.result()
    else:
        with ThreadPoolExecutor(max_workers=4) as tpe:
            sg_delete_task = tpe.submit(
                sg_client.delete_docs, url=sg_url, db=sg_db, docs=sg_docs, auth=session
            )
            cbl_update_task = tpe.submit(
                db.update_bulk_docs, cbl_db, number_of_updates=number_of_updates
            )
            sg_delete_task.result()
            cbl_update_task.result()

    replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                       channels=channels)
    # Di mode has delay for one shot replication, so need another replication only for DI mode
    if sg_mode == "di":
        replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                           channels=channels)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)

    assert len(cbl_docs) == 0, "did not delete docs after delete operation"
    replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                       channels=channels)
    # Di mode has delay for one shot replication, so need another replication only for DI mode
    if sg_mode == "di":
        replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                           channels=channels)

    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == 0, "did not delete docs after delete operation"
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    assert len(sg_docs) == 0, "did not delete docs in sg after delete operation in CBL"
    replicator.stop(repl)

    # create docs with deleted docs id and verify replication happens without any issues.
    if attachments:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels, attachments_generator=attachment.generate_2_png_10_10)
    else:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels)

    replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                       channels=channels)

    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    assert len(cbl_docs) == num_of_docs
    assert len(sg_docs) == len(cbl_docs), "new doc created with same doc id as deleted docs are not created and replicated"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("highrev_source, attachments", [
    ('sg', True),
    ('cbl', True),
    ('sg', False),
    ('cbl', False),
])
def test_default_conflict_scenario_highRevGeneration_wins(params_from_base_test_setup, highrev_source, attachments):

    """
        @summary:
        1. Create docs in CBL.
        2. Replicate docs to SG with push_pull and continous false
        3. Wait unitl replication done and stop replication.
        4. update doc 1 times in Sg and update doc 2 times in CBL and vice versa in 2nd scenario
        5. Start replication with push pull and continous False.
        6. Wait until replication done
        7. Verify doc with higher rev id is updated in CBL.
        8. Now update docs in sync gateway 3 times.
        9. Start replication with push pull and continous False.
        10. Wait until replication is done
        11. As sync-gateway revision id is higher, updates from sync-gateway wins
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    channels = ["replication-channel"]
    num_of_docs = 10

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Create bulk doc json
    if attachments:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels, attachments_generator=attachment.generate_2_png_10_10)
    else:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels)
    sg_client = MobileRestClient()

    # Start and stop continuous replication
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, name="autotest", password="password", channels=channels, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        baseUrl=base_url, sg_admin_url=sg_admin_url, sg_db=sg_db, channels=channels, sg_client=sg_client, cbl_db=cbl_db, sg_blip_url=sg_blip_url, username="autotest", password="password", replication_type="push_pull", continuous=False, auth=auth)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]

    if highrev_source == 'cbl':
        sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, auth=session)
        db.update_bulk_docs(cbl_db, number_of_updates=2)
    else:
        sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session, number_updates=2)
        db.update_bulk_docs(cbl_db)

    replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                       channels=channels)
    if sg_mode == "di":
        replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                           channels=channels)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)

    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    sg_docs_values = [doc['doc'] for doc in sg_docs]

    if highrev_source == 'cbl':
        for doc in cbl_docs:
            assert cbl_docs[doc]["updates-cbl"] == 2, "cbl with high rev id is not updated "
    else:
        for doc in cbl_docs:
            assert cbl_docs[doc]["updates"] == 2, "cbl with high rev id is not updated "
        for i in range(len(sg_docs_values)):
            assert sg_docs_values[i]["updates"] == 2, "sg with high rev id is not updated"

    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=3, auth=session)
    replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                       channels=channels)
    # Di mode has delay for one shot replication, so need another replication only for DI mode
    repl = None
    if sg_mode == "di":
        repl = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, continuous=False,
                                                  channels=channels)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    for doc in cbl_docs:
        if highrev_source == 'cbl':
            verify_updates = 4
        else:
            verify_updates = 5
        count = 0
        while count < 30 and cbl_docs[doc]["updates"] != verify_updates:
            time.sleep(5)
            cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
            count += 1

        assert cbl_docs[doc]["updates"] == verify_updates, "cbl with high rev id is not updated "
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    sg_docs_values = [doc['doc'] for doc in sg_docs]
    for i in range(len(sg_docs_values)):
        assert sg_docs_values[i]["updates"] == verify_updates, "sg with high rev id is not updated"
    if sg_mode == "di":
        replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("highrevId_source, attachments", [
    ('sg', True),
    ('cbl', True),
    ('sg', False),
    ('cbl', False),
])
def test_default_conflict_scenario_highRevID_wins(params_from_base_test_setup, highrevId_source, attachments):
    """
        @summary:
        1. Create docs in CBL.
        2. Replicate docs to SG with push_pull and continous false
        3. Wait unitl replication done and stop replication.
        4. For high revision id in Sg: update doc 1 time in Sg and and create a conflict with lowest revision id to have higher revision in CBL
           For high revision id in Sg : update doc 1 time in Sg and and create a conflict with highest revision id to have lower revision in CBL
        5. Start replication pull with one shot replication
        6. Wait until replication done
        7. Verfiy doc with higher rev id is updated in CBL.
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    channels = ["replication-channel"]
    num_of_docs = 10

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Create bulk doc json
    if attachments:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels, attachments_generator=attachment.generate_2_png_10_10)
    else:
        db.create_bulk_docs(num_of_docs, "replication", db=cbl_db, channels=channels)
    sg_client = MobileRestClient()

    # Start and stop continuous replication
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, name="autotest", password="password", channels=channels, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        baseUrl=base_url, sg_admin_url=sg_admin_url, sg_db=sg_db, channels=channels, sg_client=sg_client, cbl_db=cbl_db, sg_blip_url=sg_blip_url, username="autotest", password="password", replication_type="push_pull", continuous=False, auth=auth)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]

    db.update_bulk_docs(database=cbl_db, number_of_updates=2)
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, auth=session)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]

    if highrevId_source == 'cbl':
        new_revision = "3-00000000000000000000000000000000"
    if highrevId_source == 'sg':
        new_revision = "3-ffffffffffffffffffffffffffffffff"
        for i in range(len(sg_docs)):
            sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"],
                                   parent_revisions=sg_docs[i]["value"]["rev"], new_revision=new_revision, auth=session)
        replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator,
                                           target_url=sg_blip_url, replication_type="push_pull", continuous=False,
                                           channels=channels, err_check=True)
        # Di mode has delay for one shot replication, so need another replication only for DI mode
        if sg_mode == "di":
            replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url, replication_type="push_pull", continuous=False,
                                               channels=channels, err_check=True)

    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)

    if highrevId_source == 'cbl':
        for doc in cbl_docs:
            assert cbl_docs[doc]["updates-cbl"] == 2, "higher revision id on CBL did not win with conflict resolution in cbl"
            assert cbl_docs[doc]["updates"] == 0, "higher revision id on CBL did not win with conflict resolution in cbl"
    if highrevId_source == 'sg':
        for doc in cbl_docs:
            assert cbl_docs[doc]["updates"] == 1, "higher revision id on SG did not win with conflict resolution in cbl"
            try:
                cbl_docs[doc]["updates-cbl"]
                assert False, "higher revision id on SG did not win with conflict resolution in cbl"
            except KeyError:
                assert True


@pytest.mark.listener
@pytest.mark.replication
def test_default_conflict_with_two_conflictsAndTomstone(params_from_base_test_setup):
    """
        @summary:
        1. create docs in sg.
        2. Create two conflicts with 2-hex in sg.
        3. update doc in sg to have new revision to one of the conflicted branch of sg, the counter for property updates increments to 1
        4. Start replication with push pull and continous true
        5. wait until replication is done.
        6. Verify that default conflict resolver resolved appropriately.
        7. Now update in cbl with counter updates-cbl property to 1
        8. Wait until replication is done i.e updates docs from cbl should get replicated to sg
        9. Verify docs with latest update from cbl got updated to sg.
        9. Tombstone the active reivsion which got updated at step3, so the property of 'updates' should get reomoved on sg.
        10.Continue the replication and wait until replication is idle.
        11. Verify docs in cbl got replicated to sync-gateway with deleted doc removed from sg.

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels = ["replication-channel"]
    num_of_docs = 10
    username = "autotest"
    password = "password"

    if no_conflicts_enabled:
        pytest.skip('Cannot work with no-conflicts enabled')

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in SG.
    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)

    # 2. Create two conflicts with 2-hex in sg.
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa", auth=session)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa9b", auth=session)

    # 3. update doc in sg to have new revision to one of the conflicted branch of sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, delay=None,
                          auth=session, channels=channels)

    # 4. pull replication to CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                              replication_type="push_pull", continuous=True, channels=channels)

    # 5. Now update doc in cbl and replicate to sync_gateway
    db.update_bulk_docs(database=cbl_db, number_of_updates=1)
    replicator.wait_until_replicator_idle(repl)

    # 5. Verify updated doc from cbl is pushed to sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    count = 0
    for doc in sg_docs:
        while count < 30:
            try:
                doc["doc"]["updates-cbl"]
                break
            except KeyError:
                sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
                sg_docs = sg_docs["rows"]
                time.sleep(1)
                count += 1
        assert doc["doc"]["updates-cbl"] == 1, "cbl update did not pushed to sg"

    # 6. Tombstone the latest active revision in sg
    sg_client.delete_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)

    # 7. Continue replication to CBL
    replicator.wait_until_replicator_idle(repl)

    # 8. Verify cbl is resolved docs appropriately and deleted docs in sg is updated in cbl
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    count = 0
    for id in cbl_doc_ids:
        while count < 60:
            try:
                cbl_docs[id]["updates"]
            except KeyError:
                break
            time.sleep(1)
            cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
            count += 1
        with pytest.raises(KeyError) as ke:
            cbl_docs[id]["updates"]

        assert ke.value.args[0].startswith('updates')
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_default_conflict_with_oneTombstone_conflict(params_from_base_test_setup):
    """
        @summary:
        1. create docs in sg.
        2. Create two conflicts with 2-hex in sg.
        3. Tombstone the doc which has higher and active revision in sg.
        4. Start replication and pull to cbl
        5. Verify that doc got tomstoned.
        6. update the doc in sg.
        7. Wait until replication is idle and stop the replicator
        8. Verify updates from cbl got replicated to sg.
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels = ["replication-channel"]
    num_of_docs = 10
    username = "autotest"
    password = "password"

    if no_conflicts_enabled:
        pytest.skip('Cannot work with no-conflicts enabled')

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in SG.
    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)

    # 2. Create two conflicts with 2-hex in sg.
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa", auth=session)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa9b", auth=session)

    # 3. Tombstone the doc which has higher and active revision in sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    for i in range(len(sg_docs)):
        sg_client.delete_doc(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], rev="2-41fa9b",
                             auth=session)

    # 4. replication to CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                              replication_type="push_pull", continuous=True, channels=channels)

    # 5. Now update doc in cbl and replicate to sync_gateway
    db.update_bulk_docs(database=cbl_db, number_of_updates=1)
    replicator.wait_until_replicator_idle(repl)

    # 5. Verify updated doc from cbl is pushed to sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    for doc in sg_docs:
        assert doc["doc"]["updates-cbl"] == 1, "cbl update did not pushed to sg"

    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_default_conflict_with_three_conflicts(params_from_base_test_setup):
    """
        @summary:
        1. create docs in sg.
        2. Create three conflicts with 2-hex in sg.
        3. update doc in sg to have new revision to one of the conflicted branch of sg, the counter for property updates increments to 1
        4. Start replication with push pull and continous true
        5. wait until replication is done.
        6. Verify that default conflict resolver resolved appropriately.
        7. Now update in cbl with counter updates-cbl property to 1
        8. Wait until replication is done i.e updates docs from cbl should get replicated to sg
        9. Verify docs with latest update from cbl got updated to sg.

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels = ["replication-channel"]
    num_of_docs = 10
    username = "autotest"
    password = "password"

    if no_conflicts_enabled:
        pytest.skip('Cannot work with no-conflicts enabled')

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in SG.
    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)
    # 2. Create two conflicts with 2-hex in sg.
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa", auth=session)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa9b", auth=session)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa3b", auth=session)

    # 3. update doc in sg to have new revision to one of the conflicted branch of sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, delay=None,
                          auth=session, channels=channels)

    # 4. pull replication to CBL
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                              replication_type="push_pull", continuous=True, channels=channels)

    # 5. Now update doc in cbl and replicate to sync_gateway
    db.update_bulk_docs(database=cbl_db, number_of_updates=1)
    replicator.wait_until_replicator_idle(repl)

    # 5. Verify updated doc from cbl is pushed to sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    for doc in sg_docs:
        assert doc["doc"]["updates-cbl"] == 1, "cbl update did not pushed to sg"

    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_default_conflict_withConflicts_and_sgOffline(params_from_base_test_setup):
    """
        @summary:
        1. create docs in sg.
        2. Create two conflicts with 2-hex in sg.
        3. update doc in sg to have new revision to one of the conflicted branch of sg, the counter for property updates increments to 1
        4. Start replication with push pull and continous true
        5. wait until replication is done.
        6. Verify that default conflict resolver resolved appropriately.
        7. Stop sg.
        8. Now delete doc in cbl
        9. Wait until replication is done i.e
        9. Verify docs deleted in sg.

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels = ["replication-channel"]
    num_of_docs = 10
    username = "autotest"
    password = "password"

    if no_conflicts_enabled:
        pytest.skip('Cannot work with no-conflicts enabled')

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create docs in SG.
    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session)
    # 2. Create two conflicts with 2-hex in sg.
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa", auth=session)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa9b", auth=session)

    # 3. update doc in sg to have new revision to one of the conflicted branch of sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, delay=None,
                          auth=session, channels=channels)

    # 4. Start replication with push pull and contiinous true
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                              replication_type="push_pull", continuous=True, channels=channels)

    # 5. Stop sg
    status = c.sync_gateways[0].stop()
    assert status == 0, "sync_gateway did not stop"

    # 6. Now update and delete doc in cbl
    db.update_bulk_docs(cbl_db)
    db.cbl_delete_bulk_docs(cbl_db)

    # 7 . Start sg and wait until replication is idle
    status = c.sync_gateways[0].start(sg_config)
    assert status == 0, "sync_gateway did not start"
    count = 0
    while replicator.getActivitylevel(repl) == "offline" and count < 10:
        time.sleep(1)
        count += 1
    replicator.wait_until_replicator_idle(repl, err_check=False)

    # 8. Verify docs deleted in sg
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    for doc in sg_docs:
        try:
            doc["doc"]["updates-cbl"]
            assert False, "updated doc deleted in cbl, did not get deleted in sg"
        except KeyError:
            assert True

    # 9. update docs in sg and verify updated docs got replicated to cbl
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, delay=None,
                          auth=session, channels=channels)

    replicator.wait_until_replicator_idle(repl)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    # Wait until cbl got updates property
    count = 0
    while count < 30:
        try:
            cbl_docs[cbl_doc_ids[0]]["updates"]
            break
        except Exception:
            time.sleep(1)
            count += 1
            cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    for id in cbl_doc_ids:
        assert cbl_docs[id]["updates"] == 1, "sg updated docs did not get replicated to cbl"

    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_default_conflict_withConflicts_withChannels(params_from_base_test_setup):
    """
        @summary:
        1. create docs in sg.
        2. Create two channels and conflicts on each channel.
        3. update docs in sg.
        4. Start replication to each cbl db
        5. Wait for replication to be idle.
        6. update docs in cbl.
        7. Verify docs with updates from cbl got replicated to sg
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    no_conflicts_enabled = params_from_base_test_setup["no_conflicts_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    channels1 = ["replication-channel1"]
    channels2 = ["replication-channel2"]
    channels = ['replication-channel1', 'replication-channel2']
    num_of_docs = 10
    username1 = "autotest1"
    username2 = "autotest2"
    password = "password"

    if no_conflicts_enabled:
        pytest.skip('Cannot work with no-conflicts enabled')

    # Reset cluster to clean the data
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username1, password, channels=channels1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db, username2, password, channels=channels2, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db, username2, auth=auth)
    session2 = cookie2, session_id2

    sg_docs = document.create_docs(doc_id_prefix='sg_docs', number=num_of_docs, channels=channels)
    sg_docs = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session1)

    # Create two conflicts with 2-hex in sg by user1.
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa", auth=session1)
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["rev"],
                               new_revision="2-41fa9b", auth=session1)

    # Create two conflicts with 2-hex in sg by user2.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session2)
    sg_docs = sg_docs["rows"]
    for i in range(len(sg_docs)):
        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"],
                               parent_revisions=sg_docs[i]["value"]["rev"], new_revision="2-31fa", auth=session2)

        sg_client.add_conflict(url=sg_url, db=sg_db, doc_id=sg_docs[i]["id"], parent_revisions=sg_docs[i]["value"]["rev"],
                               new_revision="2-31fa9b", auth=session2)

    # sg update docs
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session2)
    sg_docs = sg_docs["rows"]
    sg_client.update_docs(url=sg_url, db=sg_db, docs=sg_docs, number_updates=1, delay=None,
                          auth=session2, channels=channels)

    # 2.Replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    repl1 = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                               replication_type="push_pull", continuous=True, channels=channels1)

    replicator_authenticator = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    repl2 = replicator.configure_and_replicate(source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
                                               replication_type="push_pull", continuous=True, channels=channels2)

    # 5. Now update doc in cbl and replicate to sync_gateway
    db.update_bulk_docs(database=cbl_db, number_of_updates=1)
    replicator.wait_until_replicator_idle(repl1)
    replicator.wait_until_replicator_idle(repl2)
    replicator.stop(repl1)
    replicator.stop(repl2)

    # 5. Verify updated doc from cbl is pushed to sg.
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session1, include_docs=True)
    sg_docs = sg_docs["rows"]
    for doc in sg_docs:
        assert doc["doc"]["updates-cbl"] == 1, "cbl update did not pushed to sg"
        assert doc["doc"]["updates"] == 1, "sg update is removed"

    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session2, include_docs=True)
    sg_docs = sg_docs["rows"]
    for doc in sg_docs:
        assert doc["doc"]["updates-cbl"] == 1, "cbl update did not pushed to sg"
        assert doc["doc"]["updates"] == 1, "sg update is removed"
    replicator.stop(repl1)
    replicator.stop(repl2)


@pytest.mark.listener
@pytest.mark.replication
def test_CBL_push_pull_with_sg_down(params_from_base_test_setup):
    """
        @summary:
        1. Have SG
        2. Create docs in CBL.
        3. push replication to SG
        4. update docs in SG.
        5. Restart sg in one thread
        6. do replication with pull in other thread to cbl
        7. Verify all docs replicated successfully to cbl
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"
    num_of_docs = 1000

    channels = ["Replication"]
    sg_client = MobileRestClient()

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 2. Create docs in CBL.
    db.create_bulk_docs(num_of_docs, "cbl", db=cbl_db, channels=channels, attachments_generator=attachment.generate_2_png_10_10)

    # 3. push replication to SG
    replicator = Replication(base_url)

    authenticator = Authenticator(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    session = cookie, session_id
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    with ThreadPoolExecutor(max_workers=4) as tpe:
        wait_until_replicator_completes = tpe.submit(
            replicator.configure_and_replicate,
            source_db=cbl_db, replicator_authenticator=replicator_authenticator, target_db=None, target_url=sg_blip_url, replication_type="push_pull", continuous=True,
            channels=channels, err_check=False
        )

        start_sg_task = tpe.submit(
            restart_sg,
            c=c,
            sg_conf=sg_config,
            cluster_config=cluster_config
        )
        repl = wait_until_replicator_completes.result()
        start_sg_task.result()

    cbl_doc_ids = db.getDocIds(cbl_db)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    assert len(sg_docs) == len(cbl_doc_ids), "Docs did not get replicated when sync-gateway restarted"
    assert len(sg_docs) == num_of_docs
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("topology_type, num_of_docs, attachments", [
    ('1cbl_1sg', 10, True),
    ('3cbl_1sg', 10, True),
    ('1cbl_1sg', 10, False),
    ('3cbl_1sg', 10, False)
])
def test_replication_with_3Channels(params_from_base_test_setup, setup_customized_teardown_test, topology_type, num_of_docs, attachments):
    """
        @summary:
        1. Create 3 users in SG with 3 differrent channels.
        2. Create docs in sg in all 3 channels
        3. replication to CBL with continous true and push_pull on 3 CBL DBs assosiated with each sg channel.
        4. verify in CBL , docs got replicated to each DB appropirately

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    sg_mode = params_from_base_test_setup["mode"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10

    channel1 = ["Replication-1"]
    channel2 = ["Replication-2"]
    channel3 = ["Replication-3"]

    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    cbl_db2 = setup_customized_teardown_test["cbl_db2"]
    cbl_db3 = setup_customized_teardown_test["cbl_db3"]

    if topology_type == "1cbl_1sg":
        cbl_db1 = cbl_db
        cbl_db2 = cbl_db
        cbl_db3 = cbl_db

    username1 = "autotest"
    username2 = "autotest2"
    username3 = "autotest3"
    password = "password"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create 3 users in SG with 3 differrent channels.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username1, password=password, channels=channel1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db, username2, password=password, channels=channel2, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db, username2, auth=auth)
    session2 = cookie2, session_id2

    sg_client.create_user(sg_admin_url, sg_db, username3, password=password, channels=channel3, auth=auth)
    cookie3, session_id3 = sg_client.create_session(sg_admin_url, sg_db, username3, auth=auth)
    session3 = cookie3, session_id3

    # 2. Create docs in sg in all 3 channels
    if attachments:
        sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=channel1, attachments_generator=attachment.generate_2_png_10_10)
        sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=channel2, attachments_generator=attachment.generate_2_png_10_10)
        sg_docs3 = document.create_docs(doc_id_prefix='sg_docs-3', number=num_of_docs, channels=channel3, attachments_generator=attachment.generate_2_png_10_10)
    else:
        sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=channel1)
        sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=channel2)
        sg_docs3 = document.create_docs(doc_id_prefix='sg_docs-3', number=num_of_docs, channels=channel3)

    sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session1)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs2, auth=session2)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs3, auth=session3)

    # 3. replication to CBL with continous true and push_pull on 3 CBL DBs assosiated with each sg channel.
    replicator = Replication(base_url)
    replicator_authenticator1 = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url,
                                       replication_type="pull", continuous=False, channels=channel1)
    replicator_authenticator2 = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url,
                                       replication_type="pull", continuous=False, channels=channel2)
    replicator_authenticator3 = authenticator.authentication(session_id3, cookie3, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db3, replicator_authenticator=replicator_authenticator3, target_url=sg_blip_url,
                                       replication_type="pull", continuous=False, channels=channel3)

    if sg_mode == "di":
        replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url,
                                           replication_type="pull", continuous=False, channels=channel1)
        replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url,
                                           replication_type="pull", continuous=False, channels=channel2)
        replicator.configure_and_replicate(source_db=cbl_db3, replicator_authenticator=replicator_authenticator3, target_url=sg_blip_url,
                                           replication_type="pull", continuous=False, channels=channel3)
    # 4. verify in CBL , docs got replicated to each DB appropirately
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db, session1, cbl_db1, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db, session2, cbl_db2, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db, session3, cbl_db3, db)


@pytest.mark.listener
@pytest.mark.replication
def test_replication_with_privatePublicChannels(params_from_base_test_setup, setup_customized_teardown_test):
    """
    @summary:
    1. Create 2 users , one with private and other with public channel
    2. Create docs in sg in one private channel and public channel
    3. replication to CBL with continous False and push_pull to CBL .
    4. verify in CBL , only docs from public channel is replicated
    5. update docs in cbl
    6. Verify updated docs got replicated to sg
    """

    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    num_of_docs = 10

    privateChannel1 = ["Replication-1"]
    publicChannel = ["!"]

    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    username1 = "autotest"
    username2 = "autotest2"
    password = "password"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # 1. Create 2 users in SG with 2 differrent channels.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username1, password=password, channels=privateChannel1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db, username2, password=password, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db, username2, auth=auth)
    session2 = cookie2, session_id2

    # 2. Create docs in sg in one private channel and public channel
    sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=privateChannel1)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session1)

    sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=publicChannel)
    sg_client.add_bulk_docs(url=sg_admin_url, db=sg_db, docs=sg_docs2, auth=auth)

    # 3. replication to CBL with continous False and push_pull to CBL .
    replicator = Replication(base_url)
    replicator_authenticator1 = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url,
                                       replication_type="pull", continuous=False, channels=publicChannel)

    if sg_mode == "di":
        replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url,
                                           replication_type="pull", continuous=False, channels=publicChannel)
    # 4. verify in CBL , only docs from public channel is replicated
    cbl_doc_ids = db.getDocIds(cbl_db1)
    for doc in sg_docs2:
        assert doc["_id"] in cbl_doc_ids, "doc with public channel did not replicate to cbl"

    for doc in sg_docs:
        assert doc["_id"] not in cbl_doc_ids, "doc with public channel replicated to cbl"

    # 5. update docs in cbl
    # Verify updated docs got replicated to sg
    db.update_bulk_docs(cbl_db1)
    replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url,
                                       replication_type="push_pull", continuous=False, channels=publicChannel)
    sg_docs_new = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session1, include_docs=True)
    sg_docs_new = sg_docs_new["rows"]
    sg_docs2_ids = [row["_id"] for row in sg_docs2]
    for doc in sg_docs_new:
        if doc["id"] in sg_docs2_ids:
            assert doc["doc"]["updates-cbl"] == 1, "sg docs with public channel did not have updated from cbl"
        else:
            try:
                doc["doc"]["updates-cbl"]
                assert False, "private channel docs also got update from cbl"
            except KeyError:
                assert True

    sg_docs_new2 = sg_client.get_all_docs(url=sg_url, db=sg_db, auth=session2, include_docs=True)
    sg_docs_new2 = sg_docs_new2["rows"]
    for doc in sg_docs_new2:
        assert doc["doc"]["updates-cbl"] == 1, "sg docs with public channel did not have updated from cbl in session2"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("topology_type", [
    ('2cbl_2sg'),
    ('1cbl_2sg')
])
def test_replication_withChannels1_withMultipleSgDBs(params_from_base_test_setup, setup_customized_teardown_test, topology_type):
    """
        @summary:
        1. Create 2 users in SG with 2 SG dbs with 2 differrent channels.
        2. Create docs in sg in all 2 channels with 2 sg DBs
        3. replication to CBL with continous False and push_pull on 2 CBL DBs assosiated with each sg Dbs.
        4. verify in CBL , docs got replicated to each DB appropirately

    """
    sg_db1 = "sg_db1"
    sg_db2 = "sg_db2"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_mode = params_from_base_test_setup["mode"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    sg_blip_url1 = sg_blip_url.replace("db", "sg_db1")
    sg_blip_url2 = sg_blip_url.replace("db", "sg_db2")

    channel1 = ["Replication-1"]
    channel2 = ["Replication-2"]

    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    cbl_db2 = setup_customized_teardown_test["cbl_db2"]

    username1 = "autotest"
    username2 = "autotest2"
    password = "password"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    c = cluster.Cluster(config=cluster_config)
    sg_config = sg_config = sync_gateway_config_path_for_mode("listener_tests/multiple_sync_gateways", sg_mode)
    c.reset(sg_config_path=sg_config)

    # 1. Create 2 users in SG with 2 differrent channels.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db1, username1, password=password, channels=channel1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db1, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db2, username2, password=password, channels=channel2, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db2, username2, auth=auth)
    session2 = cookie2, session_id2

    # 2. Create docs in sg in 2 channels
    sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=channel1)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db1, docs=sg_docs, auth=session1)

    sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=channel2)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db2, docs=sg_docs2, auth=session2)

    # 3. replication to CBL with continous true and push_pull on 2 CBL DBs assosciated with each sg channel.
    if topology_type == "1cbl_2sg":
        cbl_db1 = cbl_db
        cbl_db2 = cbl_db
    replicator = Replication(base_url)
    replicator_authenticator1 = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    repl1 = replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url1,
                                               replication_type="pull", continuous=True, channels=channel1)
    replicator_authenticator2 = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    repl2 = replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url2,
                                               replication_type="pull", continuous=True, channels=channel2)

    # 4. verify in CBL , docs got replicated to each DB appropirately
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db1, session1, cbl_db1, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db2, session2, cbl_db2, db)
    replicator.stop(repl1)
    replicator.stop(repl2)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("topology_type", [
    ('3cbl_3sg'),
    ('1cbl_3sg')
])
def test_replication_withMultipleBuckets(params_from_base_test_setup, setup_customized_teardown_test, topology_type):
    """
        @summary:
        1. Create couple of buckets in CBS.
        2. Configure sync-gateway by mapping each sg db to each bucket.
        3. Create docs in all 3 sg dbs.
        3. Start replication to cBL with multiple replicator instances
        4. Each replicator instance to each bucket DB
        5. Verify CBL got docs from all buckets to all CBL DBs.
        6. updated docs in cbl.
        7. Verify updated docs got replicated to sg
    """
    sg_db1 = "sg_db1"
    sg_db2 = "sg_db2"
    sg_db3 = "sg_db3"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_mode = params_from_base_test_setup["mode"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    sg_blip_url1 = sg_blip_url.replace("db", "sg_db1")
    sg_blip_url2 = sg_blip_url.replace("db", "sg_db2")
    sg_blip_url3 = sg_blip_url.replace("db", "sg_db3")

    channel1 = ["Replication-1"]
    channel2 = ["Replication-2"]
    channel3 = ["Replication-3"]

    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    cbl_db2 = setup_customized_teardown_test["cbl_db2"]
    cbl_db3 = setup_customized_teardown_test["cbl_db3"]

    username1 = "autotest"
    username2 = "autotest2"
    username3 = "autotest3"
    password = "password"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    # 2. Configure sync-gateway by mapping each sg db to each bucket.
    c = cluster.Cluster(config=cluster_config)
    sg_config = sg_config = sync_gateway_config_path_for_mode("listener_tests/three_sync_gateways", sg_mode)
    c.reset(sg_config_path=sg_config)

    # 3. Create user and  Create docs in all 3 sg dbs.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db1, username1, password=password, channels=channel1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db1, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db2, username2, password=password, channels=channel2, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db2, username2, auth=auth)
    session2 = cookie2, session_id2

    sg_client.create_user(sg_admin_url, sg_db3, username3, password=password, channels=channel3, auth=auth)
    cookie3, session_id3 = sg_client.create_session(sg_admin_url, sg_db3, username3, auth=auth)
    session3 = cookie3, session_id3

    sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=channel1)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db1, docs=sg_docs, auth=session1)

    sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=channel2)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db2, docs=sg_docs2, auth=session2)

    sg_docs3 = document.create_docs(doc_id_prefix='sg_docs-3', number=num_of_docs, channels=channel3)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db3, docs=sg_docs3, auth=session3)

    # 4. Start replication to CBL with multiple replicator instances
    if topology_type == "1cbl_3sg":
        cbl_db1 = cbl_db
        cbl_db2 = cbl_db
        cbl_db3 = cbl_db
    replicator = Replication(base_url)
    replicator_authenticator1 = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    repl1 = replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url1,
                                               replication_type="pull", continuous=True, channels=channel1)
    replicator_authenticator2 = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    repl2 = replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url2,
                                               replication_type="pull", continuous=True, channels=channel2)
    replicator_authenticator3 = authenticator.authentication(session_id3, cookie3, authentication_type="session")
    repl3 = replicator.configure_and_replicate(source_db=cbl_db3, replicator_authenticator=replicator_authenticator3, target_url=sg_blip_url3,
                                               replication_type="pull", continuous=True, channels=channel3)

    replicator.stop(repl1)
    replicator.stop(repl2)
    replicator.stop(repl3)

    # 4. verify in CBL , docs got replicated to each DB appropirately
    # cbl_doc_ids = db.getDocIds(cbl_db1)
    # cbl_docs = db.getDocuments(cbl_db1, cbl_doc_ids)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db1, session1, cbl_db1, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db2, session2, cbl_db2, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db3, session3, cbl_db3, db)

    # 5 update docs in cbl and verify docs got replicated to sg
    db.update_bulk_docs(cbl_db1)
    db.update_bulk_docs(cbl_db2)
    db.update_bulk_docs(cbl_db3)

    # 6. Verify in sync-gateway docs got replicated.
    replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url1,
                                       replication_type="push", continuous=False, channels=channel1)
    replicator_authenticator2 = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url2,
                                       replication_type="push", continuous=False, channels=channel2)
    replicator_authenticator3 = authenticator.authentication(session_id3, cookie3, authentication_type="session")
    replicator.configure_and_replicate(source_db=cbl_db3, replicator_authenticator=replicator_authenticator3, target_url=sg_blip_url3,
                                       replication_type="push", continuous=False, channels=channel3)

    verify_cblDocs_in_sgDocs(sg_client, sg_url, sg_db1, session1, cbl_db1, db, topology_type=topology_type)
    verify_cblDocs_in_sgDocs(sg_client, sg_url, sg_db2, session2, cbl_db2, db, topology_type=topology_type)
    verify_cblDocs_in_sgDocs(sg_client, sg_url, sg_db3, session3, cbl_db3, db, topology_type=topology_type)


@pytest.mark.listener
@pytest.mark.replication
def test_replication_1withMultipleBuckets_deleteOneBucket(params_from_base_test_setup, setup_customized_teardown_test):
    """
        @summary:
        1. Create couple of buckets in CBS.
        2. Configure sync-gateway by mapping each sg db to each bucket.
        3. Create docs in all 3 sg dbs.
        3. Start replication to cBL with multiple replicator instances
        4. Each replicator instance to each bucket DB.
        5. Delete 3rd bucket on CBS.
        6. Continue replication.
        5. Verify docs of 3rd bucket is removed from CBL.
    """
    sg_db1 = "sg_db1"
    sg_db2 = "sg_db2"
    sg_db3 = "sg_db3"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_mode = params_from_base_test_setup["mode"]
    db = params_from_base_test_setup["db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    num_of_docs = 10
    sg_blip_url1 = sg_blip_url.replace("db", "sg_db1")
    sg_blip_url2 = sg_blip_url.replace("db", "sg_db2")
    sg_blip_url3 = sg_blip_url.replace("db", "sg_db3")

    channel1 = ["Replication-1"]
    channel2 = ["Replication-2"]
    channel3 = ["Replication-3"]

    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    cbl_db2 = setup_customized_teardown_test["cbl_db2"]
    cbl_db3 = setup_customized_teardown_test["cbl_db3"]

    username1 = "autotest"
    username2 = "autotest2"
    username3 = "autotest3"
    password = "password"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)

    # 1. Create couple of buckets in CBS.
    cluster_util = ClusterKeywords(cluster_config)
    topology = cluster_util.get_cluster_topology(cluster_config)
    cb_server_url = topology["couchbase_servers"][0]
    cb_server = couchbaseserver.CouchbaseServer(url=cb_server_url)

    # 2. Configure sync-gateway by mapping each sg db to each bucket.
    c = cluster.Cluster(config=cluster_config)
    sg_config = sg_config = sync_gateway_config_path_for_mode("listener_tests/three_sync_gateways", sg_mode)
    c.reset(sg_config_path=sg_config)

    # 3. Create user and  Create docs in all 3 sg dbs.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db1, username1, password=password, channels=channel1, auth=auth)
    cookie1, session_id1 = sg_client.create_session(sg_admin_url, sg_db1, username1, auth=auth)
    session1 = cookie1, session_id1

    sg_client.create_user(sg_admin_url, sg_db2, username2, password=password, channels=channel2, auth=auth)
    cookie2, session_id2 = sg_client.create_session(sg_admin_url, sg_db2, username2, auth=auth)
    session2 = cookie2, session_id2

    sg_client.create_user(sg_admin_url, sg_db3, username3, password=password, channels=channel3, auth=auth)
    cookie3, session_id3 = sg_client.create_session(sg_admin_url, sg_db3, username3, auth=auth)
    session3 = cookie3, session_id3

    sg_docs = document.create_docs(doc_id_prefix='sg_docs-1', number=num_of_docs, channels=channel1)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db1, docs=sg_docs, auth=session1)

    sg_docs2 = document.create_docs(doc_id_prefix='sg_docs-2', number=num_of_docs, channels=channel2)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db2, docs=sg_docs2, auth=session2)

    sg_docs3 = document.create_docs(doc_id_prefix='sg_docs-3', number=num_of_docs, channels=channel3)
    sg_client.add_bulk_docs(url=sg_url, db=sg_db3, docs=sg_docs3, auth=session3)

    # 4. Start replication to CBL with multiple replicator instances
    replicator = Replication(base_url)
    replicator_authenticator1 = authenticator.authentication(session_id1, cookie1, authentication_type="session")
    repl1 = replicator.configure_and_replicate(source_db=cbl_db1, replicator_authenticator=replicator_authenticator1, target_url=sg_blip_url1,
                                               replication_type="push_pull", continuous=True, channels=channel1)
    replicator_authenticator2 = authenticator.authentication(session_id2, cookie2, authentication_type="session")
    repl2 = replicator.configure_and_replicate(source_db=cbl_db2, replicator_authenticator=replicator_authenticator2, target_url=sg_blip_url2,
                                               replication_type="push_pull", continuous=True, channels=channel2)
    replicator_authenticator3 = authenticator.authentication(session_id3, cookie3, authentication_type="session")
    repl3 = replicator.configure_and_replicate(source_db=cbl_db3, replicator_authenticator=replicator_authenticator3, target_url=sg_blip_url3,
                                               replication_type="push_pull", continuous=True, channels=channel3)

    # 5. Deleted 3rd bucket on CBS.
    cb_server.delete_bucket(name="data-bucket-3")

    # 6. Continue replication.
    replicator.wait_until_replicator_idle(repl1)
    replicator.wait_until_replicator_idle(repl2)
    replicator.wait_until_replicator_idle(repl3)

    # 7. Verify 3rd bucket's docs are still exists in sg
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db1, session1, cbl_db1, db)
    verify_sgDocIds_cblDocIds(sg_client, sg_url, sg_db2, session2, cbl_db2, db)
    cbl_doc_ids = db.getDocIds(cbl_db3)
    assert len(cbl_doc_ids) == num_of_docs, "cbl docs not deleted when assosiated bucket is deleted in CBS"
    replicator.stop(repl1)
    replicator.stop(repl2)
    replicator.stop(repl3)


@pytest.mark.listener
@pytest.mark.replication
def test_replication_multipleChannels_withFilteredDocIds(params_from_base_test_setup):
    """
        @summary:
        1. Create  users in SG with 2 user channels
        2. Create docs in sg in both channels
        3. replication to CBL with continous true/false and push_pull on 1 CBL DB
           with document filters.
        4. verify in CBL , filtered docs from 2 channels got replicated
        NOTE: Only works with one shot replication for filtered doc ids
    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    sg_mode = params_from_base_test_setup["mode"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channel1 = ["ABC"]
    channel2 = ["xyz"]
    username1 = "autotest"
    username2 = "autotest2"
    num_of_docs = 10
    # channel2 = ["xyz"]
    sg_client = MobileRestClient()
    replicator = Replication(base_url)

    if sg_mode == "di":
        pytest.skip('Filter doc ids does not work with di modes')

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username1, password="password", channels=channel1, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, username1, auth=auth)
    auth_session1 = cookie, session
    sg_client.create_user(sg_admin_url, sg_db, username2, password="password", channels=channel2, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, username2, auth=auth)
    auth_session2 = cookie, session
    sg_added_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="channel1-", channels=channel1, auth=auth_session1)
    sg_added_ids1 = [row["id"] for row in sg_added_docs]

    sg_added_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="channel2-", channels=channel2, auth=auth_session2)
    sg_added_ids2 = [row["id"] for row in sg_added_docs]

    sg_combined_ids = sg_added_ids1 + sg_added_ids2
    num_of_filtered_ids = 7
    list_of_filtered_ids = random.sample(sg_combined_ids, num_of_filtered_ids)

    # Start and stop continuous replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(username=username1, password="password", authentication_type="basic")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, replication_type="push_pull", continuous=False,
                                       documentIDs=list_of_filtered_ids, channels=channel1, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    # Filter doc ids is supported only for one shot replication, Cannot support for continuous replication
    replicator_authenticator = authenticator.authentication(username=username2, password="password", authentication_type="basic")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, replication_type="push_pull", continuous=False,
                                       documentIDs=list_of_filtered_ids, channels=channel2, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    # Verify only filtered cbl doc ids are replicated to sg
    cbl_doc_ids = db.getDocIds(cbl_db)
    list_of_non_filtered_ids = set(sg_combined_ids) - set(list_of_filtered_ids)
    assert len(cbl_doc_ids) == len(list_of_filtered_ids), "filtered doc ids are not replicated "
    for id in list_of_filtered_ids:
        assert id in cbl_doc_ids, "filtered doc id is not replicated to cbl"

    # Verify non filtered docs ids are not replicated in sg
    for doc_id in list_of_non_filtered_ids:
        assert doc_id not in cbl_doc_ids, "Non filtered doc id is replicated to cbl"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("replication_type, target_db", [
    ('one_way', "sg"),
    ('two_way', "sg"),
    ('one_way', 'cbl'),
    ('two_way', 'cbl'),
])
def test_resetCheckpointWithPurge(params_from_base_test_setup, replication_type, target_db):
    """
        @summary
        create docs in cbl db1
        replicate docs to sg/cbl2 db
        purge docs in cbl db1
        replicate again
        Verify  purged docs should not get replciated
        stop replicator
        call reset api
        restart the replication
        Verify all purged docs got back in CBL
    """
    sg_db = "db"

    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    db_config = params_from_base_test_setup["db_config"]
    liteserv_version = params_from_base_test_setup["liteserv_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    if liteserv_version < "2.1":
        pytest.skip('database encryption feature not available with version < 2.1')

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    if target_db == "cbl":
        cbl_db_name2 = "cbl_db2" + str(time.time())
        cbl_db2 = db.create(cbl_db_name2, db_config)

    channel = ["ABC"]
    username = "autotest"
    num_of_docs = 10
    sg_client = MobileRestClient()
    document = Document(base_url)

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password="password", channels=channel, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    auth_session = cookie, session_id

    # Create docs and start replication to sg
    cbl_doc_ids = db.create_bulk_docs(num_of_docs, "reset-checkpoint-docs", db=cbl_db, channels=channel)
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    if replication_type == "one_way" and target_db == "sg":
        repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel, replicator_authenticator=replicator_authenticator, replication_type="push")
    if replication_type == "two_way" and target_db == "sg":
        repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel, replicator_authenticator=replicator_authenticator)
    if replication_type == "one_way" and target_db == "cbl":
        repl_config = replicator.configure(cbl_db, target_db=cbl_db2, continuous=True, replicator_authenticator=replicator_authenticator, replication_type="push")
    if replication_type == "two_way" and target_db == "cbl":
        repl_config = replicator.configure(cbl_db, target_db=cbl_db2, continuous=True, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    if replication_type == "one_way":
        replicator.stop(repl)
    sg_client.get_all_docs(url=sg_url, db=sg_db, auth=auth_session)

    # Wait until replication is idle and verify purged docs in cbl is not replicated in sg
    if replication_type == "one_way":
        if target_db == "sg":
            repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel,
                                               replicator_authenticator=replicator_authenticator, replication_type="pull")
        else:
            repl_config = replicator.configure(cbl_db, target_db=cbl_db2, continuous=True,
                                               replicator_authenticator=replicator_authenticator, replication_type="pull")
        repl = replicator.create(repl_config)
        replicator.start(repl)
        replicator.wait_until_replicator_idle(repl)

    assert db.getCount(cbl_db) == num_of_docs, "Docs in cbl is lost"
    # Purge docs in CBL
    for i in cbl_doc_ids:
        doc = db.getDocument(cbl_db, doc_id=i)
        mutable_doc = document.toMutable(doc)
        db.purge(cbl_db, mutable_doc)
    assert db.getCount(cbl_db) == 0, "Docs that got purged in CBL did not get deleted"
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)
    assert db.getCount(cbl_db) == 0, "Docs that got purged in CBL did not get deleted"

    # Reset checkpoint and do replication again from sg to cbl
    # Verify all docs are back
    replicator.resetCheckPoint(repl)
    if liteserv_version < "3.0":
        replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    assert db.getCount(cbl_db) == num_of_docs, "Docs that got purged in CBL did not got back after resetCheckpoint"
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_resetCheckpointFailure(params_from_base_test_setup):
    """
        @summary
        create docs
        replicate docs
        call reset api
        verify it throws an error that checkpoint reset is called without stopping replicator.
    """
    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    liteserv_version = params_from_base_test_setup["liteserv_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    if liteserv_version < "2.1":
        pytest.skip('resetCheckpointFailure feature not available with version < 2.1')

    if liteserv_version >= "3.0":
        pytest.skip('resetCheckpointFailure API has been deprecated in 2.8 and removed in 3.0')

    if(liteserv_platform.lower() == "ios"):
        pytest.skip('ResetCheckPoint API does not throw exception in iOS if replicator is not stopped, so skipping test')
        # It crashes the app, but does not throw error

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channel = ["ABC"]
    username = "autotest"
    num_of_docs = 10
    sg_client = MobileRestClient()

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password="password", channels=channel, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)

    # Create docs and start replication to sg
    db.create_bulk_docs(num_of_docs, "reset-checkpoint-docs", db=cbl_db, channels=channel)
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel, replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    # call reset api
    # verify it throws an error that checkpoint reset is called without stopping replicator.
    with pytest.raises(Exception) as he:
        replicator.resetCheckPoint(repl)
    message = str(he.value)
    assert 'Replicator is not stopped.' in message
    assert 'Resetting checkpoint is only allowed when the replicator is in the stopped state' in message, "Reset the checkpoint should have thrown exception to inform that replicator is not stopped."
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("replication_type, target_db", [
    ('one_way', "sg"),
    ('two_way', "sg"),
    ('one_way', 'cbl'),
    ('two_way', 'cbl'),
])
def test_resetCheckpointWithUpdate(params_from_base_test_setup, replication_type, target_db):
    """
        @summary
        create docs
        replicate docs
        purge docs
        replicate again
        Verify  purged docs should not get replciated
        stop replicator
        call reset api
        restart the replication
        Verify all purged docs got back in CBL
    """

    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    db_config = params_from_base_test_setup["db_config"]
    liteserv_version = params_from_base_test_setup["liteserv_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    if liteserv_version < "2.1":
        pytest.skip('database encryption feature not available with version < 2.1')

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    if target_db == "cbl":
        cbl_db_name2 = "cbl_db2" + str(time.time())
        cbl_db2 = db.create(cbl_db_name2, db_config)

    channel = ["ABC"]
    username = "autotest"
    num_of_docs = 10
    sg_client = MobileRestClient()

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password="password", channels=channel, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)

    # Create docs and start replication to sg
    db.create_bulk_docs(num_of_docs, "reset-checkpoint-docs", db=cbl_db, channels=channel)
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    if replication_type == "one_way" and target_db == "sg":
        repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel, replicator_authenticator=replicator_authenticator, replication_type="push")
    if replication_type == "two_way" and target_db == "sg":
        repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=True, channels=channel, replicator_authenticator=replicator_authenticator)
    if replication_type == "one_way" and target_db == "cbl":
        repl_config = replicator.configure(cbl_db, target_db=cbl_db2, continuous=True, replicator_authenticator=replicator_authenticator, replication_type="push")
    if replication_type == "two_way" and target_db == "cbl":
        repl_config = replicator.configure(cbl_db, target_db=cbl_db2, continuous=True, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    if replication_type == "one_way":
        replicator.stop(repl)

    # Now start pull replication for one-way
    if replication_type == "one_way":
        replicator.setReplicatorType(repl_config, "pull")
        repl = replicator.create(repl_config)
        replicator.start(repl)
        replicator.wait_until_replicator_idle(repl)

    assert db.getCount(cbl_db) == num_of_docs, "Docs in cbl is lost"
    update_and_resetCheckPoint(db, cbl_db, replicator, repl, replication_type, repl_config, 1)
    update_and_resetCheckPoint(db, cbl_db, replicator, repl, replication_type, repl_config, 2)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("sg_conf_name, delete_doc_type", [
    ('sync_gateway_rev_cache_size5', "purge")
])
def test_CBL_SG_replication_with_rev_messages(params_from_base_test_setup, sg_conf_name, delete_doc_type):
    """
        @summary:
        reference : https://github.com/couchbase/sync_gateway/issues/3738#issuecomment-422107759
        1. Set up SGW with xattrs enabled.
        2. Create doc in CBL
        3. push replication to SG with continuous
        4. Purge doc in SGW.
        5. Create 5 docs in CBL and push to SGW. This will flush doc-1's rev out of the SG's revision cache (size = 5) which set up sg config.
        6. Delete database and create same database again and pull replication from SGW.
        7. wait for replication to finish.
        8. Verify total and completed are same once replication is completed.
        9. Verify all docs from SGW replicated successfully.

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_mode = params_from_base_test_setup["mode"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    xattrs_enabled = params_from_base_test_setup["xattrs_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    num_of_docs = 5
    username = "autotest"
    password = "password"

    if sync_gateway_version < "2.1.1":
        pytest.skip('--no-conflicts is enabled and does not work with sg < 2.1.1 , so skipping the test')

    if not xattrs_enabled:
        pytest.skip('--xattrs is not enabled , so skipping the test')
    channels = ["Replication"]
    sg_client = MobileRestClient()

    # Reset sg config with config which is required
    # 1. Set up SGW with xattrs enabled.
    sg_config = sync_gateway_config_path_for_mode(sg_conf_name, sg_mode)
    cl = cluster.Cluster(config=cluster_config)
    cl.reset(sg_config_path=sg_config)

    # 2. Create doc in CBL
    cbl_db_name = "cbl_db1" + str(time.time())
    db_config = db.configure()
    cbl_db1 = db.create(cbl_db_name, db_config)
    db.create_bulk_docs(number=1, id_prefix="rev_messages_prev", db=cbl_db1, channels=channels)
    cbl_added_doc_ids = db.getDocIds(cbl_db1)

    # 3. push replication to SG with continuous
    # Start and stop continuous replication
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    auth_session = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)

    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(username=username, password=password, authentication_type="basic")
    repl_config = replicator.configure(cbl_db1, target_url=sg_blip_url, replication_type="push", continuous=True,
                                       channels=channels, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)

    sg_docs, errors = sg_client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=cbl_added_doc_ids, auth=auth_session)
    for doc in sg_docs:
        sg_client.purge_doc(url=sg_admin_url, db=sg_db, doc=doc, auth=auth)

    # 5. Create docs in CBL and push to SGW. This will flush doc-1's rev out of the SG's revision cache (size = 1000) which set up sg config.
    db.create_bulk_docs(number=num_of_docs, id_prefix="rev_messages", db=cbl_db1, channels=channels)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 6. Delete database and create same database again and pull replication from SGW.
    db.deleteDB(cbl_db1)
    db_config1 = db.configure()
    cbl_db2 = db.create(cbl_db_name, db_config1)

    repl_config = replicator.configure(cbl_db2, target_url=sg_blip_url, replication_type="pull", continuous=True,
                                       channels=channels, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    cbl_doc_ids = db.getDocIds(cbl_db2)
    assert len(cbl_doc_ids) == num_of_docs, "number of doc ids which got replicated for SGW"
    assert replicator.getCompleted(repl) == replicator.getTotal(repl), "Replication total and completed are not same"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize(
    'replicator_authenticator',
    [
        ('basic'),
        ('session')
    ]
)
def test_replication_push_replication_guest_enabled(params_from_base_test_setup, replicator_authenticator):
    """
        @summary:
        1.Enable guest user in sync-gateway
        2. login as invalid login on cbl
        3. verify user can login successfully in cbl
        4. Also verify user with valid credentials should be able to login successfully

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    """
    TODO : https://github.com/couchbase/sync_gateway/issues/3830
    # Enable this commented code once 3830 is fixed.It should be fixed by june 2019
    invalid_username = "invalid_username"
    invalid_password = "invalid_password"
    invalid_session = "invalid_session"
    """
    valid_username = "autotest"
    valid_password = "password"
    num_docs = 5

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    sg_config = sync_gateway_config_path_for_mode("sync_gateway_guest_enabled", mode)
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC"]
    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    db.create_bulk_docs(num_docs, "cbl", db=cbl_db, channels=channels)
    sg_client.create_user(sg_admin_url, sg_db, valid_username, password=valid_password, channels=channels, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, valid_username, auth=auth)

    """
    TODO : https://github.com/couchbase/sync_gateway/issues/3830
    # Enable this commented code once 3830 is fixed.It should be fixed by june 2019
    # login as invalid user on cbl and verify user can login successfully and docs got replicated successfully

    if replicator_authenticator == "session":
        replicator_authenticator = authenticator.authentication(invalid_session, cookie, authentication_type="session")
    elif replicator_authenticator == "basic":
        replicator_authenticator = authenticator.authentication(username=invalid_username, password=invalid_password, authentication_type="basic")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True, replication_type="push", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    error = replicator.getError(repl)
    assert "401" in error, "did not throw 401 error for invalid authentication"

    replicator.stop(repl)
    """
    # Also verify user with valid credentials should be able to login successfully
    db.create_bulk_docs(num_docs, "cbl2", db=cbl_db, channels=channels)
    if replicator_authenticator == "session":
        replicator_authenticator = authenticator.authentication(session, cookie, authentication_type=replicator_authenticator)
    elif replicator_authenticator == "basic":
        replicator_authenticator = authenticator.authentication(username=valid_username, password=valid_password, authentication_type=replicator_authenticator)
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, continuous=True, replication_type="push", replicator_authenticator=replicator_authenticator)

    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db)
    assert len(sg_docs["rows"]) == num_docs * 2, "Number of sg docs is not equal to total number of cbl docs and sg docs"
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
def test_doc_removal_from_channel(params_from_base_test_setup):
    """
        @summary:
        1. Create 2 docs in CBL with channel A, B
        2. Create user in SGW with channel A, B.
        3. push_pull replicate to SGW
        4. remove doc A from channel A
        5. Remove doc B from channel A , B
        6. continue push_pull replication
        7. Verify user can only access doc A, but not doc B

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"
    document_obj = Document(base_url)

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannot run with sg version below 2.5.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channels = ["ABC", "DEF"]

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    # 1. Create 2 docs in CBL with channel ABC, DEF
    cbl_ids = db.create_bulk_docs(2, "cbl", db=cbl_db, channels=channels)

    # 2. Create users in SGW with channel ABC, DEF
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 3. push_pull replicate to SGW
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator)

    # 4. remove doc A from channel A
    doc_obj_A = db.getDocument(cbl_db, cbl_ids[0])
    doc_A_mut = document_obj.toMutable(doc_obj_A)
    doc_body_A = document_obj.toMap(doc_A_mut)
    doc_body_A["channels"] = ["DEF"]
    db.updateDocument(database=cbl_db, data=doc_body_A, doc_id=cbl_ids[0])

    # 5. Remove doc B from channel A , B
    doc_obj_B = db.getDocument(cbl_db, cbl_ids[1])
    doc_B_mut = document_obj.toMutable(doc_obj_B)
    doc_body_B = document_obj.toMap(doc_B_mut)
    doc_body_B["channels"] = []
    db.updateDocument(database=cbl_db, data=doc_body_B, doc_id=cbl_ids[1])

    # 6. continue push_pull replication
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 7. Verify user can only access doc A, but not doc B
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, include_docs=True, auth=session)["rows"]
    assert len(sg_docs) == 1, "did not remove channels appropriately"
    sg_doc_ids = [doc['id'] for doc in sg_docs]
    assert cbl_ids[0] in sg_doc_ids, "doc A does not exist for the user"
    assert cbl_ids[1] not in sg_doc_ids, "doc B exist for the user"

    # Verify user can only access doc A, but not doc B on CBL side too
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert cbl_ids[1] not in cbl_doc_ids, "user on cbl still able to access the doc even after unshare"


@pytest.mark.listener
@pytest.mark.replication
def test_doc_removal_with_multipleChannels(params_from_base_test_setup, setup_customized_teardown_test):
    """
        @summary:
        1. Create users in SGW with multiple channels
            user A -> channel_A,channel_B, channel_C;
            userB -> channel_B,
            userC-> channel_C
        2. create docs in SGW
            doca with channel_A, channel_B ;
            docb with channel_B ,
            docc with channel_C
        3. Verify User A can access docA and docC.
            docB by UserB, UserA
            docC by user A, user C
        4. Remove the channel c from all the docs.
        5. Verify userA can access only docA and doc B, but not docC
            UserB can access docB
            UserC cannot access docC
    """

    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db1 = setup_customized_teardown_test["cbl_db1"]
    cbl_db2 = setup_customized_teardown_test["cbl_db2"]
    cbl_db3 = setup_customized_teardown_test["cbl_db3"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]

    username_A = "autotestA"
    username_B = "autotestB"
    username_C = "autotestC"
    password = "password"
    num_of_docs = 1

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannot run with sg version below 2.5.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    channel_A = ["ABC", "DEF", "XYZ"]
    channel_B = ["DEF"]
    channel_C = ["XYZ"]

    doc_channel_1 = ["ABC", "DEF"]
    doc_channel_2 = ["DEF"]

    sg_client = MobileRestClient()
    replicator = Replication(base_url)

    # 1. Create users in SGW with multiple channels
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username_A, password=password, channels=channel_A, auth=auth)
    cookie_A, session_id_A = sg_client.create_session(sg_admin_url, sg_db, username_A, auth=auth)
    session_A = cookie_A, session_id_A

    sg_client.create_user(sg_admin_url, sg_db, username_B, password=password, channels=channel_B, auth=auth)
    cookie_B, session_id_B = sg_client.create_session(sg_admin_url, sg_db, username_B, auth=auth)
    session_B = cookie_B, session_id_B

    sg_client.create_user(sg_admin_url, sg_db, username_C, password=password, channels=channel_C, auth=auth)
    cookie_C, session_id_C = sg_client.create_session(sg_admin_url, sg_db, username_C, auth=auth)
    session_C = cookie_C, session_id_C

    # 2. create docs in SGW
    #    doc a with channel_A, channel_B ;
    #    docb with channel_B ,
    #    docc with Channel_A, channel_B, channel_C
    sg_docs = document.create_docs(doc_id_prefix='sg_docs-A', number=num_of_docs, channels=doc_channel_1)
    sg_docs_A = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session_A)

    sg_docs = document.create_docs(doc_id_prefix='sg_docs-B', number=num_of_docs, channels=doc_channel_2)
    sg_docs_B = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session_B)

    sg_docs = document.create_docs(doc_id_prefix='sg_docs-C', number=num_of_docs, channels=channel_C)
    sg_docs_C = sg_client.add_bulk_docs(url=sg_url, db=sg_db, docs=sg_docs, auth=session_C)

    # 3. Verify User A(cbl_db1) can access docA and docc.
    #    UserB(cbl_db2), UserA(cbl_db1) can access docB
    #    user A(cbl_db1), user C(cbl_db3) can access docc

    # 3. Pull replication from SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator_A = authenticator.authentication(session_id_A, cookie_A, authentication_type="session")
    repl1 = replicator.configure_and_replicate(source_db=cbl_db1,
                                               target_url=sg_blip_url,
                                               continuous=True,
                                               replicator_authenticator=replicator_authenticator_A,
                                               replication_type="pull")

    replicator_authenticator_B = authenticator.authentication(session_id_B, cookie_B, authentication_type="session")
    repl2 = replicator.configure_and_replicate(source_db=cbl_db2,
                                               target_url=sg_blip_url,
                                               continuous=True,
                                               replicator_authenticator=replicator_authenticator_B,
                                               replication_type="pull")

    replicator_authenticator_C = authenticator.authentication(session_id_C, cookie_C, authentication_type="session")
    repl3 = replicator.configure_and_replicate(source_db=cbl_db3,
                                               target_url=sg_blip_url,
                                               continuous=True,
                                               replicator_authenticator=replicator_authenticator_C,
                                               replication_type="pull")

    doc_ids_A = db.getDocIds(cbl_db1)
    doc_ids_B = db.getDocIds(cbl_db2)
    doc_ids_C = db.getDocIds(cbl_db3)

    for doc in sg_docs_A:
        assert doc["id"] in doc_ids_A, "docs ids of userA does not exist in cbl db1"

    for doc in sg_docs_B:
        assert doc["id"] in doc_ids_A, "docs ids of userA does not exist in cbl db1"
        assert doc["id"] in doc_ids_B, "docs ids of userB does not exist in cbl db2"

    for doc in sg_docs_C:
        assert doc["id"] in doc_ids_A, "docs ids of userA does not exist in cbl db1"
        assert doc["id"] in doc_ids_C, "docs ids of userB does not exist in cbl db2"

    # 4. Remove the channel c from all the docs
    for sg_doc in sg_docs_A:
        sg_client.update_doc(url=sg_url, db=sg_db, doc_id=sg_doc["id"],
                             number_updates=1, auth=session_A,
                             channels=["ABC", "DEF"])

    for sg_doc in sg_docs_C:
        sg_client.update_doc(url=sg_url, db=sg_db, doc_id=sg_doc["id"],
                             number_updates=1, auth=session_C,
                             channels=[])

    replicator.wait_until_replicator_idle(repl1)
    replicator.wait_until_replicator_idle(repl2)
    replicator.wait_until_replicator_idle(repl3)
    replicator.stop(repl1)
    replicator.stop(repl2)
    replicator.stop(repl3)

    # 5. Verify userA can access only docA and doc B, but not docC
    #       UserB can access docB
    #       UserC cannot access docC
    doc_ids_A = db.getDocIds(cbl_db1)
    doc_ids_B = db.getDocIds(cbl_db2)
    doc_ids_C = db.getDocIds(cbl_db3)

    for doc in sg_docs_A:
        assert doc["id"] in doc_ids_A, "docs ids of userA does not exist in cbl db1"

    for doc in sg_docs_B:
        assert doc["id"] in doc_ids_A, "docs ids of userA does not exist in cbl db1"
        assert doc["id"] in doc_ids_B, "docs ids of userB does not exist in cbl db2"

    for doc in sg_docs_C:
        assert doc["id"] not in doc_ids_A, "docs ids of userA  exist in cbl db1"
        assert doc["id"] not in doc_ids_C, "docs ids of userB  exist in cbl db2"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.syncgateway
def test_roles_replication(params_from_base_test_setup):
    """
        @summary:
        1. Create user.
        2. Create 2 roles with 2 differrent channels.
        3. Create docs on SGW in both channels.
        4. Do pull replication from SGW .
        5. Verify docs got replicated to CBL dB from only one channel
        6. Add 2nd role to the user .
        8. Add new docs to SGW in both channels.
        9.Continue to pull replication from SGW .
        10.Verify all new docs got replicated from both channels

    """
    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"
    num_docs = 10

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannot run with sg version below 2.5.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    role1_channels = ["ABC", "DEF"]
    role2_channels = ["xyz", "XXX"]
    role_name = "cache_role"
    roles = [role_name]
    new_role_name = "new_role"

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    # 1. Create user.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, roles=roles, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    # session = cookie, session_id

    # 2. Add new roles.
    sg_client.create_role(url=sg_admin_url, db=sg_db, name=role_name, channels=role1_channels, auth=auth)
    sg_client.create_role(url=sg_admin_url, db=sg_db, name=new_role_name, channels=role2_channels, auth=auth)

    # 3. Create docs on SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="role_doc",
                       channels=role1_channels, auth=auth)

    # 4. Do pull replication from SGW.
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replication_type="pull",
                                              replicator_authenticator=replicator_authenticator)
    replicator.wait_until_replicator_idle(repl)

    # 5. verify docs got replicated to CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == num_docs, "Docs did not get replicated to CBL"

    # 6. Add 2nd role to the user .
    sg_client.update_user(url=sg_admin_url, db=sg_db, name=username, roles=[role_name, new_role_name], auth=auth)

    # 7. Add new docs to SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="new_role1_doc",
                       channels=role1_channels, auth=auth)
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="new_role2_doc",
                       channels=role2_channels, auth=auth)

    # 8. Continue to pull replication from SGW
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 10.Verify all new docs got replicated from both channels
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert len(cbl_doc_ids) == num_docs * 3, "new docs which created in sgw after role change got replicated to cbl"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.syncgateway
def test_channel_update_replication(params_from_base_test_setup):
    """
        @summary:
        1. Create user.
        2. Create docs on SGW.
        3. Do pull replication from SGW.
        4. verify docs got replicated to CBL
        5 Update the user to a differrent channel while replication is happening.
        6. Add new docs to SGW.
        7. Continue to pull replication from SGW
        8. CBL  should not get any new docs which created at step #6.
        Have the coverage from CBL.Verify the docs from CBL side according to the new role.
    """
    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"
    num_docs = 10

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannot run with sg version below 2.5.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    abc_channels = ["ABC", "DEF"]
    xyz_channels = ["xyz"]

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    # 1. Create user.
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=abc_channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    # session = cookie, session_id

    # 2. Create docs on SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="role_doc",
                       channels=abc_channels, auth=auth)

    # 3. Do pull replication from SGW.
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replication_type="pull",
                                              replicator_authenticator=replicator_authenticator)
    replicator.wait_until_replicator_idle(repl)

    # 4. verify docs got replicated to CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == num_docs, "Docs did not get replicated to CBL"

    # 5. update the user to a differrent channel  to something while replication is happening .
    sg_client.update_user(url=sg_admin_url, db=sg_db, name=username, channels=xyz_channels, auth=auth)

    # 6. Add new docs to SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="new_role_doc",
                       channels=abc_channels, auth=auth)

    # 7. Continue to pull replication from SGW
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 8. CBL should not get any new docs which created at step
    cbl_doc_ids = db.getDocIds(cbl_db)
    if sync_gateway_version < "3.0":
        assert len(cbl_doc_ids) == num_docs, "new docs which created in sgw after role change got replicated to cbl"
    else:
        assert len(cbl_doc_ids) == 0, "Existing docs in cbl is not purged or new docs got replicated to cbl with channel update to the user"
    for id in cbl_doc_ids:
        assert "new_role_doc" not in id, "new doc got replicated to cbl"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.syncgateway
def test_replication_behavior_with_channelRole_modification(params_from_base_test_setup):
    """
        @summary:
        1. Create user
        2. Add new role.
        3. Create docs on SGW.
        4. Do pull replication from SGW.
        5. verify docs got replicated to CBL
        6 change the channel of the role to something while replication is happening.
        7. Add new docs to SGW.
        8. Continue to pull replication from SGW
        9. CBL should not get any new docs which created at step #6.
    """
    sg_db = "db"
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    username = "autotest"
    password = "password"
    abc_channels = ["ABC", "DEF"]
    num_docs = 10

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannot run with sg version below 2.5.0')

    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    sg_client = MobileRestClient()
    authenticator = Authenticator(base_url)
    replicator = Replication(base_url)

    # 1. Create user
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=abc_channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    # session = cookie, session_id

    # 2. Add new role.
    sg_client.create_role(url=sg_admin_url, db=sg_db, name="ABCDEF_role", channels=abc_channels, auth=auth)

    # 3. Create docs on SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="role_doc",
                       channels=abc_channels, auth=auth)

    # 4. Do pull replication from SGW.
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replication_type="pull",
                                              replicator_authenticator=replicator_authenticator)
    replicator.wait_until_replicator_idle(repl)

    # 5. verify docs got replicated to CBL
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == num_docs, "Docs did not get replicated to CBL"

    # 6 change it to new channel to the role
    abc_channels = ["XYZ"]
    sg_client.update_role(url=sg_admin_url, db=sg_db, name="ABCDEF_role", channels=["new_channel"], auth=auth)

    # 7. Add new docs to SGW.
    sg_client.add_docs(url=sg_admin_url, db=sg_db, number=num_docs, id_prefix="new_role_doc",
                       channels=abc_channels, auth=auth)

    # 8. Continue to pull replication from SGW
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # 9. CBL should not get any new docs which created at step #6.
    cbl_doc_ids = db.getDocIds(cbl_db)
    assert len(cbl_doc_ids) == num_docs, "new docs which created in sgw after role change got replicated to cbl"
    for id in cbl_doc_ids:
        assert "new_role_doc" not in id, "new doc got replicated to cbl"


@pytest.mark.parametrize("attachment_generator, cbl_action_before_init_replication", [
    [None, False],
    [generate_2_png_100_100, True]
])
def test_replication_pull_from_empty_database(params_from_base_test_setup, attachment_generator, cbl_action_before_init_replication):
    '''
    @summary:
    This test to validate pull replication on empty database. It should avoid sending deleted(tombstoned) documents.
    1. create 50 docs in SGW
    2. delete 10 docs in SGW
    3. add another 50 docs in SGW, verify SGW doc counts and the 10 docs are in tombstone state
    4. create a cbl db, if input param cbl_action_before_init_replication:
        False, do nothing (cbl db is empty, absolutely no action before replication, NO tombstone docs get pull)
        True, create 9 docs in cbl db, delete these 9 docs (cbl db is empty, but no longer in initial state, tombstone docs get pull)
    5. create a onetime pull replicator, start replication
    6. verify SGW stats, if cbl_action_before_init_replication:
        False, cbl_replication_pull.rev_send_count = 90
        True, cbl_replication_pull.rev_send_count = 100
    7. verify CBL, total number of docs is 90, and verify the doc ids are not belongs to tombstone docs
    8. delete 15 more docs on SGW
    9. restart the replication, verify SGW stats, if cbl_action_before_init_replication:
        False, cbl_replication_pull.rev_send_count = 105 (90 + 15 deleted in the second time)
        True, cbl_replication_pull.rev_send_count = 115 (100 + 15 deleted in the second time)
    10. verify CBL deleted docs in step 8 have been removed from CBL db
    '''
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_config = params_from_base_test_setup["sg_config"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    sg_url = params_from_base_test_setup["sg_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    db = params_from_base_test_setup["db"]
    db_config = params_from_base_test_setup["db_config"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    sg_db = "db"
    num_sg_docs = 50
    channels = ["ABC"]
    username = "autotest"
    password = "password"

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    auth_session = cookie, session_id

    # 1. create 50 docs on SGW
    sg_added_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=num_sg_docs, id_prefix="sg_doc_a", channels=channels, auth=auth_session, attachments_generator=attachment_generator)

    # 2. delete 10 docs in SGW
    docs_to_delete_1 = sg_added_docs[0:10]
    sg_client.delete_docs(url=sg_url, db=sg_db, docs=docs_to_delete_1, auth=auth_session)

    # 3. add another 50 docs in SGW
    sg_added_docs_2 = sg_client.add_docs(url=sg_url, db=sg_db, number=num_sg_docs, id_prefix="sg_doc_b", channels=channels, auth=auth_session, attachments_generator=attachment_generator)

    # 3b. verify the 10 deleted docs are in tombstone state
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    assert len(sg_docs) == 90, "Expected number of docs is incorrect after doc deletion in sync-gateway"
    for sg_doc in sg_docs:
        assert sg_doc not in docs_to_delete_1, "Deleted doc should not show on sg doc list"

    # 4a. create a cbl db, but do not create any doc (cbl db is empty)
    cbl_db_name = "empty-db-" + str(time.time())
    cbl_db = db.create(cbl_db_name, db_config)
    # 4b. alternate scenario - create docs on cbl then delete all docs on cbl
    if cbl_action_before_init_replication:
        db.create_bulk_docs(db=cbl_db, number=9, id_prefix="pre-repl", channels=channels)
        cbl_doc_ids = db.getDocIds(cbl_db)
        db.delete_bulk_docs(cbl_db, cbl_doc_ids)

    # 5. create a pull replicator, start replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="pull")

    # 6. verify SGW stats, cbl_replication_pull.rev_send_count sent to CBL equals to 90
    expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
    rev_send_count = expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["rev_send_count"]
    if cbl_action_before_init_replication:
        assert rev_send_count == 100, "rev_send_count {} is incorrect".format(rev_send_count)
    else:
        assert rev_send_count == 90, "rev_send_count {} is incorrect".format(rev_send_count)

    # 7. verify CBL, total number of docs is 90, and verify the doc ids are not belongs to tombstone docs
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == 90, "The number of docs pull from SGW is incorrect"
    if not cbl_action_before_init_replication:
        for deleted_sg_doc in docs_to_delete_1:
            assert deleted_sg_doc["id"] not in cbl_doc_ids, "deleted doc on SGW should not be pulled to CBL"

    # 8. delete 15 more docs on SGW
    docs_to_delete_2 = sg_added_docs_2[0:15]
    sg_client.delete_docs(url=sg_url, db=sg_db, docs=docs_to_delete_2, auth=auth_session)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    assert len(sg_docs) == 75, "Expected number of docs is incorrect after doc deletion in sync-gateway"

    # 9. restart the replication, verify SGW stats, cbl_replication_pull.rev_send_count = 115, regardless cbl_action_before_init_replication:
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    expvars = sg_client.get_expvars(sg_admin_url, auth=auth)
    rev_send_count2 = expvars["syncgateway"]["per_db"][sg_db]["cbl_replication_pull"]["rev_send_count"]
    if not cbl_action_before_init_replication:
        assert rev_send_count2 == 105, "rev_send_count {} is incorrect".format(rev_send_count2)
    else:
        assert rev_send_count2 == 115, "rev_send_count {} is incorrect".format(rev_send_count2)

    # 10. verify CBL deleted docs in step 7 have been removed from CBL db
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_docs = db.getDocuments(cbl_db, cbl_doc_ids)
    assert len(cbl_docs) == 75, "The number of docs pull from SGW is incorrect"


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, continuous, wait_time, retries, type", [
    pytest.param(500, True, "2", "25", "pull"),
    pytest.param(500, True, "3", "20", "push"),
    pytest.param(500, True, "6", "15", "pull-push"),
])
def test_replication_with_custom_retries(params_from_base_test_setup, num_of_docs, continuous, wait_time, retries,
                                         type):
    """
        @summary:
        1. Create CBL DB and create bulk doc in CBL
        2. Stop the SG
        3. Start replication with retries(push and pull)
        4. Verify replicator is retrying
        5. Start the SG and verify replicator connect to SG
    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')
    channels_sg = ["replicator"]
    username = "autotest"
    password = "password"

    # Create CBL database
    sg_client = MobileRestClient()

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    sg_controller = SyncGateway()

    # Configure replication with push_pull
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels_sg, auth=auth)
    session, replicator_authenticator, repl = replicator.create_session_configure_replicate(
        base_url, sg_admin_url, sg_db, username, password, channels_sg, sg_client, cbl_db, sg_blip_url,
        continuous=continuous, replication_type=type, max_retry_wait_time=wait_time, max_retries=retries, auth=auth)

    if type == "pull":
        sg_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="sg_doc", channels=channels_sg,
                                     auth=session)
    else:
        db.create_bulk_docs(num_of_docs, "cbl-replicator-retries", db=cbl_db, channels=channels_sg)

    start_time = time.time()
    repl_change_listener = replicator.addChangeListener(repl)

    # Stop Sync Gateway
    sg_controller = SyncGateway()
    sg_controller.stop_sync_gateways(cluster_config, url=sg_url)
    end_time = time.time()
    time.sleep(15)
    sg_controller.start_sync_gateways(cluster_config, url=sg_url, config=sg_config)
    changes_count = replicator.getChangesCount(repl_change_listener)
    if wait_time == 2:
        assert changes_count > 8
    elif wait_time == 3:
        assert changes_count > 4
    time_taken = end_time - start_time
    log_info(time_taken)

    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)
    # Give some time to replicator to retry
    # Commented as all network errors are causing test failures, but replicator retrying as expected
    replicator.wait_until_replicator_idle(repl, err_check=False)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)
    sg_docs = sg_docs["rows"]
    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    assert len(sg_docs) == cbl_doc_count, "Expected number of docs does not exist in sync-gateway after replication"
    replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, continuous, wait_time, type", [
    pytest.param(500, False, "1", "pull-push"),
])
def test_replication_with_custom_timeout(params_from_base_test_setup, num_of_docs, continuous, wait_time, type):
    """
        @summary:
        Tests verify 9 retries and on-shot replicator
        1. Create CBL DB and create bulk doc in CBL
        2. Configure the replicator with single shot replicator
        3. Stop the SG
        4. Start replication with  pull-push
        5. sleep until replicator completes 9 default attempts
        6. Assert replicator error
        7. Stop the SG
        8. Start the replicator
        9. Start the SG
        10. Verify replication retries are resets and connects to SG
        11. Verify replicator docs on SG

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')
    channels_sg = ["replicator"]
    username = "autotest"
    password = "password"

    # Create CBL database
    sg_client = MobileRestClient()

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    sg_controller = SyncGateway()

    # Configure replication with push_pull
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels_sg, auth=auth)
    authenticator = Authenticator(base_url)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")

    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=False, channels=channels_sg, max_retry_wait_time=wait_time,
                                       replication_type=type, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    db.create_bulk_docs(num_of_docs, "cbl-replicator-retries", db=cbl_db, channels=channels_sg)
    repl_change_listener = replicator.addChangeListener(repl)

    # Stop Sync Gateway
    sg_controller = SyncGateway()
    sg_controller.stop_sync_gateways(cluster_config, url=sg_url)
    start_time = time.time()
    replicator.start(repl)
    time.sleep(60)
    sg_controller.start_sync_gateways(cluster_config, url=sg_url, config=sg_config)
    end_time = time.time()
    changes_count = replicator.getChangesCount(repl_change_listener)
    log_info("*" * 90)
    log_info(changes_count)
    time_taken = end_time - start_time
    log_info(time_taken)
    try:
        replicator.wait_until_replicator_idle(repl)
        replicator.stop(repl)
        assert False, "Replicator is able to connect to SG"
    except Exception as e:
        assert "Error while replicating" in str(e)
        replicator.stop(repl)


@pytest.mark.listener
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, continuous, retries, wait_time, type", [
    pytest.param(500, True, "17", "10", "pull-push"),
    pytest.param(500, True, "17", "10", "push")
])
def test_replication_reset_retires(params_from_base_test_setup, num_of_docs, continuous, retries, wait_time, type):
    """
        @summary:
        Tests verify the reset retries and pull-push replicator
        1. Create CBL DB and create bulk doc in CBL
        2. Configure the replicator with single shot replicator
        3. Stop the SG
        4. Start SG
        5. Make sure replicator connects
        7. Stop the SG
        9. Start the SG
        10. Verify replication retries are resets and connects to SG
        11. Verify replicator docs on SG

    """
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')
    channels_sg = ["replicator"]
    username = "autotest"
    password = "password"

    # Create CBL database
    sg_client = MobileRestClient()

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    # Configure replication with push_pull
    replicator = Replication(base_url)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=channels_sg, auth=auth)
    authenticator = Authenticator(base_url)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")

    repl_config = replicator.configure(cbl_db, sg_blip_url, continuous=continuous, channels=channels_sg,
                                       max_retry_wait_time=wait_time, max_retries=retries,
                                       replication_type=type, replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    db.create_bulk_docs(num_of_docs, "cbl-replicator-retries", db=cbl_db, channels=channels_sg)
    repl_change_listener = replicator.addChangeListener(repl)

    # Stop Sync Gateway
    sg_controller = SyncGateway()
    sg_controller.stop_sync_gateways(cluster_config, url=sg_url)
    replicator.start(repl)
    start_time = time.time()
    sg_controller.start_sync_gateways(cluster_config, url=sg_url, config=sg_config)
    end_time = time.time()
    changes_count = replicator.getChangesCount(repl_change_listener)
    log_info("*" * 90)
    time_taken = end_time - start_time
    log_info(time_taken)
    log_info(changes_count)
    replicator.wait_until_replicator_idle(repl, err_check=False)

    # Stop the sg and restart the replicator
    sg_controller.stop_sync_gateways(cluster_config, url=sg_url)

    # start the sg before retries ends
    # Adding enough sleep to wait for the retries
    time.sleep(int(wait_time))
    sg_controller.start_sync_gateways(cluster_config, url=sg_url, config=sg_config)
    replicator.wait_until_replicator_idle(repl, err_check=False)

    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)
    sg_docs = sg_docs["rows"]
    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    assert len(sg_docs) == cbl_doc_count, "Expected number of docs does not exist in sync-gateway after replication"
    replicator.stop(repl)


def update_and_resetCheckPoint(db, cbl_db, replicator, repl, replication_type, repl_config, num_of_updates):
    # update docs in CBL
    db.update_bulk_docs(cbl_db)
    cbl_doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, cbl_doc_ids)

    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    # Reset checkpoint and do replication again from sg to cbl
    # Verify all docs are back

    if replication_type == "one_way":
        replicator.setReplicatorType(repl_config, "pull")
        repl = replicator.create(repl_config)

    replicator.resetCheckPoint(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    for doc in cbl_db_docs:
        assert cbl_db_docs[doc]["updates-cbl"] == num_of_updates, "cbl docs did not get latest updates"


def restart_sg(c, sg_conf, cluster_config):
    status = c.sync_gateways[0].restart(config=sg_conf, cluster_config=cluster_config)
    log_info("Restarting sg ....")
    assert status == 0, "Sync_gateway did not start"


def verify_sgDocIds_cblDocIds(sg_client, url, sg_db, session, cbl_db, db):
    sg_docs = sg_client.get_all_docs(url=url, db=sg_db, auth=session)
    sg_docs = sg_docs["rows"]
    sg_doc_ids = [row["id"] for row in sg_docs]
    cbl_doc_ids = db.getDocIds(cbl_db)
    count = 0
    while len(sg_doc_ids) != len(cbl_doc_ids):
        if count == 30:
            break

        time.sleep(1)
        cbl_doc_ids = db.getDocIds(cbl_db)
        sg_docs = sg_client.get_all_docs(url=url, db=sg_db, auth=session)
        sg_docs = sg_docs["rows"]
        sg_doc_ids = [row["id"] for row in sg_docs]
        count += 1

    for id in sg_doc_ids:
        assert id in cbl_doc_ids, "sg doc is not replicated to cbl "


def verify_cblDocs_in_sgDocs(sg_client, url, sg_db, session, cbl_db, db, topology_type="1cbl"):
    sg_docs = sg_client.get_all_docs(url=url, db=sg_db, auth=session, include_docs=True)
    sg_docs = sg_docs["rows"]
    if "1cbl" in topology_type:
        num_cbl_updates = 3
    else:
        num_cbl_updates = 1

    cbl_doc_ids = db.getDocIds(cbl_db)
    sg_doc_ids = [row["id"] for row in sg_docs]

    count = 0
    while len(sg_doc_ids) != len(cbl_doc_ids):
        if count == 30:
            break

        time.sleep(1)
        cbl_doc_ids = db.getDocIds(cbl_db)
        sg_docs = sg_client.get_all_docs(url=url, db=sg_db, auth=session, include_docs=True)
        sg_docs = sg_docs["rows"]
        sg_doc_ids = [row["id"] for row in sg_docs]
        count += 1

    for doc in sg_docs:
        assert doc["doc"]["updates-cbl"] == num_cbl_updates, "updated doc in cbl did not replicated to sg"


def setup_sg_cbl_docs(params_from_base_test_setup, sg_db, base_url, db, cbl_db, sg_url,
                      sg_admin_url, sg_blip_url, replication_type=None, document_ids=None,
                      channels=None, replicator_authenticator_type=None, headers=None,
                      cbl_id_prefix="cbl", sg_id_prefix="sg_doc",
                      num_cbl_docs=5, num_sg_docs=10, attachments_generator=None, auth=None):

    sg_client = MobileRestClient()

    db.create_bulk_docs(number=num_cbl_docs, id_prefix=cbl_id_prefix, db=cbl_db, channels=channels, attachments_generator=attachments_generator)
    cbl_added_doc_ids = db.getDocIds(cbl_db)
    # Add docs in SG
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, "autotest", auth=auth)
    auth_session = cookie, session
    sg_added_docs = sg_client.add_docs(url=sg_url, db=sg_db, number=num_sg_docs, id_prefix=sg_id_prefix, channels=channels, auth=auth_session, attachments_generator=attachments_generator)
    sg_added_ids = [row["id"] for row in sg_added_docs]

    # Start and stop continuous replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    if replicator_authenticator_type == "session":
        replicator_authenticator = authenticator.authentication(session, cookie, authentication_type="session")
    elif replicator_authenticator_type == "basic":
        replicator_authenticator = authenticator.authentication(username="autotest", password="password", authentication_type="basic")
    else:
        replicator_authenticator = None
    log_info("Configuring replicator")
    repl_config = replicator.configure(cbl_db, target_url=sg_blip_url, replication_type=replication_type, continuous=False,
                                       documentIDs=document_ids, channels=channels, replicator_authenticator=replicator_authenticator, headers=headers)
    repl = replicator.create(repl_config)
    log_info("Starting replicator")
    replicator.start(repl)
    log_info("Waiting for replicator to go idle")
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    return sg_added_ids, cbl_added_doc_ids, auth_session
