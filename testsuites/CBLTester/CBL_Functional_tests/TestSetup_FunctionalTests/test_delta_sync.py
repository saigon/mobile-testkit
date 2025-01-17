import pytest
import time

from keywords.MobileRestClient import MobileRestClient
from keywords.utils import random_string, compare_docs, get_embedded_asset_file_path
from CBLClient.Replication import Replication
from CBLClient.Dictionary import Dictionary
from CBLClient.Blob import Blob
from CBLClient.Authenticator import Authenticator
from utilities.cluster_config_utils import persist_cluster_config_environment_prop, copy_to_temp_conf
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from libraries.testkit import cluster
from libraries.testkit.prometheus import verify_stat_on_prometheus
from keywords.constants import RBAC_FULL_ADMIN


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type, file_attachment, continuous", [
    pytest.param(10, "pull", None, True, marks=pytest.mark.ce_sanity),
    pytest.param(10, "pull", "sample_text.txt", True, marks=pytest.mark.sanity),
    (1, "push", "golden_gate_large.jpg", True),
    (10, "push", None, True)
])
def test_delta_sync_replication(params_from_base_test_setup, num_of_docs, replication_type, file_attachment, continuous):
    '''
    @summary:
    1. Create docs in CBL
    2. Do push_pull replication
    3. update docs in SGW  with/without attachment
    4. Do push/pull replication
    5. Verify delta sync stats shows bandwidth saving, replication count, number of docs updated using delta sync
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    mode = params_from_base_test_setup["mode"]
    prometheus_enable = params_from_base_test_setup["prometheus_enable"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 1
    blob = Blob(base_url)
    dictionary = Dictionary(base_url)

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    # 1. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # 2. Do push replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)
    _, doc_writes_bytes = get_net_stats(sg_client, sg_admin_url, auth)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    assert len(sg_docs) == cbl_doc_count, "Expected number of docs does not exist in sync-gateway after replication"

    # 3. update docs in SGW  with/without attachment
    for doc in sg_docs:
        sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=1, auth=session, channels=channels, attachment_name=file_attachment)
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="pull")
    replicator.stop(repl)
    doc_reads_bytes1, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)
    delta_size = doc_reads_bytes1
    assert delta_size < doc_writes_bytes, "did not replicate just delta"

    if replication_type == "push":
        doc_ids = db.getDocIds(cbl_db)
        cbl_db_docs = db.getDocuments(cbl_db, doc_ids)
        image_location = get_embedded_asset_file_path(liteserv_platform, db, cbl_db, "golden_gate_large.jpg")
        for doc_id, doc_body in list(cbl_db_docs.items()):
            for _ in range(number_of_updates):
                if file_attachment:
                    mutable_dictionary = dictionary.toMutableDictionary(doc_body)
                    dictionary.setString(mutable_dictionary, "new_field_1", random_string(length=30))
                    dictionary.setString(mutable_dictionary, "new_field_2", random_string(length=80))

                    image_content = blob.createImageStream(image_location, cbl_db)
                    blob_value = blob.create("image/jpeg", stream=image_content)
                    dictionary.setBlob(mutable_dictionary, "_attachments", blob_value)
                db.updateDocument(database=cbl_db, data=doc_body, doc_id=doc_id)
    else:
        for doc in sg_docs:
            sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=number_of_updates, auth=session, channels=channels, attachment_name=file_attachment)

    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)
    replicator.stop(repl)

    # Get Sync Gateway Expvars
    expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)

    if replication_type == "push":
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_push_doc_count'] == num_of_docs, "delta push replication count is not right"
        if prometheus_enable and sync_gateway_version >= "2.8.0":
            assert verify_stat_on_prometheus("sgw_delta_sync_delta_push_doc_count"), expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_push_doc_count']
            assert verify_stat_on_prometheus("sgw_gsi_views_access_count"), expvars['syncgateway']['per_db'][sg_db]['gsi_views']['access_query_count']
    else:
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_pull_replication_count'] == 2, "delta pull replication count is not right"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_requested'] == num_of_docs * 2, "delta pull requested is not equal to number of docs"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_sent'] == num_of_docs * 2, "delta pull sent is not equal to number of docs"
        if prometheus_enable and sync_gateway_version >= "2.8.0":
            assert verify_stat_on_prometheus("sgw_delta_sync_delta_pull_replication_count"), expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_pull_replication_count']
            assert verify_stat_on_prometheus("sgw_gsi_views_access_count"), expvars['syncgateway']['per_db'][sg_db]['gsi_views']['access_query_count']

    doc_reads_bytes2, doc_writes_bytes2 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "push":
        delta_size = doc_writes_bytes2 - doc_writes_bytes1
    else:
        delta_size = doc_reads_bytes2 - doc_reads_bytes1

    if replication_type != "push" and file_attachment is not None:
        assert delta_size < doc_writes_bytes, "did not replicate just delta"

    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, doc_ids)
    compare_docs(cbl_db, db, sg_docs)


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type", [
    (10, "pull"),
    (10, "push")
])
def test_delta_sync_enabled_disabled(params_from_base_test_setup, num_of_docs, replication_type):
    '''
    @summary:
    1. Have detla sync enabled by default
    2. Create docs in CBL
    3. Do push replication to SGW
    4. update docs in SGW
    5. Do pull replication to CBL
    6. Get stats pub_net_bytes_send
    7. Disable delta sync in sg config and restart SGW
    8. update docs in SGW
    9. Do pull replication to CBL
    10. Verify there is no delta sync stats available on _expvars API
    11. Get stats pub_net_bytes_send
    12 Verify pub_net_bytes_send stats go up high when compared with step #6 as it resplicates full doc
    '''
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

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")

    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # Configure replication with push
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)

    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]

    # Get expvars and get original size of document
    _, doc_writes_bytes = get_net_stats(sg_client, sg_admin_url, auth)
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)

    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)
    replicator.stop(repl)

    # Get expvars and get original size of document
    doc_reads_bytes1, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "pull":
        delta_size = doc_reads_bytes1
    else:
        delta_size = doc_writes_bytes1 - doc_writes_bytes

    # Get Sync Gateway Expvars
    expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)
    if replication_type == "push":
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_push_doc_count'] == 10, "delta push replication count is not right"
    else:
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_pull_replication_count'] == 1, "delta pull replication count is not right"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_requested'] == num_of_docs, "delta pull requested is not equal to number of docs"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_sent'] == num_of_docs, "delta pull sent is not equal to number of docs"

    # Now disable delta sync and verify replication happens, but full doc should replicate
    enable_delta_sync(c, sg_config, cluster_config, mode, False)
    time.sleep(10)

    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)
    # Get expvars and get original size of document
    doc_reads_bytes2, doc_writes_bytes2 = get_net_stats(sg_client, sg_admin_url, auth)

    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]

    # Get expvars and get original size of document
    doc_reads_bytes3, doc_writes_bytes3 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "pull":
        delta_disabled_doc_size = doc_reads_bytes3 - doc_reads_bytes2
    else:
        delta_disabled_doc_size = doc_writes_bytes3 - doc_writes_bytes2

    compare_docs(cbl_db, db, sg_docs)
    # assert delta_disabled_doc_size == full_doc_size, "did not get full doc size"
    assert delta_disabled_doc_size > delta_size, "disabled delta doc size is more than enabled delta size"
    try:
        expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)
        expvars['syncgateway']['per_db'][sg_db]['delta_sync']
        assert False, "delta sync is not disabled"
    except KeyError:
        assert True


@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type", [
    (1, "pull"),
    (1, "push")
])
def test_delta_sync_within_expiry(params_from_base_test_setup, num_of_docs, replication_type):
    '''
    @summary:
    1. Have delta sync enabled
    2. Create docs in CBL
    3. Do push replication to SGW
    4. update docs in SGW/CBL
    5. replicate docs using pull replication
    6. get pub_net_stats_send from expvar api
    7. update docs in SGW
    8. wait for 2 minutes which makes delta revision expire
    9. replicate docs using pull replication
    10 get pub_net_stats_send from expvar api
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    sg_config = sync_gateway_config_path_for_mode("delta_sync/sync_gateway_delta_sync_2min_rev", mode)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    # 2. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # 3. Do push replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)

    # get stats_send from expvar api
    _, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)

    # 4. update docs in SGW/CBL
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, include_docs=True, auth=session)["rows"]
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)

    # 5. replicate docs using push/pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)

    # 6. get stats_send from expvar api
    doc_reads_bytes2, doc_writes_bytes2 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "pull":
        delta_size = doc_reads_bytes2
    else:
        delta_size = doc_writes_bytes2 - doc_writes_bytes1
    assert delta_size < doc_writes_bytes1, "delta size is not less than expired delta size"
    # 7. update docs in SGW
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, include_docs=True, auth=session)["rows"]
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)

    # 8. wait for 2 minutes which makes delta revision expire and update full doc
    time.sleep(130)

    # 9. replicate docs using pull replication
    replicator.configure_and_replicate(source_db=cbl_db,
                                       target_url=sg_blip_url,
                                       continuous=False,
                                       replicator_authenticator=replicator_authenticator,
                                       replication_type=replication_type)

    # compare full body on SGW and CBL and verify whole body matches
    sg_docs = sg_client.get_all_docs(url=sg_url, db=sg_db, include_docs=True, auth=session)["rows"]
    compare_docs(cbl_db, db, sg_docs)


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type", [
    (1, "pull"),
    (1, "push")
])
def test_delta_sync_utf8_strings(params_from_base_test_setup, num_of_docs, replication_type):
    '''
    @summary:
    1. Have delta sync enabled
    2. Create docs in CBL
    3. Do push replication to SGW
    4. update docs in SGW/CBL with utf8 strings
    5. replicate docs using pull replication
    6. get pub_net_stats_send from expvar api
    7. Verify that docs replicated successfully and only delta is replicated
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sg_config = params_from_base_test_setup["sg_config"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 2. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # 3. Do push replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              channels=channels,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)
    _, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)
    full_doc_size = doc_writes_bytes1

    # 4. update docs in SGW/CBL
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels, string_type="utf-8")

    # 5. replicate docs using push/pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)
    replicator.stop(repl)
    doc_reads_bytes2, doc_writes_bytes2 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "pull":
        delta_size = doc_reads_bytes2
    else:
        delta_size = doc_writes_bytes2 - doc_writes_bytes1
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    compare_docs(cbl_db, db, sg_docs)
    verify_delta_stats_counts(sg_client, sg_admin_url, replication_type, sg_db, num_of_docs, auth)
    assert delta_size < full_doc_size, "delta size is not less than full doc size when delta is replicated"


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type", [
    (1, "pull"),
    (1, "push")
])
def test_delta_sync_nested_doc(params_from_base_test_setup, num_of_docs, replication_type):
    '''
    @summary:
    1. Create docs in CBL with nested docs
    2. Do push_pull replication
    3. update docs in SGW
    4. Do push/pull replication
    5. Verify delta sync stats shows bandwidth saving, replication count, number of docs updated using delta sync
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 2. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels, generator="complex_doc")

    # 3. Do push replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)

    # get net_stats_send from expvar api
    _, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)

    # 4. update docs in SGW/CBL
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)

    # 5. replicate docs using pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)

    # 6. get pub_net_stats_send from expvar api
    doc_reads_bytes2, _ = get_net_stats(sg_client, sg_admin_url, auth)
    assert doc_reads_bytes2 < doc_writes_bytes1, "delta size is not less than full doc size"

    # 7. Verify the body of nested doc matches with sgw and cbl
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    compare_docs(cbl_db, db, sg_docs)
    verify_delta_stats_counts(sg_client, sg_admin_url, replication_type, sg_db, num_of_docs, auth)


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
def test_delta_sync_dbWorker(params_from_base_test_setup):
    '''
    @summary:
    1. Add docs in SGW
    2. Do pull replication
    3. update docs in SGW
    4. Do push/pull replication
    5. Verify docs after replication matching in SGW and CBL
    Coverage for GitHub 792: DBWorker crashes in Fleece Encoder (writePointer)
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3
    num_of_docs = 100
    replication_type = "pull"

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 1. add docs in SGW
    sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="db_worker", channels=channels, auth=session)

    # 2. Do pull replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="pull")
    replicator.stop(repl)

    # 3. update docs in SGW/CBL
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)
    sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="db_worker2", channels=channels, auth=session)

    # 4. Do pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)

    # 5. Verify docs after replication matching in SGW and CBL
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    compare_docs(cbl_db, db, sg_docs)


@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type", [
    (1, "pull"),
    (1, "push")
])
def test_delta_sync_larger_than_doc(params_from_base_test_setup, num_of_docs, replication_type):
    '''
    @summary:
    1. Have delta sync enabled
    2. Create docs in CBL
    3. Do push replication to SGW
    4. get stats from expvar api
    5. update docs in SGW/CBL , update has to be larger than doc in bytes
    6. replicate docs using pull/push replication
    7. get stats from expvar api
    8. Verify full doc is replicated. Delta size at step 7 shold be same as step 4
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sg_config = params_from_base_test_setup["sg_config"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 3

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 2. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # 3. Do push replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)

    # get stats_send from expvar api
    _, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)
    full_doc_size = doc_writes_bytes1

    # 4. update docs in SGW/CBL
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    update_larger_doc(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels)

    # 5. replicate docs using push/pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)

    # 6. get stats from expvar api
    doc_reads_bytes2, doc_writes_bytes2 = get_net_stats(sg_client, sg_admin_url, auth)
    if replication_type == "pull":
        larger_delta_size = doc_reads_bytes2
    else:
        larger_delta_size = doc_writes_bytes2 - doc_writes_bytes1

    # compare full body on SGW and CBL and verify whole body matches
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    compare_docs(cbl_db, db, sg_docs)

    assert larger_delta_size >= full_doc_size, "did not get full doc size after deltas is expired"


