"""Runtime-completed generated-file manifests for the four codegen layers."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts.agent import under_write_root
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import canonical_relative_path
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CodegenLayer = LayerName
CodegenDisposition = Literal["generated", "updated", "reused"]
CodegenFileRole = Literal["test_entry", "support", "shared_builder"]

FAMILY_TARGET_ROOTS: dict[LayerName, tuple[str, ...]] = {
    "api": ("qa/tests/api/", "qa/tests/testdata/"),
    "e2e": ("qa/tests/e2e/", "qa/tests/testdata/"),
    "fuzz": ("qa/tests/fuzz/", "qa/tests/testdata/"),
    "performance": ("qa/tests/perf/", "qa/tests/testdata/"),
}


def durable_test_path(target_path: str) -> str:
    target = canonical_relative_path(target_path)
    if not target.startswith("qa/tests/"):
        raise ValueError("codegen target_path must start with qa/tests/")
    return target


def staged_generated_path(change_id: str, family: str, target_path: str) -> str:
    """Import compatibility for later-task wheels. Does not rewrite tests/ → qa/tests/."""
    if (
        not change_id
        or change_id in {".", ".."}
        or "/" in change_id
        or "\\" in change_id
        or "\x00" in change_id
    ):
        raise ValueError("change_id must be one canonical path component")
    if family not in LAYER_NAMES:
        raise ValueError(f"unknown generation family: {family}")
    return durable_test_path(target_path)


def family_allows_target(family: LayerName, target_path: str) -> bool:
    return under_write_root(target_path, FAMILY_TARGET_ROOTS[family])


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

    repo_path: NonEmptyStr = Field(description="logical and physical path, must start with `qa/tests/`")
    disposition: CodegenDisposition
    role: CodegenFileRole
    case_ids: tuple[NonEmptyStr, ...]

    @field_validator("repo_path")
    @classmethod
    def _safe_repo_path(cls, value: str) -> str:
        return durable_test_path(_safe_project_relative_path(value))


class CodegenGeneratedFilesAuthoring(BaseModel):
    """Agent-authored envelope before runtime byte-hash completion."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    layer: CodegenLayer
    files: tuple[CodegenGeneratedFileAuthoring, ...] = Field(min_length=1)


class CodegenMappingEntry(BaseModel):
    """One closed Case ID → symbol → target file row."""

    model_config = _FROZEN

    case_id: NonEmptyStr
    symbol: NonEmptyStr
    target_file: NonEmptyStr

    @field_validator("target_file")
    @classmethod
    def _safe_target_file(cls, value: str) -> str:
        return durable_test_path(_safe_project_relative_path(value))


class CodegenMapping(BaseModel):
    """Change-level codegen mapping; source of truth for precommit case_ids."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    layer: CodegenLayer
    entries: tuple[CodegenMappingEntry, ...] = Field(min_length=1)
    schema_case_ids: tuple[NonEmptyStr, ...] | None = None

    @field_validator("entries")
    @classmethod
    def _canonical_entries(cls, value: tuple[CodegenMappingEntry, ...]) -> tuple[CodegenMappingEntry, ...]:
        return tuple(sorted(value, key=lambda item: item.case_id))

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

    @field_validator("required_capabilities")
    @classmethod
    def _canonical_required_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_authoring(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.layer:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.layer}")
        for entry in self.files:
            if not family_allows_target(self.layer, entry.repo_path):
                raise ValueError(f"generated target is outside family policy: {entry.repo_path}")
        for item in self.mapping.entries:
            if not family_allows_target(self.layer, item.target_file):
                raise ValueError(f"generated target is outside family policy: {item.target_file}")
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
