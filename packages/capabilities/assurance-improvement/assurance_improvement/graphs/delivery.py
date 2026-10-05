from __future__ import annotations

from typing import Literal


from pydantic import BaseModel, Field, create_model
from pydantic_core import PydanticUndefined

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel

from assurance_improvement.contracts.agent import ImprovementSkillInputV1
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.contracts.review import ImprovementAutoReviewAssessment
from assurance_improvement.graphs.nodes import ApplyHumanDecision
from assurance_improvement.ops.archive import op as archive
from assurance_improvement.ops.improvement_review import op as improvement_review

_AUTO_REVIEW = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-improvement-auto-review"]
_HUMAN_REVIEW = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-improvement-review"]
_EVALUATE = TASK_ATTEMPT_CONTRACTS["assurance.improvement.evaluate-memory-improvement"]
_EXPORT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.export-change-improvement"]
_APPLY = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-memory-improvement"]
_ROLLBACK = TASK_ATTEMPT_CONTRACTS["assurance.improvement.rollback-memory-improvement"]
_EvalOutcome = Literal["passed", "regressed", "awaiting_baseline", "error"]


def _without_validation_error(model: type[FrozenModel], name: str) -> type[FrozenModel]:
    fields: dict[str, object] = {}
    for field_name, field in model.model_fields.items():
        if field_name == "validation_error":
            continue
        default = ... if field.default is PydanticUndefined else field.default
        fields[field_name] = (field.annotation, default)
    return create_model(name, __base__=FrozenModel, **fields)  # type: ignore[call-overload]


_SkillFlowInput = _without_validation_error(ImprovementSkillInputV1, "ImprovementSkillFlowInput")


def _build_one_shot(
    context: CapabilityBuildContext,
    *,
    name: str,
    step: str,
    op: object,
    flow_input: type[BaseModel],
    receipt_field: str | None = None,
    ledger_inputs: tuple[object, ...] = (),
) -> BoundFlow:
    flow = Flow(name, input=flow_input, outcomes=("done", "failed"), ledger_inputs=ledger_inputs)
    flow.step(step, op, on_failure="failed", then="done")
    if receipt_field is not None:
        flow.publish_receipt(step)
    return flow.bind(context)


def build_archive_graph(context: CapabilityBuildContext) -> BoundFlow:
    return _build_one_shot(
        context,
        name="archive",
        step="archive",
        op=archive,
        flow_input=_SkillFlowInput,
        receipt_field="receipt_refs",
    )


def build_review_graph(context: CapabilityBuildContext) -> BoundFlow:
    return _build_one_shot(
        context,
        name="review",
        step="review",
        op=improvement_review,
        flow_input=_SkillFlowInput,
    )


def build_evaluate_graph(context: CapabilityBuildContext) -> BoundFlow:
    return _build_one_shot(
        context,
        name="evaluate",
        step="evaluate",
        op=_EVALUATE,
        flow_input=_EVALUATE.input_model,
        receipt_field="receipt_refs",
        ledger_inputs=(_AUTO_REVIEW.artifact("projection"),),
    )


def build_export_graph(context: CapabilityBuildContext) -> BoundFlow:
    return _build_one_shot(
        context,
        name="export",
        step="export",
        op=_EXPORT,
        flow_input=_EXPORT.input_model,
        receipt_field="receipt_refs",
    )


def build_rollback_graph(context: CapabilityBuildContext) -> BoundFlow:
    return _build_one_shot(
        context,
        name="rollback",
        step="rollback",
        op=_ROLLBACK,
        flow_input=_ROLLBACK.input_model,
        receipt_field="receipt_refs",
    )


class ApplyFlowInput(FrozenModel):
    change_id: str = Field(min_length=1)
    projection: ImprovementProjection | None = None
    assessment: ImprovementAutoReviewAssessment | None = None
    current: ImprovementProjection | None = None
    review_id: str | None = None
    eval_run_id: str | None = None
    outcome: _EvalOutcome | None = None
    report_sha256: str | None = None
    staged_sha256: str | None = None
    baseline_sha256: str | None = None
    target_digest: str | None = None
    approved_state_digest: str | None = None
    approved_version: int | None = None
    before_sha256: str | None = None
    after_sha256: str | None = None
    receipt_sha256: str | None = None
    allowed_artifact_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()


def build_apply_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "improvement",
        input=ApplyFlowInput,
        outcomes=("done", "failed", "rejected", "rework", "superseded"),
    )
    flow.step(
        "apply-auto-review",
        _AUTO_REVIEW,
        on_failure="failed",
        route_on="route",
        routes={
            "apply-evaluate": "apply-evaluate",
            "rework": "rework",
            "rejected": "rejected",
            "human-review": "human-review",
            "failed": "failed",
        },
    )
    human = flow.gate(
        "human-review",
        decision=ApplyHumanDecision,
        routes={
            "approve": "apply-human-review",
            "reject": "apply-human-review",
            "request_rework": "apply-human-review",
            "supersede": "apply-human-review",
        },
    )
    flow.step(
        "apply-human-review",
        _HUMAN_REVIEW,
        inputs={"action": human.action},
        on_failure="failed",
        route_on="route",
        routes={
            "apply-evaluate": "apply-evaluate",
            "rejected": "rejected",
            "rework": "rework",
            "superseded": "superseded",
            "failed": "failed",
        },
    )
    flow.step(
        "apply-evaluate",
        _EVALUATE,
        on_failure="failed",
        route_on="route",
        routes={"apply": "apply", "failed": "failed"},
    )
    flow.step("apply", _APPLY, on_failure="failed", then="done")
    flow.publish_receipt("apply")
    return flow.bind(context)


__all__ = [
    "build_apply_graph",
    "build_archive_graph",
    "build_evaluate_graph",
    "build_export_graph",
    "build_review_graph",
    "build_rollback_graph",
]
