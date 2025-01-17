import pytest

from requests.exceptions import HTTPError

from keywords.utils import log_info
from keywords.ClusterKeywords import ClusterKeywords
from keywords.MobileRestClient import MobileRestClient
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from libraries.testkit.cluster import Cluster
from keywords.constants import RBAC_FULL_ADMIN

from keywords import couchbaseserver
from keywords import document
from keywords import attachment


@pytest.mark.syncgateway
@pytest.mark.attachments
@pytest.mark.session
@pytest.mark.sync
@pytest.mark.oscertify
@pytest.mark.parametrize("sg_conf_name", [
    "reject_all"
])
def test_attachments_on_docs_rejected_by_sync_function(params_from_base_test_setup, sg_conf_name):
    """
    1. Start sync_gateway with sync function that rejects all writes:
    function(doc, oldDoc) {
      throw({forbidden:"No writes!"});
    }
    2. Create a doc with attachment
    3. Use CBS sdk to see if attachment doc exists.  Doc ID will look like _sync:att:sha1-Kq5sNclPz7QV2+lfQIuc6R7oRu0= (where the suffix is the digest)
    4. Assert att doc does not exist
    """

    cluster_config = params_from_base_test_setup["cluster_config"]
    mode = params_from_base_test_setup["mode"]
    sync_gateway_version = params_from_base_test_setup["sync_gateway_version"]
    need_sgw_admin_auth = params_from_base_test_setup["need_sgw_admin_auth"]

    sg_conf = sync_gateway_config_path_for_mode(sg_conf_name, mode)

    cluster = Cluster(config=cluster_config)
    cluster_helper = ClusterKeywords(cluster_config)
    cluster_helper.reset_cluster(cluster_config, sg_conf)

    topology = cluster_helper.get_cluster_topology(cluster_config)

    cbs_url = topology["couchbase_servers"][0]
    sg_url = topology["sync_gateways"][0]["public"]
    sg_url_admin = topology["sync_gateways"][0]["admin"]
    sg_db = "db"
    bucket = "data-bucket"
    auth = need_sgw_admin_auth and (RBAC_FULL_ADMIN['user'], RBAC_FULL_ADMIN['pwd']) or None

    log_info("Running 'test_attachments_on_docs_rejected_by_sync_function'")
    log_info("Using cbs_url: {}".format(cbs_url))
    log_info("Using sg_url: {}".format(sg_url))
    log_info("Using sg_url_admin: {}".format(sg_url_admin))
    log_info("Using sg_db: {}".format(sg_db))
    log_info("Using bucket: {}".format(bucket))

    sg_user_name = "sg_user"
    sg_user_password = "sg_user_password"

    sg_user_channels = ["NBC"]

    client = MobileRestClient()

    client.create_user(url=sg_url_admin, db=sg_db, name=sg_user_name, password=sg_user_password, channels=sg_user_channels, auth=auth)
    sg_user_session = client.create_session(url=sg_url_admin, db=sg_db, name=sg_user_name, auth=auth)

    # Verify docs are getting rejected
    with pytest.raises(HTTPError) as he:
        client.add_docs(url=sg_url, db=sg_db, number=100, id_prefix=sg_db, channels=sg_user_channels, auth=sg_user_session)
    assert str(he.value).startswith("403 Client Error: Forbidden for url:")

    # Create doc with attachment and push to sync_gateway
    atts = attachment.load_from_data_dir(["sample_text.txt"])
    doc_with_att = document.create_doc(doc_id="att_doc", content={"sample_key": "sample_val"}, attachments=atts, channels=sg_user_channels)

    # Verify all docs are getting rejected
    with pytest.raises(HTTPError) as he:
        client.add_doc(url=sg_url, db=sg_db, doc=doc_with_att, auth=sg_user_session)
    assert str(he.value).startswith("403 Client Error: Forbidden for url:")

    cb_server = couchbaseserver.CouchbaseServer(cbs_url)
    if sync_gateway_version >= "3.0.0":
        server_att_docs = cb_server.get_server_docs_with_prefix(bucket=bucket, prefix="_sync:att2:", ipv6=cluster.ipv6)
    else:
        server_att_docs = cb_server.get_server_docs_with_prefix(bucket=bucket, prefix="_sync:att:", ipv6=cluster.ipv6)
    num_att_docs = len(server_att_docs)
    assert num_att_docs == 0

    if sync_gateway_version >= "2.5.0":
        expvars = client.get_expvars(sg_url_admin, auth=auth)
        assert expvars["syncgateway"]["per_db"][sg_db]["security"]["num_docs_rejected"] == 2, "num_docs_rejected is not incremented"
