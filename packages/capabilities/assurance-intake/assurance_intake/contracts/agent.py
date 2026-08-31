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

_SHA256 = r"^[0-9a-f]{64}$"
_FAMILY_ORDER = ("api", "e2e", "fuzz", "performance")
TestFamily = Literal["api", "e2e", "fuzz", "performance"]
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


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
    if len(values) != len(set(values)):
        raise ValueError("selected_test_families must not contain duplicates")
    expected = tuple(family for family in _FAMILY_ORDER if family in values)
    if values != expected:
        raise ValueError("selected_test_families must use canonical family order")
    return values


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
    pass


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
        cleaned = _sorted_unique(value, label="review repair allowed path")
        if cleaned != value:
            raise ValueError("review repair allowed_paths must be sorted and unique")
        return cleaned

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("review repair instructions must be non-empty strings")
        return cleaned

    @model_validator(mode="after")
    def _case_allowed_paths_are_fields(self) -> ReviewRepairActionV1:
        if self.artifact.endswith("/case.yaml") and any(
            _FIELD_PATH.fullmatch(path) is None for path in self.allowed_paths
        ):
            raise ValueError("case.yaml repair allowed_paths must be dotted field paths")
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
        if self.validation_attempt == 0 and self.validation_error is not None:
            raise ValueError("validation_error is allowed only for the validation repair attempt")
        if self.validation_attempt == 1 and self.validation_error is None:
            raise ValueError("validation_error is required for the validation repair attempt")
        return self


class CaseReviewInputV1(_SkillInputV1):
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


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    change_id: str | None = Field(default=None, min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    selected_test_families: tuple[TestFamily, ...] = ()
    case_delta_paths: tuple[str, ...] = ()
    validation_attempt: Literal[0, 1] = 1
    review_repair: ReviewRepairContractV1 | None = None

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
