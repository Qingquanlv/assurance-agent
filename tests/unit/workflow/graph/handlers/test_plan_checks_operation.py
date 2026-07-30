"""operation:verify-plan-mechanical 落盘 evidence 文档并返回事实状态（spec C2）。"""

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.handlers.plan_checks import (
    derive_plan_layer_applicability,
    verify_plan_mechanical,
)
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


def _write_profile_plans(workspace: TaskWorkspace, layer: str, body: str = "# Plan\n") -> None:
    profile = get_layer_assurance_profile(layer)
    for rel in profile.plan_artifacts:
        path = workspace.change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _write_case(workspace: TaskWorkspace, case_type: str = "API", *, automated: bool = True) -> None:
    case_dir = workspace.change_dir / "cases" / "system" / "dept"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.yaml").write_text(
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
                        "type": case_type,
                        "module": "dept",
                        "automation": {"required": automated},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> TaskWorkspace:
    project_root = tmp_path / "proj"
    change_dir = project_root / "qa" / "changes" / "CH-1"
    (change_dir / "cases" / "system" / "dept").mkdir(parents=True)
    (project_root / ".aa").mkdir()
    (project_root / ".aa" / "data-knowledge.yaml").write_text(yaml.safe_dump(DK), encoding="utf-8")
    ws = TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree",
    )
    _write_case(ws, "API", automated=True)
    return ws


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


