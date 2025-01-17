import time
import pytest

from libraries.testkit.admin import Admin
from libraries.testkit.cluster import Cluster
from libraries.testkit.web_server import WebServer
from libraries.testkit.parallelize import in_parallel

from keywords.utils import log_info
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords.MobileRestClient import MobileRestClient
from requests.auth import HTTPBasicAuth
from keywords.constants import RBAC_FULL_ADMIN

# implements scenarios: 18 and 19
from utilities.cluster_config_utils import persist_cluster_config_environment_prop, copy_to_temp_conf


@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.parametrize("sg_conf_name, num_users, num_channels, num_docs, num_revisions, x509_cert_auth", [
    pytest.param("webhooks/webhook_offline", 5, 1, 1, 2, False, marks=[pytest.mark.sanity, pytest.mark.oscertify]),
    ("webhooks/webhook_offline", 5, 1, 1, 2, True)
])
def test_db_online_offline_webhooks_offline(params_from_base_test_setup, sg_conf_name, num_users, num_channels,
                                            num_docs, num_revisions, x509_cert_auth):
    start = time.time()

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Running 'test_db_online_offline_webhooks_offline'")
    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using num_users: {}".format(num_users))
    log_info("Using num_channels: {}".format(num_channels))
    log_info("Using num_docs: {}".format(num_docs))
    log_info("Using num_revisions: {}".format(num_revisions))

    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_conf = temp_cluster_config

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_conf)

    init_completed = time.time()
    log_info("Initialization completed. Time taken:{}s".format(init_completed - start))

    channels = ["channel-" + str(i) for i in range(num_channels)]
    password = "password"
    ws = WebServer()
    ws.start()

    sgs = cluster.sync_gateways

    admin = Admin(sgs[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])
    # Register User
    log_info("Register User")
    user_objects = admin.register_bulk_users(target=sgs[0], db="db", name_prefix="User",
                                             number=num_users, password=password, channels=channels)

    # Add User
    log_info("Add docs")
    bulk = True
    in_parallel(user_objects, 'add_docs', num_docs, bulk)

    # Update docs
    log_info("Update docs")
    in_parallel(user_objects, 'update_docs', num_revisions)
    time.sleep(10)

    # Take db offline
    sg_client = MobileRestClient()
    status = sg_client.take_db_offline(cluster_conf=cluster_conf, db="db")
    assert status == 0

    time.sleep(5)
    db_info = admin.get_db_info("db")
    log_info("Expecting db state {} found db state {}".format("Offline", db_info['state']))
    assert db_info["state"] == "Offline"

    webhook_events = ws.get_data()
    time.sleep(5)
    log_info("webhook event {}".format(webhook_events))

    try:
        last_event = webhook_events[-1]
        assert last_event['state'] == 'offline'

        # Bring db online
        status = sg_client.bring_db_online(cluster_conf=cluster_conf, db="db")
        assert status == 0

        time.sleep(5)
        db_info = admin.get_db_info("db")
        log_info("Expecting db state {} found db state {}".format("Online", db_info['state']))
        assert db_info["state"] == "Online"
        time.sleep(5)
        webhook_events = ws.get_data()
        last_event = webhook_events[-1]
        assert last_event['state'] == 'online'
        time.sleep(10)
        log_info("webhook event {}".format(webhook_events))
    except IndexError:
        log_info("Received index error")
        raise
    finally:
        ws.stop()


# implements scenarios: 21
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_users, num_channels, num_docs, num_revisions", [
    ("webhooks/webhook_offline", 5, 1, 1, 2),
])
def test_db_online_offline_webhooks_offline_two(params_from_base_test_setup, sg_conf_name, num_users, num_channels, num_docs, num_revisions):

    start = time.time()

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Running 'test_db_online_offline_webhooks_offline_two'")
    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using num_users: {}".format(num_users))
    log_info("Using num_channels: {}".format(num_channels))
    log_info("Using num_docs: {}".format(num_docs))
    log_info("Using num_revisions: {}".format(num_revisions))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_conf)

    init_completed = time.time()
    log_info("Initialization completed. Time taken:{}s".format(init_completed - start))

    channels = ["channel-" + str(i) for i in range(num_channels)]
    password = "password"
    ws = WebServer()
    ws.start()

    sgs = cluster.sync_gateways

    admin = Admin(sgs[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])
    # Register User
    log_info("Register User")
    user_objects = admin.register_bulk_users(target=sgs[0], db="db", name_prefix="User",
                                             number=num_users, password=password, channels=channels)

    # Add User
    log_info("Add docs")
    bulk = True
    in_parallel(user_objects, 'add_docs', num_docs, bulk)

    # Update docs
    log_info("Update docs")
    in_parallel(user_objects, 'update_docs', num_revisions)
    time.sleep(10)

    cluster.servers[0].delete_bucket("data-bucket")

    webhook_events = ws.get_data()
    time.sleep(5)
    log_info("webhook event {}".format(webhook_events))
    try:
        last_event = webhook_events[-1]
        assert last_event['state'] == 'offline'
    except IndexError:
        log_info("Received index error")
        raise
    finally:
        ws.stop()
