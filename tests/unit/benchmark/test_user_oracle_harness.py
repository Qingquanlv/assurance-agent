from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from urllib.request import urlopen

import pytest


REPO = Path(__file__).parents[3]
HARNESS = REPO / "benchmark" / "assurance-product" / "user_oracle_harness.py"
FIXTURE = REPO / "benchmark" / "assurance-product" / "fixtures" / "user-oracle"
BOOTSTRAP = FIXTURE / "bootstrap.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_harness():
    return _load(HARNESS, "user_oracle_harness")


def _load_bootstrap():
    return _load(BOOTSTRAP, "user_oracle_bootstrap")


def test_bootstrap_seed_contains_exactly_one_disabled_administrator(tmp_path: Path) -> None:
    bootstrap = _load_bootstrap()
    db = tmp_path / "db.sqlite3"

    bootstrap.initialize_database(
        db, FIXTURE / "sut-source" / "migrations" / "models" / "0_20260721171822_init.py"
    )

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            'SELECT username, email, password, is_active, is_superuser FROM "user"'
        ).fetchall()
    assert rows == [("admin", "admin@benchmark.invalid", bootstrap._DISABLED_PASSWORD, 1, 1)]


def test_runtime_lock_authenticates_real_source_and_complete_dependencies() -> None:
    harness = _load_harness()

    verified = harness.verify_runtime_lock(harness.FIXTURE_ROOT)

    assert verified["source_digest"].startswith("sha256:")
    assert verified["runtime_digest"].startswith("sha256:")
    assert "sut-source/app/models/admin.py" in verified["files"]
    assert "sut-source/LICENSE" in verified["files"]
    assert "qualify_runtime.py" in verified["files"]
    requirements = (harness.FIXTURE_ROOT / "requirements.in").read_text()
    assert "loguru==0.7.3" in requirements
    assert "setuptools==75.8.0" in requirements


def test_materialize_project_writes_frozen_lock(tmp_path: Path) -> None:
    harness = _load_harness()
    project = tmp_path / "project"
    selected = harness.materialize_project(project_dir=project, fault="none")
    lock = project / ".aa/user-oracle/runtime-lock.json"
    assert lock.is_file()
    document = json.loads(lock.read_bytes())
    assert document["fault"] == "none"
    assert selected["fault"] == "none"
    assert (project / "app").is_dir()
    assert (project / "requirements.lock").is_file()


def test_serve_and_stop_shared_sut(tmp_path: Path) -> None:
    harness = _load_harness()
    project = tmp_path / "project"
    harness.materialize_project(project_dir=project)
    os.environ["AA_SUT_ADMIN_PASSWORD"] = "serve-test-admin"
    os.environ["AA_SUT_RESET_PASSWORD"] = "serve-test-admin"
    os.environ["AA_SUT_SECRET_KEY"] = "serve-test-admin"
    started = harness.serve(project)
    try:
        assert started["base_url"].startswith("http://127.0.0.1:")
        assert Path(started["sqlite_path"]).is_file()
        assert started["pid"] > 1
        with urlopen(started["base_url"] + "/openapi.json", timeout=2) as response:
            assert response.status == 200
        assert not (project / ".ownership-token").exists()
        assert not (project / "owned-process.json").exists()
    finally:
        harness.stop_served(started["pid"])


def test_reserved_socket_cannot_be_claimed_by_unknown_process() -> None:
    harness = _load_harness()
    listener, port = harness.reserve_loopback_socket()
    try:
        with pytest.raises(OSError):
            harness.bind_loopback_port(port)
    finally:
        listener.close()
