"""Strict, layer-specific generated-file manifests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr, model_validator

_SHA256_PREFIXED_LENGTH = len("sha256:") + 64


class StrictWireModel(BaseModel):
    """Immutable, non-coercing base for canonical JSON wire artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _validate_repo_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("must be a normalized repository-relative POSIX path")
    return value


def _validate_prefixed_sha256(value: str) -> str:
    if (
        len(value) != _SHA256_PREFIXED_LENGTH
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value.removeprefix("sha256:"))
    ):
        raise ValueError("must be a lowercase sha256:<64-hex> digest")
    return value


def _validate_canonical_strings(values: Sequence[str], *, label: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{label} must not contain empty values")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    if values != sorted(values):
        raise ValueError(f"{label} must be canonically sorted")


class GeneratedFileEntryV1(StrictWireModel):
    repo_path: StrictStr
    disposition: Literal["generated", "updated", "reused"]
    role: Literal["test_entry", "support", "shared_builder"]
    case_ids: list[StrictStr]
    content_sha256: str

    @model_validator(mode="after")
    def validate_canonical_entry(self) -> GeneratedFileEntryV1:
        _validate_repo_path(self.repo_path)
        _validate_canonical_strings(self.case_ids, label="case_ids")
        _validate_prefixed_sha256(self.content_sha256)
        return self


class GeneratedFilesV1(StrictWireModel):
    schema_version: Literal["1"]
    change_id: str
    files: list[GeneratedFileEntryV1]

    @model_validator(mode="after")
    def validate_canonical_files(self) -> GeneratedFilesV1:
        paths = [entry.repo_path for entry in self.files]
        _validate_canonical_strings(paths, label="files.repo_path")
        return self


class ApiGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["api"]


class E2eGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["e2e"]


class FuzzGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["fuzz"]


class PerformanceGeneratedFilesV1(GeneratedFilesV1):
    layer: Literal["performance"]
