import pytest
import time

from keywords.utils import log_info
from libraries.testkit.cluster import Cluster
from keywords.SyncGateway import sync_gateway_config_path_for_mode, SyncGateway, create_docs_via_sdk
from keywords import document
from keywords.utils import host_for_url, deep_dict_compare
from couchbase.bucket import Bucket
from keywords.MobileRestClient import MobileRestClient
from keywords.ClusterKeywords import ClusterKeywords
from libraries.testkit import cluster
from concurrent.futures import ThreadPoolExecutor
from libraries.testkit.prometheus import verify_stat_on_prometheus
from libraries.testkit.syncgateway import get_buckets_from_sync_gateway_config
from utilities.cluster_config_utils import persist_cluster_config_environment_prop
from utilities.cluster_config_utils import is_centralized_persistent_config_disabled, copy_to_temp_conf
from utilities.cluster_config_utils import copy_sgconf_to_temp, replace_string_on_sgw_config
from libraries.testkit.syncgateway import construct_dbconfig_json
from shutil import copyfile
from keywords.constants import SYNC_GATEWAY_CONFIGS_CPC


@pytest.mark.syncgateway
def test_centralized_persistent_flag_off(params_from_base_test_setup):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    "1. Set up server and Syncgateway
    2. Have 2 nodes in the SGW cluster 
    3. Have disable_persistent config key to true in SGW config on 2 nodes of SGW and start sgws
    4. Set up connecting to server , add database config on one of the sGW node with config like delta_sync on on one node via rest end point
    5. Verify that only on SGW node1 delta sync on , but off on sgw node2
    6. Restart SGWs . database config with delta sync is off on sgw node1"

    """

    
    sg_conf_name = "sync_gateway_default"
    sg_obj = SyncGateway()
    # sg_conf_name2 = "xattrs/no_import"

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sync_gateway_version = params_from_base_test_setup['sync_gateway_version']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']

    if sync_gateway_version < "3.0.0":
        pytest.skip('This test can run with sgw version 3.0 and above')
    # 1. Set up server and Syncgateway
    # 2. Have 2 nodes in the SGW cluster
    sg_conf1 = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    # sg_conf2 = sync_gateway_config_path_for_mode(sg_conf_name2, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    cbs_host = host_for_url(cbs_url)
    cbs_cluster = Cluster(config=cluster_conf)

    # 3. Have disable_persistent config key to true in SGW config on 2 nodes of SGW and start sgws
    persist_cluster_config_environment_prop(cluster_conf, 'disable_persistent_config', True)
    cbs_cluster.reset(sg_config_path=sg_conf1)

    # 4. Update database config on one of the sGW node with config like delta_sync on on one node and off on another node via rest end point
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]
    sg_dbs = sg1.admin.get_dbs()
    sg_db = sg_dbs[0]
    sg1_db_config = sg1.admin.get_db_config(sg_db)
    print("sg1 db config is ", sg1_db_config)
    delta_sync_true = {'enabled': True}
    delta_sync_false = {'enabled': False}
    sg1_db_config["delta_sync"] = delta_sync_true
    """
    sg1.admin.put_db_config(sg_db, sg1_db_config)
    sg1_return_db = sg1.admin.get_db_config(sg_db)
    print("sg_return db enabled is ", sg1_return_db["delta_sync"]["enabled"])
    sg2_db_config = sg1_db_config
    sg2_db_config["delta_sync"] = delta_sync_false
    print("sg1_db_config", sg1_db_config)
    print("deltasync enabled for sg1 ", delta_sync_true)

    sg1.admin.put_db_config(sg_db, sg1_db_config)
    sg2.admin.put_db_config(sg_db, sg2_db_config)

    # 5. Verify that only on SGW node1 delta sync on , but off on sgw node2
    sg1_return_db = sg1.admin.get_db_config(sg_db)
    sg2_return_db = sg2.admin.get_db_config(sg_db)
    print("sg_return db enabled is ", sg1_return_db["delta_sync"]["enabled"])
    assert sg1_return_db["delta_sync"]["enabled"] is True, "delta sync not enabled on sgw node1"
    assert sg2_return_db["delta_sync"]["enabled"] is False, "delta sync is enabled on sgw node2"

    "/""status = sg1.restart(config=sg_conf1, cluster_config=cluster_conf)
    assert status == 0, "Sync_gateway1  did not start"
    status = sg2.restart(config=sg_conf2, cluster_config=cluster_conf)
    assert status == 0, "Sync_gateway2  did not start" ""/"

    # 6. Restart SGWs . database config with delta sync is off on sgw node1"
    sg_obj.restart_sync_gateways(cluster_config=cluster_conf)
    sg1_return_db = sg1.admin.get_db_config(sg_db)
    sg2_return_db = sg2.admin.get_db_config(sg_db)
    deep_dict_compare(sg1_return_db, sg2_return_db) """


@pytest.mark.syncgateway
@pytest.mark.parametrize("group_type", [
    ("default"),
    # ("named")
])
def test_named_and_default_group(params_from_base_test_setup, group_type):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    1. Have 2 SGW nodes with disable persistent config
    2. have default group id on one node and named group in another node
    3. Start sync gateway
    4. verify frist node has default groud id and 2nd node has named node
    5. Add database config via rest end point with revs_limit
    6. Verify revisions in the docs on each node
    """

    sg_db = 'db'
    sg_conf_name = "sync_gateway_cpc_custom_group"
    sg_obj = SyncGateway()
    # sg_conf_name2 = "xattrs/no_import"

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sync_gateway_version = params_from_base_test_setup['sync_gateway_version']
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']

    # 1. Have 2 SGW nodes with disable persistent config
    sg_conf1 = sync_gateway_config_path_for_mode(sg_conf_name, mode, cpc=True)
    print("sgconf1 name after getting cpc conf ", sg_conf1)
    # sg_conf2 = sync_gateway_config_path_for_mode(sg_conf_name2, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    cbs_host = host_for_url(cbs_url)
    cbs_cluster = Cluster(config=cluster_conf)

    # 2. have default group id on one node and named group in another node
    if group_type == "default":
        replaced_group = ""
    else:
        replaced_group_name = 'replaced_named_group'
        replaced_group = '"group_id": "{}",'.format(replaced_group_name)
    str = '{{ groupid }}'
    temp_sg_config, _ = copy_sgconf_to_temp(sg_conf1, mode)
    temp_sg_config = replace_string_on_sgw_config(temp_sg_config, str, replaced_group)
    bucket_list = ["data-bucket"]
    cpc_temp_sg_config = "{}/temp_sg_config_{}.json".format(SYNC_GATEWAY_CONFIGS_CPC, mode)
    copyfile(temp_sg_config, cpc_temp_sg_config)
    # cbs_cluster.reset(sg_config_path=temp_sg_config, bucket_list=bucket_list)
    print("temp sg config before calling reset ", temp_sg_config)
    cbs_cluster.reset(sg_config_path=temp_sg_config, use_config=True, bucket_list=bucket_list)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]
    # sg_dbs = sg1.admin.get_dbs_from_config()
    # sg_db = sg_dbs[0]
    # sg_obj.redeploy_sync_gateway_config(cluster_config=cluster_conf, sg_conf=sg_conf1, url=sg1.ip,
    #                                            sync_gateway_version=sync_gateway_version, enable_import=True)
    
    # print("bucket list is in the test is ", bucket_list)
    # sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=temp_sg_config, bucket_list=bucket_list)
    sg1_config = sg1.admin.get_config()
    sg2_config = sg2.admin.get_config()
    assert sg1_config["bootstrap"]["group_id"] == sg2_config["bootstrap"]["group_id"], "group id assigned does not have same group id on sgws nodes belongs to same cluster"
    assert sg1_config["api"]["public_interface"] == sg2_config["api"]["public_interface"], "custom public port is not assigned"
    assert sg1_config["api"]["admin_interface"] == sg2_config["api"]["admin_interface"], "custom admin port is not assigned"

    replaced_group = '"group_id": "moved_group",'
    temp_sg_config, _ = copy_sgconf_to_temp(sg_conf1, mode)
    temp_sg_config = replace_string_on_sgw_config(temp_sg_config, str, replaced_group)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=temp_sg_config, bucket_list=bucket_list)
    # sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=temp_sg_config)
    sg2_config = sg2.admin.get_config()
    assert sg1_config["bootstrap"]["group_id"] == sg2_config["bootstrap"]["group_id"], "group id assigned does not have same group id on sgws nodes belongs to same cluster"
    assert replaced_group_name == sg1_config["bootstrap"]["group_id"], "named group id not created"


