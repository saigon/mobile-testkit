import pytest
import random
import time
from collections import OrderedDict
import datetime
from CBLClient.Database import Database
from CBLClient.Authenticator import Authenticator
from CBLClient.MemoryPointer import MemoryPointer
from CBLClient.Replication import Replication
from keywords.MobileRestClient import MobileRestClient
from keywords.couchbaseserver import CouchbaseServer
from utilities.cluster_config_utils import get_cluster
from keywords.utils import log_info
from libraries.testkit.cluster import Cluster
from keywords.TestServerFactory import TestServerFactory
from keywords.constants import RESULTS_DIR, RBAC_FULL_ADMIN
from libraries.testkit import cluster
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from testsuites.CBLTester.CBL_Functional_tests.SuiteSetup_FunctionalTests.test_query import test_get_doc_ids, \
    test_any_operator, test_doc_get, test_get_docs_with_limit_offset, test_multiple_selects, test_query_where_and_or, \
    test_query_pattern_like, test_query_pattern_regex, test_query_isNullOrMissing, test_query_ordering, \
    test_query_substring, test_query_collation, test_query_join, test_query_inner_join, test_query_cross_join, \
    test_query_left_join, test_query_left_outer_join, test_equal_to, test_not_equal_to, test_greater_than, \
    test_greater_than_or_equal_to, test_less_than, test_less_than_or_equal_to, test_in, test_between, test_is, \
    test_isnot, test_not, test_single_property_fts, test_multiple_property_fts, test_fts_with_ranking, \
    test_getDoc_withValueTypeDouble, test_getDoc_withLocale, test_query_arthimetic


