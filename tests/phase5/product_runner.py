from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import uuid

import pytest

from graph_engine.composition import FrozenComposition
from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.graph.input_projection import project_task_input
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.models import InvocationProjection
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed

GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")
FAMILY_TERMINALS = ("api-done", "e2e-done", "fuzz-done", "performance-done")
_GENERATION_PREFIX = "assurance.product.agent.generation."
_JOIN_NODE_ID = "join-selected"
_SHA = "a" * 64


@dataclass(frozen=True)
class GenerationTrace:
    completed_generation_families: set[str]
    join_expected: set[str]
    join_output: object


class _SuccessHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"decision": "pass", "needs_fix": False})


class _SuccessTaskHost:
    """Deterministic in-process success host from graph-engine test doubles."""

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.succeeded({"decision": "pass", "needs_fix": False}),
        )

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="indeterminate", reason="success host"),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="indeterminate", reason="success host"),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


class ProductRun:
    def __init__(
        self,
        *,
        selected_test_families: tuple[str, ...],
        completion_order: Literal["forward", "reverse"] = "forward",
        engine_root: Path,
        composition: FrozenComposition,
    ) -> None:
        self._selected_test_families = selected_test_families
        self._completion_order: Literal["forward", "reverse"] = completion_order
        self._engine_root = engine_root
        self._composition = composition

    def run_to_generation_join(self) -> GenerationTrace:
        from assurance_product.models import ProductInputV1

        workflow = self._composition.workflow
        assert "full" in workflow.entrypoints, "workflow stops after the intake/case slice"
        assert "generation" in workflow.graphs, "generation branches are absent"
        assert _JOIN_NODE_ID in workflow.graphs["generation"].nodes, "selected-family join is absent"

        payload = _product_input(selected_test_families=self._selected_test_families)
        product_input = ProductInputV1.model_validate(payload).validate_for_entrypoint("full")
        root_input = product_input.model_dump(mode="json")
        seed = empty_invocation_seed(root_input=root_input)
        invocation_id = f"generation-{uuid.uuid4().hex}"
        with Engine(self._engine_root / invocation_id, host=_SuccessTaskHost()) as engine:
            with engine.start(
                self._composition,
                entrypoint="full",
                invocation_id=invocation_id,
                seed=seed,
                authorization=empty_runtime_authorization(),
            ) as handle:
                result = engine.run_until_blocked(handle)
        assert result.status == "succeeded", result
        return _trace_from_result(
            result.projection,
            self._composition,
            root_input,
            completion_order=self._completion_order,
        )


