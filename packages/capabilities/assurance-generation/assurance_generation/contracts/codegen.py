"""Runtime-completed generated-file manifests for the four codegen layers."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts.agent import under_write_root
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import canonical_relative_path
from assurance_intake.contracts import NonEmptyStr
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan
from assurance_generation.contracts.execution_plan import ValidationProfile

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CodegenLayer = LayerName
CodegenDisposition = Literal["generated", "updated", "reused"]
CodegenFileRole = Literal["test_entry", "support", "shared_builder"]

FAMILY_TARGET_ROOTS: dict[LayerName, tuple[str, ...]] = {
    "api": ("tests/api/", "tests/testdata/"),
    "e2e": ("tests/e2e/", "tests/testdata/"),
    "fuzz": ("tests/fuzz/", "tests/testdata/"),
    "performance": ("tests/perf/", "tests/testdata/"),
}


def staged_generated_path(change_id: str, family: str, target_path: str) -> str:
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
    target = canonical_relative_path(target_path)
    return f"qa/changes/{change_id}/generated/{family}/files/{target}"


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

    repo_path: NonEmptyStr = Field(
        description=(
            "Logical target_path under tests/; physical bytes are staged at "
            "qa/changes/<change-id>/generated/<layer>/files/<repo_path>"
        )
    )
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
        return _safe_project_relative_path(value)


class CodegenMapping(BaseModel):
    """Change-level codegen mapping; source of truth for precommit case_ids."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    layer: CodegenLayer
    entries: tuple[CodegenMappingEntry, ...] = Field(min_length=1)
    schema_case_ids: tuple[NonEmptyStr, ...] | None = None
    validation_profile: ValidationProfile | None = None
    coverage_epoch: int | None = Field(default=None, ge=0)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1 | None = None
    reviewed_case: ReviewedCaseV1 | None = None
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    case_spec_digests: dict[str, str] | None = None

    @model_validator(mode="after")
    def _unique_case_ids(self) -> Self:
        ids = tuple(item.case_id for item in self.entries)
        if len(ids) != len(set(ids)):
            raise ValueError("entries case_id values must be unique")
        bridge_ids = tuple((item.target_file, item.symbol) for item in self.entries)
        if len(bridge_ids) != len(set(bridge_ids)):
            raise ValueError("entries (target_file, symbol) values must be unique")
        verified = (
            self.validation_profile,
            self.coverage_epoch,
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case,
            self.case_execution_plan_ref,
            self.case_execution_plan_digest,
            self.case_spec_digests,
        )
        if any(value is not None for value in verified):
            if any(value is None for value in verified):
                raise ValueError("verified mapping identity must be supplied as one complete group")
            assert self.plan_digest is not None
            assert self.plan_ref is not None
            assert self.reviewed_case is not None
            assert self.coverage_epoch is not None
            assert self.case_execution_plan_ref is not None
            assert self.case_execution_plan_digest is not None
            assert self.case_spec_digests is not None
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
            if self.reviewed_case.coverage_epoch != self.coverage_epoch:
                raise ValueError("verified mapping ReviewedCase epoch does not match coverage epoch")
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("verified mapping machine plan ref and digest do not match")
            if set(self.case_spec_digests) != set(ids):
                raise ValueError("verified mapping spec digests must exactly cover mapped Case IDs")
            if any(not key or key != key.strip() for key in self.case_spec_digests):
                raise ValueError("verified mapping Case IDs must be non-empty canonical strings")
            if any(
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
                for value in self.case_spec_digests.values()
            ):
                raise ValueError("verified mapping spec digest must be a sha256 digest")
        return self

    @property
    def is_verified(self) -> bool:
        return self.validation_profile is not None


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
        for entry in self.files:
            if not family_allows_target(self.layer, entry.repo_path):
                raise ValueError(f"generated target is outside family policy: {entry.repo_path}")
        for item in self.mapping.entries:
            if not family_allows_target(self.layer, item.target_file):
                raise ValueError(f"generated target is outside family policy: {item.target_file}")
        return self


class CodegenRepairPayloadV2(BaseModel):
    """Bounded repair set required when codegen asks for the API/E2E fixer."""

    model_config = _FROZEN

    allowed_paths: tuple[NonEmptyStr, ...] = Field(min_length=1)
    summary: NonEmptyStr


class CodegenResultV2(BaseModel):
    """Versioned API/E2E codegen result with a reachable fixer verdict."""

    model_config = _FROZEN

    schema_version: Literal["2"]
    verdict: Literal["accepted", "needs_fix"]
    change_id: NonEmptyStr
    layer: Literal["api", "e2e"]
    files: tuple[GeneratedFileEntryV1, ...]
    mapping: CodegenMapping
    required_capabilities: tuple[NonEmptyStr, ...] = ()
    repair: CodegenRepairPayloadV2 | None = None

    @model_validator(mode="after")
    def _validate_result(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.layer:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.layer}")
        paths = tuple(entry.repo_path for entry in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("files.repo_path must be canonically sorted")
        if self.verdict == "needs_fix" and self.repair is None:
            raise ValueError("needs_fix codegen requires a repair payload")
        if self.verdict == "accepted" and self.repair is not None:
            raise ValueError("accepted codegen cannot carry a repair payload")
        return self


class CodegenResultV1(BaseModel):
    """Typed codegen result: hashed generated files plus the closed mapping."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    needs_fix: Literal[False] = False
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
