from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    start_lifecycle_invocation,
)

pytestmark = pytest.mark.usefixtures("installed_sources")


def _export(invocation, destination: Path):
    from assurance_product.export import export_invocation

    return export_invocation(
        invocation.engine,
        invocation.id,
        destination,
        authorization=invocation.authorization,
    )


def test_export_refuses_interrupted_invocation(product_runner, tmp_path: Path):
    from assurance_product.export import ResultExportError, export_invocation
    from graph_engine.runtime.secret_sources import empty_runtime_authorization

    interrupted = product_runner(review_decision="needs-human").run_to_terminal()
    assert interrupted.status == "interrupted"
    with pytest.raises(ResultExportError, match="interrupted"):
        export_invocation(
            interrupted._engine,
            interrupted._handle.invocation_id,
            tmp_path / "interrupted-export",
            authorization=empty_runtime_authorization(),
        )
    assert not (tmp_path / "interrupted-export").exists()


def test_export_refuses_stopped_invocation(product_runner, tmp_path: Path):
    from assurance_product.export import ResultExportError, export_invocation
    from graph_engine.runtime.secret_sources import empty_runtime_authorization

    stopped = product_runner(
        execution_sequence=("failed",),
        healing_decision="disallowed",
    ).run_to_terminal()
    assert stopped.status == "stopped"
    with pytest.raises(ResultExportError, match="stopped"):
        export_invocation(
            stopped._engine,
            stopped._handle.invocation_id,
            tmp_path / "stopped-export",
            authorization=empty_runtime_authorization(),
        )
    assert not (tmp_path / "stopped-export").exists()


def test_export_refuses_failed_invocation(tmp_path: Path, installed_sources, monkeypatch: pytest.MonkeyPatch):
    from assurance_product.export import ResultExportError, export_invocation
    from graph_engine.plugin_api import TaskOutcome
    from graph_engine.runtime.engine import Engine
    from graph_engine.runtime.host_protocol import TaskHostCallResult
    from tests.phase5.cli_support import _CompletingScriptedHost

    class _FailingHost(_CompletingScriptedHost):
        async def execute(self, call):
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.failed("internal", "forced export failure"),
            )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)

    def factory(root: Path, authorization) -> Engine:
        del authorization
        return Engine(
            root,
            host=_FailingHost(
                execution_sequence=(),
                coverage_sequence=(),
                threshold=0.90,
                coverage_rounds=1,
                review_decision="pass",
                healing_decision="allowed",
            ),
        )

    failed = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-failed",
        drive=True,
        require_succeeded=False,
        host_factory=factory,
    )
    try:
        with pytest.raises(ResultExportError, match="failed"):
            export_invocation(
                failed.engine,
                failed.id,
                tmp_path / "failed-export",
                authorization=failed.authorization,
            )
        assert not (tmp_path / "failed-export").exists()
    finally:
        failed.engine.close()


