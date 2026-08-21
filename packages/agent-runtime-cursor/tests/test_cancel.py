from __future__ import annotations

from pathlib import Path

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from fake_process_host import FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from harness import bind_spawned_fixture, complete_stream, init_only_stream  # pyright: ignore[reportMissingImports]


async def test_cancel_of_running_process_is_acknowledged_only(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(status="running", stdout=init_only_stream(cwd), exit_code=None)
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert result.status == "acknowledged"
    assert result.outcome is None
    assert fixture.host.terminations
    policy = fixture.host.terminations[0][1]
    assert policy.graceful_seconds == 5
    assert policy.forced_seconds == 10
    assert fixture.host.signals == ["graceful", "forced"]
    assert "--resume" not in fixture.host.launches[0].argv


async def test_cancel_returns_terminal_only_with_complete_stream_and_exit(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(
        status="exited",
        stdout=complete_stream(cwd),
        exit_code=0,
    )
    fixture = await bind_spawned_fixture(tmp_path, host=host)
    result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
    assert result.status == "terminal"
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"
    AgentRunResult.model_validate(result.outcome.output)
    assert fixture.host.signals == ["graceful"]


async def test_cancel_rejects_foreign_host_identity(tmp_path: Path) -> None:
    fixture = await bind_spawned_fixture(tmp_path)
    bound = thaw_json(fixture.activity.reference)
    assert isinstance(bound, dict)
    drifted_reference = {**bound, "host_boot_identity_digest": "f" * 64}
    drifted = fixture.activity.model_copy(
        update={
            "reference": drifted_reference,
            "reference_digest": canonical_digest(drifted_reference),
        }
    )
    result = await fixture.handler.cancel(fixture.request, fixture.context, drifted)
    assert result.status == "indeterminate"
    assert result.status != "acknowledged"
    assert result.reason is not None
    assert fixture.host.terminations == []
