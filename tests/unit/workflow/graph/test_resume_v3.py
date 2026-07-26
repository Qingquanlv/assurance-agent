"""S3：ResumeAnchor 命名空间辅助、v3 resume 事件形态、entrypoint restart 策略。"""

from __future__ import annotations

import pytest

from assurance_agent.workflow.core.graph_events import GraphResumedEvent, ResumeAnchor
from assurance_agent.workflow.graph.models import CompiledEntrypoint, ResumeCommand
from assurance_agent.workflow.graph.runtime import (
    _checkpoint_ns_for_invocation,
    _invocation_ids_along_ns,
    _node_id_for_invocation,
)
from assurance_agent.workflow.graph.schema_v2 import EntrypointDef, parse_workflow_v2


def test_invocation_ids_along_nested_namespace() -> None:
    ns = "root-inv/subgraph-node/child-inv/leaf-node/grandchild-inv"
    assert _invocation_ids_along_ns(ns) == ["root-inv", "child-inv", "grandchild-inv"]


def test_checkpoint_ns_prefix_per_layer() -> None:
    full = "root-inv/subgraph-node/child-inv/leaf-node/grandchild-inv"
    assert _checkpoint_ns_for_invocation(full, "root-inv") == "root-inv"
    assert _checkpoint_ns_for_invocation(full, "child-inv") == "root-inv/subgraph-node/child-inv"
    assert (
        _checkpoint_ns_for_invocation(full, "grandchild-inv")
        == "root-inv/subgraph-node/child-inv/leaf-node/grandchild-inv"
    )


def test_node_id_for_each_invocation_layer() -> None:
    full = "root-inv/subgraph-node/child-inv/leaf-node/grandchild-inv"
    assert _node_id_for_invocation(full, "root-inv", "fallback") == "subgraph-node"
    assert _node_id_for_invocation(full, "child-inv", "fallback") == "leaf-node"
    assert _node_id_for_invocation(full, "grandchild-inv", "fallback") == "fallback"


def test_graph_resumed_event_accepts_anchor_fields() -> None:
    anchor = ResumeAnchor(
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        node_id="gate",
        interrupt_id="int-1",
    )
    event = GraphResumedEvent(
        type="graph_resumed",
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        interrupt_id="int-1",
        action="fix_and_proceed",
        reason="ok",
        who="tester",
        audited_reads_sha256={},
        anchor=anchor,
        parent_anchor_ref=None,
        payload={"note": "resume"},
    )
    assert event.anchor is not None
    assert event.payload["note"] == "resume"


def test_resume_command_accepts_audited_domain_action() -> None:
    command = ResumeCommand(
        interrupt_id="INT-1",
        action="confirm_assessment",
        reason="triaged from execution evidence",
        who="reviewer",
    )

    assert command.action == "confirm_assessment"


@pytest.mark.parametrize("action", ["", "   "])
def test_resume_command_rejects_empty_or_blank_action(action: str) -> None:
    with pytest.raises(ValueError):
        ResumeCommand(
            interrupt_id="INT-1",
            action=action,
            reason="triaged from execution evidence",
            who="reviewer",
        )


# ---------------------------------------------------------------------------
# EntrypointDef.restart schema validation
# ---------------------------------------------------------------------------


def test_entrypoint_def_default_restart_is_once() -> None:
    ep = EntrypointDef(graph="workflow")
    assert ep.restart == "once"


def test_entrypoint_def_accepts_repeatable() -> None:
    ep = EntrypointDef(graph="issue-review-workflow", restart="repeatable")
    assert ep.restart == "repeatable"


def test_entrypoint_def_rejects_unknown_restart() -> None:
    with pytest.raises(Exception):
        EntrypointDef(graph="workflow", restart="always")  # type: ignore[arg-type]


def test_workflow_schema_parses_repeatable_restart() -> None:
    yaml_text = """
schema_version: "2"
name: test-restart-policy
params: {}
entrypoints:
  main:
    graph: main-graph
  repeating:
    graph: repeating-graph
    restart: repeatable
graphs:
  main-graph:
    max_supersteps: 1
    nodes:
      n: {uses: operation:no-op}
    edges: [{from: START, to: n}, {from: n, to: END}]
  repeating-graph:
    max_supersteps: 1
    nodes:
      n: {uses: operation:no-op}
    edges: [{from: START, to: n}, {from: n, to: END}]
"""
    schema = parse_workflow_v2(yaml_text)
    assert schema.entrypoints["main"].restart == "once"
    assert schema.entrypoints["repeating"].restart == "repeatable"


# ---------------------------------------------------------------------------
# CompiledEntrypoint.restart field
# ---------------------------------------------------------------------------


def test_compiled_entrypoint_default_restart_is_once() -> None:
    ep = CompiledEntrypoint(
        name="full",
        graph_id="workflow",
        allow_expr=None,
        param_overrides={},
    )
    assert ep.restart == "once"


def test_compiled_entrypoint_accepts_repeatable() -> None:
    ep = CompiledEntrypoint(
        name="issue-review",
        graph_id="issue-review-workflow",
        allow_expr=None,
        param_overrides={},
        restart="repeatable",
    )
    assert ep.restart == "repeatable"


def test_canonical_workflow_issue_entrypoints_are_repeatable() -> None:
    """Verify the packaged workflow compiles the issue entrypoints as repeatable."""
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
    from pathlib import Path

    schema = load_workflow_v2(Path("."))
    compiled = compile_workflow(schema)

    for ep_name in ("issue-review", "issue-analyze", "issue-reconcile"):
        ep = compiled.entrypoints.get(ep_name)
        assert ep is not None, f"entrypoint {ep_name!r} not found in compiled workflow"
        assert ep.restart == "repeatable", f"entrypoint {ep_name!r} should be repeatable, got {ep.restart!r}"

    for ep_name in ("full", "execute", "archive", "retro"):
        ep = compiled.entrypoints.get(ep_name)
        assert ep is not None, f"entrypoint {ep_name!r} not found in compiled workflow"
        assert ep.restart == "once", f"entrypoint {ep_name!r} should be once, got {ep.restart!r}"
