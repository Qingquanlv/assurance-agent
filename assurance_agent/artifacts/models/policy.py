""".aa/policy.yaml — 组织策略常量表（spec C3 / C3b）。

v1 只做常量，不做规则语言、不做 per-path 匹配、不做继承：gate DSL 本身已是条件
语言，policy 只当它的常量源。C3b 将 plan check 处置细化为 check_id → action 对照表，
并加入 coverage / fuzz / healing 阶段常量。
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.assurance import CASE_TYPES, KNOWN_PLAN_CHECK_IDS, CaseType

PlanCheckAction = Literal["warn", "block", "require_human"]
EvidenceKind = Literal["covered", "execution_recent", "fuzz_run", "perf_run", "pass_status"]
_REQUIRED_CASE_TYPES = CASE_TYPES


def _default_evidence_sufficiency() -> "EvidenceSufficiency":
    return EvidenceSufficiency(
        recency_hours=72,
        required_kinds={
            "API": ["covered", "execution_recent"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        },
        on_insufficient="require_human",
    )


class EvidenceSufficiency(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recency_hours: int = Field(gt=0)
    required_kinds: dict[CaseType, list[EvidenceKind]]
    on_insufficient: PlanCheckAction

    @model_validator(mode="after")
    def _require_all_case_type_keys(self) -> Self:
        missing = [key for key in _REQUIRED_CASE_TYPES if key not in self.required_kinds]
        if missing:
            raise ValueError(f"required_kinds missing keys: {', '.join(missing)}")
        for case_type, kinds in self.required_kinds.items():
            if len(kinds) != len(set(kinds)):
                raise ValueError(f"required_kinds[{case_type}] contains duplicate kinds")
        return self


class CoverageFloor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_high: float
    risk_medium: float


class FuzzPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_when_endpoint_has_auth: bool


class HealingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auth_module: PlanCheckAction


class Policy(BaseModel):
    """只有被 gate 表达式消费的字段才允许存在。"""

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