@pytest.mark.syncgateway
def test_roll_out_config_new_node(params_from_base_test_setup, setup_sgws):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    1. set up server with bucket1
    2. Set up 1 sgw node with bootstrap config with Group1
    3. Add static config and restart the sgw
    4. Add database config and create db1 , db2 via rest end point
    5. Add new node to the cluster using the same group, Group1
    with bootstrap and static config
    6. Add one more new database config on sgw1
    7. Verify _config end point on sgw2 and verify all configs of sgw1 are inherited to sgw2 including database config added at step 6
    """

    sg_db1 = 'db1'
    sg_db2 = 'db2'
    sg_conf_name = setup_sgws["sg_conf_name"]
    sg_obj = SyncGateway()

    cluster_conf = setup_sgws['cluster_conf']
    mode = params_from_base_test_setup['mode']
    sg_platform = params_from_base_test_setup['sg_platform']

    # 1. set up server with bucket1
    # 2. Set up 1 sgw node with bootstrap config with Group1
    # temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
    # persist_cluster_config_environment_prop(temp_cluster_config, 'disable_persistent_config', False)
    sg_conf1 = setup_sgws["sg_conf1"]
    sg_cpc_confname = "sync_gateway_cpc_custom_group"
    replaced_group = '"group_id": "SGW_Group_1",'
    str = '{{ groupid }}'
    sg_cpc_conf1 = sync_gateway_config_path_for_mode(sg_cpc_confname, mode, cpc=True)
    # cluster_conf = temp_cluster_config
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    sg_3_url = cluster_topology["sync_gateways"][2]["public"]
    cbs_cluster = setup_sgws["cbs_cluster"]
    sg1 = setup_sgws["sg1"]
    sg2 = setup_sgws["sg2"]

    # 3. Add static config and restart the sgw
    cbs_cluster.reset(sg_config_path=sg_conf1, sgdb_creation=False)
    temp_sg_config, _ = copy_sgconf_to_temp(sg_cpc_conf1, mode)
    temp_sg_config = replace_string_on_sgw_config(temp_sg_config, str, replaced_group)
    sg1.stop()
    sg2.stop()
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_one_url, config=temp_sg_config, use_config=True)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=temp_sg_config, use_config=True)


    sg2_config = sg2.admin.get_config()
    # 4. Add database config and create db1 , db2 via rest end point
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    # sg2.admin.create_db(sg_db1, dbconfig)
    # sg2.admin.put_db_config(sg_db1, dbconfig)

    # 5. Add new node to the cluster using the same group, Group1 with bootstrap and static config
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_3_url, config=temp_sg_config, use_config=True)

    sg1_db_config = sg2.admin.get_db_config(sg_db1)
    sg2_db_config = sg1.admin.get_db_config(sg_db1)
    assert sg1_db_config[""][""] == sg2_db_config[""][""], "dbconfig database did not match"


@pytest.mark.syncgateway
def test_db_config_in_two_groups(params_from_base_test_setup):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    1. set up 3 sgw nodes
    2. Add a bootstrap config on first 2 nodes to Group - Group1
    3. Add a bootstrap config on 3rd node to Group - Group2
    4. Add database level config via rest api to Group1 to connect to bucket 1 
    5. Add database leve config via rest api to Group2 to connect to bucket 2 
    6. Verify that first 2 nodes of SGW of Group1 connected to bucket 1 and 3rd node of SGW of Group 2 connect to bucket2.
    Verify through _config rest end point
    7. write docs on each sg_db1 , sg_db2, on one node. verify doc appears on other SGW node2, but not on SGW node 3.
    8. Add revs limit 20 on Group1 and revs_limit 25 on Group2
    9. update docs on Group1 and Group2 for 50 times
    10. Verify nodes on Group1 has revision history of 20 and Group2 has 25

    """

    sg_db = 'db'
    sg_conf_name = "sync_gateway_default"
    sg_obj = SyncGateway()
    # sg_conf_name2 = "xattrs/no_import"

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sg_platform = params_from_base_test_setup['sg_platform']

    # 1. set up 3 sgw nodes 
    sg_config = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    # sg_conf2 = sync_gateway_config_path_for_mode(sg_conf_name2, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    sg_3_url = cluster_topology["sync_gateways"][2]["public"]
    sg_username = "autotest"
    sg_password = "password"
    sg_channels = ["cpc_testing"]
    sg_username2 = "autotest2"
    sg_channels2 = ["cpc_testing2"]
    bucket_list = ["data-bucket"]
    # TODO sg_three_url = cluster_topology["sync_gateways"][2]["public"]
    cbs_host = host_for_url(cbs_url)
    cbs_cluster = Cluster(config=cluster_conf)


    # 2. Add a bootstrap config on first 2 nodes to Group - Group1
    cbs_cluster.reset(sg_config_path=sg_config, sgdb_creation=False)
    time.sleep(15)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]

    # 3. Add a bootstrap config on 3rd node to Group - Group2
    sg_cpc_confname = "sync_gateway_cpc_custom_group"
    replaced_group1 = '"group_id": "SGW_Group_1",'
    replaced_group2 = '"group_id": "SGW_Group_2",'
    str = '{{ groupid }}'
    sg_cpc_conf1 = sync_gateway_config_path_for_mode(sg_cpc_confname, mode, cpc=True)
    temp_sg_config, _ = copy_sgconf_to_temp(sg_cpc_conf1, mode)
    r_temp_sg_config = replace_string_on_sgw_config(temp_sg_config, str, replaced_group1)
    r_temp_sg_config2 = replace_string_on_sgw_config(temp_sg_config, str, replaced_group2)
    cpc_temp_sg_config = "{}/temp_sg_config_{}.json".format(SYNC_GATEWAY_CONFIGS_CPC, mode)
    copyfile(r_temp_sg_config, cpc_temp_sg_config)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_one_url, config=r_temp_sg_config, bucket_list=bucket_list, use_config=True)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_3_url, config=r_temp_sg_config, bucket_list=bucket_list, use_config=True)
    copyfile(r_temp_sg_config2, cpc_temp_sg_config)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=r_temp_sg_config2, bucket_list=bucket_list, use_config=True)
    time.sleep(15)

    #4. Add a database config on first 2 nodes with revs_limit 20 to Group - Group1
    revs_limit = 20
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    print("db config is ", dbconfig)
    sg1.admin.create_db(sg_db, dbconfig)
    
    # 5. Add database leve config via rest api to Group2 to connect to bucket 2
    revs_limit = 25
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig2 = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    print("db config2 is ", dbconfig2)
    sg2.admin.create_db(sg_db, dbconfig2)

    # 6. Verify that first 2 nodes of SGW of Group1 connected to bucket 1 and 3rd node of SGW of Group 2 connect to bucket2.
    #   Verify through _config rest end point
    sg_client.create_user(sg1.admin.admin_url, sg_db, sg_username, sg_password, channels=sg_channels)
    auto_user = sg_client.create_session(url=sg1.admin.admin_url, db=sg_db, name=sg_username)
    # 7. write docs on each sg_db1 , sg_db2, on one node. verify doc appears on other SGW node2, but not on SGW node 3.
    sg_docs = document.create_docs('sgw1-cpc', number=2, channels=sg_channels)
    sg_client.add_bulk_docs(url=sg1.url, db=sg_db, docs=sg_docs, auth=auto_user)
    sg_docs = sg_client.get_all_docs(url=sg1.url, db=sg_db, auth=auto_user)["rows"]

    sg_client.create_user(sg2.admin.admin_url, sg_db, sg_username2, sg_password, channels=sg_channels2)
    auto_user2 = sg_client.create_session(url=sg2.admin.admin_url, db=sg_db, name=sg_username2)
    sg_docs = document.create_docs('sgw2-cpc', number=2, channels=sg_channels2)
    sg_client.add_bulk_docs(url=sg2.url, db=sg_db, docs=sg_docs, auth=auto_user2)
    sg_docs = sg_client.get_all_docs(url=sg1.url, db=sg_db, auth=auto_user2)["rows"]

    sg_client.update_docs(url=sg1.url, db=sg_db, docs=sg_docs, number_updates=50, auth=auto_user)
    sg_client.update_docs(url=sg2.url, db=sg_db, docs=sg_docs, number_updates=50, auth=auto_user2)
    sg1_return_db = sg1.admin.get_db_config(sg_db)
    sg2_return_db = sg2.admin.get_db_config(sg_db)

    assert sg1_return_db["revs_limit"] == 20, "revs limit is not assigned value to sg1"
    assert sg2_return_db["revs_limit"] == 25, "revs limit is not assigned value to sg3"


