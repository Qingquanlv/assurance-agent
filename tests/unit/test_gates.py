import hashlib as _hashlib
import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.gates import check_gate, resolve_change_path
import yaml
from assurance_agent.workflow.orchestration.schema import normalize_gates
from tests.helpers_aa import loc_for


class _GateSchema:
    def __init__(self, gates: dict) -> None:
        self.gates = gates


def _gates_schema(text: str) -> _GateSchema:
    doc = yaml.safe_load(text)
    return _GateSchema(normalize_gates(doc.get("gates") or {}))


EMPTY = WorkflowState()  # gates 接收 WorkflowState，不接收裸 dict

SCHEMA = _gates_schema("""
schema_version: "1"
name: t
params:
  force_continue: { type: bool, default: false }
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    needs_human_review_when: "decision == 'needs_human_review'"
    reject_when: "decision == 'reject'"
    pass_when: "decision == 'pass'"
""")


def _write_review(change_dir: Path, payload: dict) -> None:
    d = change_dir / "review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case-review.json").write_text(json.dumps(payload))


def test_pass(tmp_path: Path):
    _write_review(tmp_path, {"decision": "pass", "auto_fix_allowed": False})
    v = check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {})
    assert v.verdict == "pass"


def test_needs_fix_order_wins(tmp_path: Path):
    _write_review(tmp_path, {"decision": "needs_fix", "auto_fix_allowed": True})
    assert check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == "needs_fix"


def test_reject(tmp_path: Path):
    _write_review(tmp_path, {"decision": "reject", "auto_fix_allowed": False})
    assert check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == "reject"


def test_missing_field_is_stop(tmp_path: Path):
    _write_review(tmp_path, {"auto_fix_allowed": True})  # 无 decision
    assert check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == "stop"


def test_invalid_json_stop(tmp_path: Path):
    d = tmp_path / "review"
    d.mkdir(parents=True)
    (d / "case-review.json").write_text("{not json")
    assert check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == "stop"


