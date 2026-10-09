from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assurance_product import worker_lifecycle as lifecycle, worker_cleanup as cleanup


@pytest.fixture(autouse=True)
def current_lifecycle_modules(monkeypatch):
    # installed_sources evicts collected modules when it installs extracted wheels.
    # The coordinator and cleanup must share the same ownership exception identity.
    from assurance_product import worker_lifecycle, worker_cleanup

    monkeypatch.setitem(globals(), "lifecycle", worker_lifecycle)
    monkeypatch.setitem(globals(), "cleanup", worker_cleanup)


def worker(workspace: Path) -> subprocess.Popen[str]:
    code = """import signal, sys, time
from pathlib import Path
from assurance_product.worker_lifecycle import acquire_execution
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with acquire_execution(Path(sys.argv[1]), "run"):
 print("ready", flush=True)
 time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(workspace)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def test_real_process_excludes_same_workspace_and_allows_independent_workspace(tmp_path: Path) -> None:
    process = worker(tmp_path / "one")
    try:
        with pytest.raises(lifecycle.ExecutionConflict):
            with lifecycle.acquire_execution(tmp_path / "one", "other"):
                pytest.fail("admitted overlapping writer")
        with lifecycle.acquire_execution(tmp_path / "two", "other"):
            pass
    finally:
        process.kill()
        process.wait()


def test_force_stop_confirms_exit_before_replacement(tmp_path: Path) -> None:
    process = worker(tmp_path)
    try:
        result = lifecycle.request_stop(tmp_path, force=True, timeout=0.1)
        assert result == "stopped"
        process.wait(timeout=2)
        with lifecycle.acquire_execution(tmp_path, "replacement"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_identity_mismatch_refuses_signal_and_stays_blocked(tmp_path: Path) -> None:
    process = worker(tmp_path)
    try:
        owner_path = lifecycle.control_root(tmp_path) / "owner.json"
        owner = json.loads(owner_path.read_text())
        owner["process"]["created"] = "wrong"
        owner_path.write_text(json.dumps(owner))
        assert lifecycle.request_stop(tmp_path, force=True, timeout=0.01) == "unconfirmed"
        assert process.poll() is None
        with pytest.raises(lifecycle.ExecutionConflict):
            with lifecycle.acquire_execution(tmp_path, "replacement"):
                pass
    finally:
        process.kill()
        process.wait()


def test_external_acknowledgment_keeps_dead_worker_blocked(tmp_path: Path) -> None:
    process = worker(tmp_path)
    process.kill()
    process.wait()
    assert lifecycle.request_stop(tmp_path, confirm_external=lambda owner: False) == "unconfirmed"
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "replacement"):
            pass
    assert lifecycle.request_stop(tmp_path, confirm_external=lambda owner: True) == "stopped"
    with lifecycle.acquire_execution(tmp_path, "replacement"):
        pass


def test_nested_ownership_requires_exact_invocation(tmp_path: Path) -> None:
    with lifecycle.acquire_execution(tmp_path, "one") as owner:
        with lifecycle.acquire_execution(tmp_path, "one") as nested:
            assert owner is nested
        with pytest.raises(lifecycle.ExecutionConflict):
            with lifecycle.acquire_execution(tmp_path, "two"):
                pass


def test_background_reservation_handoff_is_atomic(tmp_path: Path) -> None:
    code = """import sys, time
from pathlib import Path
from assurance_product.worker_lifecycle import admit_background
with admit_background(Path(sys.argv[1]), "run"):
 time.sleep(60)
