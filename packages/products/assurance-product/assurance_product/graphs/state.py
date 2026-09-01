from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState


def replace_receipts(existing: object, incoming: object) -> list[dict[str, object]]:
    if incoming is None:
        return (
            [dict(item) for item in existing]
            if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes))
            else []
        )
    if not isinstance(incoming, Sequence) or isinstance(incoming, (str, bytes)):
        raise TypeError("receipts must be a list")
    receipts: list[dict[str, object]] = []
    for item in incoming:
        if not isinstance(item, Mapping):
            raise TypeError("receipt must be a mapping")
        receipts.append({str(key): value for key, value in item.items()})
    return receipts


class ProductStateDocument(FrozenModel):
    schema_version: str
    change_id: str
    requirement: str
    run_mode: str
    selected_test_families: list[str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, Any]]
    decision: str
    receipts: list[dict[str, str]]
    output: dict[str, Any]
    status: str
    feature_input: dict[str, Any]
    feature_output: dict[str, Any]
    rounds_budget: int
    rounds_used: int
    artifact_paths: list[str]
    evidence_refs: list[dict[str, str]]
    receipt_refs: list[dict[str, str]]


class ProductState(CheckpointBridgeState, total=False):
    schema_version: str
    change_id: str
    requirement: str
    run_mode: str
    selected_test_families: list[str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, object]]
    decision: str
    receipts: Annotated[list[dict[str, object]], replace_receipts]
    output: dict[str, object]
    status: str
    feature_input: dict[str, object]
    feature_output: dict[str, object]
    rounds_budget: int
    rounds_used: int
    artifact_paths: list[str]
    evidence_refs: list[dict[str, str]]
    receipt_refs: list[dict[str, str]]


__all__ = [
    "ProductState",
    "ProductStateDocument",
    "replace_receipts",
]
