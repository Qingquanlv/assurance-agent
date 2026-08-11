""".aa/policy.yaml — 组织策略常量表（spec C3 / C3b）。

v1 只做常量，不做规则语言、不做 per-path 匹配、不做继承：gate DSL 本身已是条件
语言，policy 只当它的常量源。C3b 将 plan check 处置细化为 check_id → action 对照表，
并加入 coverage / fuzz / healing 阶段常量。

`evidence_sufficiency` 一块横跨两侧，两侧的分工是这个字段唯一需要记住的事：
**计算**在 Python（recency 算术、按 case type 取 required kinds；floors/cadence
裁决在 ``evidence.metrics_sufficiency``），产出的是事实而非路由；**处置**在 gate
DSL —— `trace-sufficiency-gate` 直接读 `policy.evidence_sufficiency.on_insufficient`，
把 case 证据充分性事实映射成 pass/needs_human_review/stop。`floors` / `cadence` /
`mutation_budget_seconds` 与数值 floor 的 `on_insufficient` 处置由
`evaluate_metrics_sufficiency` 消费（§6 warn→pass+shortboards / require_human→
needs_human / block→stop），待 Task 8 的 `metrics-sufficiency-gate` 接线；顶层
字段仍由 trace gate 的 DSL 引用满足「有 gate 消费者」守卫。它带字段级默认值，
因此不含该块的历史 policy 文件仍然可加载；默认值与打包 policy-default.yaml
逐字一致。

`coverage_floor` 已 deprecated：由 `evidence_sufficiency.floors` 取代，包内保留以
兼容旧 policy，禁止新增消费（见 test_plan_check_gate 守卫）。
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.assurance import KNOWN_PLAN_CHECK_IDS
from assurance_agent.artifacts.models.common import RiskTier
from assurance_agent.artifacts.models.metrics import (
    METRIC_SHAPES,
    METRIC_SURFACES,
    MetricCadence,
    MetricKey,
    MetricLayer,
)
from assurance_agent.artifacts.models.trace import TraceCaseType

PlanCheckAction = Literal["warn", "block", "require_human"]

# 证据 kind：`covered`/`fuzz_run`/`perf_run` 直接读投影事实，`execution_recent`/
# `pass_status` 由评估期按 as_of 与 recency_hours 派生。
EvidenceKind = Literal["covered", "execution_recent", "fuzz_run", "perf_run", "pass_status"]

# Which measurement a floor judges on a MetricEntry. Canonical MetricKey only —
# scope is never encoded in the key name (no `*_touched` variants; Task 1 standing
# decision). `holds` is for boolean metrics; `value`/`touched` for numeric ones.
FloorTarget = Literal["value", "touched", "holds"]


class CoverageFloor(BaseModel):
    """Deprecated: superseded by ``evidence_sufficiency.floors``. Kept for compat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_high: float
    risk_medium: float


class FuzzPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_when_endpoint_has_auth: bool


class HealingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auth_module: PlanCheckAction


class MetricFloor(BaseModel):
    """One floor entry: canonical metric key is the map key; this is the threshold.

    Exactly one of ``min`` / ``must_hold``; ``target == "holds"`` iff ``must_hold``
    is set. Optional ``surface`` lets a multi-surface metric (today only
    ``assertion_strength``) be judged on one classifier instead of the pooled
    aggregate — deferred use is fine; the field is allowed so Task 8 need not
    reshape policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: FloorTarget
    min: float | None = None
    must_hold: bool | None = None
    surface: MetricLayer | None = None

    @model_validator(mode="after")
    def _exactly_one_threshold_and_target_agrees(self) -> MetricFloor:
        has_min = self.min is not None
        has_hold = self.must_hold is not None
        if has_min == has_hold:
            raise ValueError("exactly one of min / must_hold is required")
        if (self.target == "holds") != has_hold:
            raise ValueError("target must be 'holds' if and only if must_hold is set")
        return self


class MetricCadenceSchedule(BaseModel):
    """Which metrics each cadence collects. Keys match ``MetricsDocument.cadence``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: list[MetricKey]
    nightly: list[MetricKey]

    @field_validator("pr", "nightly")
    @classmethod
    def _no_duplicate_keys(cls, value: list[MetricKey]) -> list[MetricKey]:
        if len(value) != len(set(value)):
            raise ValueError("cadence lists must not repeat a metric key")
        return value


