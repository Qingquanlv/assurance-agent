"""Execution manifest, raw result, and command-receipt contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from assurance_execution.contracts.selection import SelectedTargets
from assurance_intake.contracts import CaseId, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ExecutionStatus = Literal["passed", "failed", "skipped"]
ExecutionVerdict = Literal["passed", "failed"]
EXECUTION_VERDICTS: tuple[ExecutionVerdict, ...] = ("failed", "passed")
ExecutionFamily = Literal["api", "e2e", "fuzz", "performance"]
EXECUTION_FAMILIES: tuple[ExecutionFamily, ...] = ("api", "e2e", "fuzz", "performance")


class ExecutionManifest(BaseModel):
    """One published execution batch.

    ``executed_at`` is the batch's authoritative instant and is ``AwareDatetime``:
    a naive value is rejected rather than localized to a guessed zone. It stays
    optional so manifests written before it existed keep loading.
    """

    model_config = _FROZEN

    schema_version: Literal["1.0"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    executed_at: AwareDatetime | None = None
    selected_targets: SelectedTargets
    result_files: dict[str, str]
    tests_tree_sha256: str | None = None
    test_files_sha256: dict[str, str] | None = None
    product_tree_sha256: str | None = None
    final_status: GateStatus | None = None


class RawTestResultV1(BaseModel):
    """One normalized test outcome from a confined runner."""

    model_config = _FROZEN

    test: NonEmptyStr
    status: ExecutionStatus
    duration_ms: int = Field(ge=0)
    message: str = ""
    case_id: CaseId | None = None


class ExecutionCommandReceiptV1(BaseModel):
    """Facts from one selected family's confined execution spawn."""

    model_config = _FROZEN

    family: ExecutionFamily
    command: tuple[NonEmptyStr, ...] = Field(min_length=1)
    exit_code: int
    collected: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts_sum_to_collected(self) -> Self:
        if self.passed + self.failed + self.skipped != self.collected:
            raise ValueError("execution receipt counts must sum to collected")
        expected_runner = "locust" if self.family == "performance" else "pytest"
        forbidden_runner = "pytest" if self.family == "performance" else "locust"
        if expected_runner not in self.command or forbidden_runner in self.command:
            raise ValueError("execution command receipt runner must match its family")
        return self


class ExecutionReceiptV1(BaseModel):
    """One Agent activity containing ordered per-family command receipts."""

    model_config = _FROZEN

    commands: tuple[ExecutionCommandReceiptV1, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _commands_are_non_empty_unique_and_ordered(self) -> Self:
        families = tuple(command.family for command in self.commands)
        if not families:
            raise ValueError("execution command receipts must be non-empty")
        expected = tuple(family for family in EXECUTION_FAMILIES if family in families)
        if families != expected:
            raise ValueError("execution command receipts must be unique and canonically ordered")
        return self

    @property
    def exit_code(self) -> int:
        return next((command.exit_code for command in self.commands if command.exit_code != 0), 0)

    @property
    def collected(self) -> int:
        return sum(command.collected for command in self.commands)

    @property
    def passed(self) -> int:
        return sum(command.passed for command in self.commands)

    @property
    def failed(self) -> int:
        return sum(command.failed for command in self.commands)

    @property
    def skipped(self) -> int:
        return sum(command.skipped for command in self.commands)
