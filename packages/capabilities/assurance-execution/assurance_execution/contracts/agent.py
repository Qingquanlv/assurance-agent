"""Private prepare/finalize and handler request models owned by assurance-execution."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.execution import ExecutionReceiptV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, require_same_plan

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
    agent_profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
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


class ExecutionPrepareInputV1(FrozenModel):
    """Root data from which the execution task locks the test selection."""

    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    selected_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    capability_leafs: tuple[str, ...]
    coverage_epoch: int = Field(default=0, ge=0)
    coverage_epoch_token: str = Field(default="0", min_length=1)
    repair_round: int = Field(default=0, ge=0)
    execution_kind: Literal["execute", "run"] = "execute"
    generation_result: GenerationCycleResultV1 | None = None

    @field_validator("selected_test_families")
    @classmethod
    def _selected_test_families(
        cls,
        value: tuple[Literal["api", "e2e", "fuzz", "performance"], ...],
    ) -> tuple[Literal["api", "e2e", "fuzz", "performance"], ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("selected_test_families must be non-empty and unique")
        order = {"api": 0, "e2e": 1, "fuzz": 2, "performance": 3}
        return tuple(sorted(value, key=order.__getitem__))

    @field_validator("capability_leafs")
    @classmethod
    def _prepare_capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @model_validator(mode="after")
    def _plan_matches_generation(self) -> ExecutionPrepareInputV1:
        if self.coverage_epoch_token != str(self.coverage_epoch):
            raise ValueError("coverage_epoch_token must equal coverage_epoch")
        if self.generation_result is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.generation_result.plan_digest,
                self.generation_result.plan_ref,
            )
        return self


class RunTestsInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    batch_id: str = Field(min_length=1)
    selected_targets: SelectedTargets
    mapping: ClosedMappingV1
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)
    timeout_seconds: int = Field(default=3600, ge=31, le=3600)
    coverage_epoch: int = Field(default=0, ge=0)
    execution_kind: Literal["execute", "run"] = "execute"

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
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
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
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    batch_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    case_ids: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    mapping: ClosedMappingV1
    selected_targets: SelectedTargets
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)
    coverage_epoch: int = Field(default=0, ge=0)
    repair_round: int = Field(default=0, ge=0)
    generation_result: GenerationCycleResultV1 | None = None
    execution_view_root: str
    execution_view_digest: str = Field(pattern=_SHA256)
    executed_at: AwareDatetime

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

    @field_validator("execution_view_root")
    @classmethod
    def _execution_view_root(cls, value: str) -> str:
        return _canonical_relative_paths((value,))[0]


class ExecuteInputV1(SkillInputV1):
    pass


class RunSkillInputV1(SkillInputV1):
    pass


class AgentFinalizeInputV1(SkillInputV1):
    agent_result: AgentRunResult
