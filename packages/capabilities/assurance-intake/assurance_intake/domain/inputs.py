"""Validated pieces shared by every intake skill input."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import Field, field_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import TestFamily, validate_family_tuple

SHA256_PATTERN = r"^[0-9a-f]{64}$"


FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


MRC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = sorted_unique(values, label="artifact path")
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


def canonical_test_families(values: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
    return validate_family_tuple(values)


def validate_case_delta_paths(change_id: str, paths: tuple[str, ...]) -> tuple[str, ...]:
    del change_id
    prefix = ("qa", "cases")
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[:2] != prefix or parts[-1] != "case.yaml":
            raise ValueError("case_delta_paths must be exact current-change cases/<module>/case.yaml paths")
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
