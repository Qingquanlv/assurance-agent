from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import uuid

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskOutcome,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import (
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.models import InvocationProjection
from graph_engine.runtime.seed import empty_invocation_seed

from tests.product.product_runner import (
    _PUBLIC_DIGEST,
    _install_public_shaped_end_output,
    _product_alias,
    _product_input,
    _restore_end_output,
    _scripted_authorization,
)
from tests.product.test_workflow_modularization_golden import _modular_composition

_ADVANCE_ID = "assurance.healing.repair-round.advance"
_INSTALLED_SOURCES = None
_GRAPH_EXPORTS = {
    "execution-execute": "execution.execute",
    "execution-run": "execution.rerun",
    "generation": "generation.generate",
    "healing-coverage-repair": "healing.repair-coverage",
    "healing-fix-proposal": "healing.repair-failure",
    "issue-analyze": "quality.issue-analyze",
    "issue-reconcile": "quality.issue-reconcile",
    "issue-review": "quality.issue-review",
    "quality": "quality.assess",
    "quality-report": "quality.report",
}


def bind_installed_sources(installed_sources) -> None:
    global _INSTALLED_SOURCES
    _INSTALLED_SOURCES = installed_sources


@dataclass(frozen=True)
class ExecutionLoopTrace:
    public_exports: tuple[str, ...]
    terminal: str
    status: str
    advance_outputs: tuple[dict[str, object], ...]
    task_capabilities: tuple[str, ...]
    projection: InvocationProjection


def drive_failed_execution(
    *,
    classification: str,
    fix_eligible: bool,
    execution_sequence: tuple[str, ...] = ("failed",),
    healing_rounds: int = 1,
) -> ExecutionLoopTrace:
    return drive_execution_loop(
        execution_sequence=execution_sequence,
        classifications=(classification,),
        fix_eligible=(fix_eligible,),
        healing_rounds=healing_rounds,
    )


def drive_execution_loop(
    *,
    execution_sequence: tuple[str, ...],
    classifications: tuple[str, ...],
    fix_eligible: tuple[bool, ...],
    healing_rounds: int = 1,
    entrypoint: str = "execute",
) -> ExecutionLoopTrace:
    if _INSTALLED_SOURCES is None:
        raise AssertionError("installed_sources fixture is not bound")
    composition = _modular_composition(_INSTALLED_SOURCES)
    host = _ExecutionLoopHost(
        execution_sequence=execution_sequence,
        classifications=classifications,
        fix_eligible=fix_eligible,
    )
    root_input = _loop_input(healing_rounds=healing_rounds)
    with TemporaryDirectory(prefix="execution-loop-") as raw:
        project = Path(raw).resolve() / "project"
        project.mkdir()
        from assurance_product.product import prepare_change_workspace

        workspace = prepare_change_workspace(project, "CH-DEMO-001")
        engine = Engine(workspace.paths.runtime_root, host=host)
        _install_public_shaped_end_output()
        try:
            handle = engine.start(
                composition,
                entrypoint=entrypoint,
                invocation_id=f"exec-loop-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, root_input)),
                authorization=_scripted_authorization(),
                workspace_binding=workspace.runtime_binding(),
            )
            result = engine.run_until_blocked(handle)
            projection = result.projection
            return ExecutionLoopTrace(
                public_exports=_public_exports(projection, composition),
                terminal=_terminal_name(projection, result.status),
                status=result.status,
                advance_outputs=tuple(host.advance_outputs),
                task_capabilities=_task_capabilities(projection, composition),
                projection=projection,
            )
        finally:
            _restore_end_output()
            engine.close()


def _loop_input(*, healing_rounds: int) -> dict[str, object]:
    from assurance_product.models import ProductInputV1

    payload = _product_input(selected_test_families=("api",), coverage_rounds=1)
    payload["budgets"] = {
        "review_rounds": 1,
        "coverage_rounds": 1,
        "healing_rounds": healing_rounds,
        "execution_retries": 1,
    }
    payload["artifacts"] = [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}]
    payload["decision"] = "pass"
    payload["case_delta_paths"] = ()
    return ProductInputV1.model_validate(payload).validate_for_entrypoint("execute").model_dump(mode="json")


