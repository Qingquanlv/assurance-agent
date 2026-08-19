from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.product import install_product, reset_product, select_product
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.capability_state import CapabilityCatalogError
from assurance_agent.workflow.graph.compiler import compile_loaded_workflow, compile_packaged_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts, parse_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import (
    LoadedWorkflowV2,
    load_workflow_v2_with_origin,
    parse_workflow_v2,
)
from tests.helpers_aa import overlay_workflow_yaml, write_aa_config

_SAMPLE_CONTRACTS = Path("examples/minimal-product/aa_sample/_resources/schemas/execution-contracts.yaml")

_NOOP_OVERLAY = overlay_workflow_yaml(node="noop", uses="operation:no-op")


class _NeverInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected invoke: {request}")


def test_sample_entry_point_does_not_register_run_tests() -> None:
    reset_product()
    select_product("sample")
    from assurance_agent.workflow.driver.capability_catalog import current_operations

    assert "operation:sample-ping" in current_operations()
    assert "operation:run-tests" not in current_operations()


def test_sample_packaged_schema_is_not_assurance_full(tmp_path: Path) -> None:
    reset_product()
    select_product("sample")
    loaded = load_workflow_v2_with_origin(tmp_path)
    assert loaded.origin == "packaged"
    assert "ping" in loaded.schema.entrypoints
    assert "full" not in loaded.schema.entrypoints
    compiled = compile_loaded_workflow(loaded, load_execution_contracts(tmp_path))
    assert "ping" in compiled.schema.entrypoints


def test_sample_execution_contracts_parse() -> None:
    catalog = parse_execution_contracts(_SAMPLE_CONTRACTS.read_text(encoding="utf-8"))
    assert "operation:sample-ping" in catalog.contracts
    assert catalog.contracts["operation:sample-ping"].side_effect_free is True
    assert catalog.contracts["operation:sample-ping"].reads == ()


def test_sample_catalog_rejects_assurance_run_tests(tmp_path: Path) -> None:
    reset_product()
    select_product("assurance")
    loaded = load_workflow_v2_with_origin(tmp_path)
    contracts = load_execution_contracts(tmp_path)
    assurance_compiled = compile_packaged_workflow(loaded.schema, contracts)

    from aa_sample.product import SampleProduct

    install_product(SampleProduct())
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    with pytest.raises(CapabilityCatalogError, match="unregistered operations"):
        assemble_graph_runtime(
            project_root=tmp_path,
            change_dir=tmp_path / "qa" / "changes" / "CH-1",
            compiled=assurance_compiled,
            contracts=contracts,
            adapter=_NeverInvoker(),
        )


def test_sample_catalog_rejects_overlay_no_op_at_assemble(tmp_path: Path) -> None:
    reset_product()
    select_product("sample")
    loaded = LoadedWorkflowV2(schema=parse_workflow_v2(_NOOP_OVERLAY), origin="project")
    compiled = compile_loaded_workflow(loaded, contracts=None)
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    with pytest.raises(CapabilityCatalogError, match="unregistered operations"):
        assemble_graph_runtime(
            project_root=tmp_path,
            change_dir=tmp_path / "qa" / "changes" / "CH-1",
            compiled=compiled,
            contracts=parse_execution_contracts(_SAMPLE_CONTRACTS.read_text(encoding="utf-8")),
            adapter=_NeverInvoker(),
        )
