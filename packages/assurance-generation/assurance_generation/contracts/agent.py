"""Private prepare/finalize request models owned by assurance-generation."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.plans import canonical_relative_path
from assurance_intake.contracts import RiskTier

_SHA256 = r"^[0-9a-f]{64}$"


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def _canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(canonical_relative_path(path) for path in _sorted_unique(values, label="artifact path"))


def _canonical_write_root(path: str) -> str:
    stripped = path.rstrip("/")
    if not stripped:
        raise ValueError("write root must be a non-empty canonical relative prefix")
    canonical_relative_path(stripped)
    if path != stripped and not path.endswith("/"):
        raise ValueError("write root must be a canonical relative prefix")
    return path


class AgentBindingDataV1(FrozenModel):
    execution: FrozenExecutionSelection
    request_policy_digest: str = Field(pattern=_SHA256)
    request_config_digest: str = Field(pattern=_SHA256)


class FamilyConstraintsV1(FrozenModel):
    write_roots: tuple[str, ...]
    operations: tuple[str, ...]
    risks: tuple[RiskTier, ...]

    @field_validator("write_roots")
    @classmethod
    def _write_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        roots = tuple(_canonical_write_root(item) for item in value)
        if not roots:
            raise ValueError("write_roots must be non-empty")
        return roots

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        operations = _sorted_unique(value, label="operation")
        if not operations:
            raise ValueError("operations must be non-empty")
        return operations

    @field_validator("risks")
    @classmethod
    def _risks(cls, value: tuple[RiskTier, ...]) -> tuple[RiskTier, ...]:
        if not value:
            raise ValueError("risks must be non-empty")
        return tuple(sorted(set(value)))


class PlanInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    reviewed_cases: dict[str, Any]
    family_constraints: FamilyConstraintsV1

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("reviewed_cases")
    @classmethod
    def _reviewed_cases(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("reviewed_cases must be a mapping")
        return value


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()
    baseline_tree_id: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)


class CodegenInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    capability_leafs: tuple[str, ...]
    reviewed_plan: dict[str, Any]
    reviewed_cases: dict[str, Any]
    family_constraints: FamilyConstraintsV1
    baseline_tree_id: str = Field(pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("reviewed_plan")
    @classmethod
    def _reviewed_plan(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("reviewed_plan must be a mapping")
        return value

    @field_validator("reviewed_cases")
    @classmethod
    def _reviewed_cases(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("reviewed_cases must be a mapping")
        return value


class CodegenFixInputV1(CodegenInputV1):
    allowed_paths: tuple[str, ...]
    approved_proposal: dict[str, Any]

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = _canonical_relative_paths(value)
        if not paths:
            raise ValueError("allowed_paths must be non-empty")
        return paths

    @field_validator("approved_proposal")
    @classmethod
    def _approved_proposal(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("approved_proposal must be a mapping")
        if value.get("status") != "approved":
            raise ValueError("approved_proposal.status must be approved")
        return value


def under_write_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False
