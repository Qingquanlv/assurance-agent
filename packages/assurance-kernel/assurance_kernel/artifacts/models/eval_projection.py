"""Compact immutable Eval evidence published for Retro consumption."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")
EvalProjectionVerdict = Literal["pass", "pass_with_warnings", "fail", "inconclusive", "needs_human_review"]


class EvalRunProjection(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    run_id: str = Field(min_length=1)
    suite: str = Field(min_length=1)
    verdict: EvalProjectionVerdict
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    source_change_ids: tuple[str, ...]
    failure_signature: str | None = None
    sample_ids: tuple[str, ...]
    raw_report_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


__all__ = ["EvalProjectionVerdict", "EvalRunProjection"]
