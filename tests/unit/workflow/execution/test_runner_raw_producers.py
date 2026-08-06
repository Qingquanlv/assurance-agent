"""Runner producers for the metrics raw/ layer (api target only).

``run_pytest_target`` owns two raw artifact producers after an api batch:

- ``raw/changed-lines.json`` — product diff vs ``.aa/cache/diff-base/`` (the
  SUT has no .git); the base advances only after a passed batch, but bootstraps
  after any batch so a never-green SUT still acquires a reference point.
- ``raw/auth-matrix-executions.json`` — SUT recorder JSONL (env
  ``AA_AUTH_MATRIX_RECORD``) joined with pytest json-report outcomes by the
  nodeid ``[...]`` bracket param.

Both are fail-closed: no base / no JSONL → no file, never fabricated content.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from assurance_agent.workflow.execution import runners
from assurance_agent.workflow.execution.product_diff import DIFF_BASE_REL, snapshot_product_tree
from assurance_agent.workflow.execution.runners import run_pytest_target


def _stub_pytest(report_tests: list[dict], *, jsonl_records: list[dict] | None = None):
    """Fake subprocess.run writing the canned json report (+ optional recorder JSONL)."""

    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": report_tests}), encoding="utf-8")
        if jsonl_records is not None:
            record_path = Path(kwargs["env"]["AA_AUTH_MATRIX_RECORD"])
            record_path.write_text(
                "".join(json.dumps(record) + "\n" for record in jsonl_records),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, stdout="pytest output", stderr="")

    return fake_run


_PASSED_TEST = {
    "nodeid": "tests/api/t.py::test_tc_api_001__ok",
    "outcome": "passed",
    "call": {"outcome": "passed", "duration": 0.0},
}


def _run_api(project_root: Path, batch_dir: Path):
    return run_pytest_target(
        project_root=project_root,
        batch_dir=batch_dir,
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
    )


def test_api_first_run_writes_no_changed_lines_but_snapshots(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dept.py").write_text("one\n", encoding="utf-8")
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([_PASSED_TEST]))

    result = _run_api(tmp_path, tmp_path / "batch")

    assert result.status == "passed"
    assert not (tmp_path / "batch" / "raw" / "changed-lines.json").exists()
    assert (tmp_path / DIFF_BASE_REL / "index.json").is_file()


def test_api_run_after_base_writes_changed_lines_and_advances_base(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dept.py").write_text("one\ntwo\n", encoding="utf-8")
    snapshot_product_tree(tmp_path)
    (tmp_path / "app" / "dept.py").write_text("one\nTWO\nthree\n", encoding="utf-8")
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([_PASSED_TEST]))

    _run_api(tmp_path, tmp_path / "batch")

    payload = json.loads((tmp_path / "batch" / "raw" / "changed-lines.json").read_text(encoding="utf-8"))
    assert payload == {"app/dept.py": [2, 3]}

    # Base advanced past the green batch: a second identical run diffs clean.
    _run_api(tmp_path, tmp_path / "batch2")
    payload2 = json.loads((tmp_path / "batch2" / "raw" / "changed-lines.json").read_text(encoding="utf-8"))
    assert payload2 == {}


def test_failed_api_batch_still_diffs_but_does_not_advance_base(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dept.py").write_text("one\n", encoding="utf-8")
    snapshot_product_tree(tmp_path)
    (tmp_path / "app" / "dept.py").write_text("one\ntwo\n", encoding="utf-8")
    failed_test = {**_PASSED_TEST, "outcome": "failed", "call": {"outcome": "failed", "duration": 0.0}}
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([failed_test]))

    result = _run_api(tmp_path, tmp_path / "batch")

    assert result.status == "failed"
    payload = json.loads((tmp_path / "batch" / "raw" / "changed-lines.json").read_text(encoding="utf-8"))
    assert payload == {"app/dept.py": [2]}
    # Snapshot NOT advanced: the next run still diffs against the last green base.
    (tmp_path / "app" / "dept.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([_PASSED_TEST]))
    _run_api(tmp_path, tmp_path / "batch2")
    payload2 = json.loads((tmp_path / "batch2" / "raw" / "changed-lines.json").read_text(encoding="utf-8"))
    assert payload2 == {"app/dept.py": [2, 3]}


def test_failed_first_api_batch_still_bootstraps_the_base(tmp_path: Path, monkeypatch) -> None:
    """A red first batch must leave a base behind, or A1 is wedged forever.

    Advancing is gated on green; bootstrapping cannot be, because a SUT whose api
    suite never goes fully green would never get a base and every batch would
    report diff_coverage as a collection gap.
    """
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dept.py").write_text("one\n", encoding="utf-8")
    failed_test = {**_PASSED_TEST, "outcome": "failed", "call": {"outcome": "failed", "duration": 0.0}}
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([failed_test]))

    result = _run_api(tmp_path, tmp_path / "batch")

    assert result.status == "failed"
    assert not (tmp_path / "batch" / "raw" / "changed-lines.json").exists()
    assert (tmp_path / DIFF_BASE_REL / "index.json").is_file()

    # The base is now usable: the next batch measures against it while still red.
    (tmp_path / "app" / "dept.py").write_text("one\ntwo\n", encoding="utf-8")
    _run_api(tmp_path, tmp_path / "batch2")
    payload = json.loads((tmp_path / "batch2" / "raw" / "changed-lines.json").read_text(encoding="utf-8"))
    assert payload == {"app/dept.py": [2]}


def test_non_api_target_writes_no_producers(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "dept.py").write_text("one\n", encoding="utf-8")
    snapshot_product_tree(tmp_path)
    (tmp_path / "app" / "dept.py").write_text("one\ntwo\n", encoding="utf-8")

    envs: list[dict] = []

    def fake_run(args, **kwargs):
        envs.append(kwargs["env"])
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": [_PASSED_TEST]}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)
    run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="e2e",
        test_dir="tests/e2e",
    )

    assert "AA_AUTH_MATRIX_RECORD" not in envs[0]
    raw = tmp_path / "batch" / "raw"
    assert not (raw / "changed-lines.json").exists()
    assert not (raw / "auth-matrix-executions.json").exists()


_MATRIX_TESTS = [
    {
        "nodeid": "tests/api/test_auth_matrix.py::test_auth_cell[/api/v1/dept/list-GET-admin_token]",
        "outcome": "passed",
        "call": {"outcome": "passed", "duration": 0.0},
    },
    {
        "nodeid": "tests/api/test_auth_matrix.py::test_auth_cell[/api/v1/dept/list-GET-guest_token]",
        "outcome": "failed",
        "call": {"outcome": "failed", "duration": 0.0},
    },
]


def test_api_run_joins_recorder_jsonl_with_report_outcomes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    records = [
        # Deliberately unsorted input; output must be sorted by (route, method, token).
        {
            "route": "/api/v1/dept/list",
            "method": "GET",
            "token": "guest_token",
            "status_code": 200,
            "parameterized_id": "/api/v1/dept/list-GET-guest_token",
        },
        {
            "route": "/api/v1/dept/list",
            "method": "GET",
            "token": "admin_token",
            "status_code": 200,
            "parameterized_id": "/api/v1/dept/list-GET-admin_token",
        },
        {
            # No matching report nodeid → dropped, never guessed.
            "route": "/api/v1/dept/list",
            "method": "GET",
            "token": "ghost_token",
            "status_code": 200,
            "parameterized_id": "/api/v1/dept/list-GET-ghost_token",
        },
    ]
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest(_MATRIX_TESTS, jsonl_records=records))

    _run_api(tmp_path, tmp_path / "batch")

    payload = json.loads(
        (tmp_path / "batch" / "raw" / "auth-matrix-executions.json").read_text(encoding="utf-8")
    )
    assert payload == [
        {
            "actual_status_code": 200,
            "method": "GET",
            "outcome": "passed",
            "parameterized_id": "/api/v1/dept/list-GET-admin_token",
            "route": "/api/v1/dept/list",
            "token": "admin_token",
        },
        {
            "actual_status_code": 200,
            "method": "GET",
            "outcome": "failed",
            "parameterized_id": "/api/v1/dept/list-GET-guest_token",
            "route": "/api/v1/dept/list",
            "token": "guest_token",
        },
    ]


def test_api_run_without_recorder_jsonl_writes_no_executions(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(runners.subprocess, "run", _stub_pytest([_PASSED_TEST]))

    _run_api(tmp_path, tmp_path / "batch")

    assert not (tmp_path / "batch" / "raw" / "auth-matrix-executions.json").exists()


def test_api_run_sets_record_env_for_subprocess(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    envs: list[dict] = []

    def fake_run(args, **kwargs):
        envs.append(kwargs["env"])
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": [_PASSED_TEST]}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)
    _run_api(tmp_path, tmp_path / "batch")

    record_path = Path(envs[0]["AA_AUTH_MATRIX_RECORD"])
    assert record_path.parent == (tmp_path / "batch" / "raw")
    assert record_path.name == "auth-matrix-records.jsonl"
