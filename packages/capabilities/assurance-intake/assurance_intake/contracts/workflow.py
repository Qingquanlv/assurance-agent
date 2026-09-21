"""Versioned workflow contracts shared by the quality-goal loop."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

_SHA256 = r"^[0-9a-f]{64}$"


def _canonical_relative(path: str) -> str:
    posix = PurePosixPath(path)
    if (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError("evidence path must be canonical and relative")
    return path


class EvidenceArtifactRefV1(FrozenModel):
    path: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative(value)


def merge_history_refs(left: object, right: object) -> list[dict[str, str]]:
    """Retain every committed review round across nested graph and retry updates."""
    by_path: dict[str, EvidenceArtifactRefV1] = {}
    for batch in (left, right):
        if batch is None:
            continue
        if not isinstance(batch, (list, tuple)):
            raise TypeError("history refs must be a list")
        for item in batch:
            ref = EvidenceArtifactRefV1.model_validate(item)
            previous = by_path.get(ref.path)
            if previous is not None and previous.digest != ref.digest:
                raise ValueError(f"conflicting history ref for {ref.path}")
            by_path[ref.path] = ref
    return [by_path[path].model_dump(mode="json") for path in sorted(by_path)]


def require_same_plan(
    left_digest: str,
    left_ref: EvidenceArtifactRefV1,
    right_digest: str,
    right_ref: EvidenceArtifactRefV1,
) -> None:
    """Reject any attempt to combine evidence from two frozen plans."""
    if left_digest != right_digest or left_ref != right_ref:
        raise ValueError("plan binding does not match")


def _canonical_refs(
    refs: tuple[EvidenceArtifactRefV1, ...], *, label: str, required: bool = True
) -> tuple[EvidenceArtifactRefV1, ...]:
    if required and not refs:
        raise ValueError(f"{label} must not be empty")
    ordered = tuple(sorted(refs, key=lambda item: (item.path, item.digest)))
    if refs != ordered or len({(item.path, item.digest) for item in refs}) != len(refs):
        raise ValueError(f"{label} must be sorted and unique")
    paths = [item.path for item in refs]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{label} must bind one digest per path")
    return refs


def _case_path(change_id: str, path: str) -> bool:
    del change_id
    parts = PurePosixPath(path).parts
    return len(parts) >= 4 and parts[:2] == ("qa", "cases") and parts[-1] == "case.yaml"


class ReviewedCaseV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    case_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    review_ref: EvidenceArtifactRefV1
    selection_ref: EvidenceArtifactRefV1

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        if len(PurePosixPath(value).parts) != 1:
            raise ValueError("change_id must be a canonical path segment")
        return _canonical_relative(value)

    @field_validator("preparation_refs")
    @classmethod
    def _preparation_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, label="preparation_refs")

    @field_validator("case_refs")
    @classmethod
    def _case_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, label="case_refs")

    @model_validator(mode="after")
    def _refs_match_change(self) -> Self:
        plan_path = f"qa/results/plan/{self.plan_digest}/resolved-assurance-plan.json"
        if self.plan_ref.path != plan_path:
            raise ValueError("plan_ref must bind the current frozen plan")
        if self.plan_ref not in self.preparation_refs:
            raise ValueError("preparation_refs must include plan_ref")
        if any(not _case_path(self.change_id, item.path) for item in self.case_refs):
            raise ValueError("case_refs must contain exact current-change case.yaml paths")
        review_path = "qa/results/review/case-review.json"
        if self.review_ref.path != review_path:
            raise ValueError("review_ref must bind the current case-review.json")
        expected_selection = f"qa/results/cases/epochs/{self.coverage_epoch}/selection.json"
        if self.selection_ref.path != expected_selection:
            raise ValueError("selection_ref must bind the current epoch selection.json")
        return self


class CaseReworkContextV1(FrozenModel):
    previous_case: ReviewedCaseV1
    inspect_receipt: ReceiptRef
    assessment_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    gaps_ref: EvidenceArtifactRefV1
    target_case_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("assessment_refs")
    @classmethod
    def _assessment_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, label="assessment_refs")

    @field_validator("target_case_paths")
    @classmethod
    def _target_case_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted({_canonical_relative(path) for path in value}))
        if value != canonical:
            raise ValueError("target_case_paths must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _targets_match_change(self) -> Self:
        change_id = self.previous_case.change_id
        if any(not _case_path(change_id, path) for path in self.target_case_paths):
            raise ValueError("target_case_paths must be exact current-change case.yaml paths")
        if self.gaps_ref not in self.assessment_refs:
            raise ValueError("gaps_ref must be one of assessment_refs")
        return self


class CaseFlowResultV1(FrozenModel):
    status: Literal["reviewed", "rejected", "exhausted"]
    reviewed_case: ReviewedCaseV1 | None = None
    receipt: ReceiptRef | None = None

    @model_validator(mode="after")
    def _reviewed_requires_evidence(self) -> Self:
        has_evidence = self.reviewed_case is not None and self.receipt is not None
        if self.status == "reviewed" and not has_evidence:
            raise ValueError("reviewed requires case versions and committed review")
        if self.status != "reviewed" and (self.reviewed_case is not None or self.receipt is not None):
            raise ValueError("unsuccessful case flow cannot publish reviewed evidence")
        return self


__all__ = [
    "CaseFlowResultV1",
    "CaseReworkContextV1",
    "EvidenceArtifactRefV1",
    "merge_history_refs",
    "ReviewedCaseV1",
    "require_same_plan",
]
