from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)


def test_cluster_ref_context_defaults_empty():
    ref = ClusterRef(id="1", name="type1-cluster-c1")
    assert ref.context == ""


def test_cluster_ref_context_coerces_null_to_empty():
    ref = ClusterRef.model_validate({"id": "1", "name": "c1", "context": None})
    assert ref.context == ""


def test_cluster_ref_context_passthrough():
    ref = ClusterRef(id="1", name="c1", context="c1")
    assert ref.context == "c1"


def test_node_info_null_labels_coerced():
    n = NodeInfo.model_validate({"id": "1", "name": "node1", "labels": None})
    assert n.labels == {}


def test_cluster_node_info_full():
    info = ClusterNodeInfo.model_validate(
        {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {"dc": "1"}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
        }
    )
    assert info.cluster.context == "c1"
    assert info.node.labels["dc"] == "1"


def test_bastion_resolution_models_construct():
    mapping = BastionMapping(
        patterns=["type1-.*"], runner="r1", bastion="bastion-a", bastion_ip="10.0.0.5"
    )
    node_res = NodeBastionResolution(
        node_type="baremetal",
        node=NodeInfo(id="1", name="node1"),
        cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
        bastion_type="type1",
        bastion_type_source="config",
        matched_mapping=mapping,
        matched_pattern="type1-.*",
    )
    assert node_res.bastion_type_source == "config"
    cluster_res = ClusterBastionResolution(
        cluster_name="type1-cluster-c1",
        has_slash=False,
        bastion_type="type1",
        matched_mapping=mapping,
        matched_pattern="type1-.*",
    )
    assert cluster_res.has_slash is False