def test_write_profile_plans_helper_creates_exact_paths(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api")
    assert (workspace.change_dir / "plans" / "api-plan.md").is_file()


def test_api_writes_version_two_evidence_at_the_profile_path(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert isinstance(result, TaskResult) and result.status == "succeeded"
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == "2"
    assert doc["layer"] == "api"
    assert doc["applicability"]["applicable"] is True


@pytest.mark.parametrize(
    ("layer", "case_type", "checks_path"),
    [
        ("e2e", "E2E", "e2e-plan-checks.json"),
        ("fuzz", "Fuzz", "fuzz-plan-checks.json"),
        ("performance", "Performance", "performance-plan-checks.json"),
    ],
)
def test_other_layers_write_their_own_profile_declared_paths(
    workspace: TaskWorkspace, layer: str, case_type: str, checks_path: str
) -> None:
    _write_case(workspace, case_type, automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    result = verify_plan_mechanical(_task(layer), workspace, _context())
    assert result.status == "succeeded"
    assert (workspace.change_dir / "review" / checks_path).is_file()


def test_empty_scope_layer_succeeds_without_plan_or_l1_and_is_not_applicable(
    workspace: TaskWorkspace,
) -> None:
    _write_case(workspace, "E2E", automated=False)
    result = verify_plan_mechanical(_task("e2e"), workspace, _context())
    assert result.status == "succeeded"
    assert result.value == {"status": "not_applicable"}
    doc = json.loads((workspace.change_dir / "review" / "e2e-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["applicability"]["applicable"] is False
    assert all(check["status"] == "not_applicable" for check in doc["checks"])


def test_applicable_layer_missing_one_exact_plan_is_invalid_output(workspace: TaskWorkspace) -> None:
    profile = get_layer_assurance_profile("api")
    for rel in profile.plan_artifacts[1:]:
        path = workspace.change_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Plan\n", encoding="utf-8")
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert profile.plan_artifacts[0] in (result.error or "")


def test_applicable_layer_missing_data_knowledge_is_invalid_output(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api")
    (workspace.project_root / ".aa" / "data-knowledge.yaml").unlink()
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_string_automation_required_is_invalid_output_not_empty_scope(workspace: TaskWorkspace) -> None:
    case_dir = workspace.change_dir / "cases" / "system" / "dept"
    (case_dir / "case.yaml").write_text(
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
                        "automation": {"required": "true"},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_unknown_layer_is_invalid_input(workspace: TaskWorkspace) -> None:
    result = verify_plan_mechanical(_task(layer="mobile"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_review_at_canonical_path_supplies_required_capabilities_on_reviewed_pass(
    workspace: TaskWorkspace,
) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    review_path = workspace.change_dir / "review" / "api-plan-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(
        json.dumps({"required_capabilities": ["domain_factories.dept.missing_thing"]}),
        encoding="utf-8",
    )
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "succeeded"
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    capability_check = next(check for check in doc["checks"] if check["check_id"] == "capability_keys")
    assert capability_check["status"] == "fail"
    assert capability_check["findings"][0]["actual"] == "domain_factories.dept.missing_thing"


def test_a_failing_check_is_not_a_task_failure(workspace: TaskWorkspace) -> None:
    """capability_keys 未命中是机械 check 的 fail，不是 task 的 failed（spec C2）。"""
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    review_path = workspace.change_dir / "review" / "api-plan-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(
        json.dumps({"required_capabilities": ["domain_factories.dept.missing_thing"]}),
        encoding="utf-8",
    )

    result = verify_plan_mechanical(_task("api"), workspace, _context())

    assert result.status == "succeeded"
    assert result.value == {"status": "fail"}
    assert result.error_kind is None
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["status"] == "fail"


def test_missing_review_on_first_pass_yields_empty_required_capabilities(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "succeeded"
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    capability_check = next(check for check in doc["checks"] if check["check_id"] == "capability_keys")
    assert capability_check["status"] == "pass"


def test_malformed_present_review_raises_invalid_output_instead_of_ignoring(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    review_path = workspace.change_dir / "review" / "api-plan-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("not: [valid, yaml", encoding="utf-8")
    result = verify_plan_mechanical(_task("api"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_case_loading_does_not_tighten_the_registered_case_schema(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api")
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_API_001",
                        "title": "extra fields tolerated",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "API",
                        "module": "dept",
                        "automation": {"required": True},
                        "owner": "someone",
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )

    result = verify_plan_mechanical(_task(), workspace, _context())

    assert result.status == "succeeded"
    assert result.error_kind is None


# ---------------------------------------------------------------------------
# operation:derive-plan-layer-applicability — cases-only preflight (no plans/review/L1)


def _applicability_task(layer: str = "e2e") -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="derive-plan-layer-applicability",
        graph_id="e2e-branch",
        target="operation:derive-plan-layer-applicability",
        input={"with": {"layer": layer}},
    )


def test_derive_plan_layer_applicability_is_registered() -> None:
    assert "operation:derive-plan-layer-applicability" in default_operations()
    assert (
        default_operations()["operation:derive-plan-layer-applicability"] is derive_plan_layer_applicability
    )


def test_automated_matching_cases_are_applicable(workspace: TaskWorkspace) -> None:
    _write_case(workspace, "E2E", automated=True)
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "succeeded"
    assert result.value == {
        "layer": "e2e",
        "applicable": True,
        "reason_code": "automated_cases_present",
        "case_ids": ["TC_DEPT_001"],
    }


def test_empty_cases_directory_is_not_applicable(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    change_dir = project_root / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    ws = TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree",
    )
    result = derive_plan_layer_applicability(_applicability_task("e2e"), ws, _context())
    assert result.status == "succeeded"
    assert result.value == {
        "layer": "e2e",
        "applicable": False,
        "reason_code": "no_automated_cases",
        "case_ids": [],
    }


def test_manual_only_cases_are_not_applicable(workspace: TaskWorkspace) -> None:
    _write_case(workspace, "E2E", automated=False)
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "succeeded"
    assert result.value == {
        "layer": "e2e",
        "applicable": False,
        "reason_code": "no_automated_cases",
        "case_ids": [],
    }


def test_other_layer_cases_do_not_make_target_layer_applicable(workspace: TaskWorkspace) -> None:
    # workspace fixture already wrote an automated API case; asking for e2e must be inapplicable.
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "succeeded"
    assert result.value["applicable"] is False
    assert result.value["reason_code"] == "no_automated_cases"


def test_malformed_case_yaml_is_invalid_output(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text("not: [valid, yaml", encoding="utf-8")
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_non_list_bucket_is_invalid_output(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump({"schema_version": "1.0", "added": "not-a-list", "modified": [], "removed": []}),
        encoding="utf-8",
    )
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_strict_rejection_of_string_automation_required(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_002",
                        "title": "case",
                        "status": "active",
                        "priority": "P1",
                        "severity": "major",
                        "type": "E2E",
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
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_unknown_layer_is_invalid_input_for_applicability(workspace: TaskWorkspace) -> None:
    result = derive_plan_layer_applicability(_applicability_task("mobile"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
