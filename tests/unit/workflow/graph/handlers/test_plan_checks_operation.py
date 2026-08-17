"""run_layer_plan_checks 与 applicability 写 not_applicable 文档（spec C2 / B 组）。"""

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.plan_checks import run_layer_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.handlers.plan_checks import (
    derive_plan_layer_applicability,
    load_sorted_cases,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
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


_BASE_PLAN_REVIEW = {
    "schema_version": "1.0",
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


def _write_plan_review(workspace: TaskWorkspace, layer: str, **overrides: object) -> None:
    profile = get_layer_assurance_profile(layer)
    payload = {
        **_BASE_PLAN_REVIEW,
        "review_type": f"{layer}-plan",
        "change_id": "CH-1",
        **overrides,
    }
    path = workspace.change_dir / profile.review_artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _task(layer: str = "api", *, require_review: bool = False) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="review",
        graph_id="api-plan-cycle",
        target=f"skill:aa-{layer}-plan-reviewer",
        input={"with": {"layer": layer, "require_review": require_review}},
    )


def _context() -> RuntimeContext:
    return RuntimeContext.model_construct(change_id="CH-1", params={})


def verify_plan_mechanical(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Adapter that preserves the old mechanical-op tests against run_layer_plan_checks."""
    layer = str(task.input.get("with", {}).get("layer", "")) if isinstance(task.input, dict) else ""
    raw_require_review = (
        task.input.get("with", {}).get("require_review", False) if isinstance(task.input, dict) else False
    )
    if not isinstance(raw_require_review, bool):
        return task_failure("invalid_input", "require_review must be a boolean")
    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError as err:
        return task_failure("invalid_input", str(err))
    try:
        cases = load_sorted_cases(workspace.change_dir)
        applicability = derive_layer_applicability(cases, profile)
        if not applicability.applicable:
            document = run_layer_plan_checks(layer=layer, cases=cases)
        else:
            plan_texts = {
                rel: (workspace.change_dir / rel).read_text(encoding="utf-8")
                for rel in profile.plan_artifacts
            }
            review_path = workspace.change_dir / profile.review_artifact
            review_payload = None
            if review_path.is_file():
                raw = json.loads(review_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError(f"{profile.review_artifact} is not a JSON object")
                review_payload = raw
            l1_path = workspace.project_root / ".aa" / "data-knowledge.yaml"
            data_knowledge = None
            if l1_path.is_file():
                loaded = yaml.safe_load(l1_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data_knowledge = loaded
            document = run_layer_plan_checks(
                layer=layer,
                cases=cases,
                plan_texts=plan_texts,
                review_payload=review_payload,
                data_knowledge=data_knowledge,
                change_id=context.change_id,
                require_review=raw_require_review,
            )
        path = workspace.change_dir / profile.checks_artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(document))
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"status": document.status})


def test_write_profile_plans_helper_creates_exact_paths(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api")
    assert (workspace.change_dir / "plans" / "api-plan.md").is_file()


def test_api_writes_version_two_evidence_at_the_profile_path(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, "api")
    result = verify_plan_mechanical(_task("api", require_review=True), workspace, _context())
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
    if layer in {"api", "e2e"}:
        _write_plan_review(workspace, layer)
        result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    else:
        result = verify_plan_mechanical(_task(layer, require_review=False), workspace, _context())
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
    _write_plan_review(
        workspace,
        "api",
        required_capabilities=["capabilities.domain_factories.dept.missing_thing"],
    )
    result = verify_plan_mechanical(_task("api", require_review=True), workspace, _context())
    assert result.status == "succeeded"
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    capability_check = next(check for check in doc["checks"] if check["check_id"] == "capability_keys")
    assert capability_check["status"] == "fail"
    assert capability_check["findings"][0]["actual"] == "capabilities.domain_factories.dept.missing_thing"


def test_a_failing_check_is_not_a_task_failure(workspace: TaskWorkspace) -> None:
    """capability_keys 未命中是机械 check 的 fail，不是 task 的 failed（spec C2）。"""
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(
        workspace,
        "api",
        required_capabilities=["capabilities.domain_factories.dept.missing_thing"],
    )

    result = verify_plan_mechanical(_task("api", require_review=True), workspace, _context())

    assert result.status == "succeeded"
    assert result.value == {"status": "fail"}
    assert result.error_kind is None
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["status"] == "fail"


def test_missing_review_on_first_pass_yields_empty_required_capabilities(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    result = verify_plan_mechanical(_task("api", require_review=False), workspace, _context())
    assert result.status == "succeeded"
    doc = json.loads((workspace.change_dir / "review" / "api-plan-checks.json").read_text(encoding="utf-8"))
    capability_check = next(check for check in doc["checks"] if check["check_id"] == "capability_keys")
    assert capability_check["status"] == "pass"


def test_malformed_present_review_raises_invalid_output_instead_of_ignoring(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api", "Data knowledge: `.aa/data-knowledge.yaml`\n")
    review_path = workspace.change_dir / "review" / "api-plan-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("not: [valid, yaml", encoding="utf-8")
    result = verify_plan_mechanical(_task("api", require_review=False), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_string_require_review_is_invalid_input(workspace: TaskWorkspace) -> None:
    _write_profile_plans(workspace, "api")
    task = ExecutableTask.model_construct(
        task_id="t1",
        node_id="review",
        graph_id="api-plan-cycle",
        target="skill:aa-api-plan-reviewer",
        input={"with": {"layer": "api", "require_review": "true"}},
    )
    result = verify_plan_mechanical(task, workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "require_review" in (result.error or "")


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_missing_review_is_invalid_output(workspace: TaskWorkspace, layer: str) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_malformed_review_is_invalid_output(workspace: TaskWorkspace, layer: str) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    profile = get_layer_assurance_profile(layer)
    review_path = workspace.change_dir / profile.review_artifact
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("not: [valid, yaml", encoding="utf-8")
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


@pytest.mark.parametrize(
    ("layer", "wrong_review_type"),
    [("api", "e2e-plan"), ("e2e", "api-plan")],
)
def test_reviewed_mode_wrong_review_type_is_invalid_output(
    workspace: TaskWorkspace, layer: str, wrong_review_type: str
) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, layer, review_type=wrong_review_type)
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "review_type" in (result.error or "")


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_wrong_change_id_is_invalid_output(workspace: TaskWorkspace, layer: str) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, layer, change_id="CH-OTHER")
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "change_id" in (result.error or "")


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_missing_l1_is_invalid_output(workspace: TaskWorkspace, layer: str) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, layer)
    (workspace.project_root / ".aa" / "data-knowledge.yaml").unlink()
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_malformed_l1_yaml_is_invalid_output(workspace: TaskWorkspace, layer: str) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, layer)
    (workspace.project_root / ".aa" / "data-knowledge.yaml").write_text("not: [valid, yaml", encoding="utf-8")
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_reviewed_mode_l1_rejected_by_data_knowledge_is_invalid_output(
    workspace: TaskWorkspace, layer: str
) -> None:
    _write_case(workspace, "E2E" if layer == "e2e" else "API", automated=True)
    _write_profile_plans(workspace, layer, "Data knowledge: `.aa/data-knowledge.yaml`\n")
    _write_plan_review(workspace, layer)
    (workspace.project_root / ".aa" / "data-knowledge.yaml").write_text(
        yaml.safe_dump({"version": "not-an-int"}), encoding="utf-8"
    )
    result = verify_plan_mechanical(_task(layer, require_review=True), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"


def test_inapplicable_scope_ignores_malformed_plans_review_and_l1(workspace: TaskWorkspace) -> None:
    _write_case(workspace, "E2E", automated=False)
    (workspace.project_root / ".aa" / "data-knowledge.yaml").write_text("not: [valid, yaml", encoding="utf-8")
    review_path = workspace.change_dir / "review" / "e2e-plan-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("{not json", encoding="utf-8")
    (workspace.change_dir / "plans" / "e2e-plan.md").parent.mkdir(parents=True)
    (workspace.change_dir / "plans" / "e2e-plan.md").write_text("not a plan bundle", encoding="utf-8")

    result = verify_plan_mechanical(_task("e2e", require_review=True), workspace, _context())

    assert result.status == "succeeded"
    assert result.value == {"status": "not_applicable"}
    doc = json.loads((workspace.change_dir / "review" / "e2e-plan-checks.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == "2"
    assert doc["applicability"]["applicable"] is False
    assert [check["check_id"] for check in doc["checks"]] == list(PLAN_CHECK_IDS)
    assert all(
        check["status"] == "not_applicable" and check["applicability_reason"] == "layer_not_applicable"
        for check in doc["checks"]
    )


def test_fuzz_unreviewed_mode_with_optional_malformed_review_is_invalid_output(
    workspace: TaskWorkspace,
) -> None:
    _write_case(workspace, "Fuzz", automated=True)
    _write_profile_plans(workspace, "fuzz")
    profile = get_layer_assurance_profile("fuzz")
    review_path = workspace.change_dir / profile.review_artifact
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    result = verify_plan_mechanical(_task("fuzz", require_review=False), workspace, _context())
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
    value = result.value
    assert isinstance(value, dict)
    assert value["applicable"] is False
    assert value["reason_code"] == "no_automated_cases"


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


def test_missing_added_key_is_invalid_output(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump({"schema_version": "1.0", "modified": [], "removed": []}),
        encoding="utf-8",
    )
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "missing required key 'added'" in (result.error or "")


def test_other_layer_automated_missing_case_id_is_invalid_output(workspace: TaskWorkspace) -> None:
    case_path = workspace.change_dir / "cases" / "system" / "dept" / "case.yaml"
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "title": "cross-layer automated",
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
    result = derive_plan_layer_applicability(_applicability_task("e2e"), workspace, _context())
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "case_id must be a non-empty string" in (result.error or "")


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


def test_inapplicable_preflight_writes_not_applicable_checks(tmp_path: Path) -> None:
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
    profile = get_layer_assurance_profile("e2e")
    path = change_dir / profile.checks_artifact
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "not_applicable"
    assert payload["applicability"]["applicable"] is False


def test_applicable_preflight_does_not_write_checks(workspace: TaskWorkspace) -> None:
    result = derive_plan_layer_applicability(_applicability_task("api"), workspace, _context())
    assert result.status == "succeeded"
    assert isinstance(result.value, dict)
    assert result.value["applicable"] is True
    profile = get_layer_assurance_profile("api")
    assert not (workspace.change_dir / profile.checks_artifact).exists()
