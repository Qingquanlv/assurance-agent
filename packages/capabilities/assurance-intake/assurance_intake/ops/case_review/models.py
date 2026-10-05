"""Case-review op input."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from assurance_intake.contracts.agent import SkillInputV1, canonical_relative_paths
from assurance_intake.contracts.common import SHA256_PATTERN
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.case_delta import refresh_case_attempt_input, validate_case_delta_paths


class CaseReviewInputV1(SkillInputV1):
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(default=0, ge=0)
    review_round: int = Field(default=0, ge=0)
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_delta_paths: tuple[str, ...] = ()
    review_input_paths: tuple[str, ...] = ()
    marker_ref: EvidenceArtifactRefV1 | None = None
    proposal_ref: EvidenceArtifactRefV1 | None = None
    matrix_ref: EvidenceArtifactRefV1 | None = None
    artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    rework_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="before")
    @classmethod
    def _refresh_attempt_input(cls, data: object) -> object:
        return refresh_case_attempt_input(data)

    @field_validator("case_delta_paths", "review_input_paths")
    @classmethod
    def _review_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = canonical_relative_paths(value)
        if canonical != value:
            raise ValueError("case-review paths must be sorted and unique")
        return canonical

    @model_validator(mode="after")
    def _paths_match_change(self) -> CaseReviewInputV1:
        validate_case_delta_paths(self.case_delta_paths)
        if self.review_input_paths:
            change_root = "qa"
            expected = tuple(
                sorted(
                    (
                        f"{change_root}/.qa.yaml",
                        *self.case_delta_paths,
                        f"{change_root}/proposal.md",
                        f"{change_root}/requirement.md",
                        f"{change_root}/results/trace/minimum-coverage-matrix.json",
                    )
                )
            )
            if self.review_input_paths != expected:
                raise ValueError("review_input_paths must exactly match current case-design outputs")
        return self
