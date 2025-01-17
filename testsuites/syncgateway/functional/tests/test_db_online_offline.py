import time
import concurrent.futures
import uuid

import pytest

from libraries.testkit.admin import Admin
from libraries.testkit.cluster import Cluster
from libraries.testkit.user import User
from libraries.testkit.verify import verify_changes

import libraries.testkit.settings

from keywords.constants import RBAC_FULL_ADMIN
from requests.exceptions import HTTPError
from keywords.utils import log_info
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords.MobileRestClient import MobileRestClient
from utilities.cluster_config_utils import persist_cluster_config_environment_prop, copy_to_temp_conf

from requests.auth import HTTPBasicAuth

NUM_ENDPOINTS = 13


# Scenario 1
@pytest.mark.sanity
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs, x509_cert_auth", [
    ("bucket_online_offline/bucket_online_offline_default", 100, False)
])
def test_online_default_rest(params_from_base_test_setup, sg_conf_name, num_docs, x509_cert_auth):
    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    disable_tls_server = params_from_base_test_setup["disable_tls_server"]
    if x509_cert_auth and disable_tls_server:
        pytest.skip("x509 test cannot run tls server disabled")
    if x509_cert_auth:
        temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
        persist_cluster_config_environment_prop(temp_cluster_config, 'x509_certs', True)
        persist_cluster_config_environment_prop(temp_cluster_config, 'server_tls_skip_verify', False)
        cluster_conf = temp_cluster_config

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # all db endpoints should function as expected
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0

    # Scenario 4
    # Check the db has an Online state at each running sync_gateway
    for sg in cluster.sync_gateways:
        admin = Admin(sg)
        if auth:
            admin.auth = HTTPBasicAuth(auth[0], auth[1])
        db_info = admin.get_db_info("db")
        assert db_info["state"] == "Online"


# Scenario 2
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_offline_false", 100)
])
def test_offline_false_config_rest(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # all db endpoints should function as expected
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0

    # Scenario 4
    # Check the db has an Online state at each running sync_gateway
    for sg in cluster.sync_gateways:
        admin = Admin(sg)
        if auth:
            admin.auth = HTTPBasicAuth(auth[0], auth[1])
        db_info = admin.get_db_info("db")
        assert db_info["state"] == "Online"


# Scenario 3
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default", 100)
])
def test_online_to_offline_check_503(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # all db endpoints should function as expected
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0

    # Take bucket offline
    sg_client = MobileRestClient()
    status = sg_client.take_db_offline(cluster_conf=cluster_conf, db="db")
    assert status == 0

    # all db endpoints should return 503
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=False, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)

    # We hit NUM_ENDPOINT unique REST endpoints + num of doc PUT failures
    assert len(errors) == NUM_ENDPOINTS + (num_docs * 2)
    for error_tuple in errors:
        log_info("({},{})".format(error_tuple[0], error_tuple[1]))
        assert error_tuple[1] == 503


