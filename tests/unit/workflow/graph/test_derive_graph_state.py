from __future__ import annotations

import ast
from pathlib import Path

import assurance_kernel.workflow.graph as graph_package
from assurance_agent.workflow.graph.checkpoint import (
    derive_graph_state,
    fold_invocation_events,
    project_workflow_state,
)
from tests.helpers_graph_v6 import v6_started_bindings


GRAPH_ROOT = Path(graph_package.__file__).resolve().parent


def test_derive_graph_state_matches_fold_then_project() -> None:
    events: list[dict[str, object]] = [
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "inv-1",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "gd-1",
            "contract_digests": {"skill:noop": "cd-1"},
            **v6_started_bindings(),
            "params": {"run_mode": "full"},
            "params_sha256": "ps-1",
            "root_tree_id": "tree-0",
            "max_parallel_tasks": 2,
            "checkpoint_ns": "inv-1",
            "structural_path": "main",
        }
    ]
    derived = derive_graph_state("inv-1", events)
    expected = project_workflow_state(fold_invocation_events("inv-1", events))
    assert derived == expected


def test_graph_package_never_calls_set_state() -> None:
    offenders: list[str] = []
    for path in GRAPH_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "set_state":
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id == "set_state":
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []
