from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
import uuid

import pytest

from graph_engine.canonical import JSONValue
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
_AGENT_PREFIX = "assurance.product.agent."
_JOIN_NODE_ID = "join-selected"
_SHA = "a" * 64
_EXECUTION_FINALIZE = (
    f"{_AGENT_PREFIX}execution.execute.finalize",
    f"{_AGENT_PREFIX}execution.run.finalize",
)
_INSPECT_FINALIZE = f"{_AGENT_PREFIX}quality.inspect.finalize"
_REPORT_FINALIZE = f"{_AGENT_PREFIX}quality.report.finalize"


@dataclass(frozen=True)
class ReportTrace:
    exists: bool
    coverage: float | None = None


@dataclass(frozen=True)
class FlowTrace:
    status: str
    report: ReportTrace
    logical_steps: tuple[str, ...]
    _activation_counts: Mapping[str, int]

    def activations(self, prepare_stem: str) -> int:
        return self._activation_counts.get(prepare_stem, 0)

    def logical_steps_between(self, start: str, end: str) -> tuple[str, ...]:
        matches = [index for index, step in enumerate(self.logical_steps) if _step_matches(step, start)]
        if not matches:
            raise AssertionError(f"logical step {start!r} was not activated")
        first = matches[0]
        for index in range(first + 1, len(self.logical_steps)):
            if _step_matches(self.logical_steps[index], end):
                return self.logical_steps[first + 1 : index]
        raise AssertionError(f"logical step {end!r} was not activated after {start!r}")


@dataclass(frozen=True)
class GenerationTrace:
    completed_generation_families: set[str]
    join_expected: set[str]
    join_output: object


def _step_matches(step: str, query: str) -> bool:
    return step == query or step.startswith(f"{query}.")


class _SuccessHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"decision": "pass", "needs_fix": False})


class _ScriptedTaskHost:
    """Deterministic in-process host that scripts execution and coverage outcomes."""

    def __init__(
        self,
        *,
        execution_sequence: tuple[str, ...],
        coverage_sequence: tuple[float, ...],
        threshold: float,
        coverage_rounds: int,
    ) -> None:
        self._execution_sequence = execution_sequence
        self._coverage_sequence = coverage_sequence
        self._threshold = threshold
        self._coverage_rounds = coverage_rounds
        self._execution_index = 0
        self._coverage_index = 0
        self._inspect_count = 0
        self._last_status = "passed"
        self._last_measured = 1.0
        self._exhausted = False

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        return TaskHostCallResult(operation="execute", outcome=self._outcome(call.request.capability_id))

    def _outcome(self, capability_id: str) -> TaskOutcome:
        if capability_id in _EXECUTION_FINALIZE:
            status = self._next_execution()
            self._last_status = status
            return TaskOutcome.succeeded({"status": status})
        if capability_id == _INSPECT_FINALIZE:
            measured = self._next_coverage()
            rounds_used = self._inspect_count
            self._inspect_count += 1
            self._last_measured = measured
            decision = measured >= self._threshold
            self._exhausted = measured < self._threshold and rounds_used >= self._coverage_rounds
            return TaskOutcome.succeeded(
                {
                    "coverage": {
                        "measured": measured,
                        "threshold": self._threshold,
                        "rounds_used": rounds_used,
                        "rounds_budget": self._coverage_rounds,
                        "decision": decision,
                    }
                }
            )
        if capability_id == _REPORT_FINALIZE:
            output = cast(JSONValue, {"report": {"exists": True, "coverage": self._last_measured}})
            if self._last_status == "infrastructure_failure":
                return TaskOutcome.stopped("infrastructure_failure", output)
            if self._exhausted:
                return TaskOutcome.stopped("coverage_budget_exhausted", output)
            return TaskOutcome.succeeded(output)
        return TaskOutcome.succeeded({"decision": "pass", "needs_fix": False})

    def _next_execution(self) -> str:
        if self._execution_index < len(self._execution_sequence):
            status = self._execution_sequence[self._execution_index]
            self._execution_index += 1
            return status
        return "passed"

    def _next_coverage(self) -> float:
        if not self._coverage_sequence:
            return 1.0
        if self._coverage_index < len(self._coverage_sequence):
            measured = self._coverage_sequence[self._coverage_index]
            self._coverage_index += 1
            return measured
        return self._coverage_sequence[-1]

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="indeterminate", reason="scripted host"),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="indeterminate", reason="scripted host"),
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
        execution_sequence: tuple[str, ...] = (),
        coverage_sequence: tuple[float, ...] = (),
        threshold: float = 0.90,
        coverage_rounds: int | None = None,
        engine_root: Path,
        composition: FrozenComposition,
    ) -> None:
        self._selected_test_families = selected_test_families
        self._completion_order: Literal["forward", "reverse"] = completion_order
        self._execution_sequence = execution_sequence
        self._coverage_sequence = coverage_sequence
        self._threshold = threshold
        self._coverage_rounds = coverage_rounds
        self._engine_root = engine_root
        self._composition = composition

    def run_to_report(self) -> FlowTrace:
        workflow = self._composition.workflow
        graphs = workflow.graphs
        full = graphs.get("full")
        has_downstream = (
            full is not None
            and any(node_id in full.nodes for node_id in ("execute", "quality", "report"))
            and "quality-report" in graphs
        )
        assert has_downstream, "canonical graph ends at the selected-family join"
        projection, status = self._run_engine()
        assert status in {"succeeded", "stopped"}, status
        return _flow_trace_from_result(projection, self._composition)

    def run_to_generation_join(self) -> GenerationTrace:
        workflow = self._composition.workflow
        assert "full" in workflow.entrypoints, "workflow stops after the intake/case slice"
        assert "generation" in workflow.graphs, "generation branches are absent"
        assert _JOIN_NODE_ID in workflow.graphs["generation"].nodes, "selected-family join is absent"

        projection, status = self._run_engine()
        assert status == "succeeded", status
        return _trace_from_result(
            projection,
            self._composition,
            self._root_input(),
            completion_order=self._completion_order,
        )

    def _resolved_coverage_rounds(self) -> int:
        if self._coverage_rounds is not None:
            return self._coverage_rounds
        if self._coverage_sequence:
            return max(1, len(self._coverage_sequence) - 1)
        return 1

    def _root_input(self) -> dict[str, object]:
        from assurance_product.models import ProductInputV1

        payload = _product_input(
            selected_test_families=self._selected_test_families,
            coverage_rounds=self._resolved_coverage_rounds(),
        )
        return ProductInputV1.model_validate(payload).validate_for_entrypoint("full").model_dump(mode="json")

    def _run_engine(self) -> tuple[InvocationProjection, str]:
        root_input = self._root_input()
        seed = empty_invocation_seed(root_input=cast(JSONValue, root_input))
        invocation_id = f"assurance-{uuid.uuid4().hex}"
        host = _ScriptedTaskHost(
            execution_sequence=self._execution_sequence,
            coverage_sequence=self._coverage_sequence,
            threshold=self._threshold,
            coverage_rounds=self._resolved_coverage_rounds(),
        )
        with Engine(self._engine_root / invocation_id, host=host) as engine:
            with engine.start(
                self._composition,
                entrypoint="full",
                invocation_id=invocation_id,
                seed=seed,
                authorization=empty_runtime_authorization(),
            ) as handle:
                result = engine.run_until_blocked(handle)
        return result.projection, result.status


def _product_input(
    *,
    selected_test_families: tuple[str, ...],
    coverage_rounds: int = 1,
) -> dict[str, object]:
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
            "coverage_rounds": coverage_rounds,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    }


def _logical_step(capability: str) -> str | None:
    if not capability.startswith(_AGENT_PREFIX) or not capability.endswith(".finalize"):
        return None
    return capability.removeprefix(_AGENT_PREFIX).removesuffix(".finalize")


def _prepare_stem(capability: str) -> str | None:
    if not capability.startswith(_AGENT_PREFIX) or not capability.endswith(".prepare"):
        return None
    return f"assurance.{capability.removeprefix(_AGENT_PREFIX).removesuffix('.prepare')}"


def _flow_trace_from_result(
    projection: InvocationProjection,
    composition: FrozenComposition,
) -> FlowTrace:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    logical_steps: list[str] = []
    activation_counts: dict[str, int] = {}
    report = ReportTrace(exists=False)
    for activation in projection.activations:
        if activation.status not in {"completed", "stopped"}:
            continue
        graph = graphs[activation.graph_instance_id]
        compiled = composition.workflow.graphs[graph.graph_id]
        node = compiled.nodes[activation.node_id]
        if node.definition.kind != "task" or node.definition.capability is None:
            continue
        capability = node.definition.capability
        step = _logical_step(capability)
        if step is not None:
            logical_steps.append(step)
        stem = _prepare_stem(capability)
        if stem is not None:
            activation_counts[stem] = activation_counts.get(stem, 0) + 1
        if capability == _REPORT_FINALIZE:
            payload = thaw_json(activation.output)
            if payload is None and activation.attempts:
                payload = thaw_json(activation.attempts[-1].output)
            coverage = None
            exists = False
            if isinstance(payload, Mapping):
                reported = payload.get("report")
                if isinstance(reported, Mapping):
                    exists = bool(reported.get("exists"))
                    raw_coverage = reported.get("coverage")
                    if isinstance(raw_coverage, int | float):
                        coverage = float(raw_coverage)
            report = ReportTrace(exists=exists, coverage=coverage)
    terminal = projection.status
    return FlowTrace(
        status=terminal,
        report=report,
        logical_steps=tuple(logical_steps),
        _activation_counts=activation_counts,
    )


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
        selected_test_families: tuple[str, ...] = ("api",),
        completion_order: Literal["forward", "reverse"] = "forward",
        execution_sequence: tuple[str, ...] = (),
        coverage_sequence: tuple[float, ...] = (),
        threshold: float = 0.90,
        coverage_rounds: int | None = None,
    ) -> ProductRun:
        assert composition is not None, "workflow stops after the intake/case slice"
        return ProductRun(
            selected_test_families=selected_test_families,
            completion_order=completion_order,
            execution_sequence=execution_sequence,
            coverage_sequence=coverage_sequence,
            threshold=threshold,
            coverage_rounds=coverage_rounds,
            engine_root=engine_root,
            composition=composition,
        )

    return factory
