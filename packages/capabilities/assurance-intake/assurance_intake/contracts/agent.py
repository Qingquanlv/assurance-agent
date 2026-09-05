"""Private prepare/finalize request models owned by assurance-intake."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel
from graph_engine.frozen_json import FrozenJSONValue

from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.common import TestFamily, validate_family_tuple
from assurance_intake.contracts.workflow import (
    CaseReworkContextV1,
    EvidenceArtifactRefV1,
)

_SHA256 = r"^[0-9a-f]{64}$"
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_MRC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def _canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = _sorted_unique(values, label="artifact path")
    for path in paths:
        posix = PurePosixPath(path)
        if (
            posix.is_absolute()
            or "\\" in path
            or (len(path) >= 2 and path[1] == ":")
            or posix.as_posix() != path
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("artifact path must be canonical and relative")
    return paths


def _canonical_test_families(values: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
    return validate_family_tuple(values)


def _validate_case_delta_paths(change_id: str, paths: tuple[str, ...]) -> tuple[str, ...]:
    prefix = ("qa", "changes", change_id, "cases")
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 6 or parts[:4] != prefix or parts[-1] != "case.yaml":
            raise ValueError("case_delta_paths must be exact current-change cases/<module>/case.yaml paths")
    return paths


class AgentBindingDataV1(FrozenModel):
    agent_profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    execution: FrozenExecutionSelection
    request_policy_digest: str = Field(pattern=_SHA256)
    request_config_digest: str = Field(pattern=_SHA256)


class _SkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        posix = PurePosixPath(value)
        if (
            len(posix.parts) != 1
            or posix.is_absolute()
            or "\\" in value
            or posix.as_posix() != value
            or value in {"", ".", ".."}
        ):
            raise ValueError("change_id must be a canonical path segment")
        return value

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


class IntakeInputV1(_SkillInputV1):
    requirement: str = Field(min_length=1)

    @field_validator("requirement")
    @classmethod
    def _requirement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("requirement must be a non-empty string")
        return text


class ExploreInputV1(_SkillInputV1):
    candidate_test_families: tuple[TestFamily, ...] = Field(min_length=1)

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return _canonical_test_families(value)


class ReviewRepairActionV1(FrozenModel):
    finding_id: str = Field(min_length=1)
    artifact: str = Field(min_length=1)
    case_id: str | None = Field(default=None, min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    instructions: tuple[str, ...] = Field(min_length=1)

    @field_validator("artifact")
    @classmethod
    def _artifact(cls, value: str) -> str:
        return _canonical_relative_paths((value,))[0]

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("review repair allowed path must be a non-empty string")
        if cleaned != value or len(value) != len(set(value)):
            raise ValueError("review repair allowed_paths must be trimmed and unique")
        return cleaned

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("review repair instructions must be non-empty strings")
        return cleaned

    @model_validator(mode="after")
    def _artifact_locator_is_bounded(self) -> ReviewRepairActionV1:
        if self.artifact.endswith("/case.yaml"):
            if self.case_id is None:
                raise ValueError("case.yaml repair requires an exact case_id")
            if any(_FIELD_PATH.fullmatch(path) is None for path in self.allowed_paths):
                raise ValueError("case.yaml repair allowed_paths must be dotted field paths")
        elif self.artifact.endswith("/proposal.md"):
            if self.case_id is not None:
                raise ValueError("proposal.md repair case_id must be null")
            if len(self.allowed_paths) != 1 or not self.allowed_paths[0].startswith("## "):
                raise ValueError("proposal.md repair requires one full level-two Markdown heading")
            heading = self.allowed_paths[0]
            if "\n" in heading or "\r" in heading or not heading[3:].strip():
                raise ValueError("proposal.md repair requires one full level-two Markdown heading")
        elif self.artifact.endswith("/trace/minimum-coverage-matrix.json"):
            if self.case_id is not None:
                raise ValueError("minimum coverage matrix repair case_id must be null")
            if any(_MRC_ID.fullmatch(path) is None for path in self.allowed_paths):
                raise ValueError("minimum coverage matrix repair allowed_paths must be exact mrc_id values")
        elif self.artifact.endswith("/.qa.yaml"):
            raise ValueError(".qa.yaml cannot be repaired automatically")
        else:
            raise ValueError("review repair artifact type is not supported")
        return self


class ReviewRepairContractV1(FrozenModel):
    review_path: str = Field(min_length=1)
    review_sha256: str = Field(pattern=_SHA256)
    baseline_file_digests: dict[str, str] = Field(min_length=1)
    baseline_case_documents: FrozenJSONValue = Field(default_factory=dict)
    actions: tuple[ReviewRepairActionV1, ...] = Field(min_length=1)

    @field_validator("review_path")
    @classmethod
    def _review_path(cls, value: str) -> str:
        return _canonical_relative_paths((value,))[0]

    @field_validator("baseline_file_digests")
    @classmethod
    def _baseline_file_digests(cls, value: dict[str, str]) -> dict[str, str]:
        canonical = _canonical_relative_paths(tuple(value))
        if tuple(value) != canonical:
            raise ValueError("baseline_file_digests keys must be sorted canonical paths")
        if any(re.fullmatch(_SHA256, digest) is None for digest in value.values()):
            raise ValueError("baseline_file_digests values must be sha256 digests")
        return value

    @model_validator(mode="after")
    def _actions_match_baseline(self) -> ReviewRepairContractV1:
        targets = {action.artifact for action in self.actions}
        missing = targets.difference(self.baseline_file_digests)
        if missing:
            raise ValueError(f"review repair targets are missing baseline files: {sorted(missing)}")
        identities = [(action.finding_id, action.artifact, action.case_id) for action in self.actions]
        if len(identities) != len(set(identities)):
            raise ValueError("review repair actions must be unique")
        return self


class CaseDesignInputV1(_SkillInputV1):
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(default=0, ge=0)
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_rework_context: CaseReworkContextV1 | None = None
    selected_test_families: tuple[TestFamily, ...] = ()
    case_delta_paths: tuple[str, ...] = Field(min_length=1)
    exploration: ExploreAdvisoryV1 | None = None
    validation_attempt: Literal[0, 1] = 0
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    review_repair: ReviewRepairContractV1 | None = None

    @field_validator("selected_test_families")
    @classmethod
    def _selected_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return _canonical_test_families(value)

    @field_validator("case_delta_paths")
    @classmethod
    def _case_delta_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _canonical_relative_paths(value)
        if canonical != value:
            raise ValueError("case_delta_paths must be sorted and unique")
        return canonical

    @model_validator(mode="after")
    def _case_delta_paths_match_change(self) -> CaseDesignInputV1:
        _validate_case_delta_paths(self.change_id, self.case_delta_paths)
        if self.case_rework_context is not None:
            previous = self.case_rework_context.previous_case
            if previous.change_id != self.change_id:
                raise ValueError("case rework change_id must match case design")
            if self.coverage_epoch != previous.coverage_epoch + 1:
                raise ValueError("case rework must advance coverage_epoch exactly once")
            unauthorized = set(self.case_rework_context.target_case_paths) - set(self.case_delta_paths)
            if unauthorized:
                raise ValueError("case rework targets must be locked by case_delta_paths")
        if self.validation_attempt == 0 and self.validation_error is not None:
            raise ValueError("validation_error is allowed only for the validation repair attempt")
        if self.validation_attempt == 1 and self.validation_error is None:
            raise ValueError("validation_error is required for the validation repair attempt")
        return self


class CaseReviewInputV1(_SkillInputV1):
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(default=0, ge=0)
    review_round: int = Field(default=0, ge=0)
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_delta_paths: tuple[str, ...] = Field(min_length=1)
    review_input_paths: tuple[str, ...] = ()

    @field_validator("case_delta_paths", "review_input_paths")
    @classmethod
    def _review_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _canonical_relative_paths(value)
        if canonical != value:
            raise ValueError("case-review paths must be sorted and unique")
        return canonical

    @model_validator(mode="after")
    def _paths_match_change(self) -> CaseReviewInputV1:
        _validate_case_delta_paths(self.change_id, self.case_delta_paths)
        if self.review_input_paths:
            change_root = f"qa/changes/{self.change_id}"
            expected = tuple(
                sorted(
                    (
                        f"{change_root}/.qa.yaml",
                        *self.case_delta_paths,
                        f"{change_root}/proposal.md",
                        f"{change_root}/requirement.md",
                        f"{change_root}/trace/minimum-coverage-matrix.json",
                    )
                )
            )
            if self.review_input_paths != expected:
                raise ValueError("review_input_paths must exactly match current case-design outputs")
        return self


class ArtifactListResultV1(FrozenModel):
    output_files: tuple[str, ...] = Field(min_length=1)

    @field_validator("output_files")
    @classmethod
    def _output_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


class ArtifactDigestV1(FrozenModel):
    path: str
    digest: str = Field(pattern=_SHA256)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative_paths((value,))[0]


class FinalizedArtifactsV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[ArtifactDigestV1, ...]) -> tuple[ArtifactDigestV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("finalized artifact paths must be sorted and unique")
        return value


class CaseDesignOutputV1(FrozenModel):
    validation_status: Literal["pass", "needs_fix"]
    validation_attempt: Literal[0, 1]
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)
    artifacts: tuple[ArtifactDigestV1, ...] = ()
    review_repair: ReviewRepairContractV1 | None = None

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[ArtifactDigestV1, ...]) -> tuple[ArtifactDigestV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("case-design artifact paths must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _status_matches_error(self) -> CaseDesignOutputV1:
        if self.validation_status == "pass" and self.validation_error is not None:
            raise ValueError("passing case design cannot carry validation_error")
        if self.validation_status == "needs_fix" and (
            self.validation_attempt != 1 or self.validation_error is None
        ):
            raise ValueError("case design needing repair requires attempt 1 and validation_error")
        return self


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    change_id: str | None = Field(default=None, min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    selected_test_families: tuple[TestFamily, ...] = ()
    case_delta_paths: tuple[str, ...] = ()
    validation_attempt: Literal[0, 1] = 1
    review_repair: ReviewRepairContractV1 | None = None
    coverage_epoch: int = Field(default=0, ge=0)
    review_round: int = Field(default=0, ge=0)
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    case_rework_context: CaseReworkContextV1 | None = None

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("selected_test_families")
    @classmethod
    def _selected_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return _canonical_test_families(value)

    @field_validator("case_delta_paths")
    @classmethod
    def _case_delta_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            return ()
        canonical = _canonical_relative_paths(value)
        if canonical != value:
            raise ValueError("case_delta_paths must be sorted and unique")
        return canonical

    @model_validator(mode="after")
    def _case_delta_paths_match_change(self) -> AgentFinalizeInputV1:
        if self.case_delta_paths:
            if self.change_id is None:
                raise ValueError("change_id is required with case_delta_paths")
            _validate_case_delta_paths(self.change_id, self.case_delta_paths)
        return self


class CaseFinalizeInputV1(AgentFinalizeInputV1):
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
