from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_intake.contracts.explore import EXPLORE_OUTPUT_PATHS
from assurance_intake.operations.explore_context import build_explore_context

_LEAFS = (
    "capabilities.domain_factories.dept.make_dept",
    "entities.dept.constraints.name_unique",
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "qa/requirement.md",
        "# Dept\n\nRework `app/api/v1/depts/depts.py` and `app/controllers/missing.py`.\n"
        "Mention app/api/v1/depts/depts.py twice.\n",
    )
    _write(tmp_path, "app/api/v1/depts/depts.py", "router = None\n")
    _write(
        tmp_path,
        "qa/cases/system/dept/case.yaml",
        "schema_version: '1.0'\n"
        "added:\n"
        "  - case_id: TC_DEPT_API_001\n"
        "    title: create top-level department\n"
        "    module: system.dept\n"
        "modified: []\n"
        "removed: []\n",
    )
    _write(
        tmp_path,
        "qa/issues/problems.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-09-17T00:00:00Z",
                "problems": [
                    {
                        "problem_id": "PROB-1",
                        "title": "500 on duplicate department name",
                        "status": "open",
                        "assessment": {
                            "classification": "product_bug",
                            "severity": "high",
                            "authority": "llm_provisional",
                        },
                    }
                ],
            }
        ),
    )
    return tmp_path


def test_output_paths_are_fixed() -> None:
    assert EXPLORE_OUTPUT_PATHS == (
        "qa/results/explore/exploration-draft.json",
        "qa/results/explore/impact-inventory.json",
    )


def test_context_projects_requirement_hints_cases_and_history(tmp_path: Path) -> None:
    context = build_explore_context(_workspace(tmp_path), change_id="CH-1", capability_leafs=_LEAFS)

    impact = context.impact
    assert impact.diff_base == "content-snapshot"
    assert [seed.model_dump(mode="json") for seed in impact.seeds] == [
        {
            "seed_id": "CF-001",
            "path": "app/api/v1/depts/depts.py",
            "symbol": None,
            "reason": "requirement_hint",
        }
    ]
    assert impact.unobserved_hints == ("app/controllers/missing.py",)
    assert [(case.evidence_id, case.case_id, case.module, case.path) for case in impact.candidate_cases] == [
        ("CS-001", "TC_DEPT_API_001", "system.dept", "qa/cases/system/dept/case.yaml")
    ]
    assert [
        (p.evidence_id, p.problem_id, p.classification, p.status) for p in impact.historical_problems
    ] == [("HI-001", "PROB-1", "product_bug", "open")]
    assert impact.factory_leafs == ("capabilities.domain_factories.dept.make_dept",)
    assert impact.resolvable_ids() >= {"CF-001", "CS-001", "TC_DEPT_API_001", "HI-001", "PROB-1"}
    assert "no_diff: no authenticated diff projection was supplied" in context.degraded_reasons
    assert not any(
        reason.startswith(
            ("no_cases", "no_history", "case_history_not_projected", "problem_history_not_projected")
        )
        for reason in context.degraded_reasons
    )
    assert context.degraded is True


def test_context_prefers_sealed_change_evidence_over_requirement_hints(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write(
        workspace,
        "qa/results/explore/change-evidence.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "base_ref": "1111111",
                "head_ref": "2222222",
                "changed_files": [
                    {
                        "path": "app/controllers/dept.py",
                        "status": "modified",
                        "symbols": ["update_dept", "delete_dept"],
                        "digest": "a" * 64,
                    },
                    {
                        "path": "web/src/views/system/dept/index.vue",
                        "status": "modified",
                        "symbols": [],
                        "digest": "b" * 64,
                    },
                ],
            }
        ),
    )

    context = build_explore_context(workspace, change_id="CH-1", capability_leafs=_LEAFS)

    assert context.impact.diff_base == "change-evidence"
    assert [(s.seed_id, s.path, s.symbol, s.reason) for s in context.impact.seeds] == [
        ("CF-001", "app/controllers/dept.py", "update_dept", "diff"),
        ("CF-002", "app/controllers/dept.py", "delete_dept", "diff"),
        ("CF-003", "web/src/views/system/dept/index.vue", None, "diff"),
    ]
    assert context.impact.unobserved_hints == ()
    assert not any(reason.startswith("no_diff") for reason in context.degraded_reasons)


def test_context_rejects_change_evidence_for_another_change(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write(
        workspace,
        "qa/results/explore/change-evidence.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-OTHER",
                "base_ref": "1",
                "head_ref": "2",
                "changed_files": [],
            }
        ),
    )
    with pytest.raises(ValueError, match="change_id"):
        build_explore_context(workspace, change_id="CH-1", capability_leafs=_LEAFS)


def test_context_rejects_malformed_change_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write(workspace, "qa/results/explore/change-evidence.json", "{not json")
    with pytest.raises(ValueError, match="change-evidence.json"):
        build_explore_context(workspace, change_id="CH-1", capability_leafs=_LEAFS)


def test_context_is_honest_when_nothing_is_available(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    context = build_explore_context(tmp_path, change_id="CH-1", capability_leafs=())

    assert context.impact.seeds == ()
    assert context.impact.candidate_cases == ()
    assert context.impact.historical_problems == ()
    assert context.impact.factory_leafs == ()
    for prefix in ("no_git", "no_diff", "no_cases", "no_history", "no_archives"):
        assert any(reason.startswith(prefix) for reason in context.degraded_reasons), prefix
    assert context.degraded is True


def test_unreadable_case_files_are_reported_not_hidden(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write(workspace, "qa/cases/system/user/case.yaml", "added: [\n")
    context = build_explore_context(workspace, change_id="CH-1", capability_leafs=_LEAFS)
    assert "case_unreadable: qa/cases/system/user/case.yaml" in context.degraded_reasons
    assert [case.case_id for case in context.impact.candidate_cases] == ["TC_DEPT_API_001"]
