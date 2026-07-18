from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

_OPS = {"gte": lambda v, t: v >= t, "lte": lambda v, t: v <= t, "eq": lambda v, t: v == t}


class RegressionComparison(BaseModel):
    regressed: bool
    has_hard_gate: bool
    inconclusive: bool = False
    has_regression_policy: bool = False
    details: list[str] = Field(default_factory=list)


def _hard_thresholds(suite: dict) -> list[dict]:
    return [t for t in suite.get("thresholds", []) if t.get("gate") == "hard"]


def compare_suite_regression(
    baseline: dict,
    candidate: dict,
    suite: dict,
    *,
    baseline_provenance: dict | None = None,
    candidate_provenance: dict | None = None,
) -> RegressionComparison:
    hard = _hard_thresholds(suite)
    policy = suite.get("regression")
    if isinstance(policy, dict):
        details: list[str] = []
        baseline_provenance = baseline_provenance or {}
        candidate_provenance = candidate_provenance or {}
        policy_sha256 = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        expected = {
            "suite_version": str(suite.get("version", "1")),
            "repeat": policy.get("repeat", 1),
            "regression_policy_sha256": policy_sha256,
        }
        for field, expected_value in expected.items():
            baseline_value = baseline_provenance.get(field)
            candidate_value = candidate_provenance.get(field)
            if expected_value is None or baseline_value is None or candidate_value is None:
                details.append(f"{field}: missing regression provenance")
            elif baseline_value != expected_value or candidate_value != expected_value:
                details.append(
                    f"{field}: expected {expected_value!r}, baseline {baseline_value!r}, "
                    f"candidate {candidate_value!r}"
                )
        metrics = policy.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            details.append("regression.metrics: no metrics configured")
        if details:
            return RegressionComparison(
                regressed=False,
                inconclusive=True,
                has_hard_gate=bool(hard),
                has_regression_policy=True,
                details=details,
            )

        assert isinstance(metrics, dict)
        regressed = False
        for metric, metric_policy in metrics.items():
            base_val = baseline.get(metric)
            cand_val = candidate.get(metric)
            if base_val is None or cand_val is None:
                details.append(f"{metric}: missing baseline or candidate evidence")
                continue
            direction = metric_policy.get("direction") if isinstance(metric_policy, dict) else None
            max_regression = metric_policy.get("max_regression") if isinstance(metric_policy, dict) else None
            if direction not in {"higher_is_better", "lower_is_better"} or not isinstance(
                max_regression, (int, float)
            ):
                details.append(f"{metric}: invalid regression policy")
                continue
            degradation = base_val - cand_val if direction == "higher_is_better" else cand_val - base_val
            if degradation > float(max_regression):
                regressed = True
                details.append(
                    f"{metric}: {base_val} → {cand_val} "
                    f"(regression {degradation:g} > {float(max_regression):g})"
                )
        if details and not regressed:
            return RegressionComparison(
                regressed=False,
                inconclusive=True,
                has_hard_gate=bool(hard),
                has_regression_policy=True,
                details=details,
            )
        return RegressionComparison(
            regressed=regressed,
            has_hard_gate=bool(hard),
            has_regression_policy=True,
            details=details,
        )

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
    return comparison.has_hard_gate and not comparison.regressed and not comparison.inconclusive