# Scenario 5 - continuous
# NOTE: Was disabled for di
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default", 5000)
])
def test_online_to_offline_changes_feed_controlled_close_continuous(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    sg_client = MobileRestClient()
    seth = admin.register_user(target=cluster.sync_gateways[0], db="db", name="seth", password="password", channels=["ABC"])
    doc_pusher = admin.register_user(target=cluster.sync_gateways[0], db="db", name="doc_pusher", password="password", channels=["ABC"])

    docs_in_changes = dict()
    doc_add_errors = list()

    with concurrent.futures.ThreadPoolExecutor(max_workers=libraries.testkit.settings.MAX_REQUEST_WORKERS) as executor:
        futures = dict()
        futures[executor.submit(seth.start_continuous_changes_tracking, termination_doc_id=None)] = "continuous"
        futures[executor.submit(doc_pusher.add_docs, num_docs)] = "docs_push"
        offline_retries = 0
        time_sec = 10
        while offline_retries < 10:
            try:
                assert sg_client.take_db_offline(cluster_conf, "db") == 0
                futures[executor.submit(sg_client.take_db_offline, cluster_conf, "db")] = "db_offline_task"
                break
            except AssertionError as error:
                offline_retries = offline_retries + 1
                time.sleep(time_sec)
                if offline_retries == 10:
                    raise error
        for future in concurrent.futures.as_completed(futures):
            task_name = futures[future]

            if task_name == "db_offline_task":
                log_info("DB OFFLINE")
                # make sure db_offline returns 200
                # sync-gateway-db-offline.yml checks for 200
                assert future.result() == 0
            elif task_name == "docs_push":
                log_info("DONE PUSHING DOCS")
                doc_add_errors = future.result()
            elif task_name == "continuous":
                docs_in_changes = future.result()
                log_info("DOCS FROM CHANGES")
                for k, v in list(docs_in_changes.items()):
                    log_info("DFC -> {}:{}".format(k, v))

    log_info("Number of docs from _changes ({})".format(len(docs_in_changes)))
    log_info("Number of docs add errors ({})".format(len(doc_add_errors)))

    # Some docs should have made it to _changes
    assert len(docs_in_changes) > 0

    # Bring db back online
    status = sg_client.bring_db_online(cluster_conf=cluster_conf, db="db")
    assert status == 0

    # Get all docs that have been pushed
    # Verify that changes returns all of them
    all_docs = doc_pusher.get_all_docs()
    num_docs_pushed = len(all_docs["rows"])
    verify_changes(doc_pusher, expected_num_docs=num_docs_pushed, expected_num_revisions=0, expected_docs=doc_pusher.cache)

    # Check that the number of errors return when trying to push while db is offline + num of docs in db
    # should equal the number of docs
    assert num_docs_pushed + len(doc_add_errors) == num_docs


# Scenario 6 - longpoll
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.oscertify
@pytest.mark.basicauth
@pytest.mark.parametrize("sg_conf_name, num_docs, num_users", [
    ("bucket_online_offline/bucket_online_offline_default", 5000, 40)
])
def test_online_to_offline_continous_changes_feed_controlled_close_sanity_mulitple_users(params_from_base_test_setup, sg_conf_name, num_docs, num_users):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))
    log_info("Using num_users: {}".format(num_users))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    sg_client = MobileRestClient()
    users = admin.register_bulk_users(target=cluster.sync_gateways[0], db="db", name_prefix="user", password="password", number=num_users, channels=["ABC"])
    feed_close_results = list()

    with concurrent.futures.ThreadPoolExecutor(max_workers=libraries.testkit.settings.MAX_REQUEST_WORKERS) as executor:
        # start continuous tracking with no timeout, will block until connection is closed by db going offline
        futures = {executor.submit(user.start_continuous_changes_tracking, termination_doc_id=None): user.name for user in users}

        time.sleep(5)
        futures[executor.submit(sg_client.take_db_offline, cluster_conf, "db")] = "db_offline_task"

        for future in concurrent.futures.as_completed(futures):
            task_name = futures[future]

            if task_name == "db_offline_task":
                log_info("DB OFFLINE")
                # make sure db_offline returns 200
                # sync-gateway-db-offline.yml checks for 200
                assert future.result() == 0
            if task_name.startswith("user"):
                # Long poll will exit with 503, return docs in the exception
                log_info("POLLING DONE")
                try:
                    docs = future.result()
                    feed_close_results.append(docs)
                except Exception as e:
                    log_info("Continious feed close error: {}".format(e))
                    # continuous should be closed so this exception should never happen
                    assert 0

    # Assert that the feed close results length is num_users
    assert len(feed_close_results) == num_users

    # No docs should be returned
    for feed_result in feed_close_results:
        assert len(feed_result) == 0


