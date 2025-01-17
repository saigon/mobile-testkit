import time

import pytest
from couchbase.exceptions import DocumentNotFoundException
from requests.exceptions import HTTPError

from keywords import document
from keywords.ClusterKeywords import ClusterKeywords
from keywords.MobileRestClient import MobileRestClient
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords.timeutils import Time
from keywords.utils import host_for_url, log_info
from libraries.testkit.cluster import Cluster
from utilities.cluster_config_utils import get_sg_version, get_cluster
from keywords.constants import RBAC_FULL_ADMIN


"""
Test suite for Sync Gateway's expiry feature.
  Functional details (from commit):
  Expiry support for SG documents
  When writing a document to Sync Gateway (via PUT doc or bulk_docs), users can set the '_exp' property in the body of the document.  When Sync Gateway writes the document, it will:
  - Set a Couchbase Server expiry on the document, based on the exp value (see below for format)
  - Strip the _exp property from the document body (to avoid compatibility issues around the leading underscore when that document is replicated elsewhere)
  - Set an expiry property - `exp` - in the document's sync metadata, to provide some visibility on the expiry value

  When Sync Gateway reads a document (via document GET, bulk_get), it will recreate the _exp property in the body as an ISO-8601 only if the query string includes show_exp=true .

  Supported formats for the incoming _exp value:
  - JSON number.  Will be treated as a Couchbase Server expiry value (ttl in second when less than 30 days, unix time when greater)
  - JSON string (numeric format) - same as JSON number
  - JSON string (as ISO-8601 date).  Converted to Couchbase server expiry value.
  - JSON null.  Sets expiry to zero (no expiry)

  Note that subsequent updates to the document will use the expiry value on the update.
  If no expiry value is set on a new revision of a document that was previously set to expire, the new version of the document will have no expiry set.

  Testing Notes
    1. Setting the _exp property triggers expiry by setting the Couchbase Server expiry for the document.  However, it does NOT remove the document from the in-memory revision cache,
    where Sync Gateway stores the 5000 most recently requested documents.  Scheduling removal from the rev cache isn't in scope for 1.3, and shouldn't be a significant issue for real-world
    scenarios (customers who need to manage bucket contents using expiry will typically not have expired docs in their rev cache due to throughput).  However, it has some testing implications:
      - Attempts to retrieve a document by doc ID after expiry (e.g. GET /db/doc) won't find the doc, as that operation will always attempt to load the latest copy of the doc from the bucket
      - Attempts to retrieve a document by doc ID AND rev ID after expiry (e.g. GET /db/doc?rev=1-abc) WILL find the doc, as that operation will first attempt to get the doc from the rev cache
    For this reason, the expiry tests below should use GET /db/doc (without rev) when testing for expiry
    2. The absolute time expiry tests require the tests to know the Couchbase Server clock (within a few seconds), and calculate a date in various formats a few seconds beyond that time.  If this
    presents any problems, we can review to see if there's any alternative.
    3. The tests all attempt to set an expiry 3 seconds in the future, then wait 5 seconds to attempt retrieval.  You can tune that to whatever you think the tolerance of the test framework can
    handle, such that:
      - For non-TTL expiry values, you can tune down the 3 seconds, as long as the date doesn't end up in the past by the time it's written to Couchbase Server      - You can tune down the wait for attempted get after expiry (from 2s) as long as you avoid race scenarios where the doc hasn't expired by the time you request it.
"""


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    pytest.param("sync_gateway_default_functional_tests", marks=[pytest.mark.sanity, pytest.mark.oscertify]),
    ("sync_gateway_default_functional_tests_no_port"),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_numeric_expiry_as_ttl(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/doc1 via SG with property "_exp":3
       PUT /db/doc2 via SG with property "_exp":10
    2. Wait five seconds
    3. Get /db/doc1.  Assert response is 404
       Get /db/doc2.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot using couchbases protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_numeric_expiry_as_ttl'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=3, channels=sg_user_channels)
    doc_exp_10_body = document.create_doc(doc_id="exp_10", expiry=10, channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_10 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_10_body, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire, but still be in the window to get doc_exp_10
    time.sleep(5)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    log_info("Response data", he.value)
    res_message = str(he.value)
    if xattrs_enabled:
        assert res_message.startswith("403 Client Error: Forbidden for url:")
    else:
        assert res_message.startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )

    # doc_exp_10 should be available still
    doc_exp_10_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_10["id"], auth=sg_user_session)
    assert doc_exp_10_result["_id"] == "exp_10"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    ("sync_gateway_default_functional_tests"),
    ("sync_gateway_default_functional_tests_no_port"),
    pytest.param("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210", marks=pytest.mark.oscertify)
])
def test_string_expiry_as_ttl(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/doc1 via SG with property "_exp":"3"
       PUT /db/doc2 via SG with property "_exp":"10"
    2. Wait five seconds
    3. Get /db/doc1.  Assert response is 404
       Get /db/doc2.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbases protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_string_expiry_as_ttl'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = "couchbase://{}".format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry="3", channels=sg_user_channels)
    doc_exp_10_body = document.create_doc(doc_id="exp_10", expiry="10", channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_10 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_10_body, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire, but still be in the window to get doc_exp_10
    time.sleep(5)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )

    # doc_exp_10 should be available still
    doc_exp_10_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_10["id"], auth=sg_user_session)
    assert doc_exp_10_result["_id"] == "exp_10"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    pytest.param("sync_gateway_default_functional_tests", marks=pytest.mark.oscertify),
    ("sync_gateway_default_functional_tests_no_port"),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_numeric_expiry_as_unix_date(params_from_base_test_setup, sg_conf_name):
    """
    1. Calculate (server time + 3 seconds) as unix time (i.e. Epoch time, e.g. 1466465122)
    2. PUT /db/doc1 via SG with property "_exp":[unix time]
       PUT /db/doc2 via SG with property "_exp":1767225600  (Jan 1 2026)
    3. Wait five seconds
    4. Get /db/doc1.  Assert response is 404
       Get /db/doc2.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_numeric_expiry_as_unix_date'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = "couchbase://{}".format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    time_util = Time()
    unix_time_3s_ahead = time_util.get_unix_timestamp(delta=3)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=unix_time_3s_ahead, channels=sg_user_channels)
    doc_exp_years_body = document.create_doc(doc_id="exp_years", expiry=1767225600, channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_years = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_years_body, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire
    time.sleep(10)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )

    # doc_exp_years should be available still
    doc_exp_years_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_years["id"], auth=sg_user_session)
    assert doc_exp_years_result["_id"] == "exp_years"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    ("sync_gateway_default_functional_tests"),
    ("sync_gateway_default_functional_tests_no_port"),
    pytest.param("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210", marks=pytest.mark.oscertify)
])
def test_string_expiry_as_unix_date(params_from_base_test_setup, sg_conf_name):
    """
    1. Calculate (server time + 3 seconds) as unix time (i.e. Epoch time, e.g. 1466465122)
    2. PUT /db/doc1 via SG with property "_exp":"[unix time]"
       PUT /db/doc2 via SG with property "_exp":"1767225600"  (Jan 1 2026) Note: the maximum epoch time supported by CBS is maxUint32, or Sun 07 Feb 2106, in case you want to move it out further than 2026.
    3. Wait five seconds
    4. Get /db/doc1.  Assert response is 404
       Get /db/doc2.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_string_expiry_as_unix_date'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = "couchbase://{}".format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    time_util = Time()
    unix_time_3s_ahead = time_util.get_unix_timestamp(delta=3)

    # Convert unix timestamp to string
    unix_time_3s_ahead_string = str(unix_time_3s_ahead)

    # Using string representation for unix time
    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=unix_time_3s_ahead_string, channels=sg_user_channels)
    doc_exp_years_body = document.create_doc(doc_id="exp_years", expiry="1767225600", channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_years = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_years_body, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire
    time.sleep(10)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )

    # doc_exp_years should be available still
    doc_exp_years_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_years["id"], auth=sg_user_session)
    assert doc_exp_years_result["_id"] == "exp_years"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    ("sync_gateway_default_functional_tests"),
    pytest.param("sync_gateway_default_functional_tests_no_port", marks=pytest.mark.oscertify),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_string_expiry_as_iso_8601_date(params_from_base_test_setup, sg_conf_name):
    """
    1. Calculate (server time + 3 seconds) as ISO-8601 date (e.g. 2016-01-01T00:00:00.000+00:00)
    2. PUT /db/doc1 via SG with property "_exp":"[date]"
       PUT /db/doc2 via SG with property "_exp":"2026-01-01T00:00:00.000+00:00"
    3. Wait five seconds
    4. Get /db/doc1.  Assert response is 404
       Get /db/doc2.  Assert response is 20
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_string_expiry_as_ISO_8601_Date'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    time_util = Time()
    iso_datetime = time_util.get_iso_datetime(delta=3)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=iso_datetime, channels=sg_user_channels)
    doc_exp_years_body = document.create_doc(doc_id="exp_years", expiry="2026-01-01T00:00:00.000+00:00", channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_years = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_years_body, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire
    time.sleep(10)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )

    # doc_exp_years should be available still
    doc_exp_years_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_years["id"], auth=sg_user_session)
    assert doc_exp_years_result["_id"] == "exp_years"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    pytest.param("sync_gateway_default_functional_tests", marks=pytest.mark.oscertify),
    ("sync_gateway_default_functional_tests_no_port"),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_removing_expiry(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/doc1 via SG with property "_exp":3
    2. Update /db/doc1 with a new revision with no expiry value
    3. After 10 updates, update /db/doc1 with a revision with no expiry
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_removing_expiry'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=3, channels=sg_user_channels)
    doc_exp_10_body = document.create_doc(doc_id="exp_10", expiry=10, channels=sg_user_channels)
    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_10 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_10_body, auth=sg_user_session)

    doc_exp_3_updated = client.update_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], number_updates=10, auth=sg_user_session, remove_expiry=True)
    doc_exp_3_updated_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3_updated["id"], auth=sg_user_session)

    # Sleep should allow an expiry to happen on doc_exp_3 if it had not been removed.
    # Expected behavior is that the doc_exp_3 will still be around due to the removal of the expiry
    time.sleep(5)

    # doc_exp_3 should no longer have an expiry and should not raise an exception
    doc_exp_3_updated_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3_updated["id"], auth=sg_user_session)
    assert doc_exp_3_updated_result["_id"] == "exp_3"

    # doc_exp_10 should be available still and should not raise an exception
    doc_exp_10_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_10["id"], auth=sg_user_session)
    assert doc_exp_10_result["_id"] == "exp_10"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    ("sync_gateway_default_functional_tests"),
    ("sync_gateway_default_functional_tests_no_port"),
    pytest.param("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210", marks=pytest.mark.oscertify)
])
def test_rolling_ttl_expires(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/doc1 via SG with property "_exp":3
    2. Update /db/doc1 10 times with a new revision (also with "_exp":3)
    3. Wait 5 seconds
    4. Get /db/doc1.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_rolling_ttl_expires'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=3, channels=sg_user_channels)
    doc_exp_10_body = document.create_doc(doc_id="exp_10", expiry=10, channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_10 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_10_body, auth=sg_user_session)

    client.update_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], number_updates=10, expiry=3, auth=sg_user_session)

    # Sleep should allow doc_exp_3 to expire, but still be in the window to get doc_exp_10
    time.sleep(5)

    # doc_exp_3 should be expired
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_3["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        expected_rev=12,
        auth=auth
    )

    # doc_exp_10 should be available still
    doc_exp_10_result = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_10["id"], auth=sg_user_session)
    assert doc_exp_10_result["_id"] == "exp_10"


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.parametrize("sg_conf_name", [
    pytest.param("sync_gateway_default_functional_tests", marks=pytest.mark.oscertify),
    ("sync_gateway_default_functional_tests_no_port"),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_rolling_ttl_remove_expirary(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/doc1 via SG with property "_exp":3
    2. Once per second for 10 seconds, update /db/doc1 with a new revision (also with "_exp":3)
    3. Update /db/doc1 with a revision with no expiry
    3. Get /db/doc1.  Assert response is 200
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_rolling_ttl_remove_expirary'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = 'couchbase://{}'.format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_body = document.create_doc(doc_id="exp_3", expiry=3, channels=sg_user_channels)
    doc_exp_10_body = document.create_doc(doc_id="exp_10", expiry=10, channels=sg_user_channels)

    doc_exp_3 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_3_body, auth=sg_user_session)
    doc_exp_10 = client.add_doc(url=sg_url, db=sg_db, doc=doc_exp_10_body, auth=sg_user_session)

    client.update_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], number_updates=10, expiry=3, delay=1, auth=sg_user_session)
    client.update_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], number_updates=1, auth=sg_user_session, remove_expiry=True)

    # If expiry was not removed in the last update, this would expire doc_exp_3
    time.sleep(5)

    # doc_exp_3 should still be around due to removal of expiry
    doc_exp_3 = client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_3["id"], auth=sg_user_session)
    assert doc_exp_3["_id"] == "exp_3"

    # doc_exp_10 should be expired due to the updates (10s) + sleep (5s)
    with pytest.raises(HTTPError) as he:
        client.get_doc(url=sg_url, db=sg_db, doc_id=doc_exp_10["id"], auth=sg_user_session)

    # In XATTR mode, the expiry results in a tombstone
    # In Doc Meta mode, the expiry results in a purge
    if xattrs_enabled:
        assert str(he.value).startswith("403 Client Error: Forbidden for url:")
    else:
        assert str(he.value).startswith("404 Client Error: Not Found for url:")

    verify_doc_deletion_on_server(
        doc_id=doc_exp_10["id"],
        sdk_client=sdk_client,
        sg_client=client,
        sg_admin_url=sg_url_admin,
        sg_db=sg_db,
        xattrs_enabled=xattrs_enabled,
        auth=auth
    )


@pytest.mark.syncgateway
@pytest.mark.ttl
@pytest.mark.session
@pytest.mark.bulkops
@pytest.mark.parametrize("sg_conf_name", [
    pytest.param("sync_gateway_default_functional_tests", marks=pytest.mark.oscertify),
    ("sync_gateway_default_functional_tests_no_port"),
    ("sync_gateway_default_functional_tests_couchbase_protocol_withport_11210")
])
def test_setting_expiry_in_bulk_docs(params_from_base_test_setup, sg_conf_name):
    """
    1. PUT /db/_bulk_docs with 10 documents.  Set the "_exp":3 on 5 of these documents
    2. Wait five seconds
    3. POST /db/_bulk_get for the 10 documents.  Validate that only the 5 non-expiring documents are returned
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    xattrs_enabled = params_from_base_test_setup['xattrs_enabled']
    ssl_enabled = params_from_base_test_setup["ssl_enabled"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    # Skip the test if ssl disabled as it cannot run without port using http protocol
    if ("sync_gateway_default_functional_tests_no_port" in sg_conf_name) and get_sg_version(cluster_config) < "1.5.0":
        pytest.skip('couchbase/couchbases ports do not support for versions below 1.5')
    if "sync_gateway_default_functional_tests_no_port" in sg_conf_name and not ssl_enabled:
        pytest.skip('ssl disabled so cannot run without port')

    # Skip the test if ssl enabled as it cannot run using couchbase protocol
    # TODO : https://github.com/couchbaselabs/sync-gateway-accel/issues/227
    # Remove DI condiiton once above bug is fixed
    if "sync_gateway_default_functional_tests_couchbase_protocol_withport_11210" in sg_conf_name and (ssl_enabled or mode.lower() == "di"):
        pytest.skip('ssl enabled so cannot run with couchbase protocol')

    cluster = Cluster(config=cluster_config)
    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster_helper = ClusterKeywords(cluster_config)
    topology = cluster_helper.get_cluster_topology(cluster_config)

    cluster_helper.reset_cluster(
        cluster_config=cluster_config,
        sync_gateway_config=sg_conf
    )

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]

    log_info("Running 'test_setting_expiry_in_bulk_docs'")
    log_info("cbs_url: {}".format(cbs_url))
    log_info("sg_url: {}".format(sg_url))
    log_info("sg_url_admin: {}".format(sg_url_admin))

    sg_db = "db"
    sg_user_name = "sg_user"
    sg_user_password = "p@ssw0rd"
    sg_user_channels = ["NBC", "ABC"]
    bucket_name = "data-bucket"
    cbs_ip = host_for_url(cbs_url)

    if ssl_enabled and cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify&ipv6=allow".format(cbs_ip)
    elif ssl_enabled and not cluster.ipv6:
        connection_url = "couchbases://{}?ssl=no_verify".format(cbs_ip)
    elif not ssl_enabled and cluster.ipv6:
        connection_url = "couchbase://{}?ipv6=allow".format(cbs_ip)
    else:
        connection_url = "couchbase://{}".format(cbs_ip)
    sdk_client = get_cluster(connection_url, bucket_name)
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None
    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    doc_exp_3_bodies = document.create_docs(doc_id_prefix="exp_3", number=5, expiry=3, channels=sg_user_channels)
    doc_exp_10_bodies = document.create_docs(doc_id_prefix="exp_10", number=5, expiry=10, channels=sg_user_channels)

    bulk_bodies = doc_exp_3_bodies + doc_exp_10_bodies

    bulk_docs = client.add_bulk_docs(url=sg_url, db=sg_db, docs=bulk_bodies, auth=sg_user_session)

    # Allow exp_3 docs to expire
    time.sleep(5)

    bulk_docs_ids = [doc["id"] for doc in bulk_docs]

    expected_ids = ["exp_10_0", "exp_10_1", "exp_10_2", "exp_10_3", "exp_10_4"]
    expected_missing_ids = ["exp_3_0", "exp_3_1", "exp_3_2", "exp_3_3", "exp_3_4"]

    bulk_get_docs, errors = client.get_bulk_docs(url=sg_url, db=sg_db, doc_ids=bulk_docs_ids, auth=sg_user_session, validate=False)
    assert len(bulk_get_docs) == len(expected_ids)
    assert len(errors) == len(expected_missing_ids)

    bulk_get_doc_ids = [doc["_id"] for doc in bulk_get_docs]
    error_ids = [doc["id"] for doc in errors]

    assert bulk_get_doc_ids == expected_ids
    assert error_ids == expected_missing_ids

    client.verify_doc_ids_found_in_response(response=bulk_get_docs, expected_doc_ids=expected_ids)
    client.verify_doc_ids_not_found_in_response(response=errors, expected_missing_doc_ids=expected_missing_ids)

    for expired_doc in error_ids:
        verify_doc_deletion_on_server(
            doc_id=expired_doc,
            sdk_client=sdk_client,
            sg_client=client,
            sg_admin_url=sg_url_admin,
            sg_db=sg_db,
            xattrs_enabled=xattrs_enabled,
            auth=auth
        )


