"""Private prepare/finalize request models owned by assurance-intake."""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field, field_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel

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


class _SkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]

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


class CaseDesignInputV1(_SkillInputV1):
    pass


class CaseReviewInputV1(_SkillInputV1):
    pass


class ArtifactListResultV1(FrozenModel):
    output_files: tuple[str, ...]

    @field_validator("output_files")
    @classmethod
    def _output_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)
