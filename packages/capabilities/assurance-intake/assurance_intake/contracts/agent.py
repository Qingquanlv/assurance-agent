"""Agent op contracts shared by every intake op: the input shell and the finalized artifacts."""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field, field_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import SHA256_PATTERN, is_canonical_relative


def sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = sorted_unique(values, label="artifact path")
    if not all(is_canonical_relative(path) for path in paths):
        raise ValueError("artifact path must be canonical and relative")
    return paths


class SkillInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

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
        return sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return canonical_relative_paths(value)


class ArtifactDigestV1(FrozenModel):
    path: str
    digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return canonical_relative_paths((value,))[0]


class FinalizedArtifactsV1(FrozenModel):
    artifacts: tuple[ArtifactDigestV1, ...] = Field(min_length=1)


__all__ = [
    "ArtifactDigestV1",
    "FinalizedArtifactsV1",
    "SkillInputV1",
    "canonical_relative_paths",
    "sorted_unique",
]