def _default_floors() -> dict[RiskTier, dict[MetricKey, MetricFloor]]:
    """Packaged floor bands (§8 + Task 1 standing decision: canonical key + target)."""
    return {
        "low": {
            "constraint_coverage": MetricFloor(target="value", min=0.5),
            "auth_matrix_coverage": MetricFloor(target="touched", min=1.0),
        },
        "medium": {
            "constraint_coverage": MetricFloor(target="value", min=0.7),
            "auth_matrix_coverage": MetricFloor(target="touched", min=1.0),
            "journey_coverage": MetricFloor(target="touched", min=1.0),
        },
        "high": {
            "constraint_coverage": MetricFloor(target="touched", min=1.0),
            "auth_matrix_coverage": MetricFloor(target="touched", min=1.0),
            "journey_coverage": MetricFloor(target="touched", min=1.0),
        },
        "critical": {
            "constraint_coverage": MetricFloor(target="touched", min=1.0),
            "auth_matrix_coverage": MetricFloor(target="touched", min=1.0),
            "journey_coverage": MetricFloor(target="touched", min=1.0),
            "adversarial_clean": MetricFloor(target="holds", must_hold=True),
        },
    }


def _default_cadence() -> MetricCadenceSchedule:
    return MetricCadenceSchedule(
        pr=[
            "diff_coverage",
            "constraint_coverage",
            "auth_matrix_coverage",
            "journey_coverage",
            "threshold_slack",
        ],
        nightly=[
            "mutation_score",
            "assertion_strength",
            "adversarial_yield",
            "baseline_drift",
        ],
    )


