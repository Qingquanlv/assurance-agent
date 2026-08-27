from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.phase6.conformance import PHASE5_HANDOFF_PATH
from tests.phase6.test_legacy_activity_audit import (
    FORBIDDEN_BASENAMES,
    FakeProbe,
    write_driver,
    write_running_tasks,
)

ROOT = Path(__file__).resolve().parents[2]
CLOSEOUT = ROOT / ".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout"
CLOSED_FILES = (
    ".progression.lock",
    "driver.json",
    "driver.lock",
    "running-tasks.json",
    "workflow-state.json",
    "workflow-state.yaml",
)
CLOSED_DIRECTORY = ".graph-runtime"
PRESERVED_RELATIVE = (
    ".aa/config.yaml",
    "qa/cases/case.yaml",
    "qa/archive/CH-OLD/driver.json",
    "qa/archive/CH-OLD/events.jsonl",
    "qa/changes/CH-1/events.jsonl",
    "qa/changes/CH-1/status.json",
    "qa/changes/CH-1/.runtime/ledger/keep.txt",
    "qa/changes/CH-1/.staging/attempt/keep.txt",
    "qa/changes/CH-1/issues/events.jsonl",
    "qa/changes/CH-1/cases/keep.yaml",
    "tests/test_keep.py",
)


def _audit_module():
    from scripts import phase6_legacy_activity_audit as audit

    return audit


def _cleanup_module():
    from scripts import phase6_legacy_state_cleanup as cleanup

    return cleanup


