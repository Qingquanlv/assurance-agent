import json
from pathlib import Path

import pytest

from assurance_agent.risk import context as ctxmod
from assurance_agent.risk.context import (
    build_risk_context,
    serialize_context,
    validate_context_shape,
)
from assurance_agent.risk.safety import RiskSafetyError, assert_change_id_safe

FIXED_NOW = "2026-07-15T00:00:00+00:00"


def write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_problems(root: Path, *problems: dict) -> None:
    doc = {
        "schema_version": "1.0",
        "generated_at": "2026-07-25T05:00:00Z",
        "problems": list(problems),
    }
    write(root, "qa/issues/problems.json", json.dumps(doc))


def make_problem(**overrides: object) -> dict:
    doc: dict = {
        "problem_id": "PROB-active-001",
        "fingerprint": {"version": "1", "digest": "sha256:fingerprint-active"},
        "title": "Menu delete returns HTTP 500",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "Missing validation before delete.",
        },
        "status": "detected",
        "first_seen": {"change_id": "CHANGE-A", "occurrence_id": "OCC-a1"},
        "last_seen": {"change_id": "CHANGE-A", "occurrence_id": "OCC-a1"},
        "occurrences": ["OCC-a1"],
        "resolution": None,
        "version": 1,
    }
    doc.update(overrides)
    return doc


def test_assert_change_id_rejects_traversal() -> None:
    assert_change_id_safe("REQ-002-user-logout")
    with pytest.raises(RiskSafetyError):
        assert_change_id_safe("../escape")
    with pytest.raises(RiskSafetyError):
        assert_change_id_safe("a/b")


def test_build_context_non_git_is_degraded(tmp_path: Path) -> None:
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.no_git is True
    assert ctx.degraded is True
    assert any(r.startswith("no_git") for r in ctx.degraded_reasons)
    assert ctx.schema_version == "1.0"
    assert ctx.change_id == "CH-1"
    assert ctx.generated_at == FIXED_NOW


def test_build_context_aggregates_modules_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        ".aa/module-map.yaml",
        'rules:\n  - pattern: "backend/app/menus/**"\n    modules: ["menus"]\n    confidence: high\n',
    )
    write(
        tmp_path,
        "qa/cases/menus/case.yaml",
        "added:\n"
        "  - case_id: TC_MENU_001\n    module: menus\n    priority: P1\n    automation:\n      required: true\n",
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(
            changed_files=["backend/app/menus/service.py"], no_git=False, degraded_reasons=[]
        ),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)

    assert [m.name for m in ctx.impact.modules] == ["menus"]
    assert ctx.impact.modules[0].confidence == "high"
    assert ctx.impact.affected_case_ids == ["TC_MENU_001"]
    assert ctx.impact.affected_cases_by_module == {"menus": ["TC_MENU_001"]}
    ev_ids = [e.id for e in ctx.evidence]
    assert "EV-DIFF-MENUS-HIGH" in ev_ids
    assert ctx.case_signals[0].automation_status == "automated"
    assert any(r.startswith("no_archives") for r in ctx.degraded_reasons)
    assert any(r.startswith("no_history") for r in ctx.degraded_reasons)


def test_serialize_and_validate_shape(tmp_path: Path) -> None:
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    text = serialize_context(ctx)
    assert text.endswith("\n")
    ok, errors = validate_context_shape(ctx)
    assert ok is True and errors == []


def test_historical_issue_from_active_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_problems(tmp_path, make_problem())
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert [h.problem_id for h in ctx.historical_issues] == ["PROB-active-001"]
    issue = ctx.historical_issues[0]
    assert issue.status == "detected"
    assert issue.classification == "product_bug"
    assert issue.severity == "high"
    assert issue.first_seen_change_id == "CHANGE-A"
    assert issue.occurrence_count == 1
    assert issue.evidence_id == "EV-PROBLEM-PROB-ACTIVE-001"
    hist_evidence = [e for e in ctx.evidence if e.type == "historical_issue"]
    assert len(hist_evidence) == 1
    assert hist_evidence[0].issue_id == "PROB-active-001"
    assert hist_evidence[0].projection_digest is not None
    assert hist_evidence[0].occurrence_ids == ["OCC-a1"]
    assert not any(r.startswith("no_history") for r in ctx.degraded_reasons)


