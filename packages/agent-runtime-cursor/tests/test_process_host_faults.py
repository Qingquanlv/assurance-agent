from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_cursor.process import (
    CancelPolicy,
    CursorProcessReceipt,
    HostTerminalResult,
    LinuxProcessSupervisorHost,
    MacOSProcessGroupHost,
    ProcessLaunchRequest,
    production_process_host,
)
from graph_engine import TaskActivityProtocolViolation

from test_process_host import launch_request, spawning_child_request  # noqa: PLC2701


async def test_pid_reuse_fails_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = production_process_host(tmp_path)
    request = launch_request(
        tmp_path,
        argv=(str(Path(sys.executable).resolve()), "-c", "import time; time.sleep(5)"),
    )
    process = await host.spawn(request)
    monkeypatch.setattr(host, "_read_start_identity", lambda pid: f"proc:{pid}:reused")
    with pytest.raises(TaskActivityProtocolViolation, match="process receipt"):
        host.authenticate(process.receipt)


async def test_foreign_receipt_mac_is_rejected(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    forged = process.receipt.model_copy(update={"process_start_token": "foreign-token"})
    with pytest.raises(TaskActivityProtocolViolation, match="process receipt"):
        host.authenticate(forged)


async def test_wait_before_durable_write_is_not_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    original_write = host._write_durable_terminal  # type: ignore[attr-defined]
    observed: list[HostTerminalResult | None] = []

    def recording_write(receipt: CursorProcessReceipt, terminal: HostTerminalResult) -> None:
        terminal_path = host._terminal_path(receipt.process_start_token)  # type: ignore[attr-defined]
        observed.append(None if not terminal_path.is_file() else terminal)
        original_write(receipt, terminal)

    monkeypatch.setattr(host, "_write_durable_terminal", recording_write)
    terminal = await host.wait(process.receipt)
    assert observed == [None]
    assert host.read_durable_terminal(process.receipt) == terminal


async def test_unbound_spawn_state_unknown_for_foreign_host(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    fingerprint = {
        "host_boot_identity_digest": "f" * 64,
        "host_instance_id": "foreign",
        "request_digest": "a" * 64,
    }
    assert host.unbound_spawn_state(fingerprint) == "unknown"


async def test_unbound_spawn_state_spawned_after_spawn(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    fingerprint = {
        "host_boot_identity_digest": process.receipt.host_boot_identity_digest,
        "host_instance_id": process.receipt.host_instance_id,
        "request_digest": process.receipt.request_digest,
    }
    assert host.unbound_spawn_state(fingerprint) == "spawned"


async def test_terminate_escalates_to_forced_kill(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(spawning_child_request(tmp_path))
    await host.terminate(
        process.receipt,
        CancelPolicy(graceful_seconds=0.05, forced_seconds=0.05),
    )
    observation = await host.observe(process.receipt)
    assert observation.status == "exited"


def test_linux_host_uses_proc_start_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    host = LinuxProcessSupervisorHost(tmp_path)
    monkeypatch.setattr(
        host,
        "_read_proc_stat",
        lambda pid: f"{pid} (sleep) S 1 {pid} {pid} 0 0 1 0 0 0 0 0 20 0 1 0 424242 4096 64 999999 0 0 0 0 0 0 0 0 0 0 0",
    )
    identity = host._read_start_identity(4242)  # type: ignore[attr-defined]
    assert identity == "proc:4242:999999:1"


def test_macos_host_reports_process_group_confinement(tmp_path: Path) -> None:
    host = MacOSProcessGroupHost(tmp_path)
    identity = host.preflight(launch_request(tmp_path))
    assert identity.mechanism == "process-group"
    assert identity.descendant_inheritance is True


async def test_missing_spawn_record_is_indeterminate(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    receipt = CursorProcessReceipt(
        host_boot_identity_digest=host._boot_identity_digest,  # type: ignore[attr-defined]
        host_instance_id=host._host_instance_id,  # type: ignore[attr-defined]
        confinement_identity="pgid:1",
        process_group_identity="pgid:1",
        process_start_token="missing-token",
        executable_version_digest=canonical_digest("1.0.0"),
        request_digest=canonical_digest({"missing": True}),
        argv_policy_digest=canonical_digest({"argv": []}),
        workspace_identity_digest=canonical_digest({"cwd": str(tmp_path)}),
        started_at=0.0,
    )
    with pytest.raises(TaskActivityProtocolViolation, match="process receipt"):
        host.authenticate(receipt)


async def test_terminal_receipt_is_canonical_json(tmp_path: Path) -> None:
    host = production_process_host(tmp_path)
    process = await host.spawn(launch_request(tmp_path))
    await host.wait(process.receipt)
    terminal_path = tmp_path / ".cursor-process-host" / "terminals" / f"{process.receipt.process_start_token}.json"
    payload = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert set(payload) >= {"terminal", "receipt_mac"}
    assert set(payload["terminal"]) >= {"exit_code", "stdout", "stderr", "elapsed_seconds"}
