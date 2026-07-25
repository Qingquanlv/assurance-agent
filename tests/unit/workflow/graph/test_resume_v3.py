"""S3：ResumeAnchor 命名空间辅助与 v3 resume 事件形态。"""

from __future__ import annotations

from assurance_agent.workflow.core.graph_events import GraphResumedEvent, ResumeAnchor
from assurance_agent.workflow.graph.runtime import (
    _checkpoint_ns_for_invocation,
    _invocation_ids_along_ns,
    _node_id_for_invocation,
)


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
