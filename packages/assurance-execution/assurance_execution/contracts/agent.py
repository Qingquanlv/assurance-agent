"""Private prepare/finalize and handler request models owned by assurance-execution."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.execution import ExecutionReceiptV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets

_SHA256 = r"^[0-9a-f]{64}$"


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


class AgentBindingDataV1(FrozenModel):
    execution: FrozenExecutionSelection
    request_policy_digest: str = Field(pattern=_SHA256)
    request_config_digest: str = Field(pattern=_SHA256)


class SelectInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    selected_targets: SelectedTargets
    mappings: tuple[dict[str, Any], ...]
    reviewed_cases: dict[str, Any]
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="case id")

    @field_validator("mappings")
    @classmethod
    def _mappings(cls, value: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        if any(not item for item in value):
            raise ValueError("mappings must be non-empty documents")
        return value

    @field_validator("reviewed_cases")
    @classmethod
    def _reviewed_cases(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("reviewed_cases must be a mapping")
        return value


class RunTestsInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    selected_targets: SelectedTargets
    mapping: ClosedMappingV1
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="case id")


class NormalizeInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    selected_targets: SelectedTargets
    mapping: ClosedMappingV1
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)
    command: tuple[str, ...]
    exit_code: int
    report: dict[str, Any] = Field(default_factory=dict)
    receipt: ExecutionReceiptV1 | None = None

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="case id")

    @field_validator("command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("command must be a non-empty argv")
        return value


class SkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    mapping: ClosedMappingV1
    selected_targets: SelectedTargets
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="case id")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


class ExecuteInputV1(SkillInputV1):
    pass


class RunSkillInputV1(SkillInputV1):
    pass


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    mapping: ClosedMappingV1
    selected_targets: SelectedTargets
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="case id")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)
