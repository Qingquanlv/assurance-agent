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


def test_serialize_and_validate_shape(tmp_path: Path) -> None:
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    text = serialize_context(ctx)
    assert text.endswith("\n")
    ok, errors = validate_context_shape(ctx)
    assert ok is True and errors == []


def test_historical_issue_from_json_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(
        tmp_path,
        "qa/archive/A-001/known-product-issues.json",
        '{"issues": [{"id": "KPI-1", "module": "menus", "severity": "high", "status": "open"}]}',
    )
    write(tmp_path, "qa/archive/A-001/archive-summary.md", "archived_at: '2026-07-10T00:00:00Z'\n")
    monkeypatch.setattr(
        ctxmod,
        "get_changed_files",
        lambda root, base: ctxmod.GitDiffResult(changed_files=[], no_git=False, degraded_reasons=[]),
    )
    ctx = build_risk_context(change_id="CH-1", project_root=tmp_path, now=FIXED_NOW)
    assert [h.id for h in ctx.historical_issues] == ["KPI-1"]
    assert any(e.type == "historical_issue" and e.issue_id == "KPI-1" for e in ctx.evidence)
