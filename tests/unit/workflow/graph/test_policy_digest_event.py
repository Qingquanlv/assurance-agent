"""invocation 事件记录 policy digest——回答「这次放行依据哪版策略」。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.verification.profile_manifest import assurance_profile_digest
from assurance_agent.workflow.core.graph_events import GraphInvocationStartedEvent
from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest


def _v4_started(**overrides: object) -> GraphInvocationStartedEvent:
    base: dict[str, object] = dict(
        type="graph_invocation_started",
        invocation_id="inv-1",
        entrypoint="full",
        graph_id="workflow",
        graph_digest="dg",
        event_schema_version=4,
        ir_digest="dg",
        ingest_catalog_digest="cat",
        contract_digests={},
        policy_digest="a" * 64,
        policy_origin="project",
        gate_semantics_digest=gate_semantics_digest(),
        assurance_profile_digest=assurance_profile_digest(),
        params={},
        params_sha256="ps",
        root_tree_id="tree",
        max_parallel_tasks=1,
        checkpoint_ns="ns",
        structural_path="workflow",
    )
    base.update(overrides)
    return GraphInvocationStartedEvent(**base)  # type: ignore[arg-type]


def test_event_carries_policy_digest(tmp_path: Path) -> None:
    event = GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id="inv-1",
        entrypoint="full",
        graph_id="workflow",
        graph_digest="dg",
        contract_digests={},
        policy_digest=policy_digest(load_policy(tmp_path)),
        params={},
        params_sha256="ps",
        root_tree_id="tree",
        max_parallel_tasks=1,
        checkpoint_ns="ns",
        structural_path="workflow",
    )
    assert len(event.policy_digest) == 64


def test_policy_digest_defaults_to_empty_for_legacy_events() -> None:
    event = GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id="inv-1",
        entrypoint="full",
        graph_id="workflow",
        graph_digest="dg",
        contract_digests={},
        params={},
        params_sha256="ps",
        root_tree_id="tree",
        max_parallel_tasks=1,
        checkpoint_ns="ns",
        structural_path="workflow",
    )
    assert event.policy_digest == ""
    assert event.policy_origin == ""
    assert event.gate_semantics_digest == ""
    assert event.assurance_profile_digest == ""


@pytest.mark.parametrize(
    "field",
    ["policy_origin", "gate_semantics_digest", "assurance_profile_digest"],
)
def test_v4_started_rejects_empty_binding_field(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _v4_started(**{field: ""})


def test_v4_started_accepts_complete_binding() -> None:
    event = _v4_started()
    assert event.event_schema_version == 4
    assert event.policy_origin == "project"
    assert len(event.gate_semantics_digest) == 64
    assert len(event.assurance_profile_digest) == 64


def test_v5_started_requires_same_binding_fields() -> None:
    event = _v4_started(event_schema_version=5)
    assert event.event_schema_version == 5
    assert len(event.assurance_profile_digest) == 64
    with pytest.raises(ValidationError, match="assurance_profile_digest"):
        _v4_started(event_schema_version=5, assurance_profile_digest="")
