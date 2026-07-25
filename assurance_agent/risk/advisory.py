"""Advisory 语义校验（spec 第 5 节 §5.5–5.7）。

规则转写自 TS src/risk/validate_advisory.ts。结构校验复用 M2 的 Advisory 模型
（等价于 TS 的 schema/advisory zod 验证器，且源自打包 explore-advisory.schema.json）；
语义校验（evidence_ids / case_id 引用、置信度门槛、open-question 生命周期、断言
传播、模式一致性）在本模块实现。
"""

from typing import Any

from pydantic import ValidationError

from assurance_agent.artifacts.models import Advisory
from assurance_agent.risk.context import EvidenceEntry, RiskContext

Confidence = str  # "high" | "medium" | "low"

_CAP_RANK = {"high": 3, "medium": 2, "low": 1}
_OQ_STATUSES = {"unanswered", "answered", "deferred"}
_ASSERTION_INTENTS = {"assert_ideal", "assert_known_bug", "ignore", "undecided"}
_ANSWERED_VIA = {"explore", "auto_default", "aa-intake"}


def _cap_rank(c: str | None) -> int:
    return _CAP_RANK.get(c or "low", 1)


def _as_str_list(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _guidance(advisory: dict) -> dict | None:
    g = advisory.get("case_design_guidance")
    return g if isinstance(g, dict) else None


def _collect_evidence_ids(advisory: dict) -> list[str]:
    ids: set[str] = set()

    def scan(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                for x in _as_str_list(item.get("evidence_ids")):
                    ids.add(x)

    scan(advisory.get("watchlist"))
    g = _guidance(advisory)
    if g:
        scan(g.get("priority_hints"))
        scan(g.get("suggested_scenarios"))
    return list(ids)


def _collect_case_ids(advisory: dict) -> list[str]:
    g = _guidance(advisory)
    hints = g.get("priority_hints") if g else None
    out: list[str] = []
    if isinstance(hints, list):
        for h in hints:
            if isinstance(h, dict) and isinstance(h.get("case_id"), str):
                out.append(h["case_id"])
    return out


def _collect_issue_refs(advisory: dict) -> list[str]:
    ids: set[str] = set()

    def scan(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("issue_id"), str):
                ids.add(item["issue_id"])
            for x in _as_str_list(item.get("issue_ids")):
                ids.add(x)

    scan(advisory.get("watchlist"))
    g = _guidance(advisory)
    if g:
        scan(g.get("priority_hints"))
    return list(ids)


def _is_answered_oq(row: dict) -> bool:
    if row.get("status") == "answered":
        return True
    return (
        row.get("status") is None and row.get("answer") is not None and row.get("answered_via") == "explore"
    )


def _check_open_question_lifecycle(advisory: dict, errors: list[str]) -> None:
    oqs = advisory.get("open_questions_for_case_design")
    if not isinstance(oqs, list):
        return
    for oq in oqs:
        if not isinstance(oq, dict):
            continue
        oq_id = oq["id"] if isinstance(oq.get("id"), str) else "(unknown OQ)"
        status = oq.get("status")
        intent = oq.get("assertion_intent")
        via = oq.get("answered_via")
        if status is not None and str(status) not in _OQ_STATUSES:
            errors.append(f"{oq_id}: status must be one of unanswered, answered, deferred")
        if intent is not None and str(intent) not in _ASSERTION_INTENTS:
            errors.append(
                f"{oq_id}: assertion_intent must be one of assert_ideal, assert_known_bug, ignore, undecided"
            )
        if via is not None and str(via) not in _ANSWERED_VIA:
            errors.append(f"{oq_id}: answered_via must be one of explore, auto_default, aa-intake")
        if status == "answered":
            if intent is None:
                errors.append(f"{oq_id}: status answered requires assertion_intent")
            if via is None:
                errors.append(f"{oq_id}: status answered requires answered_via")
            if via != "auto_default" and oq.get("answer") is None and oq.get("answer_text") is None:
                errors.append(f"{oq_id}: status answered requires answer or answer_text")
        if status == "unanswered":
            if intent is not None:
                errors.append(f"{oq_id}: status unanswered must not set assertion_intent")
            if via is not None:
                errors.append(f"{oq_id}: status unanswered must not set answered_via")
        if status == "deferred":
            reason = oq.get("deferred_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{oq_id}: status deferred requires deferred_reason")
            if intent is not None:
                errors.append(f"{oq_id}: status deferred must not set assertion_intent")


def _check_mode_consistency(advisory: dict, interaction_mode: str | None, errors: list[str]) -> None:
    if interaction_mode != "autonomous":
        return
    oqs = advisory.get("open_questions_for_case_design")
    if not isinstance(oqs, list):
        return
    for oq in oqs:
        if not isinstance(oq, dict):
            continue
        via = oq.get("answered_via")
        if via in ("explore", "aa-intake"):
            oq_id = oq["id"] if isinstance(oq.get("id"), str) else "(unknown OQ)"
            errors.append(f"{oq_id}: autonomous run forbids answered_via {via}")


def _evidence_confidence_cap(ev: EvidenceEntry) -> Confidence:
    if ev.type == "historical_issue":
        return "high" if ev.projection_digest is not None else "low"
    return "high"


def _check_confidence_items(context: RiskContext, items: Any, label: str, errors: list[str]) -> None:
    if not isinstance(items, list):
        return
    evidence_by_id = {e.id: e for e in context.evidence}
    module_conf = {m.name: m.confidence for m in context.impact.modules}

    def qualifies_high(ev_id: str) -> bool:
        ev = evidence_by_id.get(ev_id)
        if ev is None:
            return False
        if ev.type == "historical_issue":
            return ev.projection_digest is not None
        if ev.type == "test_pass_rate":
            return ev.below_fail_threshold is True
        if ev.type == "code_change":
            return _cap_rank(ev.confidence) >= _cap_rank("medium")
        return False

    for item in items:
        if not isinstance(item, dict):
            continue
        conf = item.get("confidence")
        ev_ids = [x for x in _as_str_list(item.get("evidence_ids")) if x in evidence_by_id]
        if conf == "high":
            if not ev_ids:
                errors.append(f"{label}: confidence high requires non-empty evidence_ids")
            if context.staleness.get("stale") is True:
                errors.append(f"{label}: confidence high forbidden when staleness.stale is true")
            caps = [_evidence_confidence_cap(evidence_by_id[i]) for i in ev_ids]
            if caps and all(_cap_rank(c) <= _cap_rank("low") for c in caps):
                errors.append(f"{label}: confidence high cannot rely only on low-cap evidence")
            if ev_ids and not any(qualifies_high(i) for i in ev_ids):
                errors.append(
                    f"{label}: confidence high requires a qualifying evidence "
                    "(historical_issue source 1–2, test_health below threshold, or diff module confidence >= medium)"
                )
            modules = [m for i in ev_ids if (m := evidence_by_id[i].module) is not None]
            if modules and all(
                _cap_rank(module_conf.get(m, "medium")) < _cap_rank("medium") for m in modules
            ):
                errors.append(f"{label}: confidence high requires diff module confidence >= medium")
        if not ev_ids and conf != "low":
            errors.append(f"{label}: missing evidence_ids must use confidence low")


def validate_advisory(
    context: RiskContext,
    advisory: dict,
    known_case_ids: list[str],
    *,
    interaction_mode: str | None = None,
    orchestrator_skill: str | None = None,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if isinstance(advisory.get("schema_version"), str):
        try:
            Advisory.model_validate(advisory)
        except ValidationError as err:
            return (
                False,
                [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors()],
            )

    evidence_by_id = {e.id: e for e in context.evidence}
    known_issue_ids = {h.problem_id for h in context.historical_issues}
    affected = set(context.impact.affected_case_ids) | set(known_case_ids)

    for ev_id in _collect_evidence_ids(advisory):
        if ev_id not in evidence_by_id:
            errors.append(f"evidence_ids references unknown id: {ev_id}")

    for case_id in _collect_case_ids(advisory):
        if case_id not in affected:
            errors.append(f"case_id not in context.affected_case_ids or qa/cases: {case_id}")

    _check_confidence_items(context, advisory.get("watchlist"), "watchlist", errors)
    g = _guidance(advisory)
    _check_confidence_items(
        context, g.get("priority_hints") if g else None, "case_design_guidance.priority_hints", errors
    )

    _check_open_question_lifecycle(advisory, errors)
    _check_mode_consistency(advisory, interaction_mode, errors)

    for issue_id in _collect_issue_refs(advisory):
        if issue_id not in known_issue_ids:
            errors.append(f"issue reference not in context.historical_issues: {issue_id}")

    return (len(errors) == 0, errors)
