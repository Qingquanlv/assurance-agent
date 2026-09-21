"""Normalized execution evidence bound to a closed mapping."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from assurance_execution.contracts.execution import (
    EXECUTION_FAMILIES,
    ExecutionFamily,
    ExecutionReceiptV1,
    RawTestResultV1,
)
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_intake.contracts import EvidenceArtifactRefV1, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class FamilyExecutionOutcomeV1(BaseModel):
    model_config = _FROZEN
    family: ExecutionFamily
    state: Literal["executed", "blocked"]
    reason_code: (
        Literal[
            "runner_unsupported",
            "expectation_unconfirmed",
            "environment_unavailable",
            "collection_failed",
            "execution_interrupted",
        ]
        | None
    ) = None
    diagnostic_refs: tuple[EvidenceArtifactRefV1, ...] = ()

    @model_validator(mode="after")
    def _state_matches_reason(self) -> Self:
        if self.state == "executed":
            if self.reason_code is not None:
                raise ValueError("executed family cannot carry a block reason")
            return self
        if self.reason_code is None or not self.diagnostic_refs:
            raise ValueError("blocked family requires a reason and diagnostic refs")
        return self


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
    # Stated by the producer, never inferred here. Validation must leave the
    # sealed document byte-identical so its digest still names the bytes.
    family_outcomes: tuple[FamilyExecutionOutcomeV1, ...]

    @model_validator(mode="after")
    def _results_must_be_selected(self) -> Self:
        selected_families = tuple(
            family for family in EXECUTION_FAMILIES if getattr(self.selected_targets, family)
        )
        outcomes = self.family_outcomes
        outcome_families = tuple(item.family for item in outcomes)
        if outcome_families != selected_families:
            raise ValueError("family_outcomes must cover selected targets in canonical order")
        executed = tuple(item.family for item in outcomes if item.state == "executed")
        command_families = tuple(command.family for command in self.receipt.commands)
        if command_families != executed:
            raise ValueError("execution command receipts must exactly cover executed families")
        mapping_families = frozenset(entry.layer for entry in self.mapping.mappings)
        if mapping_families != frozenset(selected_families):
            raise ValueError("execution mapping must contain a mapped test for every selected family")
        mapping_by_test = {entry.test: entry for entry in self.mapping.mappings}
        executed_tests = frozenset(entry.test for entry in self.mapping.mappings if entry.layer in executed)
        seen: list[str] = []
        for result in self.results:
            if result.test not in executed_tests:
                raise ValueError("execution evidence contains a test outside the executed mapping")
            seen.append(result.test)
        if len(seen) != len(set(seen)) or set(seen) != set(executed_tests):
            raise ValueError("execution evidence must uniquely cover executed mapping entries")
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
    observations_ref: EvidenceArtifactRefV1 | None = None
    mapping_digest: NonEmptyStr
    receipt_digest: NonEmptyStr
