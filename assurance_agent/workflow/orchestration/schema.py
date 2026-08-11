"""Shared gate / verdict models + gate normalization for graph schema v2.

v1 ``WorkflowSchema`` / ``PhaseDef`` / ``LoopDef`` / ``parse_schema`` were removed
in the GraphRuntime cutover. Topology lives in ``workflow.graph.schema_v2``.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from assurance_agent.exceptions import AaError

ALLOWED_AGENTS = {
    "aa-explorer",
    "aa-doc-author",
    "aa-test-author",
    "aa-reviewer",
    "aa-reporter",
    "aa-archiver",
}
ORCHESTRATOR_INTERNAL = {"skill-registry-check"}
_WHEN_SUFFIX = "_when"


class Verdict(StrEnum):
    PASS = "pass"
    NEEDS_FIX = "needs_fix"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECT = "reject"
    STOP = "stop"
    ENTER = "enter"
    SKIP = "skip"
    CONTINUE = "continue"
    EXIT = "exit"


KNOWN_VERDICTS = frozenset(Verdict)
# 四态安全裁决的 canonical 声明顺序（Spec:80）。求值仍按声明顺序 first-true-wins
# （对齐源版 engine.ts adjudicate），但加载期强制这四个 verdict 若同时出现必须按此序声明，
# 使「声明顺序 == 安全优先级」不可被作者颠覆。
_SAFETY_ORDER = (
    Verdict.NEEDS_FIX,
    Verdict.NEEDS_HUMAN_REVIEW,
    Verdict.REJECT,
    Verdict.PASS,
)


class SchemaError(AaError):
    """schema 结构非法或静态校验失败。"""


class GateRule(BaseModel):
    field: str
    verdict: Verdict
    expr: str


class ReadEntry(BaseModel):
    path: str
    alias: str


class GateDef(BaseModel):
    id: str
    reads: list[ReadEntry] = Field(default_factory=list)
    rules: list[GateRule] = Field(default_factory=list)
    causes: dict[str, str] = Field(default_factory=dict)
    invalid_json: Verdict | None = None
    missing_field_is: Verdict | None = None
    missing_file_is: Verdict | None = None
    default: Verdict = Verdict.STOP


def derive_alias(file_path: str) -> str:
    base = file_path.split("/")[-1]
    no_ext = re.sub(r"\.[^.]+$", "", base)
    return re.sub(r"[^A-Za-z0-9]", "_", no_ext)


def _normalize_reads(raw: object) -> list[ReadEntry]:
    if not isinstance(raw, list):
        return []
    out: list[ReadEntry] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(ReadEntry(path=entry, alias=derive_alias(entry)))
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            path = entry["path"]
            alias = entry["as"] if isinstance(entry.get("as"), str) else derive_alias(path)
            out.append(ReadEntry(path=path, alias=alias))
        else:
            raise SchemaError(f"invalid reads entry: {entry!r}")
    return out


def _normalize_gate(gate_id: str, raw: dict[str, object]) -> GateDef:
    rules: list[GateRule] = []
    for key, val in raw.items():  # dict 保留 YAML 声明顺序
        if key.endswith(_WHEN_SUFFIX):
            try:
                verdict = Verdict(key[: -len(_WHEN_SUFFIX)])
            except ValueError as exc:
                raise SchemaError(f"gate '{gate_id}' unknown verdict in '{key}'") from exc
            rules.append(GateRule(field=key, verdict=verdict, expr=str(val).strip()))

    def optional_verdict(field: str) -> Verdict | None:
        value = raw.get(field)
        if value is None:
            return None
        try:
            return Verdict(str(value))
        except ValueError as exc:
            raise SchemaError(f"gate '{gate_id}' {field} unknown verdict '{value}'") from exc

    raw_causes = raw.get("causes")
    if raw_causes is None:
        causes: dict[str, str] = {}
    elif isinstance(raw_causes, dict):
        causes = {}
        for cause, expression in raw_causes.items():
            cause_name = str(cause).strip()
            expression_text = str(expression).strip()
            if not cause_name or not expression_text:
                raise SchemaError(f"gate '{gate_id}' causes must use non-empty names and expressions")
            causes[cause_name] = expression_text
    else:
        raise SchemaError(f"gate '{gate_id}' causes must be a mapping")

    return GateDef(
        id=gate_id,
        reads=_normalize_reads(raw.get("reads")),
        rules=rules,
        causes=causes,
        invalid_json=optional_verdict("invalid_json"),
        missing_field_is=optional_verdict("missing_field_is"),
        missing_file_is=optional_verdict("missing_file_is"),
        default=optional_verdict("default") or Verdict.STOP,
    )


def normalize_gates(raw: object) -> dict[str, GateDef]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("gates must be a mapping")
    normalized: dict[str, GateDef] = {}
    for gate_id, gate_raw in raw.items():
        if gate_raw is not None and not isinstance(gate_raw, dict):
            raise SchemaError(f"gate {gate_id!r} must be a mapping")
        normalized[str(gate_id)] = _normalize_gate(str(gate_id), gate_raw or {})
    return normalized
