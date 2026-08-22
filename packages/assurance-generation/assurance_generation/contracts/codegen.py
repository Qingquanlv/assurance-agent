"""Runtime-completed generated-file manifests for the four codegen layers."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CodegenLayer = LayerName
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


class CodegenGeneratedFilesAuthoring(BaseModel):
    """Agent-authored envelope before runtime byte-hash completion."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[CodegenGeneratedFileAuthoring, ...]


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


def _require_exact_leafs(keys: tuple[str, ...], info: ValidationInfo) -> None:
    context = info.context or {}
    leafs = context.get("capability_leafs")
    if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
    for key in keys:
        if key not in leafs:
            raise ValueError(f"unknown capability leaf: {key}")


class CodegenAuthoringV1(CodegenGeneratedFilesAuthoring):
    """Agent-authored codegen document before workspace-byte authentication."""

    mapping: CodegenMapping
    required_capabilities: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _validate_authoring(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.layer:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.layer}")
        return self


class CodegenResultV1(BaseModel):
    """Typed codegen result: hashed generated files plus the closed mapping."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[GeneratedFileEntryV1, ...]
    mapping: CodegenMapping
    required_capabilities: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.layer:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.layer}")
        paths = tuple(entry.repo_path for entry in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("files.repo_path must be canonically sorted")
        return self


class CodegenFixCandidateV1(BaseModel):
    """Approved API/E2E codegen-fix candidate with an authenticated allowed set."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    family: Literal["api", "e2e"]
    baseline_tree_id: NonEmptyStr
    allowed_paths: tuple[NonEmptyStr, ...]
    files: tuple[GeneratedFileEntryV1, ...]
    mapping: CodegenMapping
    required_capabilities: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _validate_fix_candidate(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.family:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.family}")
        allowed = tuple(sorted(set(self.allowed_paths)))
        if self.allowed_paths != allowed:
            raise ValueError("allowed_paths must be sorted and unique")
        for entry in self.files:
            if entry.repo_path not in allowed:
                raise ValueError(f"undeclared generated/modified test file: {entry.repo_path}")
        return self