# Scenario 6 - longpoll
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.oscertify
@pytest.mark.basicauth
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default", 5000)
])
def test_online_to_offline_changes_feed_controlled_close_longpoll_sanity(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    sg_client = MobileRestClient()
    seth = admin.register_user(target=cluster.sync_gateways[0], db="db", name="seth", password="password", channels=["ABC"])
    docs_in_changes = dict()

    with concurrent.futures.ThreadPoolExecutor(max_workers=libraries.testkit.settings.MAX_REQUEST_WORKERS) as executor:
        futures = dict()
        # start longpoll tracking with no timeout, will block until longpoll is closed by db going offline
        futures[executor.submit(seth.start_longpoll_changes_tracking, termination_doc_id=None, timeout=0, loop=False)] = "polling"
        time.sleep(5)
        futures[executor.submit(sg_client.take_db_offline, cluster_conf, "db")] = "db_offline_task"

        for future in concurrent.futures.as_completed(futures):
            task_name = futures[future]

            if task_name == "db_offline_task":
                log_info("DB OFFLINE")
                # make sure db_offline returns 200
                # sync-gateway-db-offline.yml checks for 200
                assert future.result() == 0
            if task_name == "polling":
                # Long poll will exit with 503, return docs in the exception
                log_info("POLLING DONE")
                try:
                    docs_in_changes, last_seq_num = future.result()
                except Exception as e:
                    log_info("Longpoll feed close error: {}".format(e))
                    # long poll should be closed so this exception should never happen
                    assert 0

    # Account for _user doc
    # last_seq may be of the form '1' for channel cache or '1-0' for distributed index
    seq_num_component = last_seq_num.split("-")
    assert 1 == int(seq_num_component[0])
    assert len(docs_in_changes) == 0


# Scenario 6 - longpoll
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.oscertify
@pytest.mark.basicauth
@pytest.mark.parametrize("sg_conf_name, num_docs, num_users", [
    ("bucket_online_offline/bucket_online_offline_default", 5000, 40)
])
def test_online_to_offline_longpoll_changes_feed_controlled_close_sanity_mulitple_users(params_from_base_test_setup, sg_conf_name, num_docs, num_users):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))
    log_info("Using num_users: {}".format(num_users))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    sg_client = MobileRestClient()
    users = admin.register_bulk_users(target=cluster.sync_gateways[0], db="db", name_prefix="user", password="password", number=num_users, channels=["ABC"])

    feed_close_results = list()

    with concurrent.futures.ThreadPoolExecutor(max_workers=libraries.testkit.settings.MAX_REQUEST_WORKERS) as executor:
        # start longpoll tracking with no timeout, will block until longpoll is closed by db going offline
        futures = {executor.submit(user.start_longpoll_changes_tracking, termination_doc_id=None, timeout=0, loop=False): user.name for user in users}

        time.sleep(5)
        futures[executor.submit(sg_client.take_db_offline, cluster_conf, "db")] = "db_offline_task"

        for future in concurrent.futures.as_completed(futures):
            task_name = futures[future]

            if task_name == "db_offline_task":
                log_info("DB OFFLINE")
                # make sure db_offline returns 200
                # sync-gateway-db-offline.yml checks for 200
                assert future.result() == 0
            if task_name.startswith("user"):
                # Long poll will exit with 503, return docs in the exception
                log_info("POLLING DONE")
                try:
                    docs_in_changes, last_seq_num = future.result()
                    feed_close_results.append((docs_in_changes, last_seq_num))
                except Exception as e:
                    log_info("Longpoll feed close error: {}".format(e))
                    # long poll should be closed so this exception should never happen
                    assert 0

    # Assert that the feed close results length is num_users
    assert len(feed_close_results) == num_users

    # Account for _user doc
    # last_seq may be of the form '1' for channel cache or '1-0' for distributed index
    for feed_result in feed_close_results:
        docs_in_changes = feed_result[0]
        seq_num_component = feed_result[1].split("-")
        assert len(docs_in_changes) == 0
        assert int(seq_num_component[0]) > 0


