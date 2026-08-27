from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import pytest

from tests.phase6.conformance import PHASE5_HANDOFF_PATH, handoff_canonical_digest, parse_phase5_handoff

ROOT = Path(__file__).resolve().parents[2]
CLOSEOUT = ROOT / ".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout"
PRODUCT_ROOT = ROOT / "packages/assurance-product"
AGENT_CLI = ROOT / "assurance_agent"
FORBIDDEN_BASENAMES = ("events.jsonl", "status.json", ".runtime", ".staging")


class FakeProbe:
    def __init__(self, mapping: dict[int, Literal["dead", "live", "unresolved"]]) -> None:
        self.mapping = mapping
        self.seen: list[int] = []

    def state(self, pid: int) -> Literal["dead", "live", "unresolved"]:
        self.seen.append(pid)
        return self.mapping.get(pid, "unresolved")


def write_driver(path: Path, pid: int, *, directory: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": "run-1",
        "status": "running",
        "pid": pid,
        "start_token": "tok",
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "directory": directory if directory is not None else str(path.parent),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_running_tasks(path: Path, pids: tuple[int, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [
        {
            "task_id": f"task-{index}",
            "attempt_id": f"attempt-{index}",
            "pid": pid,
            "host": "localhost",
            "session_id": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
            "lease_expires_at": "2026-01-01T01:00:00+00:00",
        }
        for index, pid in enumerate(pids)
    ]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")


def seed_activity_project(root: Path) -> Path:
    project = root / "sut"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_driver(change / "driver.json", pid=101)
    write_running_tasks(change / "running-tasks.json", (101,))
    archive = project / "qa" / "archive" / "CH-OLD"
    archive.mkdir(parents=True)
    write_driver(archive / "driver.json", pid=202)
    nested = change / "issues"
    nested.mkdir()
    write_driver(nested / "driver.json", pid=303)
    (change / "events.jsonl").write_text('{"type":"event"}\n', encoding="utf-8")
    (change / "status.json").write_text('{"status":"achieved"}\n', encoding="utf-8")
    (change / ".runtime" / "ledger").mkdir(parents=True)
    (change / ".runtime" / "ledger" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (change / ".staging" / "attempt").mkdir(parents=True)
    (change / ".staging" / "attempt" / "keep.txt").write_text("keep\n", encoding="utf-8")
    return project


def _audit_module():
    from scripts import phase6_legacy_activity_audit as audit

    return audit


def test_activity_audit_scans_only_immediate_change_roots(tmp_path: Path) -> None:
    audit = _audit_module()
    project = seed_activity_project(tmp_path)
    result = audit.audit_projects((project,), probe=FakeProbe({101: "dead", 202: "live", 303: "live"}))

    assert [item.relative_path for item in result.records] == [
        "qa/changes/CH-1/driver.json",
        "qa/changes/CH-1/running-tasks.json",
    ]
    assert result.ready_for_cutover is True


@pytest.mark.parametrize("state", ["live", "unresolved"])
def test_live_or_unresolved_activity_blocks_cutover(tmp_path: Path, state: str) -> None:
    audit = _audit_module()
    project = seed_activity_project(tmp_path)
    result = audit.audit_projects((project,), probe=FakeProbe({101: state}))  # type: ignore[arg-type]

    assert result.ready_for_cutover is False
    assert any(item.activity_state == state for item in result.records)


def test_audit_never_selects_change_local_result_names(tmp_path: Path) -> None:
    audit = _audit_module()
    project = seed_activity_project(tmp_path)
    result = audit.audit_projects((project,), probe=FakeProbe({101: "dead"}))
    selected = {Path(item.relative_path).name for item in result.records}

    assert selected.isdisjoint(FORBIDDEN_BASENAMES)
    assert all(
        not item.relative_path.endswith(f"/{name}") for item in result.records for name in FORBIDDEN_BASENAMES
    )


def test_audit_does_not_follow_symlink_change_root(tmp_path: Path) -> None:
    audit = _audit_module()
    project = tmp_path / "sut"
    real = tmp_path / "outside" / "CH-1"
    real.mkdir(parents=True)
    write_driver(real / "driver.json", pid=101)
    changes = project / "qa" / "changes"
    changes.mkdir(parents=True)
    (changes / "CH-1").symlink_to(real)

    result = audit.audit_projects((project,), probe=FakeProbe({101: "live"}))

    assert result.records == ()
    assert result.ready_for_cutover is True


def test_malformed_or_identity_mismatch_is_unresolved(tmp_path: Path) -> None:
    audit = _audit_module()
    project = tmp_path / "sut"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "driver.json").write_text("not-json", encoding="utf-8")
    other = project / "qa" / "changes" / "CH-2"
    other.mkdir()
    write_driver(other / "driver.json", pid=202, directory="/tmp/elsewhere")

    result = audit.audit_projects((project,), probe=FakeProbe({101: "dead", 202: "dead"}))

    assert result.ready_for_cutover is False
    assert {item.activity_state for item in result.records} == {"unresolved"}


def test_process_probe_uses_signal_zero_only(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _audit_module()
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", fake_kill)
    assert audit.OsProcessProbe().state(9) == "dead"
    assert calls == [(9, 0)]


def test_process_probe_permission_and_invalid_pid_are_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = _audit_module()

    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", fake_kill)
    probe = audit.OsProcessProbe()
    assert probe.state(9) == "unresolved"
    assert probe.state(0) == "unresolved"
    assert probe.state(-3) == "unresolved"


def test_frozen_handoff_audit_is_zero_root_zero_live() -> None:
    audit = _audit_module()
    handoff = parse_phase5_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)
    result = audit.audit_from_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)

    assert handoff.project_roots == ()
    assert result.project_roots == ()
    assert result.records == ()
    assert result.ready_for_cutover is True
    assert result.handoff_digest == handoff_canonical_digest(handoff)
    assert result.digest == audit.activity_audit_digest(result)


def test_published_activity_audit_matches_frozen_empty_roots() -> None:
    audit = _audit_module()
    published = audit.ActivityAuditV1.model_validate(
        json.loads((CLOSEOUT / "activity-audit.json").read_text(encoding="utf-8"))
    )
    live = audit.audit_from_handoff(PHASE5_HANDOFF_PATH, repo_root=ROOT)

    assert published.project_roots == ()
    assert published.records == ()
    assert published.ready_for_cutover is True
    assert published.digest == live.digest


def test_scripts_are_never_imported_by_assurance_product_or_aa() -> None:
    needles = ("phase6_legacy_activity_audit", "phase6_legacy_state_cleanup", "legacy_activity_audit")
    surfaces = [PRODUCT_ROOT, AGENT_CLI]
    for surface in surfaces:
        for path in surface.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, f"{path} imports {needle}"
