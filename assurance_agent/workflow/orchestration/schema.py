"""workflow-schema.yaml 加载器 + 静态校验 + 类型化模型。"""
from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from assurance_agent import resources
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.dsl import (
    BUILTIN_ARITY,
    DslError,
    collect_calls,
    collect_gate_refs,
    parse_expression,
)

ALLOWED_AGENTS = {"aa-doc-author", "aa-test-author", "aa-reviewer", "aa-reporter", "aa-archiver"}
ORCHESTRATOR_INTERNAL = {"skill-registry-check"}
ALLOWED_OWNED_BY = {"full", "intake", "execute"}
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


class ParamSpec(BaseModel):
    type: str
    values: list[object] | None = None
    default: object = None


class PhaseDef(BaseModel):
    id: str
    skill: str | None = None
    requires: list[str] = Field(default_factory=list)
    requires_mode: str = "all"
    produces: list[str] = Field(default_factory=list)
    owned_by: list[str] | None = None
    gate: str | None = None
    when: str | None = None
    ready_when: str | None = None
    loop: str | None = None
    repair_of: str | None = None
    max_attempts_param: str | None = None
    agent: str | None = None


class LoopDef(BaseModel):
    id: str
    members: list[str] = Field(default_factory=list)
    counter: str = ""
    max_param: str = ""
    allocate_on: str = ""
    exit_gate: str = ""


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
    invalid_json: Verdict | None = None
    missing_field_is: Verdict | None = None
    missing_file_is: Verdict | None = None
    default: Verdict = Verdict.STOP


class WorkflowSchema(BaseModel):
    schema_version: str = ""
    name: str = ""
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    phases: list[PhaseDef] = Field(default_factory=list)
    loops: dict[str, LoopDef] = Field(default_factory=dict)
    gates: dict[str, GateDef] = Field(default_factory=dict)

    def _phase(self, phase_id: str) -> PhaseDef | None:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    def has_phase(self, phase_id: str) -> bool:
        return self._phase(phase_id) is not None

    def phase_produces(self, phase_id: str) -> list[str] | None:
        p = self._phase(phase_id)
        return list(p.produces) if p else None

    def gate_for_phase(self, phase_id: str) -> str | None:
        p = self._phase(phase_id)
        return p.gate if p else None

    def default_param_values(self) -> dict[str, object]:
        return {name: spec.default for name, spec in self.params.items()}

    def produces_alias_map(self) -> dict[str, str]:
        """全局反向别名表：对齐源版 engine.ts 的 reverseAlias。

        遍历**所有** phase 的 `produces`，`derive_alias` 后取首见者，得到
        alias→canonical path。这是 phase `when` / `ready_when` / loop `allocate_on`
        求值时装载 evidence 别名（如 `fix_proposal`）的唯一来源——不再依赖任何
        per-phase `reads` 字段。
        """
        out: dict[str, str] = {}
        for phase in self.phases:
            for produced in phase.produces:
                alias = derive_alias(produced)
                if alias:  # `qa/archive/<change-id>/` 目录 produces 不生成 evidence alias
                    out.setdefault(alias, produced)
        return out


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

    return GateDef(
        id=gate_id,
        reads=_normalize_reads(raw.get("reads")),
        rules=rules,
        invalid_json=optional_verdict("invalid_json"),
        missing_field_is=optional_verdict("missing_field_is"),
        missing_file_is=optional_verdict("missing_file_is"),
        default=optional_verdict("default") or Verdict.STOP,
    )


def parse_schema(yaml_text: str) -> WorkflowSchema:
    try:
        doc = yaml.safe_load(yaml_text)
        if not isinstance(doc, dict):
            raise SchemaError("schema root is not a mapping")

        params = {k: ParamSpec(**v) for k, v in (doc.get("params") or {}).items()}
        phases = [PhaseDef.model_validate(_coerce_phase(p)) for p in (doc.get("phases") or [])]
        loops = {
            lid: LoopDef(id=lid, **{k: v for k, v in (lv or {}).items()})
            for lid, lv in (doc.get("loops") or {}).items()
        }
        gates = {
            gid: _normalize_gate(gid, gv or {})
            for gid, gv in (doc.get("gates") or {}).items()
        }
        schema = WorkflowSchema(
            schema_version=str(doc.get("schema_version", "")),
            name=str(doc.get("name", "")),
            params=params,
            phases=phases,
            loops=loops,
            gates=gates,
        )
    except SchemaError:
        raise
    except (AttributeError, TypeError, ValidationError, yaml.YAMLError) as exc:
        raise SchemaError(f"invalid workflow schema: {exc}") from exc
    _validate(schema)
    return schema