"""
    with lifecycle.acquire_execution(tmp_path, "run") as owner:
        process = lifecycle.launch_reserved(owner, [sys.executable, "-c", code, str(tmp_path)], os.environ)
    try:
        with pytest.raises(lifecycle.ExecutionConflict):
            with lifecycle.acquire_execution(tmp_path, "replacement"):
                pass
        assert lifecycle.request_stop(tmp_path, force=True, timeout=0.1) == "stopped"
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_stale_run_stop_cannot_signal_new_owner(tmp_path: Path) -> None:
    process = worker(tmp_path)
    try:
        assert lifecycle.request_stop(tmp_path, force=True, expected_invocation="old-run") == "unconfirmed"
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_cleanup_failure_preserves_admission_block(tmp_path: Path) -> None:
    process = worker(tmp_path)
    process.kill()
    process.wait()

    def broken_cleanup(owner):
        raise OSError("disk unavailable")

    assert lifecycle.request_stop(tmp_path, cleanup=broken_cleanup) == "unconfirmed"
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "new"):
            pass
    assert lifecycle.request_stop(tmp_path) == "stopped"


def test_quiescent_library_calls_release_normally_in_one_process(tmp_path: Path) -> None:
    with lifecycle.acquire_execution(tmp_path, "one"):
        pass
    with lifecycle.acquire_execution(tmp_path, "two"):
        pass


def test_delayed_old_stop_nonce_cannot_terminate_replacement(tmp_path: Path, monkeypatch) -> None:
    import threading
    from contextlib import contextmanager

    original = lifecycle.control_lock
    captured = threading.Event()
    release = threading.Event()

    @contextmanager
    def delayed_control(root):
        if threading.current_thread().name == "delayed-stop":
            captured.set()
            assert release.wait(10)
        with original(root):
            yield

    old = worker(tmp_path)
    replacement = None
    result = []
    monkeypatch.setattr(lifecycle, "control_lock", delayed_control)
    thread = threading.Thread(
        target=lambda: result.append(lifecycle.request_stop(tmp_path, force=True, timeout=0.1)),
        name="delayed-stop",
    )
    try:
        thread.start()
        assert captured.wait(5)
        assert lifecycle.request_stop(tmp_path, force=True, timeout=0.1) == "stopped"
        old.wait(timeout=2)
        replacement = worker(tmp_path)
        release.set()
        thread.join(timeout=5)
        assert result == ["unconfirmed"]
        assert replacement.poll() is None
    finally:
        release.set()
        thread.join(timeout=5)
        for process in (old, replacement):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


def test_abnormal_registered_library_execution_stays_blocked(tmp_path: Path) -> None:
    import asyncio

    # Composition fixtures evict/reimport workspace packages. Use the same
    # current module instance as the journal imported below.
    from assurance_product import worker_lifecycle as lifecycle
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
    from graph_engine.attempts.models.keys import AttemptKey

    workspace = lifecycle.control_root(tmp_path)

    async def register():
        opened = ChangeWorkspace.prepare(tmp_path.resolve(), "run")
        async with open_sqlite_checkpointer(opened) as backend:
            await SqliteAttemptCheckpointStore(backend).register_generation(
                {"invocation_id": "run"}, lambda ordinal: AttemptKey(digest="a" * 64), max_attempts=3
            )

    with pytest.raises(RuntimeError, match="cancelled"):
        with lifecycle.acquire_execution(tmp_path, "run"):
            asyncio.run(register())
            raise RuntimeError("cancelled")
    assert json.loads((workspace / "owner.json").read_text())["state"] == "stopping"
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "run"):
            pass


@pytest.mark.parametrize("replacement", ["missing", "empty", "missing_rows"])
@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_stop_refuses_lost_registered_persistence(
    tmp_path: Path, replacement: str, crash_after_commit: bool
) -> None:
    import asyncio
    import sqlite3
    from assurance_product import worker_lifecycle as lifecycle
    from assurance_product.retained_host import confirm_owned_calls

    code = """import asyncio, os, sys, time
from pathlib import Path
from assurance_product.worker_lifecycle import acquire_execution
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
from graph_engine.attempts.models.keys import AttemptKey
async def register(root):
 async with open_sqlite_checkpointer(ChangeWorkspace.prepare(root, "run")) as backend:
  if sys.argv[2] == "crash":
   commit = backend._conn.commit
   async def crash_commit():
    await commit()
    print("ready", flush=True)
    os._exit(17)
   backend._conn.commit = crash_commit
  await SqliteAttemptCheckpointStore(backend).register_generation({"invocation_id":"run"}, lambda ordinal: AttemptKey(digest="a"*64), max_attempts=3)
with acquire_execution(Path(sys.argv[1]), "run"):
 asyncio.run(register(Path(sys.argv[1])))
 print("ready", flush=True)
 time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), "crash" if crash_after_commit else "normal"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    if crash_after_commit:
        assert process.wait(timeout=5) == 17
    else:
        process.kill()
        process.wait()
    root = lifecycle.control_root(tmp_path)
    db = tmp_path / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT ordinal FROM assurance_attempt_generations").fetchall() == [(1,)]
    db.unlink()
    if replacement != "missing":
        with sqlite3.connect(db) as connection:
            if replacement == "missing_rows":
                connection.execute(
                    "CREATE TABLE assurance_attempt_generations (attempt_key_digest TEXT, owner_nonce TEXT)"
                )
                connection.execute("CREATE TABLE assurance_host_calls (call_digest TEXT, owner_nonce TEXT)")
    record = json.loads((root / "owner.json").read_text())
    with pytest.raises(lifecycle.ExecutionConflict):
        cleanup.validated_stop_checkpoint(record)
    assert (
        lifecycle.request_stop(
            tmp_path,
            force=True,
            confirm_external=lambda owner: asyncio.run(confirm_owned_calls(owner)),
            cleanup=cleanup.cleanup_owned_resources,
        )
        == "unconfirmed"
    )
    assert json.loads((root / "owner.json").read_text())["state"] == "stopping"
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "replacement"):
            pytest.fail("lost generation budget admitted")


