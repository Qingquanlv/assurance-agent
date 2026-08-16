"""invocation 事件记录 policy digest——回答「这次放行依据哪版策略」。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.verification.profile_manifest import assurance_profile_digest
from assurance_agent.workflow.core.graph_events import GraphInvocationStartedEvent
from tests.helpers_graph_v6 import v6_semantic_bindings


def _v6_started(**overrides: object) -> GraphInvocationStartedEvent:
    base: dict[str, object] = dict(
        type="graph_invocation_started",
        invocation_id="inv-1",
        entrypoint="full",
        graph_id="workflow",
        graph_digest="dg",
        event_schema_version=6,
        ir_digest="dg",
        ingest_catalog_digest="cat",
        contract_digests={},
        policy_digest="a" * 64,
        policy_origin="project",
        assurance_profile_digest=assurance_profile_digest(),
        params={},
        params_sha256="ps",
        root_tree_id="tree",
        max_parallel_tasks=1,
        checkpoint_ns="ns",
        structural_path="workflow",
        **v6_semantic_bindings(),
    )
    base.update(overrides)
    return GraphInvocationStartedEvent(**base)  # type: ignore[arg-type]


def test_event_carries_policy_digest(tmp_path: Path) -> None:
    event = _v6_started(policy_digest=policy_digest(load_policy(tmp_path)))
    assert len(event.policy_digest) == 64
    assert event.event_schema_version == 6


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 7])
def test_started_rejects_event_schema_version_other_than_6(version: int) -> None:
    with pytest.raises(ValidationError, match="event_schema_version 6"):
        _v6_started(event_schema_version=version)


@pytest.mark.parametrize(
    "field",
    [
        "policy_origin",
        "assurance_profile_digest",
        "gate_semantics_object_id",
        "topology_safety_semantics_object_id",
        "topology_safety_semantics_digest",
        "commit_safety_semantics_object_id",
        "commit_safety_semantics_digest",
    ],
)
def test_v6_started_rejects_empty_binding_field(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _v6_started(**{field: ""})


def test_v6_started_accepts_complete_six_field_binding() -> None:
    event = _v6_started()
    assert event.event_schema_version == 6
    assert event.gate_semantics_object_id == v6_semantic_bindings()["gate_semantics_object_id"]
    assert event.commit_safety_semantics_digest == v6_semantic_bindings()["commit_safety_semantics_digest"]
