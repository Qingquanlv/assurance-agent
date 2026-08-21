from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import thaw_json
from agent_runtime_cursor.handler import CursorHandler
from agent_runtime_cursor.process import CursorProcessReceipt, ProcessLaunchRequest
from fake_process_host import BOOT_DIGEST, FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import TaskActivityReconcileResult, TaskOutcome
from cursor_harness import (  # pyright: ignore[reportMissingImports]
    bind_spawned_fixture,
    complete_stream,
    cursor_cut,
    error_stream,
    execute_fixture,
    init_only_stream,
    request,
)


def _reconcile(result: object) -> TaskActivityReconcileResult:
    assert isinstance(result, TaskActivityReconcileResult)
    return result


async def test_spawn_failure_proven_to_create_no_child() -> None:
    fixture = await cursor_cut("before_spawn")
    result = _reconcile(await fixture.reconcile_after_restart())
    assert result.status == "not_dispatched"
    assert fixture.spawn_count == 0
    assert fixture.host.launches == []


async def test_host_boot_change_is_indeterminate_never_absent() -> None:
    fixture = await cursor_cut("after_bind")
    fixture.host.boot_identity_digest = "c" * 64
    result = _reconcile(await fixture.reconcile_after_restart())
    assert result.status == "indeterminate"
    assert result.status != "absent"
    assert result.reason is not None
    assert fixture.spawn_count == 1


async def test_dead_host_is_indeterminate_never_absent() -> None:
    fixture = await cursor_cut("after_bind")
    fixture.host.alive = False
    result = _reconcile(await fixture.reconcile_after_restart())
    assert result.status == "indeterminate"
    assert result.status != "absent"
    assert fixture.spawn_count == 1