def _coerce_phase(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise SchemaError(f"phase is not a mapping: {raw!r}")
    if not isinstance(raw.get("id"), str):
        raise SchemaError(f"phase missing string id: {raw!r}")
    if "reads" in raw:
        raise SchemaError(f"phase '{raw['id']}'.reads is not supported; use global produces aliases")
    for key in ("when", "ready_when"):
        if isinstance(raw.get(key), str):
            raw[key] = raw[key].strip()  # type: ignore[index]
    return raw


def _all_predicates(schema: WorkflowSchema) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in schema.phases:
        if p.when:
            out.append((f"phase '{p.id}'.when", p.when))
        if p.ready_when:
            out.append((f"phase '{p.id}'.ready_when", p.ready_when))
    for loop in schema.loops.values():
        if loop.allocate_on:
            out.append((f"loop '{loop.id}'.allocate_on", loop.allocate_on))
    for g in schema.gates.values():
        for r in g.rules:
            out.append((f"gate '{g.id}'.{r.field}", r.expr))
    return out


def _detect_gate_cycle(schema: WorkflowSchema) -> list[str]:
    edges: dict[str, list[str]] = {}
    for g in schema.gates.values():
        refs: set[str] = set()
        for r in g.rules:
            try:
                refs.update(collect_gate_refs(parse_expression(r.expr)))
            except DslError:
                pass
        edges[g.id] = [x for x in refs if x in schema.gates]
    errors: list[str] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {gid: WHITE for gid in edges}
    stack: list[str] = []

    def dfs(node: str) -> bool:
        color[node] = GREY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color[nxt] == GREY:
                cycle = stack[stack.index(nxt):] + [nxt]
                errors.append("gate reference cycle: " + " -> ".join(cycle))
                return True
            if color[nxt] == WHITE and dfs(nxt):
                return True
        stack.pop()
        color[node] = BLACK
        return False

    for gid in edges:
        if color[gid] == WHITE and dfs(gid):
            break
    return errors


def _detect_phase_cycle(schema: WorkflowSchema) -> list[str]:
    edges = {p.id: list(p.requires) for p in schema.phases}
    white, grey, black = 0, 1, 2
    color = {pid: white for pid in edges}
    stack: list[str] = []
    errors: list[str] = []

    def dfs(node: str) -> bool:
        color[node] = grey
        stack.append(node)
        for dep in edges[node]:
            if dep not in color:
                continue
            if color[dep] == grey:
                cycle = stack[stack.index(dep):] + [dep]
                errors.append("phase dependency cycle: " + " -> ".join(cycle))
                return True
            if color[dep] == white and dfs(dep):
                return True
        stack.pop()
        color[node] = black
        return False

    for pid in edges:
        if color[pid] == white and dfs(pid):
            break
    return errors


def _validate(schema: WorkflowSchema) -> None:
    errors: list[str] = []
    phase_id_list = [p.id for p in schema.phases]
    phase_ids = set(phase_id_list)
    gate_ids = set(schema.gates)
    param_names = set(schema.params)

    duplicates = sorted(pid for pid in phase_ids if phase_id_list.count(pid) > 1)
    errors.extend(f"duplicate phase id '{pid}'" for pid in duplicates)

    for p in schema.phases:
        if p.requires_mode not in {"all", "any_active"}:
            errors.append(f"phase '{p.id}' invalid requires_mode '{p.requires_mode}'")
        for dep in p.requires:
            if dep not in phase_ids:
                errors.append(f"phase '{p.id}' requires unknown phase '{dep}'")
        if p.gate and p.gate not in gate_ids:
            errors.append(f"phase '{p.id}' references unknown gate '{p.gate}'")
        if p.repair_of and p.repair_of not in phase_ids:
            errors.append(f"phase '{p.id}' repair_of unknown phase '{p.repair_of}'")
        if p.max_attempts_param and p.max_attempts_param not in param_names:
            errors.append(f"phase '{p.id}' max_attempts_param unknown param '{p.max_attempts_param}'")
        if p.loop and p.loop not in schema.loops:
            errors.append(f"phase '{p.id}' references unknown loop '{p.loop}'")
        for scope in p.owned_by or []:
            if scope not in ALLOWED_OWNED_BY:
                errors.append(f"phase '{p.id}' owned_by scope '{scope}' not allowed")
        if p.id not in ORCHESTRATOR_INTERNAL:
            if p.skill is None and p.agent:
                errors.append(f"CLI phase '{p.id}' (skill: null) must not have an agent")
            if p.skill is not None:
                if not p.agent:
                    errors.append(f"agent phase '{p.id}' has a skill but no agent")
                elif p.agent not in ALLOWED_AGENTS:
                    errors.append(f"phase '{p.id}' agent '{p.agent}' not allowed")

    for loop in schema.loops.values():
        for m in loop.members:
            if m not in phase_ids:
                errors.append(f"loop '{loop.id}' member unknown phase '{m}'")
        if loop.exit_gate and loop.exit_gate not in gate_ids:
            errors.append(f"loop '{loop.id}' exit_gate unknown gate '{loop.exit_gate}'")
        if loop.max_param and loop.max_param not in param_names:
            errors.append(f"loop '{loop.id}' max_param unknown param '{loop.max_param}'")
        declared_members = {p.id for p in schema.phases if p.loop == loop.id}
        if declared_members != set(loop.members):
            errors.append(
                f"loop '{loop.id}' members disagree with phase.loop: "
                f"loop={sorted(loop.members)} phases={sorted(declared_members)}"
            )

    produced_aliases: dict[str, str] = {}
    for phase in schema.phases:
        for path in phase.produces:
            alias = derive_alias(path)
            if not alias:
                continue  # directory-only archive produce is not evidence-addressable
            if alias in produced_aliases and produced_aliases[alias] != path:
                errors.append(
                    f"global produces alias collision '{alias}': "
                    f"'{produced_aliases[alias]}' vs '{path}'"
                )
            produced_aliases[alias] = path

    for g in schema.gates.values():
        seen: dict[str, str] = {}
        for r in g.reads:
            if r.alias in seen and seen[r.alias] != r.path:
                errors.append(f"gate '{g.id}' alias collision '{r.alias}'")
            seen[r.alias] = r.path
        # Verdict 已在 normalization 时转成唯一 StrEnum；未知值无法进入模型。
        # 四态安全顺序：needs_fix→needs_human_review→reject→pass 若同时出现须按此序声明。
        present = [v for v in _SAFETY_ORDER if any(r.verdict == v for r in g.rules)]
        declared = [r.verdict for r in g.rules if r.verdict in _SAFETY_ORDER]
        if declared != present:
            errors.append(
                f"gate '{g.id}' safety verdicts must be declared in canonical order "
                f"{present}, got {declared}"
            )

    for loc, expr in _all_predicates(schema):
        try:
            ast_node = parse_expression(expr)
        except DslError as exc:
            errors.append(f"{loc}: invalid predicate — {exc}")
            continue
        for call in collect_calls(ast_node):
            expected = BUILTIN_ARITY.get(call.callee)
            if expected is None:
                errors.append(f"{loc}: unknown function '{call.callee}'")
            elif len(call.args) != expected:
                errors.append(f"{loc}: '{call.callee}' expects {expected} arg(s), got {len(call.args)}")
        for gid in collect_gate_refs(ast_node):
            if gid not in gate_ids:
                errors.append(f"{loc}: references unknown gate '{gid}'")

    errors.extend(_detect_phase_cycle(schema))
    errors.extend(_detect_gate_cycle(schema))

    if errors:
        raise SchemaError("schema validation failed:\n  - " + "\n  - ".join(errors))


def load_workflow_schema(project_root: Path, explicit: Path | None = None) -> WorkflowSchema:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else project_root / explicit
        if not path.exists():
            raise SchemaError(f"explicit schema not found: {path}")
        return parse_schema(path.read_text(encoding="utf-8"))
    for rel in (Path(".aa/workflow-schema.yaml"), Path("schemas/workflow-schema.yaml")):
        candidate = project_root / rel
        if candidate.exists():
            return parse_schema(candidate.read_text(encoding="utf-8"))
    return parse_schema(resources.read_text("schemas", "workflow-schema.yaml"))