@pytest.mark.parametrize("unsafe", ["file", "ancestor"])
@pytest.mark.parametrize("phase", ["confirmation", "cleanup"])
def test_stop_rejects_unsafe_database_before_sqlite_access(
    tmp_path: Path, monkeypatch, unsafe: str, phase: str
) -> None:
    import asyncio
    import sqlite3
    from assurance_product import worker_lifecycle as lifecycle
    from assurance_product.retained_host import confirm_owned_calls

    workspace = tmp_path / "workspace"
    process = worker(workspace)
    process.kill()
    process.wait()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    target = foreign / "checkpoints.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE foreign_data (value TEXT)")
        connection.execute("INSERT INTO foreign_data VALUES ('preserve')")
    original = target.read_bytes()
    runtime = workspace / "qa" / ".runtime"
    runtime.mkdir(parents=True)
    if unsafe == "file":
        (runtime / "langgraph").mkdir()
        (runtime / "langgraph" / "checkpoints.sqlite3").symlink_to(target)
    else:
        (runtime / "langgraph").symlink_to(foreign, target_is_directory=True)
    opened = []

    def forbidden_connect(*args, **kwargs):
        opened.append(args)
        raise AssertionError("unsafe checkpoint reached SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    assert (
        lifecycle.request_stop(
            workspace,
            force=True,
            confirm_external=(lambda owner: asyncio.run(confirm_owned_calls(owner)))
            if phase == "confirmation"
            else (lambda owner: True),
            cleanup=cleanup.cleanup_owned_resources,
        )
        == "unconfirmed"
    )
    assert opened == []
    assert target.read_bytes() == original
    assert sorted(path.name for path in foreign.iterdir()) == ["checkpoints.sqlite3"]
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(workspace, "replacement"):
            pytest.fail("unsafe checkpoint admitted")


def test_released_owner_cannot_reset_lost_generation_budget(tmp_path: Path) -> None:
    from assurance_product import worker_lifecycle as lifecycle

    with lifecycle.acquire_execution(tmp_path, "run") as owner:
        lifecycle.update_owner(owner, lambda record: record.setdefault("attempts", []).append("a" * 64))
    with pytest.raises(lifecycle.ExecutionConflict, match="persistence is missing"):
        with lifecycle.acquire_execution(tmp_path, "run"):
            pytest.fail("released ownership lost its finite budget")


@pytest.mark.parametrize("replacement", ["missing", "empty"])
def test_generation_adoption_publishes_evidence_before_commit_crash(tmp_path: Path, replacement: str) -> None:
    import sqlite3
    from assurance_product import worker_lifecycle as lifecycle

    code = """import asyncio, os, sys
from pathlib import Path
from assurance_product.worker_lifecycle import acquire_execution
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
from graph_engine.attempts.models.keys import AttemptKey
root = Path(sys.argv[1])
async def generation(adopt):
 async with open_sqlite_checkpointer(ChangeWorkspace.prepare(root, "run")) as backend:
  journal = SqliteAttemptCheckpointStore(backend)
  if not adopt:
   await journal.register_generation({"invocation_id":"run"}, lambda ordinal: AttemptKey(digest="a"*64), max_attempts=3)
  else:
   commit = backend._conn.commit
   async def crash_commit():
    await commit()
    os._exit(17)
   backend._conn.commit = crash_commit
   await journal.latest_generation({"invocation_id":"run"})
with acquire_execution(root, "run"):
 asyncio.run(generation(False))
with acquire_execution(root, "run"):
 asyncio.run(generation(True))
"""
    process = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=10)
    assert process.returncode == 17
    record = json.loads((lifecycle.control_root(tmp_path) / "owner.json").read_text())
    db = tmp_path / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT owner_nonce FROM assurance_attempt_generations").fetchall() == [
            (record["nonce"],)
        ]
    assert record["attempts"] == ["a" * 64]
    db.unlink()
    if replacement == "empty":
        with sqlite3.connect(db):
            pass
    assert (
        lifecycle.request_stop(tmp_path, force=True, cleanup=cleanup.cleanup_owned_resources) == "unconfirmed"
    )
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path, "replacement"):
            pytest.fail("adopted generation lost its budget evidence")
