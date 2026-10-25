"""
app/services/node_batch_service.py

NodeBatchService — runs a batch cordon / uncordon under the access policy.

The policy is judged per node first; refused nodes come back as ``denied`` in
``results`` and the rest go to NodeService through ``call_kube`` (off the event
loop, as every Kubernetes call must). A batch the policy refused entirely
never resolves the cluster at all: nothing would be sent to it.
"""

from __future__ import annotations

from typing import Literal

from app.domain.access_policy import PolicyAction
from app.domain.kubernetes_models import BatchNodeActionData
from app.domain.models import User
from app.repositories.cluster_repository import ClusterRepository
from app.services.kube_client import call_kube
from app.services.node_service import NodeService, batch_data, denied_result, unique_node_names
from app.services.policy_service import PolicyService

BatchAction = Literal["cordon", "uncordon"]

_POLICY_ACTION: dict[BatchAction, PolicyAction] = {
    "cordon": "node.cordon",
    "uncordon": "node.uncordon",
}


class NodeBatchService:

    def __init__(self, nodes: NodeService, policy: PolicyService) -> None:
        self._nodes = nodes
        self._policy = policy

    async def run(
        self,
        *,
        user: User,
        repo: ClusterRepository,
        cluster: str,
        action: BatchAction,
        node_names: list[str],
    ) -> BatchNodeActionData:
        names = unique_node_names(node_names)
        denied = await self._policy.denied_nodes(
            user, _POLICY_ACTION[action], cluster=cluster, nodes=names,
        )
        if names and all(n in denied for n in names):
            return batch_data(cluster, action, [denied_result(n, denied[n]) for n in names])

        # An empty batch (every name blank) still goes through call_kube, as
        # before the policy existed: an unknown cluster is then a 404.
        batch = self._nodes.cordon_many if action == "cordon" else self._nodes.uncordon_many
        return await call_kube(
            repo,
            cluster,
            lambda kube: batch(cluster=cluster, node_names=names, kube=kube, denied=denied),
        )
