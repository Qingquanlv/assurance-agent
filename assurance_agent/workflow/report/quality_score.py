"""Deterministic Quality Score (M1 + M3 weights). CLI is the only scorer.

Inactive dimensions are dropped and remaining weights renormalised so active
dimensions can still reach 100. Transcribed from TS quality_score.ts.
"""

from pydantic import BaseModel

from assurance_agent.artifacts.models import QualityScoreBreakdown

_KEYS = ("functional", "coverage", "fuzz", "performance")


class ScoreDimension(BaseModel):
    active: bool
    ratio: float
    weight: float


def _clamp01(n: float) -> float:
    if n != n:  # NaN
        return 0.0
    return max(0.0, min(1.0, n))


def _round1(n: float) -> float:
    return round(n * 10) / 10


def compute_quality_score(
    dims: dict[str, ScoreDimension],
) -> tuple[int, QualityScoreBreakdown]:
    active_weight = sum(dims[k].weight for k in _KEYS if dims[k].active)
    parts: dict[str, float | str] = {k: "N/A" for k in _KEYS}
    if active_weight <= 0:
        return 0, QualityScoreBreakdown.model_validate(parts)

    total = 0.0
    for key in _KEYS:
        dim = dims[key]
        if not dim.active:
            continue
        points = (dim.weight / active_weight) * 100 * _clamp01(dim.ratio)
        parts[key] = _round1(points)
        total += points
    return round(total), QualityScoreBreakdown.model_validate(parts)
