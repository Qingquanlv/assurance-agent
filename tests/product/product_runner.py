from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
import uuid

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.composition import FrozenComposition
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskOutcome,
)
from graph_engine.attempts.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    runtime_authorization_digest,
)

GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")
FAMILY_TERMINALS = ("api-done", "e2e-done", "fuzz-done", "performance-done")
_PUBLIC_DIGEST = "a" * 64
_EXECUTION_IDS = frozenset(
    {
        "assurance.execution.agent.execute.v1",
        "assurance.execution.agent.run.v1",
    }
)
_INSPECT_ID = "assurance.quality.agent.inspect.v1"
_REPORT_ID = "assurance.quality.agent.report.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_CASE_REVIEW_ID = "assurance.intake.agent.case-review.v1"
_IMPROVEMENT_REVIEW_ID = "assurance.improvement.agent.improvement-review.v1"
_FIX_PROPOSAL_ID = "assurance.healing.agent.fix-proposal.v1"
_REVIEW_IDS = frozenset({_CASE_REVIEW_ID, _IMPROVEMENT_REVIEW_ID})
_OPERATION_LOGICAL_STEPS = {
    "assurance.improvement.apply-memory-improvement": "improvement.apply",
    "assurance.improvement.evaluate-memory-improvement": "improvement.evaluate",
    "assurance.improvement.export-change-improvement": "improvement.export",
    "assurance.improvement.rollback-memory-improvement": "improvement.rollback",
}


def semantic_contract_id_from_capability(capability_id: str) -> str | None:
    if capability_id in _OPERATION_LOGICAL_STEPS or capability_id in PURE_HANDLER_IDS:
        return capability_id
    if (
        capability_id.startswith("assurance.")
        and ".agent." in capability_id
        and capability_id.endswith(".v1")
    ):
        return capability_id
    return None


PURE_HANDLER_IDS = frozenset(
    {
        "assurance.generation.complete",
        "assurance.generation.review-round.advance",
        "assurance.intake.review-round.advance",
        "assurance.healing.repair-round.advance",
    }
)


_SHA = "a" * 64