# Scenario 6 - longpoll
# NOTE: Was disabled for di
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.oscertify
@pytest.mark.basicauth
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default", 5000)
])
def test_online_to_offline_changes_feed_controlled_close_longpoll(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    seth = admin.register_user(target=cluster.sync_gateways[0], db="db", name="seth", password="password", channels=["ABC"])
    doc_pusher = admin.register_user(target=cluster.sync_gateways[0], db="db", name="doc_pusher", password="password", channels=["ABC"])
    sg_client = MobileRestClient()
    bulk = False
    docs_in_changes = dict()
    doc_add_errors = list()

    with concurrent.futures.ThreadPoolExecutor(max_workers=libraries.testkit.settings.MAX_REQUEST_WORKERS) as executor:
        futures = dict()
        futures[executor.submit(seth.start_longpoll_changes_tracking, termination_doc_id=None)] = "polling"
        time.sleep(5)
        futures[executor.submit(doc_pusher.add_docs, num_docs, bulk)] = "docs_push"
        futures[executor.submit(sg_client.take_db_offline, cluster_conf, "db")] = "db_offline_task"
        for future in concurrent.futures.as_completed(futures):
            task_name = futures[future]

            if task_name == "db_offline_task":
                log_info("DB OFFLINE")
                # make sure db_offline returns 200
                # sync-gateway-db-offline.yml checks for 200
                assert future.result() == 0
            if task_name == "docs_push":
                log_info("DONE PUSHING DOCS")
                doc_add_errors = future.result()
            if task_name == "polling":
                # Long poll will exit with 503, return docs in the exception
                log_info("POLLING DONE")
                try:
                    docs_in_changes = future.result()
                except Exception as e:
                    log_info(e)
                    log_info("POLLING DONE EXCEPTION")
                    log_info("ARGS: {}".format(e.args))
                    docs_in_changes = e.args[0]["docs"]
                    last_seq_num = e.args[0]["last_seq_num"]
                    log_info("DOCS FROM longpoll")
                    for k, v in list(docs_in_changes.items()):
                        log_info("DFC -> {}:{}".format(k, v))
                    log_info("LAST_SEQ_NUM FROM longpoll {}".format(last_seq_num))

    log_info("Number of docs from _changes ({})".format(len(docs_in_changes)))
    log_info("last_seq_num _changes ({})".format(last_seq_num))
    log_info("Number of docs add errors ({})".format(len(doc_add_errors)))

    # Some docs should have made it to _changes
    assert len(docs_in_changes) > 0

    # Make sure some docs failed due to db being taken offline
    assert len(doc_add_errors) > 0

    seq_num_component = last_seq_num.split("-")
    if mode == "cc":
        # assert the last_seq_number == number _changes + 2 (_user doc starts and one and docs start at _user doc seq + 2)
        assert len(docs_in_changes) + 3 == int(seq_num_component[0])
    else:
        # assert the value is not an empty string
        assert last_seq_num != ""

    # Bring db back online
    sg_client = MobileRestClient()
    status = sg_client.bring_db_online(cluster_conf=cluster_conf, db="db")
    assert status == 0

    # Get all docs that have been pushed
    # Verify that changes returns all of them
    all_docs = doc_pusher.get_all_docs()
    num_docs_pushed = len(all_docs["rows"])
    verify_changes(doc_pusher, expected_num_docs=num_docs_pushed, expected_num_revisions=0, expected_docs=doc_pusher.cache)

    # Check that the number of errors return when trying to push while db is offline + num of docs in db
    # should equal the number of docs
    assert num_docs_pushed + len(doc_add_errors) == num_docs


# Scenario 6
# NOTE: Was disabled for di
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_offline_true", 100)
])
def test_offline_true_config_bring_online(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # all db endpoints should fail with 503
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=False, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)

    assert len(errors) == NUM_ENDPOINTS + (num_docs * 2)
    for error_tuple in errors:
        log_info("({},{})".format(error_tuple[0], error_tuple[1]))
        assert error_tuple[1] == 503

    # Scenario 9
    # POST /db/_online
    # Take bucket online
    sg_client = MobileRestClient()
    status = sg_client.bring_db_online(cluster_conf=cluster_conf, db="db")
    assert status == 0

    # all db endpoints should succeed
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0


