"""Runtime-completed generated-file manifests for the four codegen layers."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts.agent import under_write_root
from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.plans import (
    FuzzStrategyV1,
    PerformanceScenarioV1,
    PlanCoverageRow,
    canonical_relative_path,
    ObligationMethodPlanV1,
)
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CodegenLayer = LayerName
CodegenDisposition = Literal["generated", "updated", "reused"]
CodegenFileRole = Literal["test_entry", "support", "shared_builder"]

FAMILY_DIRS: dict[LayerName, str] = {
    "api": "api",
    "e2e": "e2e",
    "fuzz": "fuzz",
    "performance": "perf",
}

FAMILY_TARGET_ROOTS: dict[LayerName, tuple[str, ...]] = {
    family: (f"qa/tests/{directory}/", f"qa/tests/testdata/{directory}/")
    for family, directory in FAMILY_DIRS.items()
}


def case_module_from_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) < 4 or parts[:2] != ("qa", "cases") or parts[-1] != "case.yaml":
        raise ValueError("case path must be qa/cases/<module>/case.yaml")
    return "/".join(parts[2:-1])


def locked_test_file(family: LayerName, module: str) -> str:
    leaf = PurePosixPath(module).name
    return f"qa/tests/{FAMILY_DIRS[family]}/{module}/test_{leaf}.py"


def locked_testdata_file(family: LayerName, module: str) -> str:
    return f"qa/tests/testdata/{FAMILY_DIRS[family]}/{module}.py"


def durable_test_path(target_path: str) -> str:
    target = canonical_relative_path(target_path)
    if not target.startswith("qa/tests/"):
        raise ValueError("codegen target_path must start with qa/tests/")
    return target


def family_allows_target(family: LayerName, target_path: str) -> bool:
    return under_write_root(target_path, FAMILY_TARGET_ROOTS[family])


class LockedModuleV1(BaseModel):
    model_config = _FROZEN

    module: NonEmptyStr
    case_ids: tuple[NonEmptyStr, ...]
    test_file: NonEmptyStr
    testdata_file: NonEmptyStr


class CodegenScopeV1(BaseModel):
    """Host-built closed set of cases, capabilities, and write roots for codegen."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    family: LayerName
    change_id: NonEmptyStr
    case_ids: tuple[NonEmptyStr, ...]
    required_capabilities: tuple[NonEmptyStr, ...]
    coverage: tuple[PlanCoverageRow, ...]
    write_roots: tuple[NonEmptyStr, ...]
    locked_modules: tuple[LockedModuleV1, ...]
    locked_outputs: tuple[NonEmptyStr, ...]
    fuzz_strategy: FuzzStrategyV1 | None = None
    performance_scenarios: tuple[PerformanceScenarioV1, ...] = ()

    @model_validator(mode="after")
    def _validate_scope(self, info: ValidationInfo) -> Self:
        context = info.context or {}
        leafs = context.get("capability_leafs")
        if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
            raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
        ids = tuple(sorted(set(self.case_ids)))
        if self.case_ids != ids:
            raise ValueError("case_ids must be sorted and unique")
        if not self.case_ids:
            raise ValueError("codegen scope requires at least one reviewed case")
        if not self.coverage:
            raise ValueError("codegen scope must include operation and risk partitions")
        covered = tuple(row.case_id for row in self.coverage)
        if covered != self.case_ids:
            raise ValueError("coverage must include every case_id exactly once in case_ids order")
        if self.write_roots != FAMILY_TARGET_ROOTS[self.family]:
            raise ValueError("write_roots must match the family target roots")
        for key in self.required_capabilities:
            if key not in leafs:
                raise ValueError(f"unknown capability leaf: {key}")
        for row in self.coverage:
            for key in row.required_capabilities:
                if key not in leafs:
                    raise ValueError(f"unknown capability leaf: {key}")
        if self.family == "fuzz":
            if self.fuzz_strategy is None:
                raise ValueError("fuzz scope requires endpoint/property strategy")
        elif self.fuzz_strategy is not None:
            raise ValueError("fuzz_strategy is only valid for fuzz scopes")
        if self.family == "performance":
            if not self.performance_scenarios:
                raise ValueError("performance scope requires scenario identity and numeric thresholds")
            scenario_ids = [scenario.scenario_id for scenario in self.performance_scenarios]
            if len(scenario_ids) != len(set(scenario_ids)):
                raise ValueError("performance scenario_id values must be unique")
            for scenario in self.performance_scenarios:
                if scenario.capability not in leafs:
                    raise ValueError(f"unknown capability leaf: {scenario.capability}")
        elif self.performance_scenarios:
            raise ValueError("performance_scenarios is only valid for performance scopes")
        covered_lock = tuple(sorted(case_id for row in self.locked_modules for case_id in row.case_ids))
        if covered_lock != self.case_ids:
            raise ValueError("locked_modules case_ids must match scope.case_ids exactly once")
        generated = []
        for row in self.locked_modules:
            if row.test_file != locked_test_file(self.family, row.module):
                raise ValueError(f"locked test_file does not match host rule: {row.test_file}")
            if row.testdata_file != locked_testdata_file(self.family, row.module):
                raise ValueError(f"locked testdata_file does not match host rule: {row.testdata_file}")
            generated.extend((row.test_file, row.testdata_file))
        expected_outputs = tuple(
            sorted(
                (
                    *generated,
                    f"qa/results/codegen/{self.family}-codegen-summary.md",
                    f"qa/results/codegen/{self.family}-generated-files.json",
                )
            )
        )
        if self.locked_outputs != expected_outputs:
            raise ValueError("locked_outputs must be the sorted generated files plus codegen sidecars")
        return self


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
    if "capability_leafs" not in context:
        # Shape checks run before a caller supplies the closed leaf set.
        return
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
    method_plans: tuple[ObligationMethodPlanV1, ...] = ()

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
    method_plans: tuple[ObligationMethodPlanV1, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self, info: ValidationInfo) -> Self:
        _require_exact_leafs(self.required_capabilities, info)
        if self.mapping.layer != self.layer:
            raise ValueError(f"mapping layer {self.mapping.layer!r} does not match {self.layer}")
        paths = tuple(entry.repo_path for entry in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("files.repo_path must be canonically sorted")
        return self