@pytest.mark.syncgateway
def test_union_of_dbconfigs_replace(params_from_base_test_setup):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    1. set up 3 sgw nodes
    2. Add a bootstrap config and have all 3 nodes belong to same group
    3. Add database config on node1 with sg_db1
    4. Add database config on node with sg_db2
    5. Add database config on node with sg_db3
    6. Verify db config end point on one of the node and verify it shows all 3 db configs
    7. Create doc, doc1 on node on sg_db2.
    8. Verify all 3 nodes can access the doc1 on sg_db2
    """

    sg_db1 = 'sg_db1'
    sg_db2 = 'sg_db2'
    sg_db3 = 'sg_db3'
    sg_conf_name = "sync_gateway_default_bootstrap"
    sg_obj = SyncGateway()

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sg_platform = params_from_base_test_setup['sg_platform']

    # 1. set up 3 sgw nodes 
    sg_config = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    sg_db2_username = "autotest"
    sg_password = "password"
    sg_channels = ["cpc_testing"]
    sg_username2 = "autotest2"
    sg_channels2 = ["cpc_testing2"]
    cbs_cluster = Cluster(config=cluster_conf)
    cbs_cluster.reset(sg_config_path=sg_config)
    time.sleep(15)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]

    # 3. Add database config on node1 with sg_db1
    revs_limit = 20
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db1, dbconfig)

    # 4. Add database config on node with sg_db2
    revs_limit = 25
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db2, dbconfig)

    # 5. Add database config on node with sg_db3
    revs_limit = 30
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db3, dbconfig)

    # 6. Verify db config end point on one of the node and verify it shows all 3 db configs
    sg1_db1_config = sg1.admin.get_db_config(sg_db1) 
    
    # 7. Create doc, doc1 on node on sg_db2.
    sg_client.create_user(sg1.admin.admin_url, sg_db2, sg_db2_username, sg_password, channels=sg_channels)
    auto_user = sg_client.create_session(url=sg1.admin.admin_url, db=sg_db2, name=sg_db2_username)
    sg_docs = document.create_docs('cpc-union', number=2, channels=sg_channels)
    sg_client.add_bulk_docs(url=sg1.url, db=sg_db2, docs=sg_docs, auth=auto_user)
    
    
    # 8. Verify all 3 nodes can access the doc1 on sg_db2
    sg_docs = sg_client.get_all_docs(url=sg1.url, db=sg_db2, auth=auto_user)["rows"]
    assert len(sg_docs) == 2, "sg1 node could not access sg_db2 docs"

    sg_docs = sg_client.get_all_docs(url=sg2.url, db=sg_db2, auth=auto_user)["rows"]
    assert len(sg_docs) == 2, "sg2 node could not access sg_db2 docs"

    sg_docs = sg_client.get_all_docs(url=sg3.url, db=sg_db2, auth=auto_user)["rows"]
    assert len(sg_docs) == 2, "sg3 node could not access sg_db2 docs"


@pytest.mark.syncgateway
def test_union_of_dbconfigs(params_from_base_test_setup):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    1. Have bootstrap config on sgw config which has server, username, password of the bucket
    2. Start SGW
    3. Add database on sgw via rest end point with invalid credentials of the bucket on the response from rest apoi
 
    """

    sg_db1 = 'sg_db1'
    sg_db2 = 'sg_db2'
    sg_db3 = 'sg_db3'
    sg_conf_name = "sync_gateway_default_bootstrap"
    sg_obj = SyncGateway()

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sg_platform = params_from_base_test_setup['sg_platform']

    # 1. set up 3 sgw nodes 
    # TODO: remove below 3 lines after persistent config is default to false
    # temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
    # persist_cluster_config_environment_prop(temp_cluster_config, 'disable_persistent_config', False)
    # cluster_conf = temp_cluster_config
    sg_config = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    sg_username = "autotest"
    sg_password = "password"
    sg_channels = ["cpc_testing"]
    sg_username2 = "autotest2"
    sg_channels2 = ["cpc_testing2"]
    cbs_cluster = Cluster(config=cluster_conf)
    cbs_cluster.reset(sg_config_path=sg_config)
    time.sleep(15)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]

    # 3. Add database config on node1 with sg_db1
    revs_limit = 20
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db1, dbconfig)

    # 4. Add database config on node with sg_db2
    revs_limit = 25
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db2, dbconfig)

    # 5. Add database config on node with sg_db3
    revs_limit = 30
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    sg1.admin.create_db_with_rest(sg_db3, dbconfig)

    # 6. Verify db config end point on one of the node and verify it shows all 3 db configs
    sg1_db1_config = sg1.admin.get_db_config(sg_db1)