def test_export_refuses_wrong_authorization(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError, export_invocation
    from graph_engine.runtime.secret_sources import empty_runtime_authorization

    with pytest.raises(ResultExportError, match="authorization|lock"):
        export_invocation(
            completed_invocation.engine,
            completed_invocation.id,
            tmp_path / "wrong-auth-export",
            authorization=empty_runtime_authorization(),
        )
    assert not (tmp_path / "wrong-auth-export").exists()


def test_export_refuses_event_corruption(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    ledger = completed_invocation.engine_root / "invocations" / completed_invocation.id / "ledger"
    batches = [path for path in ledger.iterdir() if path.suffix == ".json"]
    assert batches
    target = batches[-1]
    os.chmod(target, 0o600)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload.append({"seq": 999999, "event": {"kind": "corrupted"}, "event_sha256": "0" * 64})
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResultExportError, match="event|ledger|corrupt"):
        _export(completed_invocation, tmp_path / "corrupt-export")
    assert not (tmp_path / "corrupt-export").exists()


def test_export_refuses_lock_drift(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    lock = completed_invocation.engine_root / "invocations" / completed_invocation.id / "invocation.lock.json"
    os.chmod(lock, 0o600)
    lock.write_bytes(lock.read_bytes() + b"\n")
    with pytest.raises(ResultExportError, match="lock"):
        _export(completed_invocation, tmp_path / "lock-drift-export")
    assert not (tmp_path / "lock-drift-export").exists()


def test_export_refuses_nonempty_destination(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    destination = tmp_path / "nonempty"
    destination.mkdir()
    (destination / "keep.txt").write_text("present\n", encoding="utf-8")
    with pytest.raises(ResultExportError, match="destination"):
        _export(completed_invocation, destination)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "present\n"


def test_export_refuses_inplace_original_sut(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    with pytest.raises(ResultExportError, match="destination|in-place|SUT|project"):
        _export(completed_invocation, completed_invocation.project_dir)
    assert (completed_invocation.project_dir / "README.md").is_file()


def test_export_refuses_symlink_destination(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    target = tmp_path / "symlink-target"
    target.mkdir()
    destination = tmp_path / "symlink-export"
    destination.symlink_to(target)
    with pytest.raises(ResultExportError, match="symlink"):
        _export(completed_invocation, destination)
    assert not any(target.iterdir())


def test_export_refuses_hardlink_destination(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    existing = tmp_path / "existing-file"
    existing.write_text("linked\n", encoding="utf-8")
    destination = tmp_path / "hardlink-export"
    os.link(existing, destination)
    with pytest.raises(ResultExportError, match="hard ?link|destination|regular"):
        _export(completed_invocation, destination)


def test_export_refuses_path_escape_destination(completed_invocation, tmp_path: Path):
    from assurance_product.export import ResultExportError

    with pytest.raises(ResultExportError, match="escape|destination|invocation"):
        _export(
            completed_invocation,
            completed_invocation.engine_root / "invocations" / completed_invocation.id / "workspace",
        )


def test_export_refuses_resolved_secret_bytes(
    tmp_path: Path, installed_sources, monkeypatch: pytest.MonkeyPatch
):
    from assurance_product.export import ResultExportError, export_invocation

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    leaked = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-secret",
        drive=True,
        extra_project_files={"secret.txt": SECRET_VALUE},
    )
    try:
        with pytest.raises(ResultExportError, match="secret"):
            export_invocation(
                leaked.engine,
                leaked.id,
                tmp_path / "secret-export",
                authorization=leaked.authorization,
            )
        assert not (tmp_path / "secret-export").exists()
    finally:
        leaked.engine.close()


def test_export_workspace_follows_authenticated_invocation_fd(
    completed_invocation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from assurance_product import export as export_mod
    from graph_engine.runtime.workspace import SnapshotStore

    invocation_root = completed_invocation.engine_root / "invocations" / completed_invocation.id
    with SnapshotStore(invocation_root / "workspace") as store:
        authenticated_head = store.head_tree_id()

    decoy_parent = tmp_path / "decoy-invocation"
    decoy_parent.mkdir()
    decoy_store = SnapshotStore.create(decoy_parent / "workspace", {"decoy.txt": b"swapped-tree"})
    decoy_head = decoy_store.head_tree_id()
    decoy_store.close()
    assert decoy_head != authenticated_head

    real_open = export_mod._open_invocation

    def open_then_rename_swap(engine_root: Path, invocation_id: str) -> int:
        invocation_fd = real_open(engine_root, invocation_id)
        parked = invocation_root.with_name(f"{invocation_id}.authenticated")
        invocation_root.rename(parked)
        decoy_parent.rename(invocation_root)
        return invocation_fd

    monkeypatch.setattr(export_mod, "_open_invocation", open_then_rename_swap)

    destination = tmp_path / "fd-export"
    exported = export_mod.export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        destination,
        authorization=completed_invocation.authorization,
    )

    with SnapshotStore(invocation_root / "workspace") as path_store:
        assert path_store.head_tree_id() == decoy_head
    assert exported.status.current_head_tree_id == authenticated_head
    assert (destination / "result-tree" / "README.md").is_file()
    assert not (destination / "result-tree" / "decoy.txt").exists()


def test_export_refuses_missing_terminal_receipt(
    tmp_path: Path, installed_sources, monkeypatch: pytest.MonkeyPatch
):
    from assurance_product.export import ResultExportError, export_invocation

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    started = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-receipt",
        drive=False,
    )
    try:
        with pytest.raises(ResultExportError, match="running|receipt|indeterminate"):
            export_invocation(
                started.engine,
                started.id,
                tmp_path / "receipt-export",
                authorization=started.authorization,
            )
        assert not (tmp_path / "receipt-export").exists()
    finally:
        started.engine.close()
