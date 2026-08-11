"""Runtime factory chooses compile entry points by schema origin."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.driver.runtime_factory import build_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentInvoker
from assurance_agent.workflow.graph.compiler import CompileError, compile_packaged_workflow, compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin


class _Never(AgentInvoker):
    def invoke(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("adapter must not be called")


def test_packaged_origin_uses_compile_packaged_workflow(tmp_path: Path) -> None:
    loaded = load_workflow_v2_with_origin(tmp_path)
    assert loaded.origin == "packaged"
    contracts = load_execution_contracts(tmp_path)
    compiled = compile_packaged_workflow(loaded.schema, contracts)
    assert compiled.digest


def test_malformed_packaged_surface_cannot_downgrade_to_core(tmp_path: Path) -> None:
    loaded = load_workflow_v2_with_origin(tmp_path)
    graphs = dict(loaded.schema.graphs)
    del graphs["api-plan-cycle"]
    mutated = loaded.schema.model_copy(update={"graphs": graphs})
    with pytest.raises(CompileError, match="packaged assurance activation failed"):
        compile_packaged_workflow(mutated, load_execution_contracts(tmp_path))
    # Core compile of the same mutated schema must not silently become the packaged gate.
    # It may still fail for other structural reasons, but must not use the activation heuristic.
    with pytest.raises(CompileError):
        compile_workflow(mutated, load_execution_contracts(tmp_path))


def test_build_graph_runtime_from_packaged_origin(tmp_path: Path) -> None:
    from tests.helpers_aa import write_aa_config

    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_aa_config(tmp_path)
    bundle = build_graph_runtime(
        project_root=tmp_path,
        change_id="CH-1",
        adapter=_Never(),
    )
    assert bundle.compiled.digest
    assert bundle.resolved is not None
    assert bundle.resolved.compiled.digest == bundle.compiled.digest