def test_missing_file_default_stop(tmp_path: Path):
    assert check_gate(SCHEMA, "case-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == "stop"


def test_change_id_placeholder_resolves_for_archive_produce(tmp_path: Path):
    change = tmp_path / "qa" / "changes" / "C-42"
    loc = loc_for(change, project_root=tmp_path)
    assert resolve_change_path(loc, "qa/archive/<change-id>/") == (tmp_path / "qa" / "archive" / "C-42")


def test_resolve_change_path_uses_location_project_root_regardless_of_depth(tmp_path: Path):
    # Non-default layout: change dir is NOT <root>/qa/changes/<id> at depth 3.
    # The removed `parents[2]` would escape the project root; the ChangeLocation
    # carries the real root, so `qa/` and `repo:` prefixes resolve correctly.
    change = tmp_path / "custom" / "CH-1"
    loc = loc_for(change, project_root=tmp_path)
    assert resolve_change_path(loc, "qa/archive/<change-id>/") == tmp_path / "qa" / "archive" / "CH-1"
    assert resolve_change_path(loc, "repo:src/x.py") == tmp_path / "src" / "x.py"
    assert resolve_change_path(loc, "review/r.json") == change / "review" / "r.json"


def test_safety_order_declaration_first_true_wins(tmp_path: Path):
    """加载期强制 canonical 安全序声明 → 声明顺序即安全优先级；needs_fix 先于 pass 命中。

    fixture 里 needs_fix_when 与 pass_when 同时为真，needs_fix 声明在前 → 裁决 needs_fix。
    注意 skill!=null 的 phase 必须带白名单 agent（Task 4 校验），故 `r` 声明 agent。
    """
    schema = _gates_schema("""
schema_version: "1"
name: t
phases:
  - id: r
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: g
gates:
  g:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'pass'"
    reject_when: "decision == 'reject'"
    pass_when: "decision == 'pass'"
""")
    _write_review(tmp_path, {"decision": "pass"})  # satisfies BOTH needs_fix_when and pass_when
    assert check_gate(schema, "g", loc_for(tmp_path), EMPTY, {}).verdict == "needs_fix"


# ── applyGateDecision: human decisions upgrade needs_human_review ────────────

_NHR_SCHEMA = _gates_schema("""
schema_version: "1"
name: t
phases:
  - id: api-plan-review
    skill: aa-api-plan-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/api-plan-review.json]
    gate: api-plan-review-gate
gates:
  api-plan-review-gate:
    reads: [review/api-plan-review.json]
    invalid_json: stop
    needs_human_review_when: "decision == 'needs_human_review'"
    pass_when: "decision == 'pass'"
""")


def _write_nhr_review(change_dir: Path) -> str:
    d = change_dir / "review"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "api-plan-review.json"
    path.write_text(json.dumps({"decision": "needs_human_review"}))
    return _hashlib.sha256(path.read_bytes()).hexdigest()


def _decide(change_dir: Path, action: str, *, review_file: str | None, review_sha256: str | None) -> None:
    event: dict = {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": "api-plan-review-gate",
        "action": action,
        "reason": "benchmark accept",
        "who": "tester",
    }
    if review_file is not None:
        event["review_file"] = review_file
        event["review_sha256"] = review_sha256
    append_event_strict(change_dir, event)


def test_gate_decision_needs_human_review_without_decision(tmp_path: Path):
    _write_nhr_review(tmp_path)
    assert check_gate(_NHR_SCHEMA, "api-plan-review-gate", loc_for(tmp_path), EMPTY, {}).verdict == (
        "needs_human_review"
    )


def test_accept_risk_upgrades_to_pass(tmp_path: Path):
    sha = _write_nhr_review(tmp_path)
    _decide(tmp_path, "accept_risk", review_file="review/api-plan-review.json", review_sha256=sha)
    verdict = check_gate(_NHR_SCHEMA, "api-plan-review-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "pass"
    assert "human_decision:accept_risk" in (verdict.matched_rule or "")


def test_fix_and_proceed_upgrades_to_needs_fix(tmp_path: Path):
    sha = _write_nhr_review(tmp_path)
    _decide(tmp_path, "fix_and_proceed", review_file="review/api-plan-review.json", review_sha256=sha)
    verdict = check_gate(_NHR_SCHEMA, "api-plan-review-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "needs_fix"


def test_decision_ignored_when_review_hash_mismatches(tmp_path: Path):
    _write_nhr_review(tmp_path)
    _decide(
        tmp_path,
        "accept_risk",
        review_file="review/api-plan-review.json",
        review_sha256="deadbeef" * 8,
    )
    verdict = check_gate(_NHR_SCHEMA, "api-plan-review-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "needs_human_review"


def test_decision_ignored_without_review_evidence(tmp_path: Path):
    _write_nhr_review(tmp_path)
    _decide(tmp_path, "accept_risk", review_file=None, review_sha256=None)
    verdict = check_gate(_NHR_SCHEMA, "api-plan-review-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "needs_human_review"


# ── fixer-safety-gate: healing.safety decisions anchor the gate (TS engine.ts) ─

_FIXER_SAFETY_SCHEMA = _gates_schema("""
schema_version: "1"
name: t
phases:
  - id: healing-rerun
    skill: null
    requires: []
    produces: [execution/execution-manifest.yaml]
    gate: fixer-safety-gate
gates:
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    missing_file_is: stop
    needs_human_review_when: "passed == false or needs_review == true"
    pass_when: "passed == true"
""")


def _write_safety_check(change_dir: Path) -> str:
    d = change_dir / "healing"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "fixer-safety-check.json"
    path.write_text(json.dumps({"passed": False, "needs_review": True}))
    return _hashlib.sha256(path.read_bytes()).hexdigest()


def _decide_healing_safety(
    change_dir: Path,
    action: str,
    *,
    review_file: str | None = None,
    review_sha256: str | None = None,
) -> None:
    event: dict = {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": "healing.safety",
        "action": action,
        "reason": "safety risk reviewed",
        "who": "tester",
    }
    if review_file is not None:
        event["review_file"] = review_file
        event["review_sha256"] = review_sha256
    append_event_strict(change_dir, event)


def test_healing_safety_accept_risk_upgrades_fixer_safety_gate(tmp_path: Path):
    sha = _write_safety_check(tmp_path)
    loc = loc_for(tmp_path)
    assert check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc, EMPTY, {}).verdict == (
        "needs_human_review"
    )
    _decide_healing_safety(
        tmp_path,
        "accept_risk",
        review_file="healing/fixer-safety-check.json",
        review_sha256=sha,
    )
    verdict = check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc, EMPTY, {})
    assert verdict.verdict == "pass"
    assert "human_decision:accept_risk" in (verdict.matched_rule or "")


def test_healing_safety_fix_and_proceed_does_not_anchor(tmp_path: Path):
    # Mirror of TS resolveDecisionSupport: healing.safety supports only accept_risk,
    # so a fix_and_proceed recorded there can never anchor the fixer-safety-gate.
    sha = _write_safety_check(tmp_path)
    _decide_healing_safety(
        tmp_path,
        "fix_and_proceed",
        review_file="healing/fixer-safety-check.json",
        review_sha256=sha,
    )
    verdict = check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "needs_human_review"


def test_healing_safety_decision_ignored_without_review_evidence(tmp_path: Path):
    _write_safety_check(tmp_path)
    _decide_healing_safety(tmp_path, "accept_risk")
    verdict = check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc_for(tmp_path), EMPTY, {})
    assert verdict.verdict == "needs_human_review"
