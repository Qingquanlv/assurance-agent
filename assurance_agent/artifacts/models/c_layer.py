"""inspect/metrics-c-layer.json (must_compat): report-only C1/C2/C3 vectors.

Option A: a dedicated document **outside** ``MetricKey`` / ``MetricsDocument`` so
C-layer rates never enter ``metrics-sufficiency-gate`` floors or single-change
verdict. Cadence is report-only (retro / long-horizon); zero denominator is
``not_evaluated`` with ``rate=None`` — never vacuous ``0/0 = 1.0``, never a
``confidence`` alias.

Four independent vectors (C2 is two fields, never one merged ratio):

- ``escape_rate`` — C1 human-confirmed escapes / human-confirmed analyses
- ``counterexample_promotion_rate`` — C2 CE→case via applied promotion receipts
- ``coverage_gap_closure_rate`` — C2 gap identities closed across batches
- ``seed_replay_stability`` — C3 seed-replay success / attempts
"""

from __future__ import annotations

import math
from typing import Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

C_LAYER_METRICS_REL = "inspect/metrics-c-layer.json"

CLayerSchemaVersion = Literal["1"]
CLayerCadence = Literal["report"]
CLayerVectorStatus = Literal["evaluated", "not_evaluated"]

CLayerVectorKey = Literal[
    "escape_rate",
    "counterexample_promotion_rate",
    "coverage_gap_closure_rate",
    "seed_replay_stability",
]
C_LAYER_VECTOR_KEYS: tuple[CLayerVectorKey, ...] = get_args(CLayerVectorKey)


class CLayerMetricEntry(BaseModel):
    """One independent C-layer rate vector.

    ``evaluated`` requires a positive denominator and a rate that restates the
    tally. ``not_evaluated`` always has ``rate=None``:

    - missing feedstock → ``numerator`` / ``denominator`` are ``None`` (never
      invented zeros)
    - present feedstock with empty denominator → ``0`` / ``0``, still
      ``not_evaluated``
    """

    model_config = _FROZEN

    status: CLayerVectorStatus
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    rate: float | None = None
    evidence_digests: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _status_and_tally_agree(self) -> Self:
        if self.status == "evaluated":
            if self.numerator is None or self.denominator is None:
                raise ValueError("evaluated C-layer entry requires numerator and denominator")
            if self.denominator < 1:
                raise ValueError("evaluated C-layer entry requires denominator >= 1")
            if self.numerator > self.denominator:
                raise ValueError(f"numerator={self.numerator} exceeds denominator={self.denominator}")
            expected = self.numerator / self.denominator
            if self.rate is None or not math.isclose(self.rate, expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"rate={self.rate} contradicts {self.numerator}/{self.denominator}")
            return self

        # not_evaluated
        if self.rate is not None:
            raise ValueError("not_evaluated C-layer entry must have rate=None")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError(
                "not_evaluated entry must set numerator and denominator together "
                "(both None for missing feedstock, or both 0 for empty denom)"
            )
        if self.denominator is not None and self.denominator != 0:
            raise ValueError(
                f"not_evaluated with a tally requires denominator=0 (got {self.numerator}/{self.denominator})"
            )
        if self.numerator is not None and self.numerator != 0:
            raise ValueError("not_evaluated empty-denom tally must be 0/0")
        return self


class CLayerMetricsDocument(BaseModel):
    """Authoritative report-only C-layer metrics at ``inspect/metrics-c-layer.json``."""

    model_config = _FROZEN

    schema_version: CLayerSchemaVersion
    change_id: NonEmptyStr
    cadence: CLayerCadence = "report"
    computed_at: AwareDatetime
    escape_rate: CLayerMetricEntry
    counterexample_promotion_rate: CLayerMetricEntry
    coverage_gap_closure_rate: CLayerMetricEntry
    seed_replay_stability: CLayerMetricEntry

    def vector(self, key: CLayerVectorKey) -> CLayerMetricEntry:
        """Lookup by closed vector key (no MetricKey coupling)."""
        return getattr(self, key)


__all__ = [
    "C_LAYER_METRICS_REL",
    "C_LAYER_VECTOR_KEYS",
    "CLayerCadence",
    "CLayerMetricEntry",
    "CLayerMetricsDocument",
    "CLayerSchemaVersion",
    "CLayerVectorKey",
    "CLayerVectorStatus",
]
