from __future__ import annotations

from pydantic import BaseModel, Field

_OPS = {"gte": lambda v, t: v >= t, "lte": lambda v, t: v <= t, "eq": lambda v, t: v == t}


class RegressionComparison(BaseModel):
    regressed: bool
    has_hard_gate: bool
    details: list[str] = Field(default_factory=list)


def _hard_thresholds(suite: dict) -> list[dict]:
    return [t for t in suite.get("thresholds", []) if t.get("gate") == "hard"]


def compare_suite_regression(baseline: dict, candidate: dict, suite: dict) -> RegressionComparison:
    hard = _hard_thresholds(suite)
    details: list[str] = []
    regressed = False
    for threshold in hard:
        metric = threshold["metric"]
        base_val = baseline.get(metric)
        cand_val = candidate.get(metric)
        if base_val is None or cand_val is None:
            regressed = True
            details.append(f"{metric}: missing baseline or candidate evidence")
            continue
        op = _OPS[threshold["op"]]
        if op(base_val, threshold["value"]) and not op(cand_val, threshold["value"]):
            regressed = True
            details.append(f"{metric}: {base_val} → {cand_val}")
    return RegressionComparison(regressed=regressed, has_hard_gate=bool(hard), details=details)


def classify_eval_gate(gate: dict) -> str:
    verdict = gate.get("verdict")
    allowed = {"pass", "pass_with_warnings", "fail", "inconclusive", "needs_human_review"}
    return str(verdict) if verdict in allowed else "inconclusive"


def should_auto_apply(comparison: RegressionComparison, suite: dict) -> bool:
    return comparison.has_hard_gate and not comparison.regressed
