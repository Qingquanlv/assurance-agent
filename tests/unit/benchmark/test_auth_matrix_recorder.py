"""SUT-side auth-matrix recorder (benchmark/vue-fastapi-admin tests helper).

``record_auth_cell`` appends one JSONL line per executed matrix cell to the
path in ``AA_AUTH_MATRIX_RECORD`` (set by ``aa run`` for the api target) and is
a no-op otherwise, so hand-run pytest is unaffected.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[3]
_RECORDER = _ROOT / "benchmark" / "vue-fastapi-admin" / "tests" / "helpers" / "auth_matrix_recorder.py"


def _load_recorder():
    spec = importlib.util.spec_from_file_location("auth_matrix_recorder", _RECORDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["auth_matrix_recorder"] = module
    spec.loader.exec_module(module)
    return module


def test_record_auth_cell_is_noop_without_env(tmp_path: Path, monkeypatch) -> None:
    recorder = _load_recorder()
    monkeypatch.delenv("AA_AUTH_MATRIX_RECORD", raising=False)
    recorder.record_auth_cell(route="/api/v1/dept/list", method="GET", token="admin_token", status_code=200)
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_record_auth_cell_appends_jsonl_with_current_param_id(tmp_path: Path, monkeypatch) -> None:
    recorder = _load_recorder()
    target = tmp_path / "records.jsonl"
    monkeypatch.setenv("AA_AUTH_MATRIX_RECORD", str(target))
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST",
        "tests/api/test_auth_matrix.py::test_auth_cell[/api/v1/dept/list-GET-admin_token] (call)",
    )
    recorder.record_auth_cell(route="/api/v1/dept/list", method="GET", token="admin_token", status_code=200)
    recorder.record_auth_cell(route="/api/v1/dept/list", method="GET", token="guest_token", status_code=403)
    recorder.close_auth_matrix_recorder()

    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert rows == [
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
            "parameterized_id": "/api/v1/dept/list-GET-admin_token",
        },
    ]


def test_record_auth_cell_without_bracket_param_records_empty_id(tmp_path: Path, monkeypatch) -> None:
    recorder = _load_recorder()
    target = tmp_path / "records.jsonl"
    monkeypatch.setenv("AA_AUTH_MATRIX_RECORD", str(target))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/api/test_x.py::test_plain (call)")
    recorder.record_auth_cell(route="/r", method="GET", token="t", status_code=200)
    recorder.close_auth_matrix_recorder()
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["parameterized_id"] == ""
