"""v6-only started-event bindings for tests."""

from __future__ import annotations

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


def v6_semantic_bindings() -> dict[str, str]:
    return {
        "gate_semantics_object_id": gate_semantics_object_digest(),
        "gate_semantics_digest": gate_semantics_digest(),
        "topology_safety_semantics_object_id": topology_safety_semantics_object_digest(),
        "topology_safety_semantics_digest": topology_safety_semantics_digest(),
        "commit_safety_semantics_object_id": commit_safety_semantics_object_digest(),
        "commit_safety_semantics_digest": commit_safety_semantics_digest(),
    }


def v6_started_bindings() -> dict[str, object]:
    return {
        "event_schema_version": 6,
        "ir_digest": "gd-1",
        "ingest_catalog_digest": "cat-1",
        "policy_digest": "a" * 64,
        "policy_origin": "project",
        "assurance_profile_digest": "c" * 64,
        **v6_semantic_bindings(),
    }
