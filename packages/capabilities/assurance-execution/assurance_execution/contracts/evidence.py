"""Normalized execution evidence bound to a closed mapping."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from assurance_execution.contracts.execution import (
    EXECUTION_FAMILIES,
    ExecutionReceiptV1,
    RawTestResultV1,
)
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_intake.contracts import EvidenceArtifactRefV1, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class _ExecutionResultBase(BaseModel):
    """Raw execution facts shared by Agent output and committed evidence."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    selected_targets: SelectedTargets
    mapping: ClosedMappingV1
    baseline_tree_id: NonEmptyStr
    runner_profile_digest: NonEmptyStr
    receipt: ExecutionReceiptV1
    results: tuple[RawTestResultV1, ...]

    @model_validator(mode="after")
    def _results_must_be_selected(self) -> Self:
        selected_families = tuple(
            family for family in EXECUTION_FAMILIES if getattr(self.selected_targets, family)
        )
        command_families = tuple(command.family for command in self.receipt.commands)
        if command_families != selected_families:
            raise ValueError(
                "execution command receipts must exactly cover selected targets in canonical order"
            )
        mapping_families = frozenset(entry.layer for entry in self.mapping.mappings)
        if mapping_families != frozenset(selected_families):
            raise ValueError("execution mapping must contain a mapped test for every selected family")
        allowed = frozenset(self.mapping.selected)
        mapping_by_test = {entry.test: entry for entry in self.mapping.mappings}
        seen: list[str] = []
        for result in self.results:
            if result.test not in allowed:
                raise ValueError("execution evidence contains a test outside the closed mapping")
            seen.append(result.test)
        if len(seen) != len(set(seen)) or set(seen) != set(allowed):
            raise ValueError("execution evidence must uniquely cover the closed mapping")
        for command in self.receipt.commands:
            family_results = tuple(
                result for result in self.results if mapping_by_test[result.test].layer == command.family
            )
            counts = {
                status: sum(result.status == status for result in family_results)
                for status in ("passed", "failed", "skipped")
            }
            result_failed = counts["failed"] > 0
            if (command.failed > 0) != result_failed:
                raise ValueError(
                    "execution command receipt failure contradicts the normalized family results"
                )
            if (
                command.collected < len(family_results)
                or command.passed < counts["passed"]
                or command.failed < counts["failed"]
                or command.skipped < counts["skipped"]
            ):
                raise ValueError("execution command receipt counts contradict the normalized family results")
        return self


class ExecutionAgentResultV1(_ExecutionResultBase):
    """Non-authoritative runner facts returned by the execution Agent."""


class ExecutionEvidenceV1(_ExecutionResultBase):
    """Committed execution facts plus Kernel-derived authority fields."""

    status: Literal["passed", "failed"] = "passed"
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    executed_at: AwareDatetime | None = None
    mapping_digest: NonEmptyStr
    receipt_digest: NonEmptyStr
