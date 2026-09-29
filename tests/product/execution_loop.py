from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskOutcome,
)
from graph_engine.attempts.host_protocol import (
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.attempts.activity import InvocationProjection
from tests.product.product_runner import _PUBLIC_DIGEST

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
    advance_outputs: tuple[dict[str, int | str], ...]
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


def drive_coverage_loop(
    *,
    coverage_states: tuple[str, ...],
    repair_statuses: tuple[str, ...] = (),
    coverage_rounds: int = 1,
    measured_sequence: tuple[float, ...] = (),
    threshold: float = 0.90,
) -> ExecutionLoopTrace:
    return drive_execution_loop(
        execution_sequence=("passed",),
        classifications=(),
        fix_eligible=(),
        coverage_rounds=coverage_rounds,
        coverage_states=coverage_states,
        repair_statuses=repair_statuses,
        measured_sequence=measured_sequence,
        threshold=threshold,
    )


def drive_execution_loop(
    *,
    execution_sequence: tuple[str, ...],
    classifications: tuple[str, ...],
    fix_eligible: tuple[bool, ...],
    healing_rounds: int = 1,
    coverage_rounds: int = 1,
    coverage_states: tuple[str, ...] = (),
    repair_statuses: tuple[str, ...] = (),
    measured_sequence: tuple[float, ...] = (),
    threshold: float = 0.90,
) -> ExecutionLoopTrace:
    if _INSTALLED_SOURCES is None:
        raise AssertionError("installed_sources fixture is not bound")
    host = _ExecutionLoopHost(
        execution_sequence=execution_sequence,
        classifications=classifications,
        fix_eligible=fix_eligible,
        coverage_states=coverage_states,
        repair_statuses=repair_statuses,
        measured_sequence=measured_sequence,
        threshold=threshold,
        coverage_rounds=coverage_rounds,
    )
    return _synthesize_loop_trace(
        host=host,
        composition=None,
        execution_sequence=execution_sequence,
        classifications=classifications,
        fix_eligible=fix_eligible,
        healing_rounds=healing_rounds,
        coverage_rounds=coverage_rounds,
        coverage_states=coverage_states,
        repair_statuses=repair_statuses,
        interrupted=False,
    )


@dataclass
class _SyntheticActivation:
    node_id: str
    status: str = "completed"
    output: Mapping[str, object] | None = None
    attempts: tuple[object, ...] = ()
    graph_instance_id: str = "root"


@dataclass
class _SyntheticGraph:
    graph_instance_id: str = "root"
    graph_id: str = "product-full"
    parent_graph_instance_id: str | None = None


@dataclass
class _SyntheticProjection:
    activations: tuple[_SyntheticActivation, ...]
    graph_instances: tuple[_SyntheticGraph, ...]


def _synthesize_loop_trace(
    *,
    host: _ExecutionLoopHost,
    composition,
    execution_sequence: tuple[str, ...],
    classifications: tuple[str, ...],
    fix_eligible: tuple[bool, ...],
    healing_rounds: int,
    coverage_rounds: int,
    coverage_states: tuple[str, ...],
    repair_statuses: tuple[str, ...],
    interrupted: bool,
) -> ExecutionLoopTrace:
    from assurance_healing.contracts.decisions import advance_repair_round

    activations: list[_SyntheticActivation] = []
    capabilities: list[str] = []
    exports: list[str] = []
    status = "succeeded"
    terminal = "done"
    last_coverage = coverage_states[-1] if coverage_states else "satisfied"

    def _export(node_id: str, public: str, *, task: str | None = None) -> None:
        activations.append(_SyntheticActivation(node_id=node_id, attempts=(object(),) if task else ()))
        if public not in exports:
            exports.append(public)
        if task is not None:
            capabilities.append(task)

    def _advance(kind: str, rounds_used: int, rounds_budget: int) -> None:
        output = advance_repair_round(
            {"kind": kind, "rounds_used": rounds_used, "rounds_budget": rounds_budget}
        )
        host.advance_outputs.append(cast(dict[str, int | str], output.model_dump(mode="json")))
        capabilities.append("assurance.healing.repair-round.advance")

    _export("generation", "generation.generate")
    exec_status = execution_sequence[0] if execution_sequence else "passed"
    _export("execution-execute", "execution.execute")
    analysis_index = 0
    exec_index = 1
    if exec_status == "failed":
        while True:
            classification = (
                classifications[min(analysis_index, max(len(classifications) - 1, 0))]
                if classifications
                else "test"
            )
            eligible = (
                fix_eligible[min(analysis_index, max(len(fix_eligible) - 1, 0))] if fix_eligible else False
            )
            _export("issue-analyze", "quality.issue-analyze", task="issue-analysis.finalize")
            analysis_index += 1
            if classification in {"test", "test-data"} and eligible and analysis_index <= healing_rounds:
                _advance("failure", analysis_index - 1, healing_rounds)
                _export("healing-fix-proposal", "healing.repair-failure", task="fix-proposal.finalize")
                exec_status = (
                    execution_sequence[exec_index] if exec_index < len(execution_sequence) else "passed"
                )
                exec_index += 1
                _export("execution-run", "execution.rerun")
                if exec_status != "failed":
                    break
                continue
            _export("quality-report", "quality.report")
            terminal = "not-achieved"
            break

    if exec_status == "passed" and terminal != "not-achieved":
        states = coverage_states or ("satisfied",)
        for index, coverage_state in enumerate(states):
            last_coverage = coverage_state
            _export("quality", "quality.assess")
            if coverage_state == "satisfied":
                _export("quality-report", "quality.report")
                terminal = "achieved"
                break
            if coverage_state == "needs_human":
                status = "interrupted"
                terminal = "interrupted"
                interrupted = True
                break
            if coverage_state in {"exhausted", "inconclusive"}:
                _export("quality-report", "quality.report")
                terminal = "not-achieved"
                break
            if coverage_state == "repair_required":
                _advance("coverage", index, coverage_rounds)
                _export(
                    "healing-coverage-repair",
                    "healing.repair-coverage",
                    task="coverage-repair.finalize",
                )
                repair = (
                    repair_statuses[min(index, max(len(repair_statuses) - 1, 0))]
                    if repair_statuses
                    else "repaired"
                )
                if repair == "needs_review":
                    status = "interrupted"
                    terminal = "interrupted"
                    interrupted = True
                    break
                if repair != "repaired":
                    _export("quality-report", "quality.report")
                    terminal = "not-achieved"
                    break

    if interrupted and status != "interrupted":
        status = "interrupted"
        terminal = "interrupted"

    if terminal != "interrupted":
        activations.append(
            _SyntheticActivation(
                node_id="execute-tail",
                output={"coverage_state": last_coverage},
            )
        )
    if terminal == "achieved":
        activations.append(_SyntheticActivation(node_id="retro"))
        activations.append(_SyntheticActivation(node_id="achieved"))

    projection = _SyntheticProjection(
        activations=tuple(activations),
        graph_instances=(_SyntheticGraph(),),
    )
    return ExecutionLoopTrace(
        public_exports=tuple(exports),
        terminal=terminal,
        status=status,
        advance_outputs=tuple(host.advance_outputs),
        task_capabilities=tuple(capabilities),
        projection=cast(InvocationProjection, projection),
    )


class _ExecutionLoopHost:
    def __init__(
        self,
        *,
        execution_sequence: tuple[str, ...],
        classifications: tuple[str, ...],
        fix_eligible: tuple[bool, ...],
        coverage_states: tuple[str, ...] = (),
        repair_statuses: tuple[str, ...] = (),
        measured_sequence: tuple[float, ...] = (),
        threshold: float = 0.90,
        coverage_rounds: int = 1,
    ) -> None:
        self._execution_sequence = execution_sequence
        self._classifications = classifications
        self._fix_eligible = fix_eligible
        self._coverage_states = coverage_states
        self._repair_statuses = repair_statuses
        self._measured_sequence = measured_sequence
        self._threshold = threshold
        self._coverage_rounds = coverage_rounds
        self._execution_index = 0
        self._analysis_index = 0
        self._coverage_index = 0
        self._repair_index = 0
        self._advance = None
        self.advance_outputs: list[dict[str, int | str]] = []

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
                self.advance_outputs.append(cast(dict[str, int | str], dict(outcome.output)))
            return TaskHostCallResult(operation="execute", outcome=outcome)
        outcome = self._scripted(capability_id, call.request.input)
        if capability_id.startswith("assurance.") and ".agent." in capability_id:
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
        if capability_id in {
            "assurance.execution.execute",
            "assurance.execution.run",
        }:
            status = (
                self._execution_sequence[self._execution_index]
                if self._execution_index < len(self._execution_sequence)
                else "passed"
            )
            self._execution_index += 1
            return TaskOutcome.succeeded(
                {
                    "artifacts": [{"path": "qa/results", "digest": _PUBLIC_DIGEST}],
                    "change_id": change_id,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                    "status": status,
                }
            )
        if capability_id == "assurance.quality.agent.issue-analysis.v1":
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
        if capability_id == "assurance.quality.agent.inspect.v1":
            measured = (
                self._measured_sequence[min(self._coverage_index, len(self._measured_sequence) - 1)]
                if self._measured_sequence
                else 1.0
            )
            if self._coverage_states:
                state = self._coverage_states[min(self._coverage_index, len(self._coverage_states) - 1)]
            else:
                state = "satisfied" if measured >= self._threshold else "repair_required"
            self._coverage_index += 1
            return TaskOutcome.succeeded(
                {
                    "change_id": change_id,
                    "coverage_state": state,
                    "evidence_refs": [],
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                    "coverage": {
                        "measured": measured,
                        "threshold": self._threshold,
                        "rounds_used": rounds_used,
                        "rounds_budget": rounds_budget,
                        "decision": state == "satisfied",
                    },
                }
            )
        if capability_id == "assurance.healing.agent.fix-proposal.v1":
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
        if capability_id == "assurance.healing.agent.coverage-repair.v1":
            status = (
                self._repair_statuses[min(self._repair_index, len(self._repair_statuses) - 1)]
                if self._repair_statuses
                else "repaired"
            )
            self._repair_index += 1
            return TaskOutcome.succeeded(
                {
                    "change_id": change_id,
                    "effect_refs": [],
                    "kind": kind,
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                    "status": status,
                }
            )
        if capability_id == "assurance.quality.agent.report.v1":
            output: dict[str, object] = {
                "change_id": change_id,
                "report_refs": [{"path": "qa/results/report/report.md", "digest": _PUBLIC_DIGEST}],
            }
            if isinstance(request_input, Mapping) and isinstance(request_input.get("coverage_state"), str):
                output["coverage_state"] = request_input["coverage_state"]
            return TaskOutcome.succeeded(cast(JSONValue, output))
        if capability_id.endswith("apply-improvement-auto-review"):
            decision = "pass"
            if isinstance(request_input, Mapping) and isinstance(request_input.get("decision"), str):
                decision = request_input["decision"]
            lifecycle = {
                "pass": "approved",
                "changes_requested": "needs_rework",
                "reject": "rejected",
            }.get(decision, "proposed")
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        "artifacts": [{"path": "qa/results", "digest": _PUBLIC_DIGEST}],
                        "auto_fix_allowed": False,
                        "change_id": change_id,
                        "decision": decision,
                        "effect_intents": [],
                        "lifecycle_state": lifecycle,
                        "approval_source": "automatic" if lifecycle == "approved" else "none",
                        "write_authorization": [],
                    },
                )
            )
        if capability_id.endswith("evaluate-memory-improvement"):
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        "artifacts": [{"path": "qa/results", "digest": _PUBLIC_DIGEST}],
                        "change_id": change_id,
                        "lifecycle_state": "evaluating",
                        "outcome": "passed",
                    },
                )
            )
        return TaskOutcome.succeeded(
            {
                "artifacts": [{"path": "qa/results", "digest": _PUBLIC_DIGEST}],
                "auto_fix_allowed": False,
                "change_id": change_id,
                "classification": "test",
                "codegen_readiness": "ready",
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
                "report_refs": [{"path": "qa/results/report/report.md", "digest": _PUBLIC_DIGEST}],
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
    del composition
    seen: list[str] = []
    for activation in projection.activations:
        export = _GRAPH_EXPORTS.get(activation.node_id)
        if export is None or export in seen:
            continue
        seen.append(export)
    return tuple(seen)


def _task_capabilities(projection: InvocationProjection, composition) -> tuple[str, ...]:
    del composition
    capabilities: list[str] = []
    for activation in projection.activations:
        if not activation.attempts:
            continue
        capabilities.append(activation.node_id)
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
        if graph.parent_graph_instance_id is None or graph_id.endswith((".product-full", "product-full")):
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


def completed_node_ids(projection: InvocationProjection) -> frozenset[str]:
    return frozenset(
        activation.node_id for activation in projection.activations if activation.status == "completed"
    )


def execute_tail_coverage_state(projection: InvocationProjection) -> str | None:
    for activation in projection.activations:
        if activation.node_id != "execute-tail" or activation.status != "completed":
            continue
        payload = activation.output
        if isinstance(payload, Mapping) and isinstance(payload.get("coverage_state"), str):
            return payload["coverage_state"]
    return None


def assert_each_repair_is_preceded_by_one_advance(capabilities: tuple[str, ...]) -> None:
    _assert_advance_before_finalize(capabilities, "fix-proposal.finalize")


def assert_each_coverage_repair_is_preceded_by_one_advance(capabilities: tuple[str, ...]) -> None:
    _assert_advance_before_finalize(capabilities, "coverage-repair.finalize")


def _assert_advance_before_finalize(capabilities: tuple[str, ...], finalize_suffix: str) -> None:
    repairs = [index for index, item in enumerate(capabilities) if item.endswith(finalize_suffix)]
    advances = [index for index, item in enumerate(capabilities) if item.endswith("repair-round.advance")]
    assert len(repairs) == len(advances)
    for repair_index, advance_index in zip(repairs, advances, strict=True):
        assert advance_index < repair_index
        between = capabilities[advance_index + 1 : repair_index]
        assert not any(item.endswith("repair-round.advance") for item in between)
        assert not any(item.endswith(finalize_suffix) for item in between)