@pytest.mark.syncgateway
def test_db_config_with_guest_user(params_from_base_test_setup):
    """
    @summary :
    Test cases link on google drive : https://docs.google.com/spreadsheets/d/19kJQ4_g6RroaoG2YYe0X11d9pU0xam-lb-n23aPLhO4/edit#gid=0
    "1. Set up SGw with bootstrap config with server url
    2. Create database on sgw via rest end point connected to server bucket 1
    3. Enable guest user in the rest end point  and have channels access to ""channel1"" and ""channel2""
    4. Verify db _config end point to check the data base config
    5. Create some docs  with ""channel1"" and some docs with ""channel2""
    6. Create some docs with ""channel3""
    7. Verify guest user can access docs created with only ""channel1"" and ""channel2"

    """

    sg_db = 'db'
    sg_conf_name = "sync_gateway_default_bootstrap"
    sg_obj = SyncGateway()
    # sg_conf_name2 = "xattrs/no_import"

    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sg_platform = params_from_base_test_setup['sg_platform']

    # 1. set up 3 sgw nodes 
    temp_cluster_config = copy_to_temp_conf(cluster_conf, mode)
    persist_cluster_config_environment_prop(temp_cluster_config, 'disable_persistent_config', False)
    cluster_conf = temp_cluster_config
    sg_config = sync_gateway_config_path_for_mode(sg_conf_name, mode, cpc=True)
    # sg_conf2 = sync_gateway_config_path_for_mode(sg_conf_name2, mode)

    sg_client = MobileRestClient()
    cluster_utils = ClusterKeywords(cluster_conf)
    cluster_topology = cluster_utils.get_cluster_topology(cluster_conf)
    cbs_url = cluster_topology['couchbase_servers'][0]
    sg_one_url = cluster_topology["sync_gateways"][0]["public"]
    sg_two_url = cluster_topology["sync_gateways"][1]["public"]
    sg_username = "autotest"
    sg_password = "password"
    sg_channels = ["cpc_testing"]
    sg_username2 = "autotest2"
    sg_channels2 = ["cpc_testing2"]
    # TODO sg_three_url = cluster_topology["sync_gateways"][2]["public"]
    cbs_host = host_for_url(cbs_url)
    cbs_cluster = Cluster(config=cluster_conf)


    # 2. Add a bootstrap config on first 2 nodes to Group - Group1
    cbs_cluster.reset(sg_config_path=sg_config)
    time.sleep(15)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]

    # 3. Add a bootstrap config on 3rd node to Group - Group2
    temp_sg_config, _, bucket_list = copy_sgconf_to_temp(sg_config, mode)
    replaced_group = '"group_id": "persistent_group_two",'
    str = '"group_id": "persistent_group1",'
    temp_sg_config = replace_string_on_sgw_config(temp_sg_config, str, replaced_group)
    sg_obj.start_sync_gateways(cluster_config=cluster_conf, url=sg_two_url, config=temp_sg_config, bucket_list=bucket_list)
    time.sleep(15)

    #4. Add a database config on first 2 nodes with revs_limit 20 to Group - Group1
    revs_limit = 20
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    print("db config is ", dbconfig)
    sg1.admin.create_db_with_rest(sg_db, dbconfig)
    
    # 5. Add database leve config via rest api to Group2 to connect to bucket 2
    revs_limit = 25
    persist_cluster_config_environment_prop(cluster_conf, 'revs_limit', revs_limit, property_name_check=False)
    db_config_file = "sync_gateway_default_db"
    dbconfig2 = construct_dbconfig_json(db_config_file, cluster_conf, sg_platform, sg_conf_name)
    print("db config2 is ", dbconfig2)
    sg2.admin.create_db_with_rest(sg_db, dbconfig2)

    # 6. Verify that first 2 nodes of SGW of Group1 connected to bucket 1 and 3rd node of SGW of Group 2 connect to bucket2.
    #   Verify through _config rest end point
    sg_client.create_user(sg1.admin.admin_url, sg_db, sg_username, sg_password, channels=sg_channels)
    auto_user = sg_client.create_session(url=sg1.admin.admin_url, db=sg_db, name=sg_username)
    # 7. write docs on each sg_db1 , sg_db2, on one node. verify doc appears on other SGW node2, but not on SGW node 3.
    sg_docs = document.create_docs('sgw1-cpc', number=2, channels=sg_channels)
    sg_client.add_bulk_docs(url=sg1.url, db=sg_db, docs=sg_docs, auth=auto_user)
    sg_docs = sg_client.get_all_docs(url=sg1.url, db=sg_db, auth=auto_user)["rows"]

    sg_client.create_user(sg2.admin.admin_url, sg_db, sg_username2, sg_password, channels=sg_channels2)
    auto_user2 = sg_client.create_session(url=sg2.admin.admin_url, db=sg_db, name=sg_username2)
    sg_docs = document.create_docs('sgw2-cpc', number=2, channels=sg_channels2)
    sg_client.add_bulk_docs(url=sg2.url, db=sg_db, docs=sg_docs, auth=auto_user2)
    sg_docs = sg_client.get_all_docs(url=sg1.url, db=sg_db, auth=auto_user2)["rows"]

    sg_client.update_docs(url=sg1.url, db=sg_db, docs=sg_docs, number_updates=50, auth=auto_user)
    sg_client.update_docs(url=sg2.url, db=sg_db, docs=sg_docs, number_updates=50, auth=auto_user2)
    sg1_return_db = sg1.admin.get_db_config(sg_db)
    sg2_return_db = sg2.admin.get_db_config(sg_db)

    assert sg1_return_db["revs_limit"] == 20, "revs limit is not assigned value to sg1"
    assert sg2_return_db["revs_limit"] == 25, "revs limit is not assigned value to sg3"


@pytest.fixture(scope="function")
def setup_sgws(params_from_base_test_setup):
    cluster_conf = params_from_base_test_setup['cluster_config']
    mode = params_from_base_test_setup['mode']
    sg_conf_name = "sync_gateway_default"
    sg_conf1 = sync_gateway_config_path_for_mode(sg_conf_name, mode)
    cbs_cluster = Cluster(config=cluster_conf)
    cbs_cluster.reset(sg_config_path=sg_conf1)
    sg1 = cbs_cluster.sync_gateways[0]
    sg2 = cbs_cluster.sync_gateways[1]
    sg1.stop()
    yield{
        "cluster_conf": cluster_conf,
        "mode": mode,
        "sg_conf1": sg_conf1,
        "cbs_cluster": cbs_cluster,
        "sg1": sg1,
        "sg2": sg2,
        "sg_conf_name": sg_conf_name
    }
    sg1.start(sg_conf1)