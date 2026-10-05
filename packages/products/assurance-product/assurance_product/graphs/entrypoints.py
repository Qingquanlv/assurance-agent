from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, model_validator

from graph_engine.flow import BoundFlow, Flow, const, ledger
from graph_engine.flow.sources import InputSource

from assurance_intake.contracts import TestFamilyPolicyV1
from assurance_intake.handoff import PLAN, REWORK_CONTEXT
from assurance_product.models import ProductInputV1

if TYPE_CHECKING:
    from assurance_product.graphs.factory import ProductFeatureBundles

_PUBLIC = {"completed": "completed", "failed": "failed"}
_ENTRYPOINT_MODELS: dict[str, type[ProductInputV1]] = {}
_QUALITY_INPUTS = {"rounds_budget": "budgets.coverage_rounds", "rounds_used": const(0)}
_PREPARE_INPUTS = {
    "source_artifacts": "artifacts",
    "coverage_epoch": const(0),
    "healing_rounds_used": const(0),
}
_RETRO_INPUTS = {
    "source_refs": "artifacts",
    "window": "retro_window",
    "artifact_paths": "allowed_artifact_paths",
}
_ISSUE_ROUTES = {
    "fix_eligible": "completed",
    "report_issue": "completed",
    "unclassified": "completed",
    "failed": "failed",
}
_DONE_ROUTES = {"done": "completed", "failed": "failed"}


def _entrypoint_model(entrypoint: str) -> type[ProductInputV1]:
    cached = _ENTRYPOINT_MODELS.get(entrypoint)
    if cached is not None:
        return cached

    class EntrypointInput(ProductInputV1):
        @model_validator(mode="after")
        def _check_entrypoint(self) -> EntrypointInput:
            self.validate_for_entrypoint(entrypoint)
            return self

    EntrypointInput.__name__ = "ProductInput_" + entrypoint.replace("-", "_")
    _ENTRYPOINT_MODELS[entrypoint] = EntrypointInput
    return EntrypointInput


def _intake_input() -> type[BaseModel]:
    class IntakeRootInput(_entrypoint_model("intake")):
        family_policy: TestFamilyPolicyV1

    IntakeRootInput.__name__ = "ProductInput_intake_root"
    return IntakeRootInput


def _mount(
    child: BoundFlow,
    *,
    name: str,
    routes: Mapping[str, str],
    inputs: Mapping[str, InputSource] | None = None,
) -> Flow:
    flow = Flow(
        name,
        input=_entrypoint_model(name),
        outcomes=("completed", "failed"),
        public=_PUBLIC,
    )
    flow.subflow("feature", child, routes=routes, inputs=dict(inputs or {}))
    return flow


def thin_root_flows(bundles: ProductFeatureBundles) -> dict[str, Flow]:
    intake = Flow(
        "intake",
        input=_intake_input(),
        outcomes=("completed", "failed"),
        public=_PUBLIC,
        ledger_inputs=(REWORK_CONTEXT,),
    )
    intake.subflow(
        "prepare",
        bundles.intake.prepare,
        routes={"prepared": "init", "failed": "failed"},
        inputs=_PREPARE_INPUTS,
    )
    intake.subflow(
        "init",
        bundles.generation.init_runtime,
        routes={"completed": "case", "failed": "failed"},
    )
    intake.subflow(
        "case",
        bundles.intake.case,
        routes={
            "reviewed": "completed",
            "rejected": "failed",
            "exhausted": "failed",
            "failed": "failed",
        },
        inputs={
            "coverage_epoch": const(0),
            "plan_ref": ledger(PLAN, many=False),
            "plan_digest": "plan_digest",
        },
    )
    return {
        "intake": intake,
        "init": _mount(
            bundles.generation.init_runtime,
            name="init",
            routes={"completed": "completed", "failed": "failed"},
        ),
        "retro": _mount(
            bundles.improvement.retro,
            name="retro",
            routes=_DONE_ROUTES,
            inputs=_RETRO_INPUTS,
        ),
        "issue-review": _mount(
            bundles.quality.issue_review,
            name="issue-review",
            routes=_ISSUE_ROUTES,
            inputs=_QUALITY_INPUTS,
        ),
        "issue-analyze": _mount(
            bundles.quality.issue_analyze,
            name="issue-analyze",
            routes=_ISSUE_ROUTES,
            inputs=_QUALITY_INPUTS,
        ),
        "issue-reconcile": _mount(
            bundles.quality.issue_reconcile,
            name="issue-reconcile",
            routes={"ready": "completed", "failed": "failed"},
            inputs=_QUALITY_INPUTS,
        ),
    }


__all__ = ["thin_root_flows"]