# Scenario 14
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default_dcp", 100),
    ("bucket_online_offline/bucket_online_offline_default", 100)
])
def test_db_offline_tap_loss_sanity(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    # all db rest enpoints should succeed
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0

    # Delete bucket to sever TAP feed
    cluster.servers[0].delete_bucket("data-bucket")

    # Check that bucket is in offline state
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=False, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == NUM_ENDPOINTS + (num_docs * 2)
    for error_tuple in errors:
        log_info("({},{})".format(error_tuple[0], error_tuple[1]))
        assert error_tuple[1] == 503


# Scenario 11
# NOTE: Was disabled for di
@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_default", 100)
])
def test_db_delayed_online(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    admin = Admin(cluster.sync_gateways[0])
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    time.sleep(2)
    sg_client = MobileRestClient()
    status = sg_client.take_db_offline(cluster_conf=cluster_conf, db="db")
    assert status == 0

    log_info("offline request response status: {}".format(status))
    time.sleep(10)

    db_info = admin.get_db_info("db")
    assert db_info["state"] == "Offline"

    status = sg_client.bring_db_online(cluster_conf=cluster_conf, db="db", delay=15)
    assert status == 0
    log_info("online request response status: {}".format(status))

    time.sleep(20)

    db_info = admin.get_db_info("db")
    assert db_info["state"] == "Online"

    # all db rest enpoints should succeed
    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
    assert len(errors) == 0


@pytest.mark.syncgateway
@pytest.mark.onlineoffline
@pytest.mark.basicauth
@pytest.mark.role
@pytest.mark.bulkops
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name, num_docs", [
    ("bucket_online_offline/bucket_online_offline_multiple_dbs_unique_buckets", 100)
])
def test_multiple_dbs_unique_buckets_lose_tap(params_from_base_test_setup, sg_conf_name, num_docs):

    cluster_conf = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    if mode == "di":
        pytest.skip("Offline tests not supported in Di mode -- see https://github.com/couchbase/sync_gateway/issues/2423#issuecomment-300841425")

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    log_info("Using cluster_conf: {}".format(cluster_conf))
    log_info("Using sg_conf: {}".format(sg_conf))
    log_info("Using num_docs: {}".format(num_docs))

    cluster = Cluster(config=cluster_conf)
    cluster.reset(sg_config_path=sg_conf)

    dbs = ["db1", "db2", "db3", "db4"]

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    # all db rest endpoints should succeed
    for db in dbs:
        log_info("Doing rest scan on db: {}".format(db))
        errors = rest_scan(cluster.sync_gateways[0], db=db, online=True, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
        assert len(errors) == 0

    log_info("Deleting data-bucket-1 and data-bucket-3")
    cluster.servers[0].delete_bucket("data-bucket-1")
    cluster.servers[0].delete_bucket("data-bucket-3")

    # Check that db2 and db4 are still Online
    log_info("Check that db2 and db4 are still Online")
    for db in ["db2", "db4"]:
        errors = rest_scan(cluster.sync_gateways[0], db=db, online=True, num_docs=num_docs, user_name="adam", channels=["CBS"], auth=auth)
        assert len(errors) == 0

    # Check that db1 and db3 go offline
    log_info("Check that db1 and db3 go offline")
    for db in ["db1", "db3"]:
        errors = rest_scan(cluster.sync_gateways[0], db=db, online=False, num_docs=num_docs, user_name="seth", channels=["ABC"], auth=auth)
        num_expected_errors = NUM_ENDPOINTS + (num_docs * 2)
        if len(errors) != num_expected_errors:
            log_info("Expected {} errors, but got {}".format(num_expected_errors, len(errors)))
            for err in errors:
                log_info("{}".format(err))
        assert len(errors) == num_expected_errors
        for error_tuple in errors:
            log_info("({},{})".format(error_tuple[0], error_tuple[1]))
            assert error_tuple[1] == 503


def rest_scan(sync_gateway, db, online, num_docs, user_name, channels, auth=None):

    # Missing ADMIN
    # TODO: GET /{db}/_session/{session-id}
    # TODO: POST /{db}/_session
    # TODO: DELETE /{db}/_session/{session-id}
    # TODO: DELETE /{db}/_user/{name}/_session/{session-id}
    # TODO: DELETE /{db}/_user/{name}/_session

    # TODO: DELETE /{db}/_user/{name}

    # TODO: POST /{db}/_role/
    # TODO: DELETE /{db}/_role/{name}

    # Missing REST
    # TODO: POST /{db}/_all_docs

    # TODO: DELETE /{db}/{doc}
    # TODO: PUT /{db}/{doc}/{attachment}
    # TODO: GET /{db}/{doc}/{attachment}

    # Missing Local Document
    # TODO: DELETE /{db}/{local-doc-id}

    # Missing Authentication
    # TODO: POST /{db}/_facebook_token

    admin = Admin(sync_gateway=sync_gateway)
    if auth:
        admin.auth = HTTPBasicAuth(auth[0], auth[1])

    error_responses = list()

    # PUT /{db}/_role/{name}
    try:
        admin.create_role(db=db, name="radio_stations", channels=["HWOD", "KDWB"])
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/_role
    try:
        roles = admin.get_roles(db=db)
        log_info(roles)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/_role/{name}
    try:
        role = admin.get_role(db=db, name="radio_stations")
        log_info(role)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # PUT /{db}/_user/{name}
    try:
        user = admin.register_user(target=sync_gateway, db=db, name=user_name, password="password", channels=channels)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/_user
    try:
        users_info = admin.get_users_info(db=db)
        log_info(users_info)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/_user/{name}
    try:
        user_info = admin.get_user_info(db=db, name=user_name)
        log_info(user_info)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}
    try:
        db_info = admin.get_db_info(db=db)
        if not online:
            assert db_info["state"] == "Offline"
        else:
            assert db_info["state"] == "Online"
        log_info(db_info)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # Create dummy user to hit endpoint if offline, user creation above will fail
    if not online:
        user = User(target=sync_gateway, db=db, name=user_name, password="password", channels=channels)

    # PUT /{db}/{name}
    add_docs_errors = user.add_docs(num_docs=num_docs)
    error_responses.extend(add_docs_errors)

    # POST /{db}/_bulk_docs
    bulk_doc_errors = user.add_docs(num_docs=num_docs, bulk=False)
    error_responses.extend(bulk_doc_errors)

    # POST /{db}/
    for i in range(num_docs):
        try:
            user.add_doc()
        except HTTPError as e:
            log_info((e.response.url, e.response.status_code))
            error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/{name}
    # PUT /{db}/{name}
    if online:
        update_docs_errors = user.update_docs(num_revs_per_doc=1)
        error_responses.extend(update_docs_errors)
    else:
        try:
            # Try to hit the GET enpoint for "test-id"
            user.update_doc("test-id")
        except HTTPError as e:
            log_info((e.response.url, e.response.status_code))
            error_responses.append((e.response.url, e.response.status_code))

    # PUT /{db}/{local-doc-id}
    local_doc_id = uuid.uuid4()
    try:
        doc = user.add_doc("_local/{}".format(local_doc_id), content={"message": "I should not be replicated"})
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/{local-doc-id}
    try:
        doc = user.get_doc("_local/{}".format(local_doc_id))
        assert doc["content"]["message"] == "I should not be replicated"
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # GET /{db}/_all_docs
    try:
        all_docs_result = user.get_all_docs()
        # num_docs /{db}/{doc} PUT + num_docs /{db}/_bulk_docs + num_docs POST /{db}/
        assert len(all_docs_result["rows"]) == num_docs * 3
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # POST /{db}/_bulk_get
    try:
        doc_ids = list(user.cache.keys())
        first_ten_ids = doc_ids[:10]
        first_ten = user.get_docs(first_ten_ids)
        assert len(first_ten) == 10
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    # wait for changes
    time.sleep(2)

    # GET /{db}/_changes
    try:
        user.get_changes()
        # If successful, verify the _changes feed
        verify_changes(user, expected_num_docs=num_docs * 3, expected_num_revisions=1, expected_docs=user.cache)
    except HTTPError as e:
        log_info((e.response.url, e.response.status_code))
        error_responses.append((e.response.url, e.response.status_code))

    return error_responses


