"""compute-auth-matrix — declared cells × parameterized execution (Task 6 / §5-A3)."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.models.data_knowledge import AuthMatrixCell
from assurance_agent.workflow.metrics import auth_matrix
from assurance_agent.workflow.metrics.auth_matrix import (
    AuthCellExecution,
    compute_auth_matrix,
    compute_auth_matrix_operation,
)

CHANGE_ID = "CH-A3-001"
BATCH = "20260805-100000"


def _cell(**overrides: object) -> AuthMatrixCell:
    payload = {
        "route": "/api/v1/dept/list",
        "method": "GET",
        "token": "admin_token",
        "expected": "allow",
        "allowed_status_codes": [200],
    }
    payload.update(overrides)
    return AuthMatrixCell.model_validate(payload)


def test_join_covers_unique_route_method_token_cells() -> None:
    cells = {
        "dept_list_admin": _cell(),
        "dept_list_guest": _cell(token="guest_token", expected="deny", allowed_status_codes=[401, 403]),
    }
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells=cells,
        executions=(
            AuthCellExecution(
                route="/api/v1/dept/list",
                method="GET",
                token="admin_token",
                outcome="passed",
                actual_status_code=200,
                parameterized_id="/api/v1/dept/list-GET-admin_token",
            ),
        ),
    )
    assert evidence.declared is not None
    assert evidence.declared.total == 2
    assert evidence.declared.covered == 1
    assert evidence.declared.uncovered == ("dept_list_guest",)
    by_id = {cell.cell_id: cell for cell in evidence.cells}
    assert by_id["dept_list_admin"].asserted is True
    assert by_id["dept_list_guest"].outcome == "missing"


def test_empty_matrix_is_typed_collection_gap() -> None:
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells={},
        executions=(),
    )
    assert evidence.declared is None
    assert evidence.collection_gaps[0].code == "collection_failed"
    assert evidence.collection_gaps[0].metric == "auth_matrix_coverage"


def test_passed_without_status_code_does_not_assert_allowed_codes() -> None:
    """Status-code proof is required; None must not vacuously match allowed_status_codes."""
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells={"dept_list_admin": _cell()},
        executions=(
            AuthCellExecution(
                route="/api/v1/dept/list",
                method="GET",
                token="admin_token",
                outcome="passed",
                actual_status_code=None,
                parameterized_id="/api/v1/dept/list-GET-admin_token",
            ),
        ),
    )
    cell = evidence.cells[0]
    assert cell.asserted is False
    assert cell.outcome == "unasserted"
    assert evidence.declared is not None
    assert evidence.declared.covered == 0
    assert evidence.value == 0.0


def test_touched_scope_counts_only_touched_identities() -> None:
    cells = {
        "dept_list_admin": _cell(),
        "user_list_admin": _cell(route="/api/v1/user/list"),
    }
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells=cells,
        executions=(
            AuthCellExecution(
                route="/api/v1/dept/list",
                method="GET",
                token="admin_token",
                outcome="passed",
                actual_status_code=200,
                parameterized_id="/api/v1/dept/list-GET-admin_token",
            ),
        ),
        touched_identities=frozenset({("/api/v1/dept/list", "GET", "admin_token")}),
    )
    assert evidence.touched is not None
    assert evidence.touched.total == 1
    assert evidence.touched.covered == 1
    assert evidence.touched.value == 1.0


def test_empty_touched_scope_is_total_zero_value_none_not_absent() -> None:
    """An explicitly empty touched scope means "nothing touched" (total=0), not "unknown"."""
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells={"dept_list_admin": _cell()},
        executions=(),
        touched_identities=frozenset(),
    )
    assert evidence.touched is not None
    assert evidence.touched.total == 0
    assert evidence.touched.covered == 0
    assert evidence.touched.value is None


def test_untouched_defaults_to_absent_scope() -> None:
    evidence = compute_auth_matrix(
        change_id=CHANGE_ID,
        batch_id=BATCH,
        cells={"dept_list_admin": _cell()},
        executions=(),
    )
    assert evidence.touched is None


def test_nodeid_fallback_is_removed() -> None:
    """The dead nodeid-parsing fallback (no status-code proof) must stay deleted."""
    assert not hasattr(auth_matrix, "extract_auth_executions_from_cases")
    assert not hasattr(auth_matrix, "_load_executions")


# ── operation-level: touched heuristic from change cases ─────────────────────

_DATA_KNOWLEDGE = """
version: 1
accounts: {}
auth:
  admin_token: {method: token}
  guest_token: {method: token}
