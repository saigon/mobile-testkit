import pytest
import concurrent.futures

from keywords.utils import log_info
from libraries.testkit.cluster import Cluster
from keywords.MobileRestClient import MobileRestClient
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from requests.exceptions import HTTPError

from keywords import couchbaseserver
from keywords import userinfo
from keywords import document
from keywords.constants import RBAC_FULL_ADMIN
import time

from utilities.cluster_config_utils import persist_cluster_config_environment_prop, copy_to_temp_conf


@pytest.mark.sanity
@pytest.mark.syncgateway
@pytest.mark.session
@pytest.mark.rollback
@pytest.mark.bulkops
@pytest.mark.basicsgw
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, x509_cert_auth", [
    ("sync_gateway_default", False)
])
def test_rollback_server_reset(params_from_base_test_setup, sg_conf_name, x509_cert_auth):
    # Ignorning this test for now until we have a fix. Tests which runs after this in Jenkins machine or local machine
    #  fails all the tests which runs after this. looks it needs reset of server or make the test to run at the end.
    """
    Test for sync gateway resiliency under Couchbase Server rollback

    Scenario
    1. Create user (seth:pass) and session
    2. Add docs targeting all vbuckets except 66
    3. Add docs to vbucket 66
    4. Verify the docs show up in seth's changes feed
    5. Delete vBucket 66 file on server
    6. Restart server
    7. User should only see docs not in vbucket 66
    """

    num_vbuckets = 1024

    cluster_config = params_from_base_test_setup["cluster_config"]
    topology = params_from_base_test_setup["cluster_topology"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    sg_url = topology["sync_gateways"][0]["public"]
    sg_admin_url = topology["sync_gateways"][0]["admin"]
    cb_server_url = topology["couchbase_servers"][0]
    cb_server = couchbaseserver.CouchbaseServer(cb_server_url)

    sg_db = "db"

    if mode == "cc":
        pytest.skip("Rollback not supported in channel cache mode")

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_config, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_config = temp_cluster_config
    cluster = Cluster(cluster_config)
    cluster.reset(sg_conf)

    client = MobileRestClient()
    seth_user_info = userinfo.UserInfo("seth", "pass", channels=["NASA"], roles=[])

    client.create_user(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        password=seth_user_info.password,
        channels=seth_user_info.channels,
        auth=auth
    )

    seth_session = client.create_session(
        url=sg_admin_url,
        db=sg_db,
        name=seth_user_info.name,
        auth=auth
    )

    # create a doc that will hash to each vbucket in parallel except for vbucket 66
    doc_id_for_every_vbucket_except_66 = []
    with concurrent.futures.ProcessPoolExecutor() as pex:
        futures = [pex.submit(document.generate_doc_id_for_vbucket, i) for i in range(num_vbuckets) if i != 66]
        for future in concurrent.futures.as_completed(futures):
            doc_id = future.result()
            doc = document.create_doc(
                doc_id=doc_id,
                channels=seth_user_info.channels
            )
            doc_id_for_every_vbucket_except_66.append(doc)

    vbucket_66_docs = []
    for _ in range(5):
        vbucket_66_docs.append(document.create_doc(
            doc_id=document.generate_doc_id_for_vbucket(66),
            channels=seth_user_info.channels
        ))

    seth_docs = client.add_bulk_docs(url=sg_url, db=sg_db, docs=doc_id_for_every_vbucket_except_66, auth=seth_session)
    seth_66_docs = client.add_bulk_docs(url=sg_url, db=sg_db, docs=vbucket_66_docs, auth=seth_session)

    assert len(seth_docs) == num_vbuckets - 1
    assert len(seth_66_docs) == 5

    # Verify the all docs show up in seth's changes feed
    all_docs = seth_docs + seth_66_docs
    assert len(all_docs) == (num_vbuckets - 1) + 5

    client.verify_docs_in_changes(
        url=sg_url,
        db=sg_db,
        expected_docs=all_docs,
        auth=seth_session
    )

    # Delete vbucket and restart server
    cb_server.delete_vbucket(66, "data-bucket")
    cb_server.restart()
    max_retries = 50
    count = 0
    while count != max_retries:
        # Try to get changes, sync gateway should be able to recover and return changes
        # A changes since=0 should now be in a rolled back state due to the data loss from the removed vbucket
        # Seth should only see the docs not present in vbucket 66, unlike all the docs as above.
        try:
            changes = client.get_changes(url=sg_url, db=sg_db, since=0, auth=seth_session)
            changes_ids = [change["id"] for change in changes["results"] if not change["id"].startswith("_user")]
            log_info("length of Changes ids are {} and vbuckets {}".format(len(changes_ids), num_vbuckets))
            if len(changes_ids) == (num_vbuckets - 1):
                break
        except HTTPError:
            if changes.status_code == 503:
                log_info('server is still down')
            else:
                raise
        time.sleep(1)
        count += 1

    # Verify that seth 66 doc does not appear in changes as vbucket 66 got deleted
    vbucket_66_docids = [doc1["id"] for doc1 in seth_66_docs]
    for doc_66 in vbucket_66_docids:
        assert doc_66 not in changes_ids, "doc {} in vbucket 66 shows up in changes ".format(doc_66)
