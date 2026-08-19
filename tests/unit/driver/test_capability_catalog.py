from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.product import select_product
from assurance_agent.workflow.driver.capability_catalog import (
    ensure_default_catalog,
    install_catalog,
    reset_catalog,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.capability_state import (
    DEFAULT_OPERATION_NAMES,
    DEFAULT_PRODUCT_ID,
    CapabilityCatalog,
    CapabilityCatalogError,
    current_capability_view,
    current_validator_ids,
    default_capability_catalog_digest,
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from tests.helpers_aa import write_aa_config


class _NeverInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected invoke: {request}")


def test_default_operations_keys_match_named_set() -> None:
    assert set(default_operations()) == DEFAULT_OPERATION_NAMES


def test_ensure_default_catalog_pins_today_closed_sets() -> None:
    select_product(DEFAULT_PRODUCT_ID)
    view = ensure_default_catalog()
    assert view.operation_names == DEFAULT_OPERATION_NAMES
    assert view.validator_ids == KNOWN_PRECOMMIT_VALIDATORS
    assert view.digest == default_capability_catalog_digest()
    assert match_artifact("review/api-plan-review.json") is not None
    again = ensure_default_catalog()
    assert again.digest == view.digest


def test_ensure_default_does_not_override_explicit_empty() -> None:
    empty = CapabilityCatalog().freeze()
    install_catalog(empty, operations={}, artifacts=())
    try:
        ensure_default_catalog()
        view = current_capability_view()
        assert view is not None
        assert view.operation_names == frozenset()
        assert current_validator_ids() == frozenset()
        assert match_artifact("review/api-plan-review.json") is None
    finally:
        reset_catalog()


_STOP_SCHEMA = textwrap.dedent(
    """\
    name: stop-test
    entrypoints:
      full: {graph: main}
    policies:
      retry:
        never: {max_attempts: 1, retry_on: []}
      timeout:
        local: {run_seconds: 60, heartbeat_seconds: 10}
      scheduler: {max_parallel_tasks: 1}
    graphs:
      main:
        max_supersteps: 4
        nodes:
          halt: {uses: operation:stop}
        edges:
          - {from: START, to: halt}
          - {from: halt, to: END}
    gates: {}
    """
)


def test_assemble_rejects_operation_missing_from_empty_catalog(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    empty = CapabilityCatalog().freeze()
    install_catalog(empty, operations={}, artifacts=())
    compiled = compile_workflow(parse_workflow_v2(_STOP_SCHEMA))
    try:
        with pytest.raises(CapabilityCatalogError, match="operation:stop"):
            assemble_graph_runtime(
                project_root=tmp_path,
                change_dir=tmp_path / "qa" / "changes" / "CH-1",
                compiled=compiled,
                contracts=ExecutionContractCatalog(contracts={}),
                adapter=_NeverInvoker(),
            )
    finally:
        reset_catalog()


def test_reset_catalog_allows_later_default_match() -> None:
    empty = CapabilityCatalog().freeze()
    install_catalog(empty, operations={}, artifacts=())
    reset_catalog()
    select_product(DEFAULT_PRODUCT_ID)
    ensure_default_catalog()
    assert match_artifact("review/api-plan-review.json") is not None


def test_ensure_default_catalog_fails_closed_for_non_default_product() -> None:
    reset_catalog()
    with pytest.raises(CapabilityCatalogError, match="select_product"):
        ensure_default_catalog()
    assert current_capability_view() is None
    install_current_product_id("sample")
    try:
        with pytest.raises(CapabilityCatalogError, match="select_product"):
            ensure_default_catalog()
        assert current_capability_view() is None
    finally:
        reset_current_product_id()