def seed_cleanup_project(root: Path) -> Path:
    project = root / "sut"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_driver(change / "driver.json", pid=101)
    (change / "driver.lock").write_text("lock\n", encoding="utf-8")
    write_running_tasks(change / "running-tasks.json", (101,))
    (change / "workflow-state.json").write_text("{}\n", encoding="utf-8")
    (change / "workflow-state.yaml").write_text("version: 1\n", encoding="utf-8")
    (change / ".progression.lock").write_text("lock\n", encoding="utf-8")
    graph_runtime = change / CLOSED_DIRECTORY / "checkpoints"
    graph_runtime.mkdir(parents=True)
    (graph_runtime / "state.json").write_text("{}\n", encoding="utf-8")
    (change / "events.jsonl").write_text('{"type":"event"}\n', encoding="utf-8")
    (change / "status.json").write_text('{"status":"achieved"}\n', encoding="utf-8")
    (change / ".runtime" / "ledger").mkdir(parents=True)
    (change / ".runtime" / "ledger" / "keep.txt").write_text("keep-runtime\n", encoding="utf-8")
    (change / ".staging" / "attempt").mkdir(parents=True)
    (change / ".staging" / "attempt" / "keep.txt").write_text("keep-staging\n", encoding="utf-8")
    (change / "issues").mkdir()
    (change / "issues" / "events.jsonl").write_text("nested\n", encoding="utf-8")
    (change / "cases").mkdir()
    (change / "cases" / "keep.yaml").write_text("keep\n", encoding="utf-8")
    aa = project / ".aa"
    aa.mkdir()
    (aa / "config.yaml").write_text("product: assurance\n", encoding="utf-8")
    cases = project / "qa" / "cases"
    cases.mkdir()
    (cases / "case.yaml").write_text("id: 1\n", encoding="utf-8")
    archive = project / "qa" / "archive" / "CH-OLD"
    archive.mkdir(parents=True)
    write_driver(archive / "driver.json", pid=202)
    (archive / "events.jsonl").write_text("old\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_keep.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return project


def digest_preserved_files(project: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for relative in PRESERVED_RELATIVE:
        path = project / relative
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def ready_audit(project: Path):
    audit = _audit_module()
    return audit.audit_projects((project,), probe=FakeProbe({101: "dead", 202: "live"}))


def project_with_unsafe_entry(root: Path, kind: str):
    project = seed_cleanup_project(root)
    audit = ready_audit(project)
    target = project / "qa" / "changes" / "CH-1" / "driver.json"
    target.unlink()
    if kind == "symlink":
        target.symlink_to(root / "outside-target")
        (root / "outside-target").write_text("outside\n", encoding="utf-8")
    elif kind == "hardlink":
        other = root / "hardlink-source"
        other.write_text("shared\n", encoding="utf-8")
        os.link(other, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        raise AssertionError(kind)
    return project, audit


def test_inspection_selects_only_closed_immediate_paths(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    entries = cleanup.inspect_legacy_runtime_state(project)
    paths = {item.relative_path for item in entries}
    assert paths == {
        *(f"qa/changes/CH-1/{name}" for name in CLOSED_FILES),
        "qa/changes/CH-1/.graph-runtime",
    }
    assert "qa/archive/CH-1/events.jsonl" not in paths
    assert "qa/changes/CH-1/issues/events.jsonl" not in paths
    assert all(Path(path).name not in FORBIDDEN_BASENAMES for path in paths)


def test_inspection_never_selects_change_local_result_names(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    entries = cleanup.inspect_legacy_runtime_state(project)
    selected = {Path(item.relative_path).name for item in entries}
    assert selected.isdisjoint(FORBIDDEN_BASENAMES)
    preserved = digest_preserved_files(project)
    assert set(preserved) >= {
        "qa/changes/CH-1/events.jsonl",
        "qa/changes/CH-1/status.json",
        "qa/changes/CH-1/.runtime/ledger/keep.txt",
        "qa/changes/CH-1/.staging/attempt/keep.txt",
    }


def test_cleanup_deletes_closed_state_and_preserves_business_data(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    before = digest_preserved_files(project)
    audit = ready_audit(project)
    report = cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)

    assert report.status == "completed"
    assert report.audit_digest == audit.digest
    assert cleanup.inspect_legacy_runtime_state(project) == ()
    assert digest_preserved_files(project) == before
    change = project / "qa" / "changes" / "CH-1"
    for name in CLOSED_FILES:
        assert not (change / name).exists()
    assert not (change / CLOSED_DIRECTORY).exists()
    for name in FORBIDDEN_BASENAMES:
        assert (change / name).exists()


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    first_audit = ready_audit(project)
    first = cleanup.cleanup_legacy_runtime_state(audit=first_audit, expected_digest=first_audit.digest)
    second_audit = ready_audit(project)
    second = cleanup.cleanup_legacy_runtime_state(audit=second_audit, expected_digest=second_audit.digest)

    assert first.removed
    assert second.removed == ()
    assert second.runtime_directories_existed == ()
    assert second.status == "completed"


def test_cleanup_requires_exact_zero_live_audit_digest(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    live_audit = _audit_module().audit_projects((project,), probe=FakeProbe({101: "live"}))
    dead_audit = ready_audit(project)

    with pytest.raises(cleanup.LegacyCleanupError, match="legacy activity is live"):
        cleanup.cleanup_legacy_runtime_state(audit=live_audit, expected_digest=live_audit.digest)
    with pytest.raises(cleanup.LegacyCleanupError, match="audit digest mismatch"):
        cleanup.cleanup_legacy_runtime_state(audit=dead_audit, expected_digest="0" * 64)
    report = cleanup.cleanup_legacy_runtime_state(audit=dead_audit, expected_digest=dead_audit.digest)
    assert report.status == "completed"


def test_cleanup_rechecks_live_pid_before_delete(tmp_path: Path) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    audit = ready_audit(project)
    with pytest.raises(cleanup.LegacyCleanupError, match="legacy activity is live"):
        cleanup.cleanup_legacy_runtime_state(
            audit=audit,
            expected_digest=audit.digest,
            probe=FakeProbe({101: "live"}),
        )
    assert (project / "qa/changes/CH-1/driver.json").is_file()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_cleanup_rejects_unsafe_selected_entry(tmp_path: Path, unsafe_kind: str) -> None:
    cleanup = _cleanup_module()
    _project, audit = project_with_unsafe_entry(tmp_path, unsafe_kind)
    with pytest.raises(cleanup.LegacyCleanupError):
        cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)


def test_cleanup_deletes_through_directory_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    audit = ready_audit(project)
    unlink_fds: list[int | None] = []
    rmdir_fds: list[int | None] = []
    real_unlink = os.unlink
    real_rmdir = os.rmdir

    def spy_unlink(path: str, *, dir_fd: int | None = None) -> None:
        unlink_fds.append(dir_fd)
        real_unlink(path, dir_fd=dir_fd)

    def spy_rmdir(path: str, *, dir_fd: int | None = None) -> None:
        rmdir_fds.append(dir_fd)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", spy_unlink)
    monkeypatch.setattr(os, "rmdir", spy_rmdir)
    cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)

    assert unlink_fds
    assert all(fd is not None for fd in unlink_fds)
    assert rmdir_fds
    assert all(fd is not None for fd in rmdir_fds)


def test_cleanup_fsyncs_mutated_change_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    audit = ready_audit(project)
    fsynced: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)
    assert fsynced


def test_cleanup_rejects_mount_crossing_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    audit = ready_audit(project)
    real_stat = os.stat

    class _ShiftedDev:
        def __init__(self, inner: os.stat_result) -> None:
            self._inner = inner
            self.st_dev = inner.st_dev + 1

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    def fake_stat(path: str | int | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        st = real_stat(path, *args, **kwargs)
        name = path if isinstance(path, str) else ""
        if name == CLOSED_DIRECTORY:
            return _ShiftedDev(st)  # type: ignore[return-value]
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(cleanup.LegacyCleanupError, match="same-device|mount"):
        cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)


def test_cleanup_retries_after_legal_partial_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = _cleanup_module()
    project = seed_cleanup_project(tmp_path)
    remaining = {"count": 2}
    real_unlink = os.unlink

    def flaky_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if remaining["count"] > 0:
            remaining["count"] -= 1
            raise OSError(5, "injected unlink failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    first_audit = ready_audit(project)
    with pytest.raises(cleanup.LegacyCleanupError):
        cleanup.cleanup_legacy_runtime_state(audit=first_audit, expected_digest=first_audit.digest)
    monkeypatch.setattr(os, "unlink", real_unlink)
    retry_audit = ready_audit(project)
    report = cleanup.cleanup_legacy_runtime_state(audit=retry_audit, expected_digest=retry_audit.digest)
    assert report.status == "completed"
    assert _cleanup_module().inspect_legacy_runtime_state(project) == ()


def test_frozen_empty_roots_cleanup_is_completed_noop() -> None:
    cleanup = _cleanup_module()
    audit = _audit_module().audit_from_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)
    report = cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)

    assert audit.project_roots == ()
    assert audit.ready_for_cutover is True
    assert report.status == "completed"
    assert report.removed == ()
    assert report.runtime_directories_existed == ()
    assert report.audit_digest == audit.digest
    assert report.digest == cleanup.legacy_cleanup_digest(report)


def test_published_cleanup_report_matches_frozen_empty_roots() -> None:
    cleanup = _cleanup_module()
    published = cleanup.LegacyCleanupReportV1.model_validate(
        json.loads((CLOSEOUT / "cleanup-report.json").read_text(encoding="utf-8"))
    )
    audit = _audit_module().audit_from_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)
    live = cleanup.cleanup_legacy_runtime_state(audit=audit, expected_digest=audit.digest)

    assert published.project_roots == ()
    assert published.removed == ()
    assert published.status == "completed"
    assert published.digest == live.digest
    assert published.audit_digest == audit.digest


def test_cleanup_script_is_not_called_by_aa_start_or_run() -> None:
    needles = ("phase6_legacy_state_cleanup", "cleanup_legacy_runtime_state", "legacy_cleanup")
    for path in (ROOT / "packages/assurance-product").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{path} calls {needle}"
    cli_text = (ROOT / "packages" / "assurance-product" / "assurance_product" / "cli.py").read_text(
        encoding="utf-8"
    )
    assert "phase6_legacy_state_cleanup" not in cli_text
