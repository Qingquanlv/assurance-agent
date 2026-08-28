"""Execution manifest, raw result, and command-receipt contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from assurance_execution.contracts.selection import SelectedTargets
from assurance_intake.contracts import CaseId, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"]
ExecutionStatus = Literal["passed", "failed", "skipped"]


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


class ExecutionReceiptV1(BaseModel):
    """Command receipt for one confined execution spawn."""

    model_config = _FROZEN

    command: tuple[NonEmptyStr, ...]
    exit_code: int
    collected: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