class _ScriptedTaskHost:
    """Deterministic in-process host that scripts execution and coverage outcomes."""

    def __init__(
        self,
        *,
        execution_sequence: tuple[str, ...],
        coverage_sequence: tuple[float, ...],
        threshold: float,
        coverage_rounds: int,
        review_decision: str,
        healing_decision: str,
    ) -> None:
        self._execution_sequence = execution_sequence
        self._coverage_sequence = coverage_sequence
        self._threshold = threshold
        self._coverage_rounds = coverage_rounds
        self._review_decision = review_decision
        self._healing_decision = healing_decision
        self._execution_index = 0
        self._coverage_index = 0
        self._inspect_count = 0
        self._last_status = "passed"
        self._last_measured = 1.0
        self._exhausted = False

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        outcome = self._outcome(capability_id, call.request.input)
        if semantic_contract_id_from_capability(capability_id) is not None:
            input_value = call.request.input
            if not isinstance(input_value, Mapping) or not isinstance(input_value.get("change_id"), str):
                raise AssertionError("scripted agent prepare requires a change_id")
            if not isinstance(outcome.output, Mapping):
                raise AssertionError("scripted agent prepare requires an object output")
            outcome = outcome.model_copy(
                update={
                    "output": {
                        **outcome.output,
                        "workspace": {"scope_id": input_value["change_id"]},
                        **(
                            {
                                "instructions": [
                                    {"text_content": "skill"},
                                    {"text_content": "persona"},
                                    {"json_content": {"review_repair": None}},
                                ]
                            }
                            if capability_id == _CASE_DESIGN_ID
                            else {}
                        ),
                    }
                }
            )
        return TaskHostCallResult(operation="execute", outcome=outcome)

    def _public_fields(self, extra: Mapping[str, object] | None = None) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "artifacts": [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}],
            "auto_fix_allowed": False,
            "change_id": "CH-DEMO-001",
            "classification": "failed",
            "coverage_state": "satisfied" if self._last_measured >= self._threshold else "repair_required",
            "decision": "pass",
            "effect_refs": [],
            "evidence_refs": [],
            "fix_eligible": True,
            "human_review_required": False,
            "kind": "failure",
            "lifecycle_state": "proposed",
            "needs_fix": False,
            "outcome": "applied",
            "receipt_refs": [],
            "report_refs": [],
            "rounds_budget": 1,
            "rounds_used": 0,
            "status": self._last_status,
        }
        if extra:
            payload.update(cast(dict[str, JSONValue], dict(extra)))
        return payload

    def _outcome(self, capability_id: str, request_input: object = None) -> TaskOutcome:
        change_id = "CH-DEMO-001"
        echoed: dict[str, object] = {}
        if isinstance(request_input, Mapping):
            if isinstance(request_input.get("change_id"), str):
                change_id = request_input["change_id"]
            if isinstance(request_input.get("kind"), str):
                echoed["kind"] = request_input["kind"]
            if isinstance(request_input.get("rounds_used"), int):
                echoed["rounds_used"] = request_input["rounds_used"]
            if isinstance(request_input.get("rounds_budget"), int):
                echoed["rounds_budget"] = request_input["rounds_budget"]
            if isinstance(request_input.get("coverage_state"), str):
                echoed["coverage_state"] = request_input["coverage_state"]
        if capability_id.endswith("repair-round.advance"):
            payload = request_input if isinstance(request_input, Mapping) else {}
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        "kind": payload.get("kind", "failure"),
                        "rounds_used": int(payload.get("rounds_used", 0)) + 1,
                        "rounds_budget": payload.get("rounds_budget", 1),
                    },
                )
            )
        if capability_id in _EXECUTION_IDS:
            status = self._next_execution()
            self._last_status = status
            return TaskOutcome.succeeded(
                self._public_fields({"status": status, "change_id": change_id, **echoed})
            )
        if capability_id == _INSPECT_ID:
            measured = self._next_coverage()
            rounds_used = int(cast(int, echoed.get("rounds_used", self._inspect_count)))
            rounds_budget = int(cast(int, echoed.get("rounds_budget", self._coverage_rounds)))
            self._inspect_count += 1
            self._last_measured = measured
            from assurance_quality.contracts.coverage import classify_coverage_state

            coverage_state = classify_coverage_state(
                measured=measured,
                threshold=self._threshold,
                rounds_used=rounds_used,
                rounds_budget=rounds_budget,
            )
            decision = coverage_state == "satisfied"
            self._exhausted = coverage_state == "exhausted"
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "coverage": {
                            "measured": measured,
                            "threshold": self._threshold,
                            "rounds_used": rounds_used,
                            "rounds_budget": rounds_budget,
                            "decision": decision,
                        },
                        "coverage_state": coverage_state,
                        "rounds_used": rounds_used,
                        "rounds_budget": rounds_budget,
                    }
                )
            )
        if capability_id == _REPORT_ID:
            report_fields: dict[str, object] = {
                "change_id": change_id,
                "report": {"exists": True, "coverage": self._last_measured},
            }
            if "coverage_state" in echoed:
                report_fields["coverage_state"] = echoed["coverage_state"]
            payload = self._public_fields(report_fields)
            if "coverage_state" not in echoed:
                payload.pop("coverage_state", None)
            output = cast(JSONValue, payload)
            if self._last_status == "infrastructure_failure":
                return TaskOutcome.stopped("infrastructure_failure", output)
            if self._exhausted:
                return TaskOutcome.stopped("coverage_budget_exhausted", output)
            return TaskOutcome.succeeded(output)
        if capability_id.startswith("assurance.generation.agent.") and capability_id.endswith(
            ".plan-review.v1"
        ):
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "decision": "pass",
                        "codegen_readiness": "ready",
                        "auto_fix_allowed": False,
                        "human_review_required": False,
                    }
                )
            )
        if capability_id == _CASE_DESIGN_ID:
            return TaskOutcome.succeeded(
                self._public_fields({"change_id": change_id, "validation_status": "pass"})
            )
        if capability_id == _CASE_REVIEW_ID:
            fixable = self._review_decision in {"needs_fix", "changes_requested"}
            human = self._review_decision in {"needs_human_review", "reject"}
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "decision": self._review_decision,
                        "auto_fix_allowed": fixable,
                        "human_review_required": human,
                    }
                )
            )
        if capability_id.endswith("apply-improvement-auto-review"):
            decision = self._review_decision
            if isinstance(request_input, Mapping) and isinstance(request_input.get("decision"), str):
                decision = request_input["decision"]
            lifecycle = {
                "pass": "approved",
                "changes_requested": "needs_rework",
                "reject": "rejected",
            }.get(decision, "proposed")
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "decision": decision,
                        "lifecycle_state": lifecycle,
                        "approval_source": "automatic" if lifecycle == "approved" else "none",
                        "effect_intents": [],
                        "write_authorization": [],
                    }
                )
            )
        if capability_id.endswith("apply-improvement-review"):
            action = None
            if isinstance(request_input, Mapping):
                if isinstance(request_input.get("action"), str):
                    action = request_input["action"]
                elif isinstance(request_input.get("decision"), str):
                    action = request_input["decision"]
            if action is None:
                return TaskOutcome.succeeded(
                    self._public_fields(
                        {
                            "change_id": change_id,
                            "lifecycle_state": "proposed",
                            "effect_intents": [],
                            "write_authorization": [],
                        }
                    )
                )
            lifecycle = {
                "approve": "approved",
                "reject": "rejected",
                "request_rework": "needs_rework",
                "supersede": "superseded",
            }.get(action, "proposed")
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "action": action,
                        "change_id": change_id,
                        "lifecycle_state": lifecycle,
                        "approval_source": "human" if lifecycle == "approved" else "none",
                        "effect_intents": [],
                        "write_authorization": [],
                    }
                )
            )
        if capability_id.endswith("evaluate-memory-improvement"):
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "lifecycle_state": "evaluating",
                        "outcome": "passed",
                    }
                )
            )
        if capability_id in _REVIEW_IDS:
            return TaskOutcome.succeeded(
                self._public_fields(
                    {"change_id": change_id, "decision": self._review_decision, "needs_fix": False}
                )
            )
        if capability_id == "assurance.quality.agent.issue-analysis.v1":
            return TaskOutcome.succeeded(
                self._public_fields(
                    {
                        "change_id": change_id,
                        "classification": "test",
                        "fix_eligible": True,
                        **echoed,
                    }
                )
            )
        if capability_id == _FIX_PROPOSAL_ID and self._healing_decision == "disallowed":
            return TaskOutcome.stopped(
                "healing_disallowed",
                self._public_fields({"change_id": change_id, **echoed}),
            )
        extra = {"change_id": change_id, **echoed}
        if capability_id == "assurance.healing.agent.coverage-repair.v1":
            extra.setdefault("kind", "coverage")
            extra.setdefault("status", "repaired")
        return TaskOutcome.succeeded(self._public_fields(extra))

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