# Reenable for 1.3
# Scenario 16
# def test_config_change_invalid_1(cluster, num_docs):
#     num_docs = 100
#
#     cluster.reset("bucket_online_offline/bucket_online_offline_offline_false_cc.json")
#     admin = Admin(cluster.sync_gateways[0])
#
#     # all db endpoints should succeed
#     errors = rest_scan(cluster.sync_gateways[0], db="db", online=True, num_docs=num_docs, user_name="seth", channels=["ABC"])
#     assert(len(errors) == 0)
#
#     config = admin.get_db_config(db="db")
#     print(config)
#
#     # Invalid config
#     new_config = {
#         "db": {
#             "server": "http://{}:8091".format(cluster.servers[0].ip),
#             "bucket": "data-bucket",
#             "users": {
#                 "seth": {"password": "password", "admin_channels": ["*", "ABC"]},
#                 "Ashvinder": {"password": "password", "admin_channels": ["*", "CBS"]},
#                 "Andy": {"password": "password", "admin_channels": ["*", "NBC"]}
#             }
#         }
#     }
#
#     # VERIFY
#     # Should status should be an error state?
#     status = admin.put_db_config(db="db", config=new_config)
#     assert(status == 201)
#
#     # Take "db" offline
#     status = admin.take_db_offline(db="db")
#     assert(status == 200)
#
#     # all db endpoints should 503
#     errors = rest_scan(cluster.sync_gateways[0], db="db", online=False, num_docs=num_docs, user_name="seth", channels=["ABC"])
#     assert(len(errors) == NUM_ENDPOINTS + num_docs)
#     for error_tuple in errors:
#         assert(error_tuple[1] == 503)
#
#     # Bring "db" online
#     # VERIFY - Correct status code
#     status = admin.bring_db_online(db="db")
#     assert(status == 500)


