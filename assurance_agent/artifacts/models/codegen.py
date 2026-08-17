"""Runtime-completed generated-file manifests for the four codegen layers."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CodegenLayer = Literal["api", "e2e", "fuzz", "performance"]
# Align with ``artifacts.models.generated_files`` (MERGE_HEAD / four-layer).
CodegenDisposition = Literal["generated", "updated", "reused"]
CodegenFileRole = Literal["test_entry", "support", "shared_builder"]


def _safe_project_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith(("/", "\\", "~"))
        or "\\" in value
        or "*" in value
        or "\x00" in value
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise ValueError("repo_path must be a safe project-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("repo_path must be a safe project-relative path")
    return value


class CodegenGeneratedFileAuthoring(BaseModel):
    """Fields supplied by the agent; byte digests are deliberately absent."""

    model_config = _FROZEN

    repo_path: NonEmptyStr
    disposition: CodegenDisposition
    role: CodegenFileRole
    case_ids: tuple[NonEmptyStr, ...]

    @field_validator("repo_path")
    @classmethod
    def _safe_repo_path(cls, value: str) -> str:
        return _safe_project_relative_path(value)


class CodegenGeneratedFileSubmission(CodegenGeneratedFileAuthoring):
    """Compatibility input: older agents may submit a non-authoritative digest."""

    content_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class CodegenGeneratedFile(CodegenGeneratedFileAuthoring):
    """Frozen runtime form with a digest of the exact file bytes."""

    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CodegenGeneratedFilesAuthoring(BaseModel):
    """Agent-authored envelope before runtime byte-hash completion."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[CodegenGeneratedFileAuthoring, ...]


class CodegenGeneratedFilesSubmission(BaseModel):
    """Compatibility parser for the authored envelope."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[CodegenGeneratedFileSubmission, ...]


class CodegenGeneratedFiles(BaseModel):
    """Canonical manifest after deterministic runtime completion."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[CodegenGeneratedFile, ...]

    @model_validator(mode="after")
    def _require_unique_sorted_paths(self) -> Self:
        paths = tuple(item.repo_path for item in self.files)
        if len(paths) != len(set(paths)):
            raise ValueError("files repo_path values must be unique")
        if paths != tuple(sorted(paths)):
            raise ValueError("files must be sorted by repo_path")
        return self


class CodegenMappingEntry(BaseModel):
    """One closed Case ID → symbol → target file row."""

    model_config = _FROZEN

    case_id: NonEmptyStr
    symbol: NonEmptyStr
    target_file: NonEmptyStr

    @field_validator("target_file")
    @classmethod
    def _safe_target_file(cls, value: str) -> str:
        return _safe_project_relative_path(value)


class CodegenMapping(BaseModel):
    """Change-level codegen mapping; source of truth for precommit case_ids."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    layer: CodegenLayer
    entries: tuple[CodegenMappingEntry, ...] = Field(min_length=1)
    schema_case_ids: tuple[NonEmptyStr, ...] | None = None

    @model_validator(mode="after")
    def _unique_case_ids(self) -> Self:
        ids = tuple(item.case_id for item in self.entries)
        if len(ids) != len(set(ids)):
            raise ValueError("entries case_id values must be unique")
        return self


__all__ = [
    "CodegenGeneratedFile",
    "CodegenGeneratedFileAuthoring",
    "CodegenGeneratedFileSubmission",
    "CodegenGeneratedFiles",
    "CodegenGeneratedFilesAuthoring",
    "CodegenGeneratedFilesSubmission",
    "CodegenLayer",
    "CodegenMapping",
    "CodegenMappingEntry",
]
