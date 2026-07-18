from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

Gate = Literal["hard", "advisory", "observe"]
CmpOp = Literal["gte", "lte", "eq"]
EvalVerdict = Literal["pass", "pass_with_warnings", "fail", "inconclusive", "needs_human_review"]
RegressionDirection = Literal["higher_is_better", "lower_is_better"]


class RegressionMetricPolicy(BaseModel):
    direction: RegressionDirection
    max_regression: float = Field(ge=0.0)


class RegressionPolicy(BaseModel):
    repeat: int = Field(default=1, ge=1)
    metrics: dict[str, RegressionMetricPolicy] = Field(default_factory=dict)


def regression_policy_sha256(policy: RegressionPolicy | None) -> str | None:
    if policy is None:
        return None
    payload = json.dumps(
        policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class DatasetSample(BaseModel):
    id: str
    suite: str
    input: dict = Field(default_factory=dict)
    expected: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    annotation_source: Literal["human", "synthetic"] = "synthetic"
    mock_judge_label: str | None = None


class SuiteThreshold(BaseModel):
    metric: str
    gate: Gate
    op: CmpOp
    value: float


class JudgeConfig(BaseModel):
    model: str
    prompt_ref: str | None = None
    api_url: str | None = None
    api_key_env: str = "AA_JUDGE_API_KEY"
    temperature: float = 0.0
    confidence_threshold: float = 0.6


class EvalSuite(BaseModel):
    name: str
    version: str = "1"
    executor: dict = Field(default_factory=dict)
    scorer: str
    thresholds: list[SuiteThreshold] = Field(default_factory=list)
    dataset_dir: str | None = None
    judge: JudgeConfig | None = None
    regression: RegressionPolicy | None = None


class SampleScore(BaseModel):
    sample_id: str
    status: Literal["ok", "error"] = "ok"
    metrics: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
    notes: dict[str, str | float] = Field(default_factory=dict)


class SuiteMetrics(BaseModel):
    run_id: str
    suite: str
    sample_count: int
    metrics: dict[str, float] = Field(default_factory=dict)
    per_sample: dict[str, dict[str, float]] = Field(default_factory=dict)


class EvalGateResult(BaseModel):
    run_id: str
    suite: str
    verdict: EvalVerdict
    hard_gate_failures: list[str] = Field(default_factory=list)
    threshold_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    inconclusive_count: int = 0
    checked: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    suite: str
    scorer: str
    selected_sample_ids: list[str] = Field(default_factory=list)
    total_samples: int = 0
    executed_samples: int = 0
    target_model: str = "unknown"
    suite_version: str = "1"
    repeat: int = 1
    regression_policy_sha256: str | None = None
    memory_overlay_sha256: str | None = None
    started_at: str
    completed_at: str | None = None


class JudgeOutput(BaseModel):
    label: Literal["covered", "partial", "missing", "hallucinated"]
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    needs_human_review: bool = False


class ExecutionResult(BaseModel):
    sample_id: str
    attempt: int
    executor: str
    status: Literal["ok", "error"]
    exit_code: int | None = None
    error: str | None = None
    extra: dict = Field(default_factory=dict)
