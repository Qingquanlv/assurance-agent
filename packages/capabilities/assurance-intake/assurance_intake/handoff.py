"""Artifact handles shared by intake ops.

Op packages cannot import one another, so a consuming op lists the handle defined
here. The producing op exposes the same ledger key through ``op.artifact``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts.ops import ArtifactHandle, InputError
from graph_engine.artifacts import ArtifactReadError, open_artifact

from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.prepare_evidence import evidence_read_error


def load_plan(root: Path, ref: object) -> ResolvedAssurancePlan:
    artifact = ref if isinstance(ref, EvidenceArtifactRefV1) else EvidenceArtifactRefV1.model_validate(ref)
    try:
        return decode_plan(open_artifact(root, artifact), artifact)
    except ArtifactReadError as error:
        raise InputError(evidence_read_error(error)) from error
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error


def plan_matches_case(plan: object, business: BaseModel) -> None:
    if not isinstance(plan, ResolvedAssurancePlan):
        raise InputError("invalid frozen assurance plan")
    if plan.change_id != getattr(business, "change_id", None) or plan.plan_digest != getattr(
        business, "plan_digest", None
    ):
        raise InputError("frozen assurance plan does not match case input")


PLAN: ArtifactHandle[ResolvedAssurancePlan] = ArtifactHandle(
    ledger_key="intake.plan",
    slot="plan_ref",
    loader=load_plan,
    check=plan_matches_case,
)
# Same ledger key, without the shared case-input check, so case review can
# keep its own mismatch messages after ``ctx.dep``.
REVIEW_PLAN: ArtifactHandle[ResolvedAssurancePlan] = ArtifactHandle(
    ledger_key="intake.plan",
    slot="plan_ref",
    loader=load_plan,
)
CASE = ArtifactHandle(ledger_key="intake.case", slot="case_refs", many=True)

__all__ = ["CASE", "PLAN", "REVIEW_PLAN", "load_plan", "plan_matches_case"]