# TODO:
# Validating retrieval of expiry value (Optional)
#    [Tags]  sanity  syncgateway  ttl
#    [Documentation]
#    ...  I think these scenarios are well-covered by unit tests, so functional tests are probably not strictly required
#    ...  unless the functional tests are trying to cover the full API parameter space.
#    ...  1. PUT /db/doc1 via SG with property "_exp":100
#    ...  2. GET /db/doc1.  Assert response doesn't include _exp property
#    ...  3. GET /db/doc1?show_exp=true.  Assert response includes _exp property, and it's a datetime approximately 100 seconds in the future
#    ...  4. POST /db/_bulk_docs with doc1 in the set of requested docs.  Assert response doesn't include _exp property
#    ...  5. POST /db/_bulk_docs?show_exp=true with doc1 in the set of requested docs.  Assert response includes _exp property, and it's a datetime approx 100s in the future.


# TODO
# Validating put with unix past timestamp (Optional)
#    [Tags]  sanity  syncgateway  ttl
#    [Documentation]

def verify_doc_deletion_on_server(doc_id, sdk_client, sg_client, sg_admin_url, sg_db, xattrs_enabled=False, expected_rev=2, auth=None):
    # If xattrs, check that the doc is a tombstone
    # by getting the rev and "_deleted" prop via _raw
    # If sync gateway is using document meta data
    # ensure doc has been purged from the server
    if xattrs_enabled:
        expired_raw_doc = sg_client.get_raw_doc(
            url=sg_admin_url,
            db=sg_db,
            doc_id=doc_id,
            auth=auth
        )
        assert expired_raw_doc["_sync"]["rev"].startswith("{}-".format(expected_rev))
        assert expired_raw_doc["_deleted"]
    else:
        with pytest.raises(DocumentNotFoundException) as nfe:
            sdk_client.get(doc_id)
        assert "DocumentNotFoundException" in str(nfe)