async def test_different_host_cannot_adopt_a_process_receipt(tmp_path: Path) -> None:
    fixture = await bind_spawned_fixture(tmp_path)
    foreign = FakeConfinedProcessHost(boot_identity_digest=BOOT_DIGEST)
    restarted = CursorHandler(fixture.config, foreign)
    result = _reconcile(await restarted.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"
    assert foreign.spawn_count == 0
    assert fixture.host.spawn_count == 1


async def test_pid_reuse_is_indeterminate_never_absent(tmp_path: Path) -> None:
    fixture = await bind_spawned_fixture(
        tmp_path, host=FakeConfinedProcessHost(status="running", exit_code=None)
    )
    bound = CursorProcessReceipt.model_validate(thaw_json(fixture.activity.reference))
    fixture.host.reuse_pid()
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"
    assert bound.process_group_identity == fixture.host._receipt.process_group_identity  # noqa: SLF001
    assert bound.process_start_token != fixture.host._receipt.process_start_token  # noqa: SLF001
    assert fixture.spawn_count == 1


async def test_confinement_unavailable_after_bind_is_indeterminate(tmp_path: Path) -> None:
    fixture = await bind_spawned_fixture(
        tmp_path, host=FakeConfinedProcessHost(status="running", exit_code=None)
    )
    fixture.host.available = False
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"


async def test_cleanup_success_after_forced_cancel_is_acknowledged_or_terminal(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(
        status="running",
        stdout=error_stream(cwd),
        exit_code=None,
        cleanup_mode="success",
    )
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert result.status == "terminal"
    assert result.outcome is not None
    assert result.outcome.status == "failed"
    assert fixture.host.signals == ["graceful", "forced"]
    assert fixture.host.status == "exited"


async def test_cleanup_ambiguity_is_indeterminate(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(
        status="running",
        stdout=init_only_stream(str(tmp_path.resolve())),
        exit_code=None,
        cleanup_mode="ambiguous",
    )
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    canceled = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert canceled.status == "indeterminate"
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"


async def test_graceful_cancel_of_still_running_child_is_acknowledged(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(
        status="running",
        stdout=init_only_stream(str(tmp_path.resolve())),
        exit_code=None,
        cleanup_mode="leave_running",
    )
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert result.status == "acknowledged"
    assert result.outcome is None
    assert fixture.host.signals == ["graceful"]
    assert fixture.host.wait_calls == []
    assert fixture.host.status == "running"


async def test_completion_cancel_race_provider_terminal_wins(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(status="exited", stdout=complete_stream(cwd), exit_code=0)
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert result.status == "terminal"
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"
    AgentRunResult.model_validate(result.outcome.output)


async def test_unknown_exit_is_indeterminate_never_absent(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(
        status="unknown", stdout=init_only_stream(str(tmp_path.resolve())), exit_code=None
    )
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"
    canceled = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert canceled.status == "indeterminate"


async def test_truncated_missing_terminal_and_exit_mismatch_are_indeterminate(tmp_path: Path) -> None:
    cases = [
        FakeConfinedProcessHost(
            stdout=complete_stream(str(tmp_path.resolve())).split(b"\n", 1)[0]
            + b"\n"
            + b'{"type":"result","subtype":"success"',
            exit_code=0,
        ),
        FakeConfinedProcessHost(stdout=init_only_stream(str(tmp_path.resolve())), exit_code=0),
        FakeConfinedProcessHost(stdout=complete_stream(str(tmp_path.resolve())), exit_code=1),
    ]
    for host in cases:
        fixture = execute_fixture(tmp_path, host)
        with pytest.raises((Exception, TaskActivityProtocolViolation)):
            await fixture.handler.execute(fixture.request, fixture.context)
        result = _reconcile(
            await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        )
        assert result.status == "indeterminate"
        assert result.status != "absent"
        assert result.outcome is None
        if result.outcome is not None:
            assert result.outcome.status != "failed"


async def test_terminal_receipt_survives_engine_crash_without_cursor_access() -> None:
    fixture = await cursor_cut("after_host_terminal_receipt")
    fixture.host.reject_spawns = True
    result = _reconcile(await fixture.reconcile_after_restart())
    assert result.status == "terminal"
    assert result.outcome is not None
    AgentRunResult.model_validate(result.outcome.output)
    assert fixture.spawn_count == 1


async def test_printed_session_id_is_never_used_for_adoption() -> None:
    fixture = await cursor_cut("after_host_terminal_receipt")
    result = _reconcile(await fixture.reconcile_after_restart())
    assert result.status == "terminal"
    assert result.outcome is not None
    encoded = result.outcome.model_dump_json()
    assert "sess-1" not in encoded
    assert "--resume" not in encoded
    receipt = CursorProcessReceipt.model_validate(thaw_json(fixture.activity.reference))
    assert receipt.stream_session_id is None
    for launch in fixture.host.launches:
        assert "--resume" not in launch.argv
        assert not any(part == "--resume" or part.startswith("--resume=") for part in launch.argv)
    assert fixture.host.session_adoptions == []


async def test_receipt_drift_is_indeterminate(tmp_path: Path) -> None:
    fixture = await bind_spawned_fixture(
        tmp_path, host=FakeConfinedProcessHost(status="running", exit_code=None)
    )
    bound = thaw_json(fixture.activity.reference)
    assert isinstance(bound, dict)
    drifted = {**bound, "process_start_token": "forged-token"}
    fixture.port.replace_bound_reference(drifted)
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"


async def test_changed_request_identity_is_indeterminate() -> None:
    fixture = await cursor_cut("after_bind")
    drifted = request(attempt=2)
    result = _reconcile(await fixture.handler.reconcile(drifted, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert fixture.spawn_count == 1


async def test_init_version_mismatch_is_indeterminate(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(stdout=complete_stream(str(tmp_path.resolve()), version="9.9.9"))
    fixture = execute_fixture(tmp_path, host)
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
    except Exception:
        pass
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "indeterminate"
    assert result.status != "absent"


async def test_typed_provider_error_is_not_mapped_to_indeterminate(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(stdout=error_stream(cwd), exit_code=1)
    fixture = execute_fixture(tmp_path, host)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    assert isinstance(outcome, TaskOutcome)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "external_effect"
    assert outcome.failure.retryable is False
    result = _reconcile(await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity))
    assert result.status == "terminal"
    assert result.outcome is not None
    assert result.outcome.status == "failed"


async def test_wait_refuses_to_invent_exit_for_a_running_child() -> None:
    host = FakeConfinedProcessHost(status="running", exit_code=None)
    process = await host.spawn(
        ProcessLaunchRequest(
            argv=("/bin/cursor", "agent", "--print"),
            cwd=Path("."),
            environment={},
            stdin=b"{}",
            shell=False,
            executable_version_digest="a" * 64,
            request_digest="b" * 64,
            argv_policy_digest="c" * 64,
            workspace_identity_digest="d" * 64,
        )
    )
    with pytest.raises(TaskActivityProtocolViolation, match="not exited"):
        await host.wait(process.receipt)
    assert process.receipt.process_start_token == "start-token-1"