class _ExecutionLoopHost:
    def __init__(
        self,
        *,
        execution_sequence: tuple[str, ...],
        classifications: tuple[str, ...],
        fix_eligible: tuple[bool, ...],
    ) -> None:
        self._execution_sequence = execution_sequence
        self._classifications = classifications
        self._fix_eligible = fix_eligible
        self._execution_index = 0
        self._analysis_index = 0
        self._advance = None
        self.advance_outputs: list[dict[str, object]] = []

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        if capability_id == _ADVANCE_ID or capability_id.endswith("repair-round.advance"):
            if self._advance is None:
                from assurance_healing.operations.workflow_state import HealingRepairRoundAdvanceHandler

                self._advance = HealingRepairRoundAdvanceHandler()
            context = TaskContext(
                project_root=Path.cwd(),
                write_root=Path.cwd(),
                workspace_identity=call.attempt_root.workspace_identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            )
            outcome = await self._advance.execute(call.request, context)
            if isinstance(outcome.output, Mapping):
                self.advance_outputs.append(dict(outcome.output))
            return TaskHostCallResult(operation="execute", outcome=outcome)
        outcome = self._scripted(capability_id, call.request.input)
        aliased = _product_alias(capability_id)
        if aliased.endswith(".prepare"):
            input_value = call.request.input
            change_id = "CH-DEMO-001"
            if isinstance(input_value, Mapping) and isinstance(input_value.get("change_id"), str):
                change_id = input_value["change_id"]
            if isinstance(outcome.output, Mapping):
                outcome = outcome.model_copy(
                    update={"output": {**outcome.output, "workspace": {"scope_id": change_id}}}
                )
        return TaskHostCallResult(operation="execute", outcome=outcome)

    def _scripted(self, capability_id: str, request_input: object) -> TaskOutcome:
        change_id = "CH-DEMO-001"
        kind = "failure"
        rounds_used = 0
        rounds_budget = 1
        if isinstance(request_input, Mapping):
            if isinstance(request_input.get("change_id"), str):
                change_id = request_input["change_id"]
            if isinstance(request_input.get("kind"), str):
                kind = request_input["kind"]
            if isinstance(request_input.get("rounds_used"), int):
                rounds_used = request_input["rounds_used"]
            if isinstance(request_input.get("rounds_budget"), int):
                rounds_budget = request_input["rounds_budget"]
        aliased = _product_alias(capability_id)
        if aliased.endswith("execution.execute.finalize") or aliased.endswith("execution.run.finalize"):
            status = (
                self._execution_sequence[self._execution_index]
                if self._execution_index < len(self._execution_sequence)
                else "passed"
            )
            self._execution_index += 1
            return TaskOutcome.succeeded(
                {
                    "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
                    "change_id": change_id,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                    "status": status,
                }
            )
        if aliased.endswith("quality.issue-analysis.finalize"):
            index = min(self._analysis_index, max(len(self._classifications) - 1, 0))
            classification = self._classifications[index] if self._classifications else "test"
            eligible = self._fix_eligible[index] if self._fix_eligible else False
            self._analysis_index += 1
            return TaskOutcome.succeeded(
                {
                    "change_id": change_id,
                    "classification": classification,
                    "evidence_refs": [],
                    "fix_eligible": eligible,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                }
            )
        if aliased.endswith("healing.fix-proposal.finalize"):
            return TaskOutcome.succeeded(
                {
                    "change_id": change_id,
                    "effect_refs": [],
                    "kind": kind,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                    "status": "repaired",
                }
            )
        return TaskOutcome.succeeded(
            {
                "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
                "auto_fix_allowed": False,
                "change_id": change_id,
                "classification": "test",
                "codegen_readiness": "ready",
                "coverage_state": "satisfied",
                "decision": "pass",
                "effect_refs": [],
                "evidence_refs": [],
                "fix_eligible": False,
                "human_review_required": False,
                "kind": kind,
                "lifecycle_state": "proposed",
                "needs_fix": False,
                "outcome": "applied",
                "receipt_refs": [],
                "report_refs": [{"path": "qa/changes/CH-DEMO-001/report/report.md", "digest": _PUBLIC_DIGEST}],
                "rounds_budget": rounds_budget,
                "rounds_used": rounds_used,
                "status": "passed",
            }
        )

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

    def read_terminal_receipts(self, identity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


def _public_exports(projection: InvocationProjection, composition) -> tuple[str, ...]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    seen: list[str] = []
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "subgraph":
            continue
        graph_ref = node.definition.graph or ""
        local_id = graph_ref.rsplit(".", 1)[-1]
        export = _GRAPH_EXPORTS.get(local_id)
        if export is None or export in seen:
            continue
        seen.append(export)
    return tuple(seen)


def _task_capabilities(projection: InvocationProjection, composition) -> tuple[str, ...]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    capabilities: list[str] = []
    for activation in projection.activations:
        if not activation.attempts:
            continue
        graph = graphs[activation.graph_instance_id]
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "task" or node.definition.capability is None:
            continue
        capabilities.append(node.definition.capability)
    return tuple(capabilities)


def _terminal_name(projection: InvocationProjection, status: str) -> str:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    ends: list[str] = []
    for activation in projection.activations:
        if activation.status != "completed":
            continue
        if activation.node_id not in {"achieved", "done", "exhausted", "not-achieved", "rejected"}:
            continue
        graph = graphs[activation.graph_instance_id]
        graph_id = graph.graph_id
        if graph.parent_graph_instance_id is None or graph_id.endswith("product-execute"):
            ends.append(activation.node_id)
    if "achieved" in ends:
        return "achieved"
    if "exhausted" in ends:
        return "exhausted"
    if "not-achieved" in ends:
        return "not-achieved"
    if ends:
        return ends[-1]
    return status


def assert_each_repair_is_preceded_by_one_advance(capabilities: tuple[str, ...]) -> None:
    repairs = [index for index, item in enumerate(capabilities) if item.endswith("fix-proposal.finalize")]
    advances = [index for index, item in enumerate(capabilities) if item.endswith("repair-round.advance")]
    assert len(repairs) == len(advances)
    for repair_index, advance_index in zip(repairs, advances, strict=True):
        assert advance_index < repair_index
        between = capabilities[advance_index + 1 : repair_index]
        assert not any(item.endswith("repair-round.advance") for item in between)
        assert not any(item.endswith("fix-proposal.finalize") for item in between)