class EvidenceSufficiency(BaseModel):
    """「有没有新鲜的执行证据」与「指标 floor/cadence」的常量表。

    充分性判定在 Python 侧（evidence.sufficiency / metrics_sufficiency）；
    `on_insufficient` 同时服务 trace gate（case 证据）与 metrics evaluator
    （数值 floor 短板）。`floors` / `cadence` / `mutation_budget_seconds` 由
    ``evidence.metrics_sufficiency`` 裁决，供 Task 8 的
    ``metrics-sufficiency-gate`` 接线。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recency_hours: int = Field(gt=0)
    # 空 list 合法且有意义：`API: []` 是组织显式声明「该类型暂不要求证据」的退出口，
    # 与 `on_insufficient: warn` 同类。要求四个 key 写全，正是为了让这种退出口只能被
    # 显式写出来，不会因为漏写 key 而当成默认值。
    required_kinds: dict[TraceCaseType, list[EvidenceKind]]
    on_insufficient: PlanCheckAction
    floors: dict[RiskTier, dict[MetricKey, MetricFloor]] = Field(default_factory=_default_floors)
    cadence: MetricCadenceSchedule = Field(default_factory=_default_cadence)
    mutation_budget_seconds: int = Field(default=300, gt=0)

    @field_validator("required_kinds")
    @classmethod
    def _every_case_type_declared(
        cls, value: dict[TraceCaseType, list[EvidenceKind]]
    ) -> dict[TraceCaseType, list[EvidenceKind]]:
        """缺 case type 会让该类型的行无声地「充分」——按 plan_checks 的先例要求写全。

        对照上面的字段注释：显式空 list 是有意的 opt-out，漏写 key 不是，所以后者必须报错。
        """
        missing = sorted(set(get_args(TraceCaseType)) - set(value))
        if missing:
            raise ValueError(f"required_kinds missing keys: {', '.join(missing)}")
        for case_type, kinds in value.items():
            if len(kinds) != len(set(kinds)):
                raise ValueError(f"required_kinds[{case_type}] contains duplicate kinds")
        return value

    @field_validator("floors")
    @classmethod
    def _every_risk_tier_declared(
        cls, value: dict[RiskTier, dict[MetricKey, MetricFloor]]
    ) -> dict[RiskTier, dict[MetricKey, MetricFloor]]:
        missing = sorted(set(get_args(RiskTier)) - set(value))
        if missing:
            raise ValueError(f"floors missing risk tiers: {', '.join(missing)}")
        return value

    @model_validator(mode="after")
    def _floors_and_cadence_keys_are_shaped(self) -> EvidenceSufficiency:
        """§12.7 / shape closure: floor+cadence keys ⊆ MetricKey (by typing) and
        each floor's target matches ``METRIC_SHAPES``; optional surface only on
        multi-surface metrics.
        """
        for tier, band in self.floors.items():
            for key, floor in band.items():
                shape = METRIC_SHAPES[key]
                if shape == "scoped_ratio" and floor.target not in ("value", "touched"):
                    raise ValueError(
                        f"floors.{tier}.{key}: scoped_ratio metrics accept target "
                        f"value|touched, not {floor.target!r}"
                    )
                if shape == "scalar" and floor.target != "value":
                    raise ValueError(
                        f"floors.{tier}.{key}: scalar metrics accept target value, not {floor.target!r}"
                    )
                if shape == "boolean" and floor.target != "holds":
                    raise ValueError(
                        f"floors.{tier}.{key}: boolean metrics accept target holds, not {floor.target!r}"
                    )
                if floor.surface is not None:
                    allowed = METRIC_SURFACES[key]
                    if not allowed:
                        raise ValueError(
                            f"floors.{tier}.{key}: surface is only valid for multi-surface metrics"
                        )
                    if floor.surface not in allowed:
                        raise ValueError(
                            f"floors.{tier}.{key}: surface {floor.surface!r} not in {sorted(allowed)}"
                        )
        # Cadence keys are already MetricKey-typed; mention MetricCadence so the
        # schedule stays aligned with MetricsDocument.cadence.
        _cadences: tuple[MetricCadence, ...] = ("pr", "nightly")
        for name in _cadences:
            getattr(self.cadence, name)
        return self


def _default_evidence_sufficiency() -> EvidenceSufficiency:
    """字段级默认值即迁移路径：不含本块的历史 policy 文件照旧可加载。"""
    return EvidenceSufficiency(
        recency_hours=72,
        required_kinds={
            "API": ["covered", "execution_recent"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        },
        on_insufficient="require_human",
        floors=_default_floors(),
        cadence=_default_cadence(),
        mutation_budget_seconds=300,
    )


class Policy(BaseModel):
    """每个字段都必须有实名消费者：gate 表达式，或登记在册的 Python 消费者。

    ``coverage_floor`` 是唯一例外：deprecated、禁止新消费，由守卫单独豁免。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    human_review_risk_levels: list[str]
    force_continue_allowed: bool
    plan_checks: dict[str, PlanCheckAction]
    coverage_floor: CoverageFloor
    fuzz: FuzzPolicy
    healing: HealingPolicy
    evidence_sufficiency: EvidenceSufficiency = Field(default_factory=_default_evidence_sufficiency)

    @field_validator("plan_checks")
    @classmethod
    def _known_check_ids(cls, value: dict[str, PlanCheckAction]) -> dict[str, PlanCheckAction]:
        unknown = sorted(set(value) - KNOWN_PLAN_CHECK_IDS)
        if unknown:
            raise ValueError(f"unknown plan_checks keys: {', '.join(unknown)}")
        missing = sorted(KNOWN_PLAN_CHECK_IDS - set(value))
        if missing:
            raise ValueError(f"plan_checks missing required keys: {', '.join(missing)}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_plan_check_action(cls, data: object) -> object:
        if isinstance(data, dict) and "plan_check_action" in data:
            raise ValueError(
                "plan_check_action is removed; use plan_checks.<check_id> (warn|block|require_human) instead"
            )
        return data
