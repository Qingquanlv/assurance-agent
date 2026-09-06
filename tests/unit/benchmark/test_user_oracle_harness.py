from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

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


@pytest.fixture(scope="session")
def qualified_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    supplied = os.environ.get("AA_USER_ORACLE_TEST_PYTHON")
    if supplied:
        return Path(supplied)
    runtime = tmp_path_factory.mktemp("user-oracle-runtime")
    subprocess.run(
        ["uv", "venv", str(runtime), "--python", "3.11"],
        check=True,
        capture_output=True,
        text=True,
    )
    python = runtime / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(FIXTURE / "requirements.lock"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return python


def _prepare(harness, tmp_path: Path, qualified_python: Path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    receipt = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "attempt-1",
        python_executable=qualified_python,
    )
    return workspace, receipt


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


def test_prepare_qualifies_actual_snapshot_and_confines_outputs(
    tmp_path: Path, qualified_python: Path
) -> None:
    harness = _load_harness()
    assert "fixture_root" not in inspect.signature(harness.prepare).parameters

    workspace, receipt = _prepare(harness, tmp_path, qualified_python)

    assert receipt["state"] == "prepared"
    assert Path(receipt["project_dir"]) == (workspace / "project").resolve()
    assert Path(receipt["sqlite_path"]) == (workspace / "runs/attempt-1/sut/db.sqlite3").resolve()
    assert receipt["runtime_qualification"]["python_version"].startswith("3.11.")
    assert receipt["runtime_qualification"]["distributions"]["loguru"] == "0.7.3"
    assert receipt["runtime_qualification"]["app_module"].startswith(receipt["sut_dir"])
    assert not list((workspace / "project").rglob("node_modules"))
    assert not list((workspace / "project").rglob("*.sqlite3"))


def test_prepare_rejects_arbitrary_fake_python(tmp_path: Path) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    fake = workspace / "python"
    fake.write_text('#!/bin/sh\nprintf \'{"python_executable":"fake"}\'\n', encoding="utf-8")
    fake.chmod(0o755)

    with pytest.raises(ValueError, match="runtime qualification is invalid"):
        harness.prepare(
            workspace_root=workspace,
            project_dir=workspace / "project",
            run_root=workspace / "runs/attempt-1",
            python_executable=fake,
        )


def test_prepare_fails_closed_without_parent_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    monkeypatch.setattr(harness, "FIXTURE_ROOT", workspace / "missing-fixture")

    with pytest.raises(ValueError, match="NOT_READY.*snapshot"):
        harness.prepare(
            workspace_root=workspace,
            project_dir=workspace / "project",
            run_root=workspace / "runs/attempt-1",
            python_executable=Path(sys.executable),
        )


@pytest.mark.parametrize("field", ["project_dir", "run_root"])
def test_prepare_rejects_paths_outside_the_worktree(tmp_path: Path, field: str) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    values = {
        "workspace_root": workspace,
        "project_dir": workspace / "project",
        "run_root": workspace / "runs/attempt-1",
        "python_executable": Path(sys.executable),
    }
    values[field] = tmp_path / "outside"

    with pytest.raises(ValueError, match="worktree"):
        harness.prepare(**values)


def test_reserved_socket_cannot_be_claimed_by_unknown_process() -> None:
    harness = _load_harness()
    listener, port = harness.reserve_loopback_socket()
    try:
        with pytest.raises(OSError):
            harness.bind_loopback_port(port)
    finally:
        listener.close()


def test_start_rejects_drifted_runtime(
    tmp_path: Path, qualified_python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace, prepared = _prepare(harness, tmp_path, qualified_python)
    (Path(prepared["sut_dir"]) / "app" / "__init__.py").write_text("DRIFT = True\n")
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        monkeypatch.setenv(name, "runtime-only")

    with pytest.raises(ValueError, match="runtime drifted"):
        harness.start(
            workspace_root=workspace,
            prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        )


def test_edited_prepare_paths_cannot_redirect_start(tmp_path: Path, qualified_python: Path) -> None:
    harness = _load_harness()
    workspace, prepared = _prepare(harness, tmp_path, qualified_python)
    receipt_path = Path(prepared["run_root"]) / "harness-prepare.json"
    edited = json.loads(receipt_path.read_text())
    outside = tmp_path / "outside"
    edited["sqlite_path"] = str(outside / "db.sqlite3")
    receipt_path.write_text(json.dumps(edited), encoding="utf-8")

    with pytest.raises(ValueError, match="receipt digest"):
        harness.start(workspace_root=workspace, prepare_receipt=receipt_path)
    assert not outside.exists()


def test_real_bootstrap_start_stop_lifecycle(
    tmp_path: Path, qualified_python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace, prepared = _prepare(harness, tmp_path, qualified_python)
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        monkeypatch.setenv(name, "runtime-only")

    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        ready_timeout_s=10,
    )
    try:
        with sqlite3.connect(started["sqlite_path"]) as connection:
            rows = connection.execute(
                'SELECT username, email, password FROM "user" WHERE is_superuser = 1'
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][:2] == ("admin", "admin@benchmark.invalid")
        assert rows[0][2].startswith("$argon2")
        assert started["process_command"][:5] == [
            str(qualified_python),
            "-B",
            "-m",
            "uvicorn",
            "app:app",
        ]
    finally:
        stopped = harness.stop(
            workspace_root=workspace,
            receipt_path=Path(prepared["run_root"]) / "owned-process.json",
            instance_id=started["instance_id"],
            timeout_s=5,
        )
    assert stopped["state"] == "stopped"


def test_stop_refuses_another_valid_uvicorn_process(tmp_path: Path, qualified_python: Path) -> None:
    harness = _load_harness()
    workspace, prepared = _prepare(harness, tmp_path, qualified_python)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "app.py").write_text("async def app(scope, receive, send):\n    pass\n", encoding="utf-8")
    listener, _ = harness.reserve_loopback_socket()
    command = [
        str(qualified_python),
        "-B",
        "-m",
        "uvicorn",
        "app:app",
        "--fd",
        str(listener.fileno()),
        "--no-access-log",
    ]
    process = subprocess.Popen(
        command,
        cwd=foreign,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(listener.fileno(),),
    )
    listener.close()
    try:
        time.sleep(0.2)
        assert process.poll() is None
        run_root = Path(prepared["run_root"])
        marker = run_root / "live-foreign.json"
        token = harness._read_ownership_token(run_root)
        forged = harness._seal(
            {
                "schema_version": "1",
                "state": "started",
                "workspace_root": str(workspace.resolve()),
                "run_root": str(run_root),
                "sut_dir": prepared["sut_dir"],
                "prepare_receipt": str(run_root / "harness-prepare.json"),
                "prepare_receipt_digest": prepared["receipt_digest"],
                "instance_id": "foreign",
                "pid": process.pid,
                "process_command": command,
                "process_fingerprint": harness._command_fingerprint(command),
                "process_birth_identity": harness._process_birth_identity(process.pid),
                "live_marker": str(marker),
                "live_marker_digest": "sha256:" + "0" * 64,
                "base_url": "http://127.0.0.1:1",
                "sqlite_path": prepared["sqlite_path"],
                "stderr_path": str(run_root / "managed-sut.log"),
                "source_digest": prepared["source_digest"],
                "runtime_digest": prepared["runtime_digest"],
                "reason": "owned_sut_started",
            },
            token,
        )
        harness._write_json(run_root / "owned-process.json", forged)

        with pytest.raises(ValueError, match="live marker"):
            harness.stop(
                workspace_root=workspace,
                receipt_path=run_root / "owned-process.json",
                instance_id="foreign",
            )
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
