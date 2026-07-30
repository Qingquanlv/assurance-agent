"""operation:verify-plan-mechanical 落盘 evidence 文档并返回事实状态（spec C2）。"""

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.graph.handlers.plan_checks import verify_plan_mechanical
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace

DK = {
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


@pytest.fixture()
def workspace(tmp_path: Path) -> TaskWorkspace:
    project_root = tmp_path / "proj"
    change_dir = project_root / "qa" / "changes" / "CH-1"
    (change_dir / "plans").mkdir(parents=True)
    (change_dir / "cases" / "system" / "dept").mkdir(parents=True)
    (project_root / ".aa").mkdir()
    (project_root / ".aa" / "data-knowledge.yaml").write_text(yaml.safe_dump(DK), encoding="utf-8")
    (change_dir / "cases" / "system" / "dept" / "case.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "added": [], "modified": [], "removed": []}),
        encoding="utf-8",
    )
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree",
    )


def _task(layer: str = "api") -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="mechanical-plan-checks",
        graph_id="api-branch",
        target="operation:verify-plan-mechanical",
        input={"with": {"layer": layer}},
    )


def _context() -> RuntimeContext:
    return RuntimeContext.model_construct(change_id="CH-1", params={})


def test_clean_plan_writes_a_passing_document(workspace: TaskWorkspace) -> None:
    (workspace.change_dir / "plans" / "api-plan.md").write_text(
        "Data knowledge: `.aa/data-knowledge.yaml`\n", encoding="utf-8"
    )
    result = verify_plan_mechanical(_task(), workspace, _context())
    assert isinstance(result, TaskResult) and result.status == "succeeded"
    assert result.value == {"status": "pass"}
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1"
    assert doc["status"] == "pass"
    assert {check["check_id"] for check in doc["checks"]} == {
        "l1_path",
        "shared_factory",
        "assert_ideal",
        "capability_keys",
    }


def test_violating_plan_writes_findings_and_reports_fail(workspace: TaskWorkspace) -> None:
    (workspace.change_dir / "plans" / "api-plan.md").write_text(
        "read `qa/.knowledge/data-knowledge.yaml`\n", encoding="utf-8"
    )
    result = verify_plan_mechanical(_task(), workspace, _context())
    assert result.status == "succeeded"
    assert result.value == {"status": "fail"}
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    l1 = next(check for check in doc["checks"] if check["check_id"] == "l1_path")
    assert l1["status"] == "fail"
    assert l1["findings"][0]["locator"] == "plans/api-plan.md:1"


def test_a_failing_check_is_not_a_task_failure(workspace: TaskWorkspace) -> None:
    """check 只报事实；block/warn 由 policy 在 gate 决定，operation 不得自行 fail。"""
    (workspace.change_dir / "plans" / "api-plan.md").write_text(
        "read `qa/.knowledge/data-knowledge.yaml`\n", encoding="utf-8"
    )
    assert verify_plan_mechanical(_task(), workspace, _context()).error_kind is None


def test_unknown_layer_is_invalid_input(workspace: TaskWorkspace) -> None:
    result = verify_plan_mechanical(_task(layer="mobile"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_missing_data_knowledge_is_invalid_output(workspace: TaskWorkspace) -> None:
    (workspace.project_root / ".aa" / "data-knowledge.yaml").unlink()
    (workspace.change_dir / "plans" / "api-plan.md").write_text("x\n", encoding="utf-8")
    result = verify_plan_mechanical(_task(), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_case_loading_does_not_tighten_the_registered_case_schema(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_API_001",
                        "title": "invalid automation flag",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "API",
                        "module": "dept",
                        "automation": {"required": "true"},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    (workspace.change_dir / "plans" / "api-plan.md").write_text("# API Plan\n", encoding="utf-8")

    result = verify_plan_mechanical(_task(), workspace, _context())

    assert result.status == "succeeded"
    assert result.error_kind is None
