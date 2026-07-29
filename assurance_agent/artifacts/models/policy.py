""".aa/policy.yaml — 组织策略常量表（spec C3）。

v1 只做常量，不做规则语言、不做 per-path 匹配、不做继承：gate DSL 本身已是条件
语言，policy 只当它的常量源。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

PlanCheckAction = Literal["warn", "block", "require_human"]


class Policy(BaseModel):
    """只有被 gate 表达式消费的字段才允许存在。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    human_review_risk_levels: list[str]
    force_continue_allowed: bool
    plan_check_action: PlanCheckAction