@pytest.mark.listener
@pytest.mark.upgrade_test
def test_upgrade_cbl(params_from_base_suite_setup):
    """
    @summary:
    1. Migrate older-pre-built db to a provided cbl apps
    2. Start the replication and replicate db to cluster
    3. Running all query tests
    4. Perform mutation operations
        a. Add new docs and replicate to cluster
        b. Update docs for migrated db and replicate to cluster
        c. Delete docs from migrated db and replicate to cluster

    @note: encrypted prebuilt databases is copied for 2.1.0 and up and unencrypted database below 2.1.0
    """
    base_url = params_from_base_suite_setup["base_url"]
    sg_db = "db"
    sg_admin_url = params_from_base_suite_setup["sg_admin_url"]
    sg_blip_url = params_from_base_suite_setup["target_url"]
    cluster_config = params_from_base_suite_setup["cluster_config"]
    sg_config = params_from_base_suite_setup["sg_config"]
    cbs_ip = params_from_base_suite_setup["cbs_ip"]
    server_url = params_from_base_suite_setup["server_url"]
    cbs_ssl = params_from_base_suite_setup["cbs_ssl"]
    need_sgw_admin_auth = params_from_base_suite_setup["need_sgw_admin_auth"]
    db = Database(base_url)

    cbl_db, upgrade_cbl_db_name = _upgrade_db(params_from_base_suite_setup)
    cbl_doc_ids = db.getDocIds(cbl_db, limit=40000)
    assert len(cbl_doc_ids) == 31591
    get_doc_id_from_cbs_query = 'select meta().id from `{}` where meta().id not' \
                                ' like "_sync%" ORDER BY id'.format("travel-sample")
    # Replicating docs to CBS
    sg_client = MobileRestClient()
    replicator = Replication(base_url)
    username = "autotest"
    password = "password"

    # Reset cluster to ensure no data in system
    c = Cluster(config=cluster_config)
    c.reset(sg_config_path=sg_config)

    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, username, password, channels=["*"], auth=auth)
    authenticator = Authenticator(base_url)
    cookie, session_id = sg_client.create_session(sg_admin_url, sg_db, username, auth=auth)
    session = cookie, session_id
    replicator_authenticator = authenticator.authentication(session_id, cookie, authentication_type="session")
    repl_config = replicator.configure(cbl_db, sg_blip_url, replication_type="push", continuous=True,
                                       replicator_authenticator=replicator_authenticator)
    repl = replicator.create(repl_config)
    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl, sleep_time=10, max_times=500)
    total = replicator.getTotal(repl)
    completed = replicator.getCompleted(repl)
    assert total == completed
    replicator.stop(repl)

    password = "password"
    cbs_bucket = "travel-sample"
    server = CouchbaseServer(server_url)
    server._create_internal_rbac_bucket_user(cbs_bucket, cluster_config=cluster_config)
    log_info("Connecting to {}/{} with password {}".format(cbs_ip, cbs_bucket, password))
    if cbs_ssl:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    else:
        connection_url = "couchbase://{}".format(cbs_ip)
    sdk_client = get_cluster(connection_url, cbs_bucket)
    log_info("Creating primary index for {}".format(cbs_bucket))
    n1ql_query = "create primary index index1 on `{}`".format(cbs_bucket)
    sdk_client.query(n1ql_query).execute()
    new_cbl_doc_ids = db.getDocIds(cbl_db, limit=40000)
    cbs_doc_ids = []
    for row in sdk_client.query(get_doc_id_from_cbs_query):
        cbs_doc_ids.append(row["id"])
    log_info("cbl_docs {}, cbs_docs {}".format(len(cbs_doc_ids), len(new_cbl_doc_ids)))
    assert sorted(cbs_doc_ids) == sorted(new_cbl_doc_ids), "Total no. of docs are different in CBS and CBL app"

    # Running selected Query tests
    # Runing Query tests
    params_for_query_tests = {"cluster_config": cluster_config,
                              "cluster_topology": params_from_base_suite_setup["cluster_topology"],
                              "base_url": params_from_base_suite_setup["base_url"],
                              "suite_source_db": cbl_db,
                              "suite_cbl_db": upgrade_cbl_db_name,
                              "sync_gateway_version": params_from_base_suite_setup["sync_gateway_version"],
                              }

    query_test_list = [
        (test_get_doc_ids, (params_for_query_tests,)),
        (test_any_operator, (params_for_query_tests,)),
        (test_doc_get, (params_for_query_tests, 'airline_10')),
        (test_doc_get, (params_for_query_tests, 'doc_id_does_not_exist')),
        (test_get_docs_with_limit_offset, (params_for_query_tests, 5, 5)),
        (test_get_docs_with_limit_offset, (params_for_query_tests, -5, -5)),
        (test_multiple_selects, (params_for_query_tests, 'name', 'type', 'country', 'France')),
        (test_query_where_and_or, (params_for_query_tests, 'type', 'hotel', 'country', 'United States', 'country',
                                   'France', 'vacancy', True)),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                   'Royal Engineers Museum')),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                   'Royal engineers museum')),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name', 'eng%e%')),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name', 'Eng%e%')),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                   '%eng____r%')),
        (test_query_pattern_like, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                   '%Eng____r%')),
        (test_query_pattern_regex, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                    '\bEng.*e\b')),
        (test_query_pattern_regex, (params_for_query_tests, 'type', 'landmark', 'country', 'name', 'name',
                                    '\beng.*e\b')),
        (test_query_isNullOrMissing, (params_for_query_tests, 'name', 100)),
        (test_query_ordering, (params_for_query_tests, 'title', 'type', 'hotel')),
        (test_query_substring, (params_for_query_tests, 'email', 'name', 'gmail.com')),
        (test_query_collation, (params_for_query_tests, 'name', 'type', 'hotel', 'country', 'France',
                                'Le Clos Fleuri')),
        (test_query_join, (params_for_query_tests, 'name', 'callsign', 'destinationairport', 'stops', 'airline',
                           'type', 'type', 'sourceairport', 'route', 'airline', 'SFO', 'airlineid')),
        (test_query_inner_join, (params_for_query_tests, 'airline', 'sourceairport', 'country', 'country',
                                 'stops', 'United States', 0, 'icao', 'destinationairport', 10)),
        (test_query_cross_join, (params_for_query_tests, 'country', 'city', 'type', 'type', 'airport', 'airline', 10)),
        (test_query_left_join, (params_for_query_tests, 'airlineid', 10)),
        (test_query_left_outer_join, (params_for_query_tests, 'airlineid', 10)),
        (test_equal_to, (params_for_query_tests, 'country', 'France')),
        (test_equal_to, (params_for_query_tests, 'type', 'airline')),
        (test_not_equal_to, (params_for_query_tests, 'country', 'United States')),
        (test_not_equal_to, (params_for_query_tests, 'type', 'airline')),
        (test_greater_than, (params_for_query_tests, 'id', 1000)),
        (test_greater_than_or_equal_to, (params_for_query_tests, 'id', 1000)),
        (test_less_than, (params_for_query_tests, 'id', 1000)),
        (test_less_than_or_equal_to, (params_for_query_tests, 'id', 1000)),
        (test_in, (params_for_query_tests, 'country', 'France', 'United States')),
        (test_between, (params_for_query_tests, 'id', 1000, 2000)),
        (test_is, (params_for_query_tests, 'callsign')),
        (test_is, (params_for_query_tests, 'iata')),
        (test_isnot, (params_for_query_tests, 'callsign')),
        (test_isnot, (params_for_query_tests, 'iata')),
        (test_not, (params_for_query_tests, 'id', 1000, 2000)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'beautifully', 'landmark', True)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'cons*', 'landmark', True)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'of the beautiful', 'landmark', True)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'local beautiful', 'landmark', True)),
        (test_single_property_fts, (params_for_query_tests, 'content', "'\"foods including'\"", 'landmark', True)),
        (test_single_property_fts, (params_for_query_tests, 'content', "'beautiful NEAR/7 \"local\"'", 'landmark',
                                    True)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'beautiful', 'landmark', False)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'cons*', 'landmark', False)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'of the beautiful', 'landmark', False)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'local beautiful', 'landmark', False)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'foods including', 'landmark', False)),
        (test_single_property_fts, (params_for_query_tests, 'content', 'beautiful NEAR/7 local', 'landmark', False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'centre art', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'tow*', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', '^Beautiful', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'name:cafe art', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'beautiful OR arts', 'landmark',
                                      True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'beauty AND art', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', '(beauty AND art) OR cafe', 'landmark',
                                      True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', '(beautiful OR art) AND photograph',
                                      'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'restaurant NOT chips', 'landmark',
                                      True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'centre art', 'landmark', False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'town*', 'landmark', True)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', '^Beautiful', 'landmark', False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'name:cafe art', 'landmark', False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'beautiful OR arts', 'landmark',
                                      False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'beautiful AND art', 'landmark',
                                      False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', '(beauty AND splendour) OR food',
                                      'landmark', False)),
        (test_multiple_property_fts, (params_for_query_tests, 'content', 'name', 'restaurant NOT chips', 'landmark',
                                      False)),
        (test_fts_with_ranking, (params_for_query_tests, 'content', 'beautiful', 'landmark')),
        (test_getDoc_withValueTypeDouble, (params_for_query_tests, 'doc_with_double_1')),
        (test_getDoc_withLocale, (params_for_query_tests, 'doc_with_double_1')),
        (test_query_arthimetic, (params_for_query_tests,))
    ]

    log_info("\nRunning Query tests")
    tests_result = OrderedDict()
    test_passed = 0
    test_failed = 0
    for item in query_test_list:
        query_test_func = item[0]
        args = item[1]
        # Running query test one by one
        log_info("\n")
        log_info("*" * 20)
        log_info("Executing {} with arguments {}".format(query_test_func.__name__, args[1:]))
        try:
            query_test_func(*args)
            log_info("PASSED")
            tests_result["{}{}".format(query_test_func.__name__, args[1:])] = "PASSED"
            test_passed += 1
        except Exception as err:
            log_info("FAILED:\n {}".format(err))
            tests_result["{}{}".format(query_test_func.__name__, args[1:])] = "FAILED"
            test_failed += 1

    log_info("\n\nTests Result: PASSED {}, FAILED {}".format(test_passed, test_failed))
    for key in tests_result:
        log_info("{}: {}".format(key, tests_result[key]))
    assert test_failed == 0, "Some query tests failed. Do check the logs for the details"

    log_info("\n\nStarting with mutation tests on upgrade CBL db")
    # Adding few docs to db
    new_doc_ids = db.create_bulk_docs(number=5, id_prefix="new_cbl_docs", db=cbl_db)

    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    new_cbl_doc_ids = db.getDocIds(cbl_db, limit=40000)
    cbs_docs = sg_client.get_all_docs(sg_admin_url, sg_db, session, auth=auth)["rows"]
    cbs_doc_ids = [doc["id"] for doc in cbs_docs]
    for new_doc_id in new_doc_ids:
        log_info("Checking if new doc - {} replicated to CBS".format(new_doc_id))
        assert new_doc_id in cbs_doc_ids, "New Docs failed to get replicated"
    assert sorted(cbs_doc_ids) == sorted(new_cbl_doc_ids), "Total no. of docs are different in CBS and CBL app"

    # updating old docs
    doc_ids_to_update = random.sample(cbl_doc_ids, 5)
    docs = db.getDocuments(cbl_db, doc_ids_to_update)
    for doc_id in docs:
        log_info("Updating CBL Doc - {}".format(doc_id))
        data = docs[doc_id]
        data["new_field"] = "test_string_for_{}".format(doc_id)
        db.updateDocument(cbl_db, doc_id=doc_id, data=data)

    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    cbs_docs = sg_client.get_all_docs(sg_admin_url, sg_db, session, auth=auth)["rows"]
    cbs_doc_ids = [doc["id"] for doc in cbs_docs]

    for doc_id in doc_ids_to_update:
        log_info("Checking for updates in doc on CBS: {}".format(doc_id))
        sg_data = sg_client.get_doc(url=sg_admin_url, db=sg_db, doc_id=doc_id, auth=auth)
        assert "new_field" in sg_data, "Updated docs failed to get replicated"
    new_cbl_doc_ids = db.getDocIds(cbl_db, limit=40000)

    assert len(new_cbl_doc_ids) == len(cbs_doc_ids), "Total no. of docs are different in CBS and CBL app"
    assert sorted(cbs_doc_ids) == sorted(new_cbl_doc_ids), "Total no. of docs are different in CBS and CBL app"

    # deleting some of migrated docs
    doc_ids_to_delete = random.sample(cbl_doc_ids, 5)
    log_info("Deleting docs from CBL - {}".format(",".join(doc_ids_to_delete)))
    db.delete_bulk_docs(cbl_db, doc_ids_to_delete)

    replicator.start(repl)
    replicator.wait_until_replicator_idle(repl)
    replicator.stop(repl)

    cbs_docs = sg_client.get_all_docs(sg_admin_url, sg_db, session, auth=auth)["rows"]
    cbs_doc_ids = [doc["id"] for doc in cbs_docs]
    for doc_id in doc_ids_to_delete:
        assert doc_id not in cbs_doc_ids, "Deleted docs failed to get replicated"

    new_cbl_doc_ids = db.getDocIds(cbl_db, limit=40000)
    assert sorted(cbs_doc_ids) == sorted(new_cbl_doc_ids), "Total no. of docs are different in CBS and CBL app"

    # Cleaning the database , tearing down
    db_path = db.getPath(cbl_db).rstrip("/\\")
    if '\\' in db_path:
        db_path = '\\'.join(db_path.split('\\')[:-1])
    else:
        db_path = '/'.join(db_path.split('/')[:-1])
    if db.exists(upgrade_cbl_db_name, db_path):
        log_info("Delete DB - {}".format(upgrade_cbl_db_name))
        db.deleteDB(cbl_db)