# # Scenario 17
# @pytest.mark.dbonlineoffline
# def test_db_online_offline_with_invalid_legal_config(cluster, disable_http_time_sec):
#    cluster.reset("bucket_online_offline/bucket_online_offline_offline_false_cc.json")
#    admin = Admin(cluster.sync_gateways[0])
#
#    # all db endpoints should succeed
#    errors = rest_scan(cluster.sync_gateways[0], db="db", online=True)
#    assert(len(errors) == 0)
#
#    #restart_status = cluster.sync_gateways[0].restart("bucket_online_offline/db_online_offline_invalid_db_cc.json")
#    #assert restart_status == 0
#
#    config = admin.get_db_config(db="db")
#    print(config)
#
#    # Invalid config
#    new_config = {
#        "db": {
#            "server": "http://{}:8091".format(cluster.servers[0].ip),
#            "bucket": "data-bucket",
#            "users": {
#                "seth": {"password": "password", "admin_channels": ["*", "ABC"]},
#                "Ashvinder": {"password": "password", "admin_channels": ["*", "CBS"]},
#                "Andy": {"password": "password", "admin_channels": ["*", "NBC"]}
#            }
#        }
#    }
#
#    status = admin.put_db_config(db="db", config=new_config)
#    assert(status == 201)
#
#    # Take "db" offline
#    status = admin.bring_db_online(db="db")
#    log.info("status: {}".format(status))
#    assert(status == 200)
#
#
#
