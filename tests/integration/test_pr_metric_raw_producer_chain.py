"""A1/A3 producer → collector chain (P0 regression guard).

Both metrics were structurally unmeasurable while nothing wrote their raw inputs:
``collect-diff-coverage`` reported a ``collection_failed`` gap on every batch
(and a non-empty ``collection_gaps`` routes the metrics gate straight to
``needs_human``), and ``compute-auth-matrix`` published no ``touched`` scope at
all, which the packaged ``target: touched`` floor reads as unmeasurable — also
``needs_human``, at every risk tier.

Each half is unit-tested on its own side of the file boundary, so these two
assert the join: an api batch, then the collector that consumes what it wrote.
The collectors are checked for *measurability* — no gap, and a floor-readable
number — because that is what the gate routes on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from assurance_agent.workflow.execution import runners
from assurance_agent.workflow.execution.product_diff import snapshot_product_tree
from assurance_agent.workflow.execution.runners import run_pytest_target
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.auth_matrix import compute_auth_matrix_operation
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage_operation
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-A1A3-001"
BATCH = "20260805-120000"

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
    allowed_status_codes: [403]
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
    title: dept list authorization matrix
    status: active
    priority: P1
    severity: major
    type: API
    module: system.dept
modified: []
removed: []
"""

_MATRIX_REPORT = [
    {
        "nodeid": "tests/api/test_auth_matrix.py::test_cell[/api/v1/dept/list-GET-admin_token]",
        "outcome": "passed",
        "call": {"outcome": "passed", "duration": 0.0},
    },
    {
        "nodeid": "tests/api/test_auth_matrix.py::test_cell[/api/v1/dept/list-GET-guest_token]",
        "outcome": "passed",
        "call": {"outcome": "passed", "duration": 0.0},
    },
]


def _fake_pytest(
    report_tests: list[dict[str, Any]],
    *,
    coverage: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
):
    """Stand in for the pytest subprocess, writing only what pytest would write."""

    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": report_tests}), encoding="utf-8")
        if coverage is not None:
            target = next(
                (a.split(":", 1)[1] for a in args if a.startswith("--cov-report=json:")),
                None,
            )
            assert target is not None, "runner did not ask pytest-cov for a json report"
            Path(target).write_text(json.dumps(coverage), encoding="utf-8")
        if records is not None:
            record_path = Path(kwargs["env"]["AA_AUTH_MATRIX_RECORD"])
            record_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake_run


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    (project_root / "tests" / "api").mkdir(parents=True)
    (project_root / "app").mkdir(exist_ok=True)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    return project_root, change_dir


def _operation_args(project_root: Path, change_dir: Path, node_id: str) -> tuple:
    task = ExecutableTask.model_construct(
        task_id="t1",
        node_id=node_id,
        graph_id="assurance",
        target=f"operation:{node_id}",
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
    return task, workspace, context


def test_api_batch_feeds_diff_coverage_without_a_collection_gap(tmp_path: Path, monkeypatch) -> None:
    project_root, change_dir = _project(tmp_path)
    product = project_root / "app" / "dept.py"
    product.write_text("one\ntwo\n", encoding="utf-8")
    snapshot_product_tree(project_root)
    product.write_text("one\nTWO\nthree\n", encoding="utf-8")

    batch_dir = change_dir / "execution" / "runs" / BATCH
    monkeypatch.setattr(
        runners.subprocess,
        "run",
        _fake_pytest(
            [{"nodeid": "tests/api/t.py::test_ok", "outcome": "passed", "call": {"outcome": "passed"}}],
            coverage={"files": {"app/dept.py": {"executed_lines": [2]}}},
        ),
    )
    run_pytest_target(
        project_root=project_root,
        batch_dir=batch_dir,
        change_id=CHANGE_ID,
        batch_id=BATCH,
        target="api",
        test_dir="tests/api",
        cov_package="app",
    )

    task, workspace, context = _operation_args(project_root, change_dir, "collect-diff-coverage")
    result = collect_diff_coverage_operation(task, workspace, context)
    assert result.status == "succeeded"

    evidence = json.loads((batch_dir / "coverage-diff.json").read_text(encoding="utf-8"))
    assert evidence["collection_gaps"] == []
    assert evidence["total_changed_lines"] == 2  # line 2 replaced, line 3 added
    assert evidence["covered_changed_lines"] == 1
    assert evidence["value"] == 0.5


def test_api_batch_feeds_a_measurable_touched_auth_scope(tmp_path: Path, monkeypatch) -> None:
    project_root, change_dir = _project(tmp_path)
    (project_root / ".aa" / "data-knowledge.yaml").write_text(_DATA_KNOWLEDGE, encoding="utf-8")
    case_dir = change_dir / "cases" / "dept"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(_DEPT_CASE, encoding="utf-8")

    batch_dir = change_dir / "execution" / "runs" / BATCH
    monkeypatch.setattr(
        runners.subprocess,
        "run",
        _fake_pytest(
            _MATRIX_REPORT,
            records=[
                {
                    "route": "/api/v1/dept/list",
                    "method": "GET",
                    "token": "admin_token",
                    "status_code": 200,
                    "parameterized_id": "/api/v1/dept/list-GET-admin_token",
                },
                {
                    "route": "/api/v1/dept/list",
                    "method": "GET",
                    "token": "guest_token",
                    "status_code": 403,
                    "parameterized_id": "/api/v1/dept/list-GET-guest_token",
                },
            ],
        ),
    )
    run_pytest_target(
        project_root=project_root,
        batch_dir=batch_dir,
        change_id=CHANGE_ID,
        batch_id=BATCH,
        target="api",
        test_dir="tests/api",
    )

    task, workspace, context = _operation_args(project_root, change_dir, "compute-auth-matrix")
    result = compute_auth_matrix_operation(task, workspace, context)
    assert result.status == "succeeded"

    evidence = json.loads((batch_dir / "auth-matrix.json").read_text(encoding="utf-8"))
    assert evidence["collection_gaps"] == []
    # The untouched /user cell stays in the declared denominator and out of touched.
    assert evidence["declared"] == {
        "total": 3,
        "covered": 2,
        "value": 2 / 3,
        "uncovered": ["user_list_admin"],
    }
    # What a `target: touched` floor reads: a number, not None.
    assert evidence["touched"]["value"] == 1.0
    assert evidence["touched"]["total"] == 2