def _upgrade_db(args):
    base_liteserv_version = args["base_liteserv_version"]
    upgraded_liteserv_version = args["upgraded_liteserv_version"]
    liteserv_platform = args["liteserv_platform"]
    upgrade_cbl_db_name = "upgraded_db"
    base_url = args["base_url"]
    encrypted_db = args["encrypted_db"]
    db_password = args["db_password"]
    utils_obj = args["utils_obj"]

    if base_liteserv_version > upgraded_liteserv_version:
        pytest.skip("Can't upgrade from higher version db to lower version db")

    supported_base_liteserv = ["1.4", "2.0.0", "2.1.5", "2.5.0", "2.7.0", "2.7.1", "2.8.0"]
    db = Database(base_url)
    if encrypted_db:
        if base_liteserv_version < "2.1.5":
            pytest.skip("Encyption is supported from 2.1.0 onwards."
                        "{} doesn't have encrypted db upgrade support".format(base_liteserv_version))
        db_config = db.configure(password=db_password)
        db_prefix = "travel-sample-encrypted"
    else:
        db_config = db.configure()
        db_prefix = "travel-sample"

    temp_db = db.create("temp_db", db_config)
    time.sleep(1)
    new_db_path = db.getPath(temp_db)
    delimiter = "/"
    if liteserv_platform in ["net-msft", "net-uwp", "java-msft", "javaws-msft"]:
        delimiter = "\\"
    new_db_path = "{}".format(delimiter).join(new_db_path.split(delimiter)[:-2]) + \
                  "{}{}.cblite2".format(delimiter, upgrade_cbl_db_name)
    base_directory = "{}".format(delimiter).join(new_db_path.split(delimiter)[:-2])
    db.deleteDB(temp_db)

    old_liteserv_db_name = ""
    if base_liteserv_version in supported_base_liteserv:
        old_liteserv_db_name = db_prefix + "-" + base_liteserv_version
    else:
        pytest.skip("Run test with one of supported base liteserv version - ".format(supported_base_liteserv))

    if liteserv_platform in ["android", "xamarin-android",
                             "java-macosx", "java-msft", "java-ubuntu", "java-centos",
                             "javaws-macosx", "javaws-msft", "javaws-ubuntu", "javaws-centos"]:
        prebuilt_db_path = "{}.cblite2.zip".format(old_liteserv_db_name)
    elif liteserv_platform == "ios" or liteserv_platform == "xamarin-ios":
        prebuilt_db_path = "Databases/{}.cblite2".format(old_liteserv_db_name)
    else:
        prebuilt_db_path = base_directory + "\\" + r"Databases\{}.cblite2".format(old_liteserv_db_name)

    log_info("Copying db of CBL-{} to CBL-{}".format(base_liteserv_version, upgraded_liteserv_version))
    prebuilt_db_path = db.get_pre_built_db(prebuilt_db_path)
    log_info("prebuild_db_path: {} new_db_path: {}".format(prebuilt_db_path, new_db_path))
    assert "Copied" == utils_obj.copy_files(prebuilt_db_path, new_db_path)
    cbl_db = db.create(upgrade_cbl_db_name, db_config)
    assert isinstance(cbl_db, MemoryPointer), "Failed to migrate db from previous version of CBL"
    return cbl_db, upgrade_cbl_db_name


