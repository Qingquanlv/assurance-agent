from pathlib import Path

import pytest

from assurance_agent.workflow.orchestration.schema import (
    SchemaError,
    load_workflow_schema,
    parse_schema,
)

GOOD = """
schema_version: "1"
name: t
params:
  run_mode: { type: enum, values: [full, case-only], default: full }
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: a
    skill: null
    requires: []
    produces: [x.json]
    gate: g
  - id: b
    skill: aa-explore
    agent: aa-doc-author
    requires: [a]
    produces: [y.json]
    when: "params.run_mode == 'full'"
gates:
  g:
    reads: [x.json]
    needs_fix_when: "decision == 'needs_fix'"
    pass_when: "decision == 'pass'"
"""


def test_parse_and_accessors():
    s = parse_schema(GOOD)
    assert s.has_phase("a") is True
    assert s.has_phase("zzz") is False
    assert s.phase_produces("b") == ["y.json"]
    assert s.gate_for_phase("a") == "g"
    assert s.gate_for_phase("b") is None
    assert s.default_param_values()["run_mode"] == "full"
    # 规则按声明顺序（canonical 安全序）：needs_fix 在前
    assert [r.verdict for r in s.gates["g"].rules] == ["needs_fix", "pass"]
    # alias 派生
    assert s.gates["g"].reads[0].alias == "x"


def test_rejects_bad_predicate():
    bad = GOOD.replace("decision == 'pass'", "decision =! 'pass'")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_unknown_gate_ref():
    bad = GOOD.replace("params.run_mode == 'full'", "gate('nope').verdict == 'pass'")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_dangling_requires():
    bad = GOOD.replace("requires: [a]", "requires: [ghost]")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_explicit_missing_is_error(tmp_path: Path):
    with pytest.raises(SchemaError):
        load_workflow_schema(tmp_path, explicit=tmp_path / "nope.yaml")


def test_packaged_default_loads(tmp_path: Path):
    # 空项目根 → 回退到包内默认 schema，且能通过静态校验
    s = load_workflow_schema(tmp_path)
    assert s.has_phase("explore")
    assert "healing" in s.loops
    assert s.gate_for_phase("case-design") == "case-design-gate"


def test_produces_alias_map_covers_fix_proposal():
    # 全局反向别名表覆盖 healing/fix-proposal.json → fix_proposal，
    # 使 api/e2e-codegen-fix 的 when 无需任何 per-phase reads 即可解析。
    s = load_workflow_schema(Path("/nonexistent"))
    amap = s.produces_alias_map()
    assert amap["fix_proposal"] == "healing/fix-proposal.json"
    assert amap["case_review"] == "review/case-review.json"


def test_rejects_unknown_verdict():
    bad = GOOD.replace("needs_fix_when:", "bogus_when:")
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_out_of_order_safety_verdicts():
    # pass 在 needs_fix 之前声明 → 违反 canonical 安全序
    bad = """
schema_version: "1"
name: t
phases:
  - id: a
    skill: null
    requires: []
    produces: [x.json]
    gate: g
gates:
  g:
    reads: [x.json]
    pass_when: "decision == 'pass'"
    needs_fix_when: "decision == 'needs_fix'"
"""
    with pytest.raises(SchemaError):
        parse_schema(bad)


def test_rejects_duplicate_phase_ids():
    bad = GOOD.replace("  - id: b", "  - id: a")
    with pytest.raises(SchemaError, match="duplicate phase id"):
        parse_schema(bad)


def test_rejects_phase_dependency_cycle():
    bad = GOOD.replace("requires: []", "requires: [b]", 1)
    with pytest.raises(SchemaError, match="phase dependency cycle"):
        parse_schema(bad)


def test_rejects_legacy_phase_reads():
    bad = GOOD.replace("    produces: [y.json]", "    produces: [y.json]\n    reads: [x.json]")
    with pytest.raises(SchemaError, match="phase 'b'.reads"):
        parse_schema(bad)


def test_packaged_healing_contract_is_canonical(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    assert schema.loops["healing"].counter == "state.phases.healing.attempts_used"
    rules = [r.verdict.value for r in schema.gates["fixer-safety-gate"].rules]
    assert rules == ["needs_human_review", "pass"]
