"""Live compiled identity mismatch must fail closed without reloading pins."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.definition_pinning import request_for_compiled
from assurance_agent.workflow.graph.runtime import GraphDefinitionChanged
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from tests.helpers_aa import write_aa_config
from tests.unit.driver.test_runtime_factory import _MINIMAL_CONTRACTS, _MINIMAL_WORKFLOW


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected invoke: {request}")


def test_definition_digest_mismatch_raises_without_loading_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live compiled identity ≠ request → GraphDefinitionChanged; no pin reload."""
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    contracts = parse_execution_contracts(_MINIMAL_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_MINIMAL_WORKFLOW), contracts)
    runtime = assemble_graph_runtime(
        project_root=tmp_path,
        change_dir=change_dir,
        compiled=compiled,
        contracts=contracts,
        adapter=NeverCalledInvoker(),
    )

    calls: list[object] = []

    def _boom(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("load_pinned_execution_definition must not run")

    monkeypatch.setattr(
        "assurance_agent.workflow.driver.runtime_factory.load_pinned_execution_definition",
        _boom,
        raising=False,
    )
    live = request_for_compiled(compiled, event_schema_version=6)
    mismatched = replace(live, graph_digest="0" * 64)

    with pytest.raises(GraphDefinitionChanged, match="graph_definition_changed"):
        runtime._definition_resolver(mismatched)  # noqa: SLF001
    assert calls == []