@pytest.mark.listener
@pytest.mark.upgrade_test
def test_upgrade_testsever_app(params_from_base_suite_setup):

    """
        @summary:
        Added to address issues like CBL-1406
        1. Create docs in cbl
        2. Upgrade the cbl to any new version
        3. Verify that doc count is marching with before the upgrade
        4. Verify after the upgrade we are able to add more docs

    """

    base_url = params_from_base_suite_setup["base_url"]
    cbl_db = "upgrade_db" + str(time.time())
    # Create CBL database
    db = Database(base_url)

    log_info("Creating a Database {}".format(cbl_db))
    source_db = db.create(cbl_db)

    upgraded_liteserv_version = params_from_base_suite_setup["upgraded_liteserv_version"]
    liteserv_platform = params_from_base_suite_setup["liteserv_platform"]
    liteserv_host = params_from_base_suite_setup["liteserv_host"]
    liteserv_port = params_from_base_suite_setup["liteserv_port"]
    debug_mode = params_from_base_suite_setup["debug_mode"]
    device_enabled = params_from_base_suite_setup["device_enabled"]
    community_enabled = params_from_base_suite_setup["community_enabled"]
    testserver = params_from_base_suite_setup["testserver"]
    test_name_cp = params_from_base_suite_setup["test_name_cp"]
    channels_sg = ["ABC"]
    # Create CBL database
    db.create_bulk_docs(20, "cbl-upgrade-docs", db=source_db, channels=channels_sg)
    base_cbl_doc_count = db.getCount(source_db)
    testserver2 = TestServerFactory.create(platform=liteserv_platform,
                                           version_build=upgraded_liteserv_version,
                                           host=liteserv_host,
                                           port=liteserv_port,
                                           community_enabled=community_enabled,
                                           debug_mode=debug_mode)
    log_info("Downloading TestServer ...")
    # Download TestServer app
    testserver2.download()

    log_info("Installing TestServer ...")
    # Install TestServer app
    if device_enabled:
        testserver2.install_device()
        testserver2.start_device("{}/logs/{}-{}-{}.txt".format(RESULTS_DIR, type(testserver).__name__,
                                                               test_name_cp,
                                                               datetime.datetime.now()))
    else:
        testserver2.install()
        testserver2.start("{}/logs/{}-{}-{}.txt".format(RESULTS_DIR, type(testserver).__name__,
                                                        test_name_cp,
                                                        datetime.datetime.now()))

    db = Database(base_url)
    source_db = db.create(cbl_db)
    upgraded_cbl_doc_count = db.getCount(source_db)
    assert upgraded_cbl_doc_count == 20, " Number of docs count is not matching"
    # Create few more docs and make sure the after upgrade we still create docs
    db.create_bulk_docs(2, "cbl-upgrade-docs_new", db=source_db, channels=channels_sg)
    upgraded_cbl_doc_count = db.getCount(source_db)
    assert upgraded_cbl_doc_count > base_cbl_doc_count, " Number of docs count is not matching"


@pytest.mark.listener
@pytest.mark.upgrade_test2
def test_switch_dbs_with_two_cbl_platforms(params_from_base_suite_setup):
    """
        @summary:
        Added to address issues like CBL-1515
        1. Create docs in SG
        2. Verify sg docs replicated to CBL
        3. Start second CBL (use different platform)
        4. Copy DB from 1st CBL to 2nd CBL
        5. Start the same SG replicator
        6. Verify docs are not replicated again in the 2nd CBL

    """
    sg_db = "db"
    sg_url = params_from_base_suite_setup["sg_url"]
    sg_admin_url = params_from_base_suite_setup["sg_admin_url"]
    sg_blip_url = params_from_base_suite_setup["target_url"]
    cluster_config = params_from_base_suite_setup["cluster_config"]
    sync_gateway_version = params_from_base_suite_setup["sync_gateway_version"]
    mode = params_from_base_suite_setup["mode"]
    liteserv_port = params_from_base_suite_setup["liteserv_port"]
    debug_mode = params_from_base_suite_setup["debug_mode"]
    device_enabled = params_from_base_suite_setup["device_enabled"]
    community_enabled = params_from_base_suite_setup["community_enabled"]
    second_liteserv_host = params_from_base_suite_setup["sencond_liteserv_host"]
    second_liteserv_version = params_from_base_suite_setup["second_liteserv_version"]
    second_liteserv_platform = params_from_base_suite_setup["second_liteserv_platform"]
    utils_obj = params_from_base_suite_setup["utils_obj"]
    test_name_cp = params_from_base_suite_setup["test_name_cp"]
    need_sgw_admin_auth = params_from_base_suite_setup["need_sgw_admin_auth"]
    base_url = params_from_base_suite_setup["base_url"]
    # Create CBL database
    db = Database(base_url)
    if sync_gateway_version < "2.0.0":
        pytest.skip('This test cannot run with sg version below 2.0')

    cbl_db_name = "upgrade_db" + str(time.time())
    # Create CBL database
    log_info("Creating a Database {}".format(cbl_db_name))
    cbl_db_obj = db.create(cbl_db_name)

    c = cluster.Cluster(config=cluster_config)
    sg_config = sync_gateway_config_path_for_mode("custom_sync/grant_access_one", mode)
    c.reset(sg_config_path=sg_config)
    channels = ["ABC"]

    sg_client = MobileRestClient()

    # 1. Add docs in SG
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    sg_client.create_user(sg_admin_url, sg_db, "autotest", password="password", channels=channels, auth=auth)
    cookie, session = sg_client.create_session(sg_admin_url, sg_db, "autotest")
    auth_session = cookie, session
    sg_client.add_docs(url=sg_url, db=sg_db, number=20, id_prefix="sg_id_prefix",
                       channels=channels, auth=auth_session)

    # Start and stop continuous replication
    replicator = Replication(base_url)
    # 2. Pull replication to CBL
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session, cookie, authentication_type="session")
    repl = replicator.configure_and_replicate(
        source_db=cbl_db_obj, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
        replication_type="pull", continuous=True, channels=channels)

    log_info(replicator.getCompleted(repl), "replicator getCompleted should be 20")
    sg_docs = sg_client.get_all_docs(url=sg_admin_url, db=sg_db, auth=auth)

    # Verify database doc counts
    cbl_doc_count = db.getCount(cbl_db_obj)
    cbl_doc_ids = db.getDocIds(cbl_db_obj)

    assert len(sg_docs["rows"]) == 20, "Number of sg docs is not equal to total number of cbl docs and sg docs"
    assert cbl_doc_count == 20, "Did not get expected number of cbl docs"

    # Check that all doc ids in SG are also present in CBL
    sg_ids = [row["id"] for row in sg_docs["rows"]]
    for doc in cbl_doc_ids:
        assert doc in sg_ids
    prebuilt_db_path = db.getPath(cbl_db_obj)
    log_info("prebuilt_db_path", prebuilt_db_path)

    # 3. Start second CBL (use different platform)
    testserver2 = TestServerFactory.create(platform=second_liteserv_platform,
                                           version_build=second_liteserv_version,
                                           host=second_liteserv_host,
                                           port=liteserv_port,
                                           community_enabled=community_enabled,
                                           debug_mode=debug_mode)
    log_info("Downloading TestServer ...")
    # Download TestServer app
    testserver2.download()

    log_info("Installing TestServer ...")
    # Install TestServer app
    if device_enabled:
        testserver2.install_device()
        testserver2.start_device("{}/logs/{}-{}-{}.txt".format(RESULTS_DIR, type(testserver2).__name__,
                                                               test_name_cp,
                                                               datetime.datetime.now()))
    else:
        testserver2.install()
        testserver2.start("{}/logs/{}-{}-{}.txt".format(RESULTS_DIR, type(testserver2).__name__,
                                                        test_name_cp,
                                                        datetime.datetime.now()))
    # 4. Copy DB from 1st CBL to 2nd CBL
    db = Database(base_url)
    temp_db_name = "temp_db_name"
    cbl2_db_obj = db.create(temp_db_name)
    new_db_path = db.getPath(cbl2_db_obj)
    db.deleteDB(cbl2_db_obj)
    assert "Copied" == utils_obj.copy_files(prebuilt_db_path, new_db_path)
    new_cbl_db_obj = db.create(cbl_db_name)

    # 5. Start the same SG replicator
    authenticator = Authenticator(base_url)
    replicator_authenticator = authenticator.authentication(session, cookie, authentication_type="session")
    second_cbl_doc_count = db.getCount(new_cbl_db_obj)
    repl = replicator.configure_and_replicate(
        source_db=new_cbl_db_obj, replicator_authenticator=replicator_authenticator, target_url=sg_blip_url,
        replication_type="pull", continuous=True, channels=channels)

    # 6. Verify docs are not replicated again in the 2nd CBL
    assert replicator.getCompleted(
        repl) == 0, "Replicator getCompleted doc should be zero, it shouldn't start from zero"
    assert second_cbl_doc_count == cbl_doc_count, "After moving the DBs docs are updating "
    replicator.stop(repl)
    testserver2.stop()
