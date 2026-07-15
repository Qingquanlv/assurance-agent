import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.orchestration.gates import check_gate, resolve_change_path
from assurance_agent.workflow.orchestration.schema import parse_schema

EMPTY = WorkflowState()  # gates 接收 WorkflowState，不接收裸 dict

SCHEMA = parse_schema("""
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
    v = check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {})
    assert v.verdict == "pass"


def test_needs_fix_order_wins(tmp_path: Path):
    _write_review(tmp_path, {"decision": "needs_fix", "auto_fix_allowed": True})
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "needs_fix"


def test_reject(tmp_path: Path):
    _write_review(tmp_path, {"decision": "reject", "auto_fix_allowed": False})
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "reject"


def test_missing_field_is_stop(tmp_path: Path):
    _write_review(tmp_path, {"auto_fix_allowed": True})  # 无 decision
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_invalid_json_stop(tmp_path: Path):
    d = tmp_path / "review"
    d.mkdir(parents=True)
    (d / "case-review.json").write_text("{not json")
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_missing_file_default_stop(tmp_path: Path):
    assert check_gate(SCHEMA, "case-review-gate", tmp_path, EMPTY, {}).verdict == "stop"


def test_change_id_placeholder_resolves_for_archive_produce(tmp_path: Path):
    change = tmp_path / "qa" / "changes" / "C-42"
    assert resolve_change_path(change, "qa/archive/<change-id>/") == (tmp_path / "qa" / "archive" / "C-42")


def test_safety_order_declaration_first_true_wins(tmp_path: Path):
    """加载期强制 canonical 安全序声明 → 声明顺序即安全优先级；needs_fix 先于 pass 命中。

    fixture 里 needs_fix_when 与 pass_when 同时为真，needs_fix 声明在前 → 裁决 needs_fix。
    注意 skill!=null 的 phase 必须带白名单 agent（Task 4 校验），故 `r` 声明 agent。
    """
    schema = parse_schema("""
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
    assert check_gate(schema, "g", tmp_path, EMPTY, {}).verdict == "needs_fix"
