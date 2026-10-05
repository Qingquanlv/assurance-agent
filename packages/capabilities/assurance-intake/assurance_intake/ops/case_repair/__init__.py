"""Case repair: apply a needs_fix review's bounded actions to the committed case-design outputs."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Dir, Finalize, Out, Prepare

from assurance_intake.contracts import CaseYamlAuthoring, MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.cases import QaYaml
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.domain.case_delta import MARKER_PATH, MATRIX_PATH, PROPOSAL_PATH
from assurance_intake.handoff import (
    CASE,
    CASE_API,
    CASE_CATALOG,
    CASE_EXPLORATION,
    CASE_INVENTORY,
    CASE_KNOWLEDGE,
    CASE_MARKER,
    CASE_MATRIX,
    CASE_PROPOSAL,
    CASE_UI,
    PLAN,
    PREPARATION,
    REWORK_CONTEXT,
)
from assurance_intake.ops import router
from assurance_intake.ops.case_repair import hooks
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, CaseRepairOutputV1
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.agent(
    "case-repair",
    transport_business=True,
    input=CaseRepairInputV1,
    prepare=Prepare(
        hook=hooks.before,
        depends=(
            PLAN,
            REWORK_CONTEXT,
            CASE,
            PREPARATION,
            CASE_INVENTORY,
            CASE_EXPLORATION,
            CASE_UI,
            CASE_API,
            CASE_CATALOG,
            CASE_KNOWLEDGE,
            CASE_MARKER,
            CASE_PROPOSAL,
            CASE_MATRIX,
        ),
        eager_artifacts=True,
        reads=(
            Out("review", hooks._REVIEW_PATH, model=CaseReviewResultV1, format="json"),
            Out("baseline_marker", MARKER_PATH, model=QaYaml, format="yaml"),
            Out("baseline_proposal", PROPOSAL_PATH),
            Out("baseline_matrix", MATRIX_PATH, model=MinimumCoverageMatrixAuthoring, format="json"),
            Dir(
                "qa/cases",
                files=lambda business: business.case_delta_paths,
                model=CaseYamlAuthoring,
                format="yaml",
                context=lambda business: {
                    "capability_leafs": frozenset(getattr(business, "capability_leafs"))
                },
            ),
        ),
    ),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-case-repair",
        writes=(
            Out("marker", MARKER_PATH, model=QaYaml, format="yaml"),
            Out("proposal", PROPOSAL_PATH),
            Out("matrix", MATRIX_PATH, model=MinimumCoverageMatrixAuthoring, format="json"),
            Dir(
                "qa/cases",
                name="case",
                files=lambda business: business.case_delta_paths,
                model=CaseYamlAuthoring,
                format="yaml",
                context=lambda business: {
                    "capability_leafs": frozenset(getattr(business, "capability_leafs")),
                    "inventory": getattr(business, "impact_inventory"),
                },
            ),
        ),
        strict_files=True,
        baseline_digests=lambda business: (
            getattr(business, "review_repair").baseline_file_digests
            if getattr(business, "review_repair", None) is not None
            else {}
        ),
    ),
    finalize=Finalize(hook=hooks.after, artifacts="auto"),
    output=CaseRepairOutputV1,
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)

__all__ = ["CaseRepairInputV1", "CaseRepairOutputV1", "op"]
