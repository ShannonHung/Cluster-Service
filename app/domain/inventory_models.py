"""
app/domain/inventory_models.py

Pydantic models mirroring deploy-service's inventory API response shapes.

Mirrors deploy-service's app/repositories/inventory_repository.py so that
cluster-service stays fully independent — it never imports deploy-service's
source tree. Keep these in sync with that module.
"""

from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, field_validator


class ClusterRef(BaseModel):
    id: str
    name: str
    # The inventory API may omit context or return null; tolerate both.
    context: str = ""

    @field_validator("context", mode="before")
    @classmethod
    def _coerce_null_context(cls, v: object) -> object:
        return v if v is not None else ""


class NodeInfo(BaseModel):
    id: str
    name: str
    labels: Dict[str, str] = Field(default_factory=dict)

    @field_validator("labels", mode="before")
    @classmethod
    def _coerce_null_labels(cls, v: object) -> object:
        return v if v is not None else {}


class ClusterNodeInfo(BaseModel):
    node_type: str
    node: NodeInfo
    cluster: ClusterRef


class BastionMapping(BaseModel):
    patterns: List[str]
    runner: str
    bastion: str
    bastion_ip: str


class NodeBastionResolution(BaseModel):
    node_type: str
    node: NodeInfo
    cluster: ClusterRef
    bastion_type: str
    bastion_type_source: Literal["config", "query_param"]
    matched_mapping: BastionMapping
    matched_pattern: str


class ClusterBastionResolution(BaseModel):
    cluster_name: str
    has_slash: bool
    bastion_type: str
    matched_mapping: BastionMapping
    matched_pattern: str