def _product_input(*, selected_test_families: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "requirement": "Add login",
        "run_mode": "implement",
        "selected_test_families": selected_test_families,
        "auto_archive": False,
        "capability_catalog": {
            "resource_id": "assurance.product.configuration.capability-catalog",
            "sha256": _SHA,
        },
        "product_policy": {
            "resource_id": "assurance.product.configuration.product-policy",
            "sha256": _SHA,
        },
        "data_knowledge": {
            "resource_id": "assurance.product.configuration.data-knowledge",
            "sha256": _SHA,
        },
        "allowed_artifact_paths": ("qa/changes",),
        "budgets": {
            "review_rounds": 1,
            "coverage_rounds": 1,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    }


def _family_from_capability(capability: str | None) -> str | None:
    if capability is None or not capability.startswith(_GENERATION_PREFIX):
        return None
    family = capability.removeprefix(_GENERATION_PREFIX).split(".", 1)[0]
    if family in GENERATION_FAMILIES:
        return family
    return None


def _trace_from_result(
    projection: InvocationProjection,
    composition: FrozenComposition,
    root_input: dict[str, object],
    *,
    completion_order: Literal["forward", "reverse"],
) -> GenerationTrace:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    completed: set[str] = set()
    for activation in projection.activations:
        if activation.status != "completed":
            continue
        graph = graphs[activation.graph_instance_id]
        compiled = composition.workflow.graphs[graph.graph_id]
        node = compiled.nodes[activation.node_id]
        if node.definition.kind != "task":
            continue
        family = _family_from_capability(node.definition.capability)
        if family is not None:
            completed.add(family)
    join_output = _join_output(
        projection,
        composition,
        root_input,
        completion_order=completion_order,
    )
    return GenerationTrace(
        completed_generation_families=completed,
        join_expected=_join_expected(join_output),
        join_output=join_output,
    )


def _join_expected(join_output: object) -> set[str]:
    if not isinstance(join_output, Mapping):
        raise AssertionError(f"join output is not an object: {type(join_output)!r}")
    families = join_output.get("selected_families")
    if not isinstance(families, list | tuple):
        raise AssertionError(f"join selected_families is missing: {join_output!r}")
    return set(families)


def _predecessor_tokens_in_order(
    raw: Mapping[str, object],
    *,
    completion_order: Literal["forward", "reverse"],
) -> dict[str, object]:
    order = FAMILY_TERMINALS if completion_order == "forward" else tuple(reversed(FAMILY_TERMINALS))
    missing = [source for source in order if source not in raw]
    extra = sorted(set(raw) - set(order))
    if missing or extra:
        raise AssertionError(
            f"join predecessors must be the four family terminals; missing={missing} extra={extra}"
        )
    return {source: raw[source] for source in order}


def _join_output(
    projection: InvocationProjection,
    composition: FrozenComposition,
    root_input: dict[str, object],
    *,
    completion_order: Literal["forward", "reverse"],
) -> object:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    join = next(
        activation
        for activation in projection.activations
        if activation.status == "completed"
        and graphs[activation.graph_instance_id].graph_id == "generation"
        and activation.node_id == _JOIN_NODE_ID
    )
    compiled = composition.workflow.graphs["generation"].nodes[_JOIN_NODE_ID]
    tokens = {item.token_id: item for item in projection.offered_tokens}
    raw_predecessors: dict[str, object] = {}
    for token_id in join.token_ids:
        token = tokens[token_id]
        if token.source is None:
            continue
        raw_predecessors[token.source] = thaw_json(token.payload)
    predecessor_tokens = _predecessor_tokens_in_order(
        raw_predecessors,
        completion_order=completion_order,
    )
    projection_def = compiled.definition.input_projection
    if projection_def is None:
        return freeze_json(join.output)
    return freeze_json(
        project_task_input(
            projection_def,
            root_input=root_input,
            node_config=dict(compiled.definition.input),
            predecessor_tokens=predecessor_tokens,
        )
    )


def _workflow_capabilities(workflow: WorkflowDef) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                node.capability
                for graph in workflow.graphs.values()
                for node in graph.nodes.values()
                if node.capability is not None
            }
        )
    )


def resolve_product_workflow_composition(workflow: WorkflowDef) -> FrozenComposition:
    from tests.phase5.runtime_composition import resolve_workflow_composition

    handlers = {capability: _SuccessHandler() for capability in _workflow_capabilities(workflow)}
    document = workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return resolve_workflow_composition(document, handlers)


@pytest.fixture(scope="session")
def product_runner(tmp_path_factory: pytest.TempPathFactory, installed_sources):
    del installed_sources
    from assurance_product.product import load_canonical_workflow

    workflow = load_canonical_workflow()
    composition = None
    if "full" in workflow.entrypoints:
        composition = resolve_product_workflow_composition(workflow)
    engine_root = tmp_path_factory.mktemp("generation-runner")

    def factory(
        *,
        selected_test_families: tuple[str, ...],
        completion_order: Literal["forward", "reverse"] = "forward",
    ) -> ProductRun:
        assert composition is not None, "workflow stops after the intake/case slice"
        return ProductRun(
            selected_test_families=selected_test_families,
            completion_order=completion_order,
            engine_root=engine_root,
            composition=composition,
        )

    return factory
