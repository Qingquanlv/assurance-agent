"""review/*-plan-checks.json — 确定性 plan check 的证据文档（spec C2）。

check 只回答「事实是否成立」；block / require_human / warn 的处置由 policy 决定，
因此本文档没有 severity 字段。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Finding(BaseModel):
    model_config = _FROZEN

    locator: str
    actual: str
    expected: str


class CheckEvidence(BaseModel):
    model_config = _FROZEN

    check_id: str
    status: Literal["pass", "fail"]
    findings: tuple[Finding, ...] = ()
    refs: tuple[str, ...] = ()


class PlanCheckDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    status: Literal["pass", "fail"]
    checks: tuple[CheckEvidence, ...] = ()

    @classmethod
    def from_checks(cls, checks: Sequence[CheckEvidence]) -> "PlanCheckDocument":
        ordered = tuple(checks)
        failed = any(check.status == "fail" for check in ordered)
        return cls(status="fail" if failed else "pass", checks=ordered)
