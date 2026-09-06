from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).parents[3]
HARNESS = REPO / "benchmark" / "assurance-product" / "user_oracle_harness.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("user_oracle_harness", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_lock_authenticates_real_tracked_source_and_dependency_files() -> None:
    harness = _load_harness()

    verified = harness.verify_runtime_lock(harness.FIXTURE_ROOT)

    assert verified["source_digest"].startswith("sha256:")
    assert verified["runtime_digest"].startswith("sha256:")
    assert "sut-source/app/models/admin.py" in verified["files"]
    assert "sut-source/LICENSE" in verified["files"]
    assert "requirements.lock" in verified["files"]


def test_prepare_uses_only_the_committed_snapshot_and_confines_all_outputs(tmp_path: Path) -> None:
    harness = _load_harness()
    assert "fixture_root" not in inspect.signature(harness.prepare).parameters
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    project = workspace / "project"
    run_root = workspace / "runs" / "attempt-1"

    receipt = harness.prepare(
        workspace_root=workspace,
        project_dir=project,
        run_root=run_root,
    )

    assert receipt["state"] == "prepared"
    assert Path(receipt["project_dir"]) == project.resolve()
    assert Path(receipt["run_root"]) == run_root.resolve()
    assert Path(receipt["sqlite_path"]) == (run_root / "sut" / "db.sqlite3").resolve()
    assert (project / "app" / "models" / "admin.py").is_file()
    assert (run_root / "sut" / "app" / "models" / "admin.py").is_file()
    assert not list(project.rglob("node_modules"))
    assert not list(project.rglob("*.sqlite3"))


def test_prepare_fails_closed_when_snapshot_is_missing_without_parent_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    missing = workspace / "missing-fixture"
    monkeypatch.setattr(harness, "FIXTURE_ROOT", missing)

    with pytest.raises(ValueError, match="NOT_READY.*snapshot"):
        harness.prepare(
            workspace_root=workspace,
            project_dir=workspace / "project",
            run_root=workspace / "runs" / "attempt-1",
        )


@pytest.mark.parametrize("field", ["project_dir", "run_root"])
def test_prepare_rejects_paths_outside_the_selected_worktree(tmp_path: Path, field: str) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    values = {
        "workspace_root": workspace,
        "project_dir": workspace / "project",
        "run_root": workspace / "runs" / "attempt-1",
    }
    values[field] = tmp_path / "outside"

    with pytest.raises(ValueError, match="worktree"):
        harness.prepare(**values)


def test_reserved_socket_cannot_be_claimed_by_an_unknown_process() -> None:
    harness = _load_harness()

    listener, port = harness.reserve_loopback_socket()
    try:
        with pytest.raises(OSError):
            harness.bind_loopback_port(port)
    finally:
        listener.close()


def test_start_rejects_a_drifted_materialized_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "attempt-1",
    )
    (Path(prepared["sut_dir"]) / "app" / "__init__.py").write_text("DRIFT = True\n")
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        monkeypatch.setenv(name, "runtime-only")

    with pytest.raises(ValueError, match="runtime drifted"):
        harness.start(
            prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
            python_executable=Path(sys.executable),
        )


def test_stop_refuses_an_unknown_process_without_signalling_it(tmp_path: Path) -> None:
    harness = _load_harness()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        forged = {
            "schema_version": "1",
            "state": "started",
            "instance_id": "forged",
            "pid": process.pid,
            "process_command": [sys.executable, "-m", "uvicorn", "app:app"],
            "process_fingerprint": "forged",
            "base_url": "http://127.0.0.1:32123",
            "sqlite_path": str((tmp_path / "db.sqlite3").resolve()),
            "source_digest": "sha256:" + "a" * 64,
            "runtime_digest": "sha256:" + "b" * 64,
            "reason": "owned_sut_started",
        }
        receipt_path = tmp_path / "owned-process.json"
        receipt_path.write_text(json.dumps(forged), encoding="utf-8")

        with pytest.raises(ValueError, match="owned process"):
            harness.stop(receipt_path=receipt_path, instance_id="forged")
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_start_and_stop_bind_the_owned_process_socket_and_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "attempt-1",
    )
    runtime_python = workspace / "runtime-python"
    runtime_python.write_text(
        """#!/usr/bin/env python3
import socket
import sys

if len(sys.argv) > 1 and sys.argv[1].endswith("bootstrap.py"):
    raise SystemExit(0)
fd = int(sys.argv[sys.argv.index("--fd") + 1])
listener = socket.socket(fileno=fd)
while True:
    connection, _ = listener.accept()
    with connection:
        connection.recv(8192)
        connection.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\nConnection: close\\r\\n\\r\\n{}")
""",
        encoding="utf-8",
    )
    runtime_python.chmod(0o755)
    monkeypatch.setenv("AA_SUT_ADMIN_PASSWORD", "runtime-only")
    monkeypatch.setenv("AA_SUT_RESET_PASSWORD", "runtime-only")
    monkeypatch.setenv("AA_SUT_SECRET_KEY", "runtime-only")

    started = harness.start(
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        python_executable=runtime_python,
        ready_timeout_s=2,
    )

    assert started["state"] == "started"
    assert started["base_url"].startswith("http://127.0.0.1:")
    assert Path(started["sqlite_path"]).is_absolute()
    assert started["source_digest"] == prepared["source_digest"]
    assert started["runtime_digest"] == prepared["runtime_digest"]
    stopped = harness.stop(
        receipt_path=Path(prepared["run_root"]) / "owned-process.json",
        instance_id=started["instance_id"],
        timeout_s=2,
    )
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "owned_sut_stopped"
