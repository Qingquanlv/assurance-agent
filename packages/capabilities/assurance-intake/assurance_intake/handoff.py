"""Artifact handles shared by intake ops.

Op packages cannot import one another, so a consuming op lists the handle defined
here. The producing op exposes the same ledger key through ``op.artifact``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, RootModel, ValidationError

from agent_runtime_contracts.ops import ArtifactHandle, InputError
from graph_engine.artifacts import ArtifactReadError, open_artifact

from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.workflow import CaseReworkContextV1, EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.prepare_evidence import evidence_read_error

_CATALOG_ID = "assurance.product.configuration.capability-catalog"
_KNOWLEDGE_ID = "assurance.product.configuration.data-knowledge"


class _MappingDocument(RootModel[dict[str, object]]):
    pass


def load_plan(root: Path, ref: object) -> ResolvedAssurancePlan:
    artifact = ref if isinstance(ref, EvidenceArtifactRefV1) else EvidenceArtifactRefV1.model_validate(ref)
    try:
        return decode_plan(open_artifact(root, artifact), artifact)
    except ArtifactReadError as error:
        raise InputError(evidence_read_error(error)) from error
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error


def load_reviewed_case(root: Path, ref: object) -> ReviewedCaseV1:
    artifact = ref if isinstance(ref, EvidenceArtifactRefV1) else EvidenceArtifactRefV1.model_validate(ref)
    try:
        return open_artifact(root, artifact, model=ReviewedCaseV1)
    except ArtifactReadError as error:
        raise InputError(str(error)) from error
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid reviewed case: {error}") from error


def _inventory_from_plan(root: Path, ref: object) -> ChangeImpactInventoryV1:
    plan = load_plan(root, ref)
    return open_artifact(root, plan.impact_inventory_ref, model=ChangeImpactInventoryV1, loader="json")


def _exploration_from_plan(root: Path, ref: object) -> object | None:
    plan = load_plan(root, ref)
    try:
        return load_exploration_document(open_artifact(root, plan.exploration_ref))
    except ArtifactReadError as error:
        if error.reason == "missing":
            return None
        raise


def _surface_model(root: Path, ref: object, *, ui: bool) -> object:
    # Lazy import avoids the generation → intake → quality → execution cycle.
    from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

    model = UiExplorationDocument if ui else ApiDiscoveryDocument
    return open_artifact(root, cast(Any, ref), model=model, loader="json")


def _plan_source(
    root: Path,
    ref: object,
    *,
    resource_id: str,
    path: str,
    format_name: Literal["json", "yaml"],
) -> _MappingDocument:
    plan = load_plan(root, ref)
    try:
        digest = dict(plan.quality_goal.source_resource_digests)[resource_id]
    except KeyError as error:
        raise InputError(f"frozen assurance plan does not bind {resource_id}") from error
    return open_artifact(root, {"path": path, "digest": digest}, model=_MappingDocument, loader=format_name)


def load_case_rework(root: Path, ref: object) -> CaseReworkContextV1:
    artifact = ref if isinstance(ref, EvidenceArtifactRefV1) else EvidenceArtifactRefV1.model_validate(ref)
    try:
        context = open_artifact(root, artifact, model=CaseReworkContextV1)
        previous = context.previous_case
        for item in (*context.assessment_refs, *previous.preparation_refs, *previous.case_refs):
            open_artifact(root, item)
    except ArtifactReadError as error:
        raise InputError(str(error)) from error
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case rework context: {error}") from error
    return context


def note_case_rework(ctx: Any) -> None:
    """Put the loaded rework document on the agent request when this round has one."""
    context = ctx.dep(REWORK_CONTEXT)
    if context is not None:
        ctx.extra("case_rework_context", context.model_dump(mode="json"))


def check_case_rework(value: object, business: BaseModel) -> None:
    """The loaded document must belong to this change and this coverage round.

    ``case_delta_paths`` is still the attempt input. Case-design infers paths later.
    """
    context = value if isinstance(value, CaseReworkContextV1) else CaseReworkContextV1.model_validate(value)
    previous = context.previous_case
    if previous.change_id != getattr(business, "change_id"):
        raise InputError("case rework change_id must match case design")
    if getattr(business, "coverage_epoch") != previous.coverage_epoch + 1:
        raise InputError("case rework must advance coverage_epoch exactly once")
    unauthorized = set(context.target_case_paths) - set(getattr(business, "case_delta_paths", ()))
    if unauthorized:
        raise InputError("case rework targets must be locked by case_delta_paths")


def _plan_ref(business: BaseModel) -> object:
    return getattr(business, "plan_ref")


def _preparation_refs(business: BaseModel) -> object:
    return getattr(business, "preparation_refs", ())


def _optional_field(name: str) -> Any:
    def read(business: BaseModel) -> object:
        return getattr(business, name, None)

    return read


PLAN: ArtifactHandle[ResolvedAssurancePlan] = ArtifactHandle(
    ledger_key="intake.plan",
    slot="plan_ref",
    loader=load_plan,
    same=("change_id", "plan_digest"),
)
# Same ledger key, without the shared case-input check, so case review can
# keep its own mismatch messages after ``ctx.dep``.
REVIEW_PLAN: ArtifactHandle[ResolvedAssurancePlan] = ArtifactHandle(
    ledger_key="intake.plan",
    ref=_plan_ref,
    loader=load_plan,
)
CASE = ArtifactHandle(ledger_key="intake.case", slot="case_refs", many=True)
REVIEWED_CASE: ArtifactHandle[ReviewedCaseV1] = ArtifactHandle(
    ledger_key="intake.reviewed_case",
    slot="reviewed_case_ref",
    loader=load_reviewed_case,
)
PREPARATION = ArtifactHandle(
    ledger_key="intake.preparation",
    ref=_preparation_refs,
    many=True,
)
REWORK_CONTEXT: ArtifactHandle[CaseReworkContextV1] = ArtifactHandle(
    ledger_key="intake.rework",
    slot="rework_ref",
    loader=load_case_rework,
    check=check_case_rework,
    optional=True,
)
CASE_INVENTORY = ArtifactHandle(
    ledger_key="intake.case-inventory",
    ref=_plan_ref,
    loader=_inventory_from_plan,
    same=("change_id",),
)
CASE_EXPLORATION = ArtifactHandle(
    ledger_key="intake.case-exploration",
    ref=_plan_ref,
    loader=_exploration_from_plan,
)
CASE_UI = ArtifactHandle(
    ledger_key="intake.case-ui",
    ref=_optional_field("ui_exploration_ref"),
    loader=lambda root, ref: _surface_model(root, ref, ui=True),
    same=("change_id",),
    optional=True,
)
CASE_API = ArtifactHandle(
    ledger_key="intake.case-api",
    ref=_optional_field("api_discovery_ref"),
    loader=lambda root, ref: _surface_model(root, ref, ui=False),
    same=("change_id",),
    optional=True,
)
CASE_CATALOG = ArtifactHandle(
    ledger_key="intake.case-catalog",
    ref=_plan_ref,
    loader=lambda root, ref: _plan_source(
        root, ref, resource_id=_CATALOG_ID, path=".aa/capability-catalog.json", format_name="json"
    ),
)
CASE_KNOWLEDGE = ArtifactHandle(
    ledger_key="intake.case-knowledge",
    ref=_plan_ref,
    loader=lambda root, ref: _plan_source(
        root, ref, resource_id=_KNOWLEDGE_ID, path=".aa/data-knowledge.yaml", format_name="yaml"
    ),
)
CASE_MARKER = ArtifactHandle(ledger_key="intake.marker", slot="marker_ref", optional=True)
CASE_PROPOSAL = ArtifactHandle(ledger_key="intake.proposal", slot="proposal_ref", optional=True)
CASE_MATRIX = ArtifactHandle(ledger_key="intake.matrix", slot="matrix_ref", optional=True)

__all__ = [
    "CASE",
    "CASE_API",
    "CASE_CATALOG",
    "CASE_EXPLORATION",
    "CASE_INVENTORY",
    "CASE_KNOWLEDGE",
    "CASE_MARKER",
    "CASE_MATRIX",
    "CASE_PROPOSAL",
    "CASE_UI",
    "PLAN",
    "PREPARATION",
    "REVIEWED_CASE",
    "REVIEW_PLAN",
    "REWORK_CONTEXT",
    "check_case_rework",
    "note_case_rework",
    "load_case_rework",
    "load_plan",
    "load_reviewed_case",
]