entities: {}
auth_matrix:
  dept_list_admin:
    route: /api/v1/dept/list
    method: GET
    token: admin_token
    expected: allow
    allowed_status_codes: [200]
  dept_list_guest:
    route: /api/v1/dept/list
    method: GET
    token: guest_token
    expected: deny
    allowed_status_codes: [401, 403]
  user_list_admin:
    route: /api/v1/user/list
    method: GET
    token: admin_token
    expected: allow
    allowed_status_codes: [200]
"""

_DEPT_CASE = """
schema_version: "1"
added:
  - case_id: TC_DEPT_API_001
    title: dept list happy path
    status: active
    priority: P1
    severity: major
    type: API
    module: system.dept
modified: []
removed: []
"""


def _operation_inputs(tmp_path: Path, *, case_yaml: str | None, executions: list[dict]) -> tuple:
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
    from assurance_agent.workflow.graph.workspace import TaskWorkspace
    from tests.helpers_aa import write_aa_config

    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (project_root / ".aa" / "data-knowledge.yaml").write_text(_DATA_KNOWLEDGE, encoding="utf-8")
    if case_yaml is not None:
        case_dir = change_dir / "cases" / "dept"
        case_dir.mkdir(parents=True)
        (case_dir / "case.yaml").write_text(case_yaml, encoding="utf-8")
    raw = change_dir / "execution" / "runs" / BATCH / "raw"
    raw.mkdir(parents=True)
    (raw / "auth-matrix-executions.json").write_text(json.dumps(executions), encoding="utf-8")

    task = ExecutableTask.model_construct(
        task_id="t1",
        node_id="compute-auth-matrix",
        graph_id="assurance",
        target="operation:compute-auth-matrix",
        input={"with": {"batch_id": BATCH}},
    )
    workspace = TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )
    context = RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={},
    )
    return task, workspace, context, change_dir


def _read_evidence(change_dir: Path) -> dict:
    path = change_dir / "execution" / "runs" / BATCH / "auth-matrix.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_operation_marks_touched_cells_from_case_modules(tmp_path: Path) -> None:
    """module system.dept → entity dept → only /dept cells enter the touched scope."""
    executions = [
        {
            "route": "/api/v1/dept/list",
            "method": "GET",
            "token": "admin_token",
            "outcome": "passed",
            "actual_status_code": 200,
            "parameterized_id": "/api/v1/dept/list-GET-admin_token",
        },
        {
            "route": "/api/v1/user/list",
            "method": "GET",
            "token": "admin_token",
            "outcome": "passed",
            "actual_status_code": 200,
            "parameterized_id": "/api/v1/user/list-GET-admin_token",
        },
    ]
    task, workspace, context, change_dir = _operation_inputs(
        tmp_path, case_yaml=_DEPT_CASE, executions=executions
    )
    result = compute_auth_matrix_operation(task, workspace, context)
    assert result.status == "succeeded"

    evidence = _read_evidence(change_dir)
    assert evidence["declared"]["total"] == 3
    assert evidence["declared"]["covered"] == 2
    touched = evidence["touched"]
    assert touched is not None
    assert touched["total"] == 2  # both dept cells, user cell excluded
    assert touched["covered"] == 1
    assert touched["uncovered"] == ["dept_list_guest"]


def test_operation_without_cases_yields_empty_touched_scope(tmp_path: Path) -> None:
    """No cases → empty touched scope (total=0, value=null) — fail-closed, never absent."""
    task, workspace, context, change_dir = _operation_inputs(tmp_path, case_yaml=None, executions=[])
    result = compute_auth_matrix_operation(task, workspace, context)
    assert result.status == "succeeded"

    evidence = _read_evidence(change_dir)
    assert evidence["touched"] is not None
    assert evidence["touched"]["total"] == 0
    assert evidence["touched"]["covered"] == 0
    assert evidence["touched"]["value"] is None
