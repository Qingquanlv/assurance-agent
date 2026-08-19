"""Reviewer finalize writes plan-checks before freeze; it does not apply policy."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

import assurance_agent.workflow.graph.finalize as finalize
from assurance_agent.workflow.graph.contracts import ResourceClaims
from assurance_agent.workflow.graph.models import CompiledWorkflow, ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore


_DK = {
    "version": 1,
    "capabilities": {
        "domain_factories": {
            "dept": {
                "make_dept": {
                    "kind": "async_factory",
                    "symbol": "tests.testdata.domain.dept.make_dept",
                }
            }
        }
    },
}

_REVIEW = {
    "schema_version": "1.0",
    "review_type": "api-plan",
    "change_id": "CH-1",
    "decision": "pass",
    "codegen_readiness": "ready",
    "auto_fix_allowed": False,
    "human_review_required": False,
    "risk_level": "low",
    "findings": [],
    "auto_fix_plan": [],
    "next_action": "continue",
    "required_capabilities": ["capabilities.domain_factories.dept.make_dept"],
}


def _workspace(tmp_path: Path) -> TaskWorkspace:
    project = tmp_path / "proj"
    change_dir = project / "qa" / "changes" / "CH-1"
    (change_dir / "plans").mkdir(parents=True)
    (change_dir / "cases" / "system" / "dept").mkdir(parents=True)
    (change_dir / "review").mkdir(parents=True)
    (project / ".aa").mkdir()
    (project / ".aa" / "data-knowledge.yaml").write_text(yaml.safe_dump(_DK), encoding="utf-8")
    (change_dir / "cases" / "system" / "dept" / "case.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_001",
                        "title": "case",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "API",
                        "module": "dept",
                        "automation": {"required": True},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    return TaskWorkspace(
        task_id="t1",
        root=project,
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        base_tree_id="",
    )


def _write_plans(ws: TaskWorkspace, body: str) -> None:
    for name in ("api-plan.md", "api-test-data-plan.md", "api-codegen-plan.md"):
        (ws.change_dir / "plans" / name).write_text(body, encoding="utf-8")


def _finalize_reviewer(
    ws: TaskWorkspace, monkeypatch: pytest.MonkeyPatch, *, result: TaskResult | None = None
) -> TaskResult:
    monkeypatch.setattr(finalize, "_ensure_outputs_frozen", lambda **kwargs: kwargs["result"])
    monkeypatch.setattr(finalize, "_ingest_frozen_outputs", lambda **kwargs: kwargs["result"])
    monkeypatch.setattr(finalize, "_apply_subgraph_exports", lambda **kwargs: kwargs["result"])
    task = cast(
        ExecutableTask,
        SimpleNamespace(
            graph_id="api-plan-cycle",
            node_id="review",
            target="skill:aa-api-plan-reviewer",
            input={},
            resources=ResourceClaims(),
        ),
    )
    context = RuntimeContext(
        project_root=ws.project_root,
        repo_root=ws.repo_root,
        change_dir=ws.change_dir,
        change_id="CH-1",
    )
    return finalize.finalize_task_result(
        compiled=cast(CompiledWorkflow, SimpleNamespace(graphs={})),
        store=cast(TreeStore, SimpleNamespace()),
        task=task,
        result=result or TaskResult(status="succeeded"),
        workspace=ws,
        context=context,
    )


def test_reviewer_finalize_writes_plan_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _workspace(tmp_path)
    _write_plans(ws, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    (ws.change_dir / "review" / "api-plan-review.json").write_text(json.dumps(_REVIEW), encoding="utf-8")

    result = _finalize_reviewer(ws, monkeypatch)

    assert result.status == "succeeded"
    path = ws.change_dir / "review" / "api-plan-checks.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "2"
    assert doc["layer"] == "api"


def test_reviewer_finalize_build_failure_is_invalid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _write_plans(ws, "Data knowledge: `.aa/data-knowledge.yaml`\n")

    result = _finalize_reviewer(ws, monkeypatch)

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert not (ws.change_dir / "review" / "api-plan-checks.json").exists()


def test_reviewer_finalize_succeeds_when_check_fails_under_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _write_plans(ws, "See `docs/data-knowledge.yaml`\n")
    (ws.change_dir / "review" / "api-plan-review.json").write_text(json.dumps(_REVIEW), encoding="utf-8")

    result = _finalize_reviewer(ws, monkeypatch)

    assert result.status == "succeeded"
    doc = json.loads((ws.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["status"] == "fail"
    l1 = next(check for check in doc["checks"] if check["check_id"] == "l1_path")
    assert l1["status"] == "fail"


def test_reviewer_plan_checks_read_l1_from_host_not_workspace(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.reviewer_plan_checks import complete_reviewer_plan_checks

    host = tmp_path / "sut"
    host_change = host / "qa" / "changes" / "CH-1"
    (host_change / "plans").mkdir(parents=True)
    (host_change / "cases" / "system" / "dept").mkdir(parents=True)
    (host_change / "review").mkdir(parents=True)
    (host / ".aa").mkdir()
    (host / ".aa" / "data-knowledge.yaml").write_text(yaml.safe_dump(_DK), encoding="utf-8")
    (host_change / "cases" / "system" / "dept" / "case.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_001",
                        "title": "case",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "API",
                        "module": "dept",
                        "automation": {"required": True},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    task_ws = tmp_path / "task-ws"
    task_ws.mkdir()
    (task_ws / ".aa").mkdir()
    (task_ws / ".aa" / "data-knowledge.yaml").write_text(
        yaml.safe_dump({"version": 1, "capabilities": {"domain_factories": {}}}),
        encoding="utf-8",
    )
    for name in ("api-plan.md", "api-test-data-plan.md", "api-codegen-plan.md"):
        (host_change / "plans" / name).write_text(
            "Data knowledge: `.aa/data-knowledge.yaml`\n",
            encoding="utf-8",
        )
    (host_change / "review" / "api-plan-review.json").write_text(json.dumps(_REVIEW), encoding="utf-8")
    workspace = TaskWorkspace(
        task_id="t1",
        root=task_ws,
        project_root=task_ws,
        repo_root=task_ws,
        change_dir=host_change,
        base_tree_id="",
    )
    task = cast(
        ExecutableTask,
        SimpleNamespace(
            graph_id="api-plan-cycle",
            node_id="review",
            target="skill:aa-api-plan-reviewer",
            input={},
            resources=ResourceClaims(),
        ),
    )
    context = RuntimeContext(
        project_root=task_ws,
        repo_root=task_ws,
        change_dir=host_change,
        change_id="CH-1",
        host_project_root=host,
    )
    complete_reviewer_plan_checks(task=task, workspace=workspace, context=context)
    doc = json.loads((host_change / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    capability = next(check for check in doc["checks"] if check["check_id"] == "capability_keys")
    assert capability["status"] == "pass"
