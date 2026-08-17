"""Layer 2: audited gate-read path patterns (TS audit_scope.ts mirror)."""

from assurance_agent.workflow.core.audit_scope import is_audited_gate_read


def test_review_json_is_audited() -> None:
    assert is_audited_gate_read("review/case-review.json")
    assert is_audited_gate_read("review/api-plan-review.json")
    assert is_audited_gate_read("review/plan-review.json")


def test_healing_safety_and_apply_summaries_are_audited() -> None:
    assert is_audited_gate_read("healing/fixer-safety-check.json")
    assert is_audited_gate_read("healing/api-apply-summary.json")
    assert is_audited_gate_read("healing/e2e-apply-summary.json")
    assert is_audited_gate_read("inspect/inspect-safety-check.json")


def test_non_audited_paths_are_excluded() -> None:
    assert not is_audited_gate_read("inspect/failure-analysis.json")
    assert not is_audited_gate_read("execution/execution-manifest.json")
    assert not is_audited_gate_read("review/nested/dir.json")
    assert not is_audited_gate_read("healing/other.json")
    assert not is_audited_gate_read("repo:src/x.py")
