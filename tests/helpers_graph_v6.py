"""v6-only started-event bindings for tests."""

from __future__ import annotations

from typing import TypedDict

from assurance_agent.workflow.graph.runtime_commit_safety import (
    commit_safety_semantics_digest,
    commit_safety_semantics_object_digest,
)
from assurance_agent.workflow.graph.topology_semantics import (
    topology_safety_semantics_digest,
    topology_safety_semantics_object_digest,
)
from assurance_agent.workflow.orchestration.gate_semantics import (
    gate_semantics_digest,
    gate_semantics_object_digest,
)


class V6SemanticBindings(TypedDict):
    gate_semantics_object_id: str
    gate_semantics_digest: str
    topology_safety_semantics_object_id: str
    topology_safety_semantics_digest: str
    commit_safety_semantics_object_id: str
    commit_safety_semantics_digest: str


class V6StartedBindings(V6SemanticBindings):
    event_schema_version: int
    ir_digest: str
    ingest_catalog_digest: str
    policy_digest: str
    policy_origin: str
    assurance_profile_digest: str


def v6_semantic_bindings() -> V6SemanticBindings:
    return {
        "gate_semantics_object_id": gate_semantics_object_digest(),
        "gate_semantics_digest": gate_semantics_digest(),
        "topology_safety_semantics_object_id": topology_safety_semantics_object_digest(),
        "topology_safety_semantics_digest": topology_safety_semantics_digest(),
        "commit_safety_semantics_object_id": commit_safety_semantics_object_digest(),
        "commit_safety_semantics_digest": commit_safety_semantics_digest(),
    }


def v6_started_bindings() -> V6StartedBindings:
    return {
        "event_schema_version": 6,
        "ir_digest": "gd-1",
        "ingest_catalog_digest": "cat-1",
        "policy_digest": "a" * 64,
        "policy_origin": "project",
        "assurance_profile_digest": "c" * 64,
        **v6_semantic_bindings(),
    }


def patch_started_event(event: dict[str, object]) -> None:
    """Fill required v6 started fields on a raw ledger event."""
    if event.get("type") == "graph_invocation_started":
        event.update(v6_started_bindings())