def test_historical_issue_from_resolved_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_problems(
        tmp_path,
        make_problem(
            problem_id="PROB-resolved-001",
            status="resolved",
            resolution={
                "resolved_at": "2026-07-26T00:00:00Z",
                "change_id": "CHANGE-B",
                "batch_id": "20260726-010000",
                "disposition": "healing/api-apply-summary.json",
                "verification_scope": ["API_MENU_001"],
                "evidence_digest": "sha256:verify",
            },
            version=2,
        ),
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues[0].status == "resolved"
    assert ctx.historical_issues[0].problem_id == "PROB-resolved-001"


def test_historical_issue_from_accepted_risk_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_problems(
        tmp_path,
        make_problem(
            problem_id="PROB-risk-001",
            status="accepted_risk",
            assessment={
                "classification": "product_bug",
                "severity": "medium",
                "authority": "human_confirmed",
                "root_cause_hypothesis": "Known edge case accepted.",
            },
            version=2,
        ),
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues[0].status == "accepted_risk"
    assert ctx.historical_issues[0].severity == "medium"


def test_historical_issue_from_not_an_issue_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_problems(
        tmp_path,
        make_problem(
            problem_id="PROB-not-issue-001",
            status="not_an_issue",
            assessment={
                "classification": "test_bug",
                "severity": "low",
                "authority": "human_confirmed",
                "root_cause_hypothesis": "Test expectation was wrong.",
            },
            version=2,
        ),
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues[0].status == "not_an_issue"
    assert ctx.historical_issues[0].classification == "test_bug"


def test_historical_issue_merge_alias_appears_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_problems(
        tmp_path,
        make_problem(
            problem_id="PROB-canonical-001",
            title="Canonical menu delete failure",
            last_seen={"change_id": "CHANGE-C", "occurrence_id": "OCC-c2"},
            occurrences=["OCC-c1", "OCC-c2"],
            version=2,
        ),
        make_problem(
            problem_id="PROB-alias-001",
            fingerprint={"version": "1", "digest": "sha256:fingerprint-alias"},
            title="Alias duplicate",
            status="resolved",
            resolution={
                "resolved_at": "2026-07-26T00:00:00Z",
                "change_id": "merged",
                "batch_id": "merged",
                "disposition": "merged_into:PROB-canonical-001",
                "verification_scope": ["merged"],
                "evidence_digest": "sha256:merge",
            },
            version=2,
        ),
    )
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert [h.problem_id for h in ctx.historical_issues] == ["PROB-canonical-001"]
    assert len([e for e in ctx.evidence if e.type == "historical_issue"]) == 1


def test_no_history_when_projection_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues == []
    assert any(r.startswith("no_history") for r in ctx.degraded_reasons)


def test_corrupt_projection_is_visible_degraded_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "qa/issues/problems.json", "{not valid json")
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues == []
    assert any(r.startswith("corrupt_problems_projection") for r in ctx.degraded_reasons)
    assert not any(r.startswith("no_history") for r in ctx.degraded_reasons)


def test_archive_legacy_issue_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_json = "known-product-issues" + ".json"
    write(
        tmp_path,
        f"qa/archive/A-001/{legacy_json}",
        '{"issues": [{"id": "KPI-1", "module": "menus", "severity": "high", "status": "open"}]}',
    )
    write(tmp_path, "qa/archive/A-001/archive-summary.md", "archived_at: '2026-07-10T00:00:00Z'\n")
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert ctx.historical_issues == []
    assert any(r.startswith("no_history") for r in ctx.degraded_reasons)