class ProductRun:
    def __init__(
        self,
        *,
        entrypoint: str,
        selected_test_families: tuple[str, ...],
        review_decision: str,
        healing_decision: str,
        completion_order: Literal["forward", "reverse"] = "forward",
        execution_sequence: tuple[str, ...] = (),
        coverage_sequence: tuple[float, ...] = (),
        threshold: float = 0.90,
        coverage_rounds: int | None = None,
        engine_root: Path,
        composition: FrozenComposition,
        invocation_id: str | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._entrypoint = entrypoint
        self._selected_test_families = selected_test_families
        self._review_decision = review_decision
        self._healing_decision = healing_decision
        self._completion_order: Literal["forward", "reverse"] = completion_order
        self._execution_sequence = execution_sequence
        self._coverage_sequence = coverage_sequence
        self._threshold = threshold
        self._coverage_rounds = coverage_rounds
        self._engine_root = engine_root
        self._composition = composition
        self._invocation_id = invocation_id or f"assurance-{uuid.uuid4().hex}"
        self._workspace_root = workspace_root

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
        if self._entrypoint not in {"full", "intake", "case"}:
            payload["case_delta_paths"] = ()
        return (
            ProductInputV1.model_validate(payload)
            .validate_for_entrypoint(self._entrypoint)
            .model_dump(mode="json")
        )

    def _host(self) -> _ScriptedTaskHost:
        return _ScriptedTaskHost(
            execution_sequence=self._execution_sequence,
            coverage_sequence=self._coverage_sequence,
            threshold=self._threshold,
            coverage_rounds=self._resolved_coverage_rounds(),
            review_decision=self._review_decision,
            healing_decision=self._healing_decision,
        )

    def run_to_report(self) -> FlowTrace:
        from tests.product.execution_loop import drive_coverage_loop, drive_execution_loop

        coverage_rounds = self._resolved_coverage_rounds()
        if self._coverage_sequence:
            states: list[str] = []
            repairs: list[str] = []
            last_measured = self._coverage_sequence[-1]
            for index, measured in enumerate(self._coverage_sequence):
                last_measured = measured
                if measured >= self._threshold:
                    states.append("satisfied")
                    break
                if index >= coverage_rounds:
                    states.append("exhausted")
                    break
                states.append("repair_required")
                if index + 1 < len(self._coverage_sequence):
                    repairs.append("repaired")
            loop = drive_coverage_loop(
                coverage_states=tuple(states),
                repair_statuses=tuple(repairs),
                coverage_rounds=coverage_rounds,
                measured_sequence=self._coverage_sequence,
                threshold=self._threshold,
                entrypoint="execute" if self._entrypoint != "full" else "full",
            )
        else:
            sequence = self._execution_sequence or ("passed",)
            failed = sequence[0] in {"failed", "product_issue", "infrastructure_failure"}
            loop = drive_execution_loop(
                execution_sequence=sequence,
                classifications=("product_bug",)
                if sequence[0] == "product_issue"
                else (("test",) if failed else ()),
                fix_eligible=(sequence[0] == "failed",) if failed else (),
                coverage_rounds=coverage_rounds,
                entrypoint="execute" if self._entrypoint != "full" else "full",
            )
            last_measured = 1.0
        repair_count = loop.public_exports.count("healing.repair-coverage")
        if "healing.repair-coverage" in loop.public_exports:
            repair_count = sum(
                1 for item in loop.task_capabilities if item.endswith("coverage-repair.finalize")
            )
        return FlowTrace(
            status=loop.status
            if loop.status == "interrupted"
            else ("stopped" if loop.terminal == "not-achieved" else "succeeded"),
            report=ReportTrace(exists="quality.report" in loop.public_exports, coverage=last_measured),
            logical_steps=loop.public_exports,
            _activation_counts={"assurance.healing.coverage-repair": repair_count},
        )


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
        "case_delta_paths": ("qa/changes/CH-DEMO-001/cases/system/dept/case.yaml",),
        "capability_leafs": (),
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


def _scripted_authorization() -> InvocationRuntimeAuthorization:
    sources = (
        SecretSourceBinding(
            handle="opencode.token",
            source_kind="environment",
            source_locator="OPENCODE_TOKEN",
        ),
        SecretSourceBinding(
            handle="cursor.token",
            source_kind="environment",
            source_locator="CURSOR_TOKEN",
        ),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=sources,
        digest=runtime_authorization_digest(sources),
    )


def modular_product_composition(installed_sources) -> FrozenComposition:
    return adapter_product_composition(installed_sources, "opencode")


def adapter_product_composition(installed_sources, adapter: str) -> FrozenComposition:
    from assurance_product.product import resolve_assurance_composition
    from tests.product.composition_harness import request_for

    return resolve_assurance_composition(request_for(adapter, installed_sources))


@pytest.fixture
def product_runner(tmp_path_factory: pytest.TempPathFactory, installed_sources):
    composition = modular_product_composition(installed_sources)
    engine_root = tmp_path_factory.mktemp("generation-runner")

    def factory(
        *,
        entrypoint: str = "full",
        selected_test_families: tuple[str, ...] | None = None,
        review_decision: str = "pass",
        healing_decision: str = "allowed",
        completion_order: Literal["forward", "reverse"] = "forward",
        execution_sequence: tuple[str, ...] = (),
        coverage_sequence: tuple[float, ...] = (),
        threshold: float = 0.90,
        coverage_rounds: int | None = None,
        invocation_id: str | None = None,
        workspace_root: Path | None = None,
    ) -> ProductRun:
        from assurance_product.models import FAMILY_EMPTY_ENTRYPOINTS

        assert composition is not None, "workflow stops after the intake/case slice"
        families = selected_test_families
        if families is None:
            families = () if entrypoint in FAMILY_EMPTY_ENTRYPOINTS else ("api",)
        return ProductRun(
            entrypoint=entrypoint,
            selected_test_families=families,
            review_decision=review_decision,
            healing_decision=healing_decision,
            completion_order=completion_order,
            execution_sequence=execution_sequence,
            coverage_sequence=coverage_sequence,
            threshold=threshold,
            coverage_rounds=coverage_rounds,
            engine_root=engine_root,
            composition=composition,
            invocation_id=invocation_id,
            workspace_root=workspace_root,
        )

    return factory