@pytest.mark.community
@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("num_of_docs, replication_type, file_attachment, continuous", [
    (10, "pushAndPull", None, True),
    (10, "pull", "sample_text.txt", True)
])
def test_delta_sync_on_community_edition(params_from_base_test_setup, num_of_docs, replication_type, file_attachment, continuous):
    '''
    @summary:
    1. Create docs in CBL
    2. Do push_pull replication
    3. update docs in SGW  with/without attachment
    4. Do push/pull replication
    5. Verify delta sync stats are not available for community edition
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    sg_config = params_from_base_test_setup["sg_config"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    liteserv_platform = params_from_base_test_setup["liteserv_platform"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_ce = params_from_base_test_setup["sg_ce"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if not sg_ce:
        pytest.skip("Test is only for community edition of SG")

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"
    number_of_updates = 1
    blob = Blob(base_url)
    dictionary = Dictionary(base_url)

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    # 1. Create docs in CBL
    db.create_bulk_docs(num_of_docs, "cbl_sync", db=cbl_db, channels=channels)

    # 2. Do push replication
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push")
    replicator.stop(repl)
    doc_read_bytes, doc_writes_bytes = get_net_stats(sg_client, sg_admin_url, auth)
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db)
    assert len(sg_docs) == cbl_doc_count, "Expected number of docs does not exist in sync-gateway after replication"

    # 3. update docs in SGW  with/without attachment
    for doc in sg_docs:
        sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=1, auth=session, channels=channels, attachment_name=file_attachment)
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="pull")
    replicator.stop(repl)
    doc_reads_bytes1, doc_writes_bytes1 = get_net_stats(sg_client, sg_admin_url, auth)
    delta_size = doc_reads_bytes1
    assert delta_size > doc_writes_bytes, "delta_sync in SG CE"

    if replication_type == "push":
        doc_ids = db.getDocIds(cbl_db)
        cbl_db_docs = db.getDocuments(cbl_db, doc_ids)

        for doc_id, doc_body in cbl_db_docs.items():
            for _ in range(number_of_updates):
                if file_attachment:
                    mutable_dictionary = dictionary.toMutableDictionary(doc_body)
                    dictionary.setString(mutable_dictionary, "new_field_1", random_string(length=30))
                    dictionary.setString(mutable_dictionary, "new_field_2", random_string(length=80))

                    if liteserv_platform == "android":
                        image_content = blob.createImageContent("/assets/golden_gate_large.jpg")
                        blob_value = blob.create("image/jpeg", stream=image_content)
                    elif liteserv_platform == "xamarin-android":
                        image_content = blob.createImageContent("golden_gate_large.jpg")
                        blob_value = blob.create("image/jpeg", stream=image_content)
                    elif liteserv_platform == "ios":
                        image_content = blob.createImageContent("Files/golden_gate_large.jpg")
                        blob_value = blob.create("image/jpeg", content=image_content)
                    elif liteserv_platform == "net-msft":
                        db_path = db.getPath(cbl_db).rstrip("\\")
                        app_dir = "\\".join(db_path.split("\\")[:-2])
                        image_content = blob.createImageContent("{}\\Files\\golden_gate_large.jpg".format(app_dir))
                        blob_value = blob.create("image/jpeg", stream=image_content)
                    else:
                        image_content = blob.createImageContent("Files/golden_gate_large.jpg")
                        blob_value = blob.create("image/jpeg", stream=image_content)
                    dictionary.setBlob(mutable_dictionary, "_attachments", blob_value)
                db.updateDocument(database=cbl_db, data=doc_body, doc_id=doc_id)
    else:
        for doc in sg_docs:
            sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=number_of_updates, auth=session, channels=channels, attachment_name=file_attachment)

    # 4. Do push/pull replication
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=continuous,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type=replication_type)
    replicator.stop(repl)

    # Get Sync Gateway Expvars
    expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)

    # 5. Verify delta sync stats are not available for community edition
    assert "delta_sync" not in expvars['syncgateway']['per_db'][sg_db], "delta_sync in SG CE"

    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    doc_ids = db.getDocIds(cbl_db)
    cbl_db_docs = db.getDocuments(cbl_db, doc_ids)
    compare_docs(cbl_db, db, sg_docs)


@pytest.mark.listener
@pytest.mark.syncgateway
@pytest.mark.replication
@pytest.mark.parametrize("doc_creation_source, doc_update_source", [
    ("cbl", "cbl"),
    ("sgw", "cbl"),
    ("cbl", "sgw"),
    ("sgw", "sgw")
])
def test_delta_sync_with_no_deltas(params_from_base_test_setup, doc_creation_source, doc_update_source):
    '''
    @summary: Testing CBSE-8339
    1. Create new docs in CBL/ SGW
    2. Do push_pull one shot replication to SGW
    3. Update doc on SGW/CBL
    4. Update doc on SGW/CBL again to have same value as rev-1
    5. update same doc in SGW/cbl which still has rev-1
    6. Verify the body of the doc matches with sgw and cbl
    '''
    sg_db = "db"
    sg_url = params_from_base_test_setup["sg_url"]
    sg_admin_url = params_from_base_test_setup["sg_admin_url"]
    cluster_config = params_from_base_test_setup["cluster_config"]
    sg_blip_url = params_from_base_test_setup["target_url"]
    base_url = params_from_base_test_setup["base_url"]
    db = params_from_base_test_setup["db"]
    cbl_db = params_from_base_test_setup["source_db"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    sg_config = params_from_base_test_setup["sg_config"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]
    num_of_docs = 1

    if sync_gateway_version < "2.5.0":
        pytest.skip('This test cannnot run with sg version below 2.5')
    channels = ["ABC"]
    username = "autotest"
    password = "password"

    # Reset cluster to ensure no data in system
    c = cluster.Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)
    enable_delta_sync(c, sg_config, cluster_config, mode, True)

    sg_client = MobileRestClient()
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password=password, channels=channels, auth=auth)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id

    # 1. Create docs in CBL/SGW
    if doc_creation_source == "cbl":
        db.create_bulk_docs(num_of_docs, "delta_test", db=cbl_db, channels=channels)
    else:
        sg_client.add_docs(url=sg_url, db=sg_db, number=num_of_docs, id_prefix="delta_test", channels=channels, auth=session)

    # 2. Do push_pull one shot replication to SGW
    replicator = Replication(base_url)
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=False,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push_pull")

    # 3. Update doc on SG
    # 4. Update doc on SG again to have same value as rev-1
    if doc_update_source == "cbl":
        doc_ids = db.getDocIds(cbl_db)
        cbl_docs = db.getDocuments(cbl_db, doc_ids)
        for doc_id in cbl_docs:
            data = cbl_docs[doc_id]
            data["location"] = "germany"
            db.updateDocument(cbl_db, doc_id=doc_id, data=data)
        cbl_docs = db.getDocuments(cbl_db, doc_ids)
        for doc_id in cbl_docs:
            data = cbl_docs[doc_id]
            data["location"] = "california"
            db.updateDocument(cbl_db, doc_id=doc_id, data=data)

    else:
        sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
        for doc in sg_docs:
            sg_client.update_doc_with_content(url=sg_url, db=sg_db, doc_id=doc["id"], key="location", value="germany", auth=session, channels=channels)

        for doc in sg_docs:
            sg_client.update_doc_with_content(url=sg_url, db=sg_db, doc_id=doc["id"], key="location", value="california", auth=session, channels=channels)

    repl = replicator.configure_and_replicate(source_db=cbl_db,
                                              target_url=sg_blip_url,
                                              continuous=True,
                                              replicator_authenticator=replicator_authenticator,
                                              replication_type="push_pull")

    # 5. update same doc in cbl/SGW which still has rev-1 . It depends on where it got updated at step3
    # if updated in cbl at step3, it has to update on SGW at step5
    # if updated in SGW at step3, it has to upddate on CBL at step5
    if doc_update_source == "cbl":
        sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
        for doc in sg_docs:
            sg_client.update_doc_with_content(url=sg_url, db=sg_db, doc_id=doc["id"], key="location", value="Arizona", auth=session, channels=channels)
    else:
        doc_ids = db.getDocIds(cbl_db)
        cbl_docs = db.getDocuments(cbl_db, doc_ids)
        for doc_id in cbl_docs:
            data = cbl_docs[doc_id]
            data["location"] = "Arizona"
            db.updateDocument(cbl_db, doc_id=doc_id, data=data)
    replicator.wait_until_replicator_idle(repl)

    # 6. Verify the body of the doc matches with sgw and cbl
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, include_docs=True, auth=auth)["rows"]
    compare_docs(cbl_db, db, sg_docs)
    replicator.stop(repl)


def update_docs(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels, string_type="normal"):
    if replication_type == "push":
        doc_ids = db.getDocIds(cbl_db)
        cbl_db_docs = db.getDocuments(cbl_db, doc_ids)
        for doc_id, doc_body in list(cbl_db_docs.items()):
            if string_type == "utf-8":
                doc_body["new-1"] = random_string(length=70).encode('utf-8')
                doc_body["new-2"] = random_string(length=70).encode('utf-8')
            else:
                doc_body["new-1"] = random_string(length=70)
                doc_body["new-2"] = random_string(length=30)
            db.updateDocument(database=cbl_db, data=doc_body, doc_id=doc_id)
    else:
        def property_updater(doc_body):
            if string_type == "utf-8":
                doc_body['sg_new_update'] = random_string(length=70).encode('utf-8')
            else:
                doc_body["sg_new_update"] = random_string(length=70)
            return doc_body
        for doc in sg_docs:
            sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=number_of_updates, auth=session, channels=channels, property_updater=property_updater)


def update_larger_doc(replication_type, cbl_db, db, sg_client, sg_docs, sg_url, sg_db, number_of_updates, session, channels):
    if replication_type == "push":
        doc_ids = db.getDocIds(cbl_db)
        cbl_db_docs = db.getDocuments(cbl_db, doc_ids)
        for doc_id, doc_body in list(cbl_db_docs.items()):
            doc_body["new-1"] = random_string(length=100)
            doc_body["new-2"] = random_string(length=100)
            doc_body["new-3"] = random_string(length=100)
            db.updateDocument(database=cbl_db, data=doc_body, doc_id=doc_id)
    else:
        for doc in sg_docs:
            sg_client.update_doc(url=sg_url, db=sg_db, doc_id=doc["id"], number_updates=number_of_updates, auth=session, channels=channels, property_updater=property_updater)


def property_updater(doc_body):
    doc_body["sg_new_update1"] = random_string(length=100)
    doc_body["sg_new_update2"] = random_string(length=100)
    doc_body["sg_new_update3"] = random_string(length=100)
    return doc_body


def get_net_stats(sg_client, sg_admin_url, auth):
    expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)
    doc_reads_bytes = expvars['syncgateway']['per_db']['db']['database']['doc_reads_bytes_blip']
    doc_writes_bytes = expvars['syncgateway']['per_db']['db']['database']['doc_writes_bytes_blip']
    return doc_reads_bytes, doc_writes_bytes


def verify_delta_stats_counts(sg_client, sg_admin_url, replication_type, sg_db, num_of_docs, auth):
    expvars = sg_client.get_expvars(url=sg_admin_url, auth=auth)
    if replication_type == "push":
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_push_doc_count'] == num_of_docs, "delta push replication count is not right"
    else:
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['delta_pull_replication_count'] == 1, "delta pull replication count is not right"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_requested'] == num_of_docs, "delta pull requested is not equal to number of docs"
        assert expvars['syncgateway']['per_db'][sg_db]['delta_sync']['deltas_sent'] == num_of_docs, "delta pull sent is not equal to number of docs"


def enable_delta_sync(c, sg_config, cluster_config, mode, delta_sync_enabled):
    temp_cluster_config = copy_to_temp_conf(cluster_config, mode)
    persist_cluster_config_environment_prop(temp_cluster_config, 'delta_sync_enabled', delta_sync_enabled)
    status = c.sync_gateways[0].restart(config=sg_config, cluster_config=temp_cluster_config)
    assert status == 0, "Sync_gateway did not start"
    time.sleep(10)
