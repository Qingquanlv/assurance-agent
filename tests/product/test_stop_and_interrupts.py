from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import uuid

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import (
    InvocationWorkspaceBinding,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from tests.product.product_runner import Engine, EngineError
from graph_engine.attempts.host_protocol import (
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.attempts.activity import InvocationProjection
from graph_engine.attempts.secret_sources import empty_runtime_authorization
from tests.product.product_runner import empty_invocation_seed

from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("product_runner")

_FORBIDDEN_TREE_NAMES = frozenset({"workspace", "trees", "attempts", "HEAD.json"})
_REVIEW_ADVANCE = "assurance.intake.review-round.advance"
_INTAKE_ENTRY_INPUT = {
    "change_id": "CH-DEMO-001",
    "requirement": "Cover department CRUD.",
    "selected_test_families": ["api"],
    "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
}


class _AssembledIntakeHost:
    def __init__(self, *, reviews: tuple[str, ...]) -> None:
        self._reviews = reviews
        self._index = 0
        self._advance = None
        self.advance_outputs: list[dict[str, int]] = []

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        capability_id = call.request.capability_id
        if capability_id == _REVIEW_ADVANCE or capability_id.endswith("review-round.advance"):
            if self._advance is None:
                from assurance_intake.operations.workflow_state import ReviewRoundAdvanceHandler

                self._advance = ReviewRoundAdvanceHandler()
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
                self.advance_outputs.append(
                    {
                        "rounds_used": int(cast(int, outcome.output["rounds_used"])),
                        "rounds_budget": int(cast(int, outcome.output["rounds_budget"])),
                    }
                )
            return TaskHostCallResult(operation="execute", outcome=outcome)
        request_input = call.request.input
        change_id = "CH-DEMO-001"
        rounds_used = 0
        rounds_budget = 2
        if isinstance(request_input, Mapping):
            if isinstance(request_input.get("change_id"), str):
                change_id = request_input["change_id"]
            if isinstance(request_input.get("rounds_used"), int):
                rounds_used = request_input["rounds_used"]
            if isinstance(request_input.get("rounds_budget"), int):
                rounds_budget = request_input["rounds_budget"]
        decision = self._reviews[min(self._index, len(self._reviews) - 1)]
        if capability_id.endswith("case-review.finalize"):
            self._index += 1
            fixable = decision in {"needs_fix", "changes_requested"}
            human = decision in {"needs_human_review"}
            if decision == "reject":
                fixable = False
                human = False
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.succeeded(
                    {
                        "artifacts": [{"path": "qa/changes", "digest": "a" * 64}],
                        "auto_fix_allowed": fixable,
                        "auto_fix_plan": [{"fix": "tighten assertion"}] if fixable else [],
                        "change_id": change_id,
                        "decision": decision,
                        "human_review_required": human,
                        "rounds_budget": rounds_budget,
                        "rounds_used": rounds_used,
                    }
                ),
            )
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.succeeded(
                {
                    "artifacts": [{"path": "qa/changes", "digest": "a" * 64}],
                    "change_id": change_id,
                    "decision": "pass",
                    "rounds_budget": rounds_budget,
                    "rounds_used": rounds_used,
                }
            ),
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


def _assembled_intake_entry_composition(installed_sources):
    from assurance_product.product import resolve_assurance_composition
    from tests.product.runtime_composition import resolve_workflow_composition

    assembled = resolve_assurance_composition(request_for("opencode", installed_sources))
    entry_id = "assurance.intake.workflow.graph.entry"
    assert entry_id in assembled.workflow.graphs
    handlers: dict[str, TaskHandler] = {}
    graphs: dict[str, object] = {}
    for graph_id, graph in assembled.workflow.graphs.items():
        if "assurance.intake.workflow.graph." not in graph_id:
            continue
        nodes: dict[str, object] = {}
        for node_id, node in graph.nodes.items():
            payload = node.definition.model_dump(mode="python", by_alias=True, exclude_unset=True)
            payload.pop("input_schema", None)
            payload.pop("output_schema", None)
            payload.pop("output_projection", None)
            capability = payload.get("capability")
            if isinstance(capability, str):
                handlers[capability] = cast(TaskHandler, object())
            nodes[node_id] = payload
        graphs[graph_id] = {
            "max_activations": graph.max_activations,
            "start": graph.start,
            "nodes": nodes,
            "edges": [
                edge.model_dump(mode="python", by_alias=True, exclude_unset=True) for edge in graph.edges
            ],
        }
    workflow = WorkflowDef.model_validate(
        {
            "name": "assembled-intake-entry",
            "entrypoints": {"entry": entry_id},
            "retry": {
                name: policy.model_dump(mode="python") for name, policy in assembled.workflow.retry.items()
            },
            "timeout": {
                name: policy.model_dump(mode="python") for name, policy in assembled.workflow.timeout.items()
            },
            "graphs": graphs,
        }
    )
    return resolve_workflow_composition(
        workflow.model_dump(mode="json", by_alias=True, exclude_unset=True),
        handlers,
    )


def _entry_end_nodes(projection: InvocationProjection) -> set[str]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    ends: set[str] = set()
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        if graph.graph_id.endswith(".entry") and activation.node_id in {"done", "rejected", "exhausted"}:
            if activation.status == "completed":
                ends.add(activation.node_id)
    return ends


def _drive_assembled_intake_entry(
    installed_sources,
    *,
    reviews: tuple[str, ...],
    resumes: tuple[str, ...],
):
    composition = _assembled_intake_entry_composition(installed_sources)
    host = _AssembledIntakeHost(reviews=reviews)
    with TemporaryDirectory(prefix="assembled-intake-entry-") as raw:
        root = Path(raw).resolve()
        project = root / "project"
        attempts = root / "attempts"
        receipts = root / "receipts"
        runtime = root / "runtime"
        for path in (project, attempts, receipts, runtime):
            path.mkdir()
        (runtime / "invocations").mkdir()
        engine = Engine(runtime, host=host)
        try:
            handle = engine.start(
                composition,
                entrypoint="entry",
                invocation_id=f"intake-entry-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, dict(_INTAKE_ENTRY_INPUT))),
                authorization=empty_runtime_authorization(),
                workspace_binding=InvocationWorkspaceBinding(
                    project_root=project,
                    attempts_root=attempts,
                    receipts_root=receipts,
                ),
            )
            result = engine.run_until_blocked(handle)
            for action in resumes:
                assert result.status == "interrupted", result.status
                handle = engine.resume(handle, action=action, payload={"decision": action})
                result = engine.run_until_blocked(handle)
            return result, host
        finally:
            engine.close()


def test_business_stop_is_resumable_only_at_declared_interrupt(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    resumed = stopped.resume({"decision": "approve"})
    if resumed.status == "interrupted":
        resumed = resumed.resume({"decision": "approve"})
    assert resumed.status == "completed"


def test_human_reject_terminates_rejected_not_completed(installed_sources):
    result, host = _drive_assembled_intake_entry(
        installed_sources,
        reviews=("needs_human_review",),
        resumes=("reject",),
    )
    assert result.status == "succeeded"
    ends = _entry_end_nodes(result.projection)
    assert ends == {"rejected"}
    assert host.advance_outputs == []


def test_request_rework_is_a_distinct_resume_action(installed_sources):
    result, host = _drive_assembled_intake_entry(
        installed_sources,
        reviews=("needs_human_review", "needs_fix", "pass"),
        resumes=("request_rework",),
    )
    assert result.status == "succeeded"
    ends = _entry_end_nodes(result.projection)
    assert ends == {"done"}
    assert tuple(item["rounds_used"] for item in host.advance_outputs) == (1, 2)
    assert host.advance_outputs[0]["rounds_budget"] == 2


def test_forged_improvement_interrupt_approval_cannot_apply(installed_sources, tmp_path: Path):
    from tests.product.product_runner import ProductRun, modular_product_composition

    def _apply(**kwargs):
        return ProductRun(
            entrypoint="improvement-apply",
            selected_test_families=(),
            review_decision="needs_human_review",
            healing_decision="allowed",
            engine_root=tmp_path / f"apply-{kwargs.get('tag', 'run')}",
            composition=modular_product_composition(installed_sources),
        ).run_to_terminal()

    stopped = _apply(tag="forged")
    assert stopped.status == "interrupted"
    with pytest.raises(EngineError):
        stopped.resume({"decision": "approve", "state": "approved", "lifecycle_state": "approved"})
    rejected = _apply(tag="reject")
    resumed = rejected.resume({"decision": "reject"})
    assert "improvement.apply" not in resumed.logical_steps
    rework = _apply(tag="rework")
    reworked = rework.resume({"decision": "request_rework"})
    assert "improvement.apply" not in reworked.logical_steps
    superseded = _apply(tag="supersede")
    closed = superseded.resume({"decision": "supersede"})
    assert "improvement.apply" not in closed.logical_steps


def test_invalid_resume_input_fails(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    with pytest.raises(EngineError):
        stopped.resume({"decision": "not-allowed"})
    with pytest.raises(EngineError):
        stopped.resume({"decision": "approve", "extra": "field"})
    still_pending = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert still_pending.status == "interrupted"


def test_healing_disallowed_is_business_stop_not_completion(product_runner):
    stopped = product_runner(
        execution_sequence=("failed",),
        healing_decision="disallowed",
    ).run_to_terminal()
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "healing_disallowed"
    assert stopped.status != "completed"
    assert stopped.status != "interrupted"


def test_nested_stop_does_not_become_normal_completion(product_runner):
    stopped = product_runner(
        execution_sequence=("failed",),
        healing_decision="disallowed",
    ).run_to_terminal()
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "healing_disallowed"
    assert stopped.has_nested_stop


def test_reported_success_is_distinct_from_stop_and_interrupt(product_runner):
    completed = product_runner().run_to_terminal()
    assert completed.status == "completed"
    assert completed.stop_reason is None


def test_infrastructure_failure_is_stop_after_report(product_runner):
    stopped = product_runner(execution_sequence=("infrastructure_failure",)).run_to_terminal()
    assert stopped.status in {"stopped", "failed"}
    assert stopped.status != "interrupted"
    if stopped.status == "stopped":
        assert "quality.report" in stopped.logical_steps


def test_interrupt_runtime_lives_under_the_change_without_tree_store(product_runner):
    stopped = product_runner(review_decision="needs_human_review").run_to_terminal()
    assert stopped.status == "interrupted"
    runtime = Path(stopped._engine._root)
    assert runtime.name == ".runtime"
    assert runtime.parent.parent.name == "changes"
    assert runtime.parent.parent.parent.name == "qa"
    names = {path.name for path in runtime.parent.rglob("*")}
    assert names.isdisjoint(_FORBIDDEN_TREE_NAMES)
