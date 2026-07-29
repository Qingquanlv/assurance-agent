"""invocation 事件记录 policy digest——回答「这次放行依据哪版策略」。"""

from pathlib import Path

from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.workflow.core.graph_events import GraphInvocationStartedEvent


def test_event_carries_policy_digest(tmp_path: Path) -> None:
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
        policy_digest=policy_digest(load_policy(tmp_path)),
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
