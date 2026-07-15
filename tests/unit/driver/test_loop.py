from pathlib import Path

from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.driver import loop as loop_mod
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.driver_state import read_driver_state
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    DefaultCliPhaseExecutor,
    DefaultHealingActionExecutor,
    PhaseContext,
    run_workflow_loop,
)
from assurance_agent.workflow.orchestration.healing_episode import (
    HealingAttemptIntent,
    HealingEpisodeAction,
    HealingEpisodeSnapshot,
)
from assurance_agent.workflow.driver.process_runner import ProcessResult
from assurance_agent.workflow.orchestration.engine import (
    DispatchEntry,
    Terminal,
    WorkflowStatus,
)


class FakeAdapter:
    """Records requests; returns a scripted result per phase (default ok)."""

    def __init__(self, scripts: dict[str, list[PhaseResult]] | None = None) -> None:
        self.requests: list[PhaseRequest] = []
        self.successes = 0
        self._scripts = scripts or {}
        self._idx: dict[str, int] = {}

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        seq = self._scripts.get(request.phase_id)
        if seq:
            i = min(self._idx.get(request.phase_id, 0), len(seq) - 1)
            self._idx[request.phase_id] = i + 1
            result = seq[i]
        else:
            result = PhaseResult(ok=True, output="ok")
        if result.ok:
            self.successes += 1
        return result


class ScriptedStatus:
    """Idempotent status keyed on adapter successes (survives re-entry/resume)."""

    def __init__(
        self,
        adapter: FakeAdapter,
        order: list[DispatchEntry],
        final: Terminal | None = None,
    ) -> None:
        self._adapter = adapter
        self._order = order
        self._final = final or Terminal(kind="completed", reason="all phases done")

    def __call__(self) -> WorkflowStatus:
        done = self._adapter.successes
        if done >= len(self._order):
            return WorkflowStatus(phases=[], next_dispatch=[], terminal=self._final)
        return WorkflowStatus(phases=[], next_dispatch=[self._order[done]], terminal=None)


def _skill(phase: str) -> DispatchEntry:
    return DispatchEntry(phase_id=phase, skill=f"aa-{phase}", agent="aa-doc-author", kind="skill")


def _run(tmp_path: Path, adapter: FakeAdapter, status_provider, **kw):
    return run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        status_provider=status_provider,
        cli_executor=kw.pop("cli_executor", _NoCli()),
        **kw,
    )


class _NoCli:
    def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
        raise AssertionError("cli executor should not run cli phases in skill-only tests")

    def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
        # Benign no-op: these scripted tests advance via ScriptedStatus keyed on
        # adapter.successes, not on real workflow-state. Real advancement is
        # covered by tests/integration/test_loop_real_provider.py.
        return PhaseResult(ok=True, output="")


def test_completed_path_exit_0(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(adapter, [_skill("explore"), _skill("case-design")])
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_COMPLETED
    assert len(adapter.requests) == 2
    driver = read_driver_state(tmp_path / "qa" / "changes" / "CH-1")
    assert driver is not None and driver.status == "completed"


def test_final_state_persistence_failure_releases_lock(tmp_path: Path, monkeypatch) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(adapter, [])
    real_write = loop_mod.write_driver_state
    calls = 0

    def fail_on_final(change_dir, state):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return real_write(change_dir, state)

    monkeypatch.setattr(loop_mod, "write_driver_state", fail_on_final)
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_ERROR
    assert "failed to persist final driver state" in result.reason
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_initial_state_persistence_failure_releases_lock(tmp_path: Path, monkeypatch) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(adapter, [])

    def fail_write(change_dir, state):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(loop_mod, "write_driver_state", fail_write)
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_ERROR
    assert "failed to persist initial driver state" in result.reason
    assert not (tmp_path / "qa/changes/CH-1/driver.lock").exists()


def test_stopped_path_exit_20(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(
        adapter, [_skill("explore")], final=Terminal(kind="stopped", reason="max healing")
    )
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_STOPPED
    driver = read_driver_state(tmp_path / "qa" / "changes" / "CH-1")
    assert driver is not None and driver.status == "failed"


def test_needs_human_review_exit_30(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(adapter, [], final=Terminal(kind="needs_human_review", reason="decide"))
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_HUMAN_REVIEW
    assert adapter.requests == []


def test_error_when_adapter_fails_exit_40(tmp_path: Path) -> None:
    adapter = FakeAdapter(scripts={"explore": [PhaseResult(ok=False, error="boom")]})
    provider = ScriptedStatus(adapter, [_skill("explore")])
    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_ERROR
    assert "boom" in result.reason


def test_error_when_no_dispatch_and_not_terminal_exit_40(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    def provider() -> WorkflowStatus:
        return WorkflowStatus(phases=[], next_dispatch=[], terminal=None)

    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_ERROR


def test_unsafe_change_id_is_rejected_before_filesystem_access(tmp_path: Path) -> None:
    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="../outside",
        scope="execute",
        adapter=FakeAdapter(),
        status_provider=lambda: (_ for _ in ()).throw(AssertionError("must not project")),
        cli_executor=_NoCli(),
    )
    assert result.exit_code == EXIT_ERROR
    assert "unsafe change id" in result.reason


def test_per_phase_retry_succeeds_on_second_attempt(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        scripts={"explore": [PhaseResult(ok=False, error="flaky"), PhaseResult(ok=True, output="ok")]}
    )
    provider = ScriptedStatus(adapter, [_skill("explore")])
    result = _run(tmp_path, adapter, provider, max_phase_attempts=2)
    assert result.exit_code == EXIT_COMPLETED
    assert adapter.successes == 1
    assert len(adapter.requests) == 2  # one retry
    signed = [e for e in read_events(tmp_path / "qa/changes/CH-1") if e["type"] == "dispatch_signed"]
    assert len(signed) == 2
    assert signed[0]["attempt_id"] != signed[1]["attempt_id"]


def test_max_iterations_safety_valve(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    def never_done() -> WorkflowStatus:
        # Always ready, adapter never advances "successes" past 0 conceptually
        return WorkflowStatus(phases=[], next_dispatch=[_skill("loopy")], terminal=None)

    result = _run(tmp_path, adapter, never_done, max_iterations=3)
    assert result.exit_code == EXIT_ERROR
    assert "max iterations" in result.reason


def test_breakpoint_pauses_then_resume_completes(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    provider = ScriptedStatus(adapter, [_skill("explore"), _skill("case-review")])

    first = _run(tmp_path, adapter, provider, break_at="case-review")
    assert first.exit_code == EXIT_HUMAN_REVIEW
    assert adapter.successes == 1
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    paused = read_driver_state(change_dir)
    assert paused is not None and paused.status == "paused" and paused.paused_on == "case-review"
    assert not (change_dir / "driver.lock").exists()  # lock released on pause

    second = _run(tmp_path, adapter, provider)
    assert second.exit_code == EXIT_COMPLETED
    assert adapter.successes == 2
    resumed = read_driver_state(change_dir)
    assert resumed is not None and resumed.status == "completed"
    assert resumed.run_id == paused.run_id  # same run resumed


def test_cli_kind_dispatched_to_executor(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    calls: list[str] = []

    class RecordingCli:
        def run_cli_phase(self, entry: DispatchEntry, ctx: PhaseContext) -> PhaseResult:
            calls.append(entry.phase_id)
            return PhaseResult(ok=True, output="ran")

        def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
            return PhaseResult(ok=True, output="committed")

    class StatusForCli:
        def __init__(self) -> None:
            self.done = 0

        def __call__(self) -> WorkflowStatus:
            if calls:
                return WorkflowStatus(
                    phases=[], next_dispatch=[], terminal=Terminal(kind="completed", reason="ok")
                )
            return WorkflowStatus(
                phases=[],
                next_dispatch=[DispatchEntry(phase_id="execution", skill=None, agent=None, kind="cli")],
                terminal=None,
            )

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="execute",
        adapter=adapter,
        status_provider=StatusForCli(),
        cli_executor=RecordingCli(),
    )
    assert result.exit_code == EXIT_COMPLETED
    assert calls == ["execution"]
    assert adapter.requests == []  # cli phase never hits the agent adapter


def test_orchestrator_kind_is_noop(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    seen: list[str] = []

    def provider() -> WorkflowStatus:
        if seen:
            return WorkflowStatus(
                phases=[], next_dispatch=[], terminal=Terminal(kind="completed", reason="ok")
            )
        seen.append("x")
        return WorkflowStatus(
            phases=[],
            next_dispatch=[DispatchEntry(phase_id="marker", skill=None, agent=None, kind="orchestrator")],
            terminal=None,
        )

    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_COMPLETED
    assert adapter.requests == []


def test_default_cli_executor_maps_run_and_state_apply() -> None:
    invocations: list[list[str]] = []

    class FakeRunner:
        def run(self, argv, cwd, *, timeout=None, stdin_text=None) -> ProcessResult:  # noqa: ANN001
            invocations.append(argv)
            return ProcessResult(exit_code=0, stdout="ok", stderr="")

    executor = DefaultCliPhaseExecutor(runner=FakeRunner(), aa_command=["aa"])
    ctx = PhaseContext(
        change_id="CH-1",
        change_dir=Path("/tmp/x"),
        project_root=Path("/tmp"),
        params={},
        parent_session_id=None,
    )
    entry = DispatchEntry(phase_id="execution", skill=None, agent=None, kind="cli")
    result = executor.run_cli_phase(entry, ctx)
    applied = executor.apply_phase_state(entry, ctx, "execution:a1")
    assert result.ok is True and applied.ok is True
    assert invocations[0] == ["aa", "run", "--change", "CH-1"]
    assert invocations[1] == [
        "aa",
        "state",
        "apply",
        "--change",
        "CH-1",
        "--phase",
        "execution",
        "--attempt-id",
        "execution:a1",
    ]


def test_default_cli_executor_gate_fail_passthrough(tmp_path: Path) -> None:
    # `aa run` exits non-zero but wrote execution manifest + gate result -> gate
    # FAIL (tests ran, some failed), route onward, NOT a driver-fatal error.
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    (change_dir / "execution").mkdir(parents=True)
    (change_dir / "execution" / "execution-manifest.yaml").write_text("x", encoding="utf-8")
    (change_dir / "execution" / "quality-gate-result.json").write_text("{}", encoding="utf-8")

    class GateFailRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, argv, cwd, *, timeout=None, stdin_text=None) -> ProcessResult:  # noqa: ANN001
            self.calls += 1
            # First call = aa run (fail); second = state apply (ok).
            return ProcessResult(exit_code=1 if self.calls == 1 else 0, stdout="", stderr="")

    executor = DefaultCliPhaseExecutor(runner=GateFailRunner(), aa_command=["aa"])
    ctx = PhaseContext(
        change_id="CH-1", change_dir=change_dir, project_root=tmp_path, params={}, parent_session_id=None
    )
    result = executor.run_cli_phase(
        DispatchEntry(phase_id="execution", skill=None, agent=None, kind="cli"), ctx
    )
    assert result.ok is True


def test_healing_await_human_exits_30(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    def provider() -> WorkflowStatus:
        return WorkflowStatus(
            phases=[],
            next_dispatch=[],
            terminal=None,
            healing_episode=HealingEpisodeSnapshot(
                state="awaiting_human",
                stage="safety",
                next_actions=[HealingEpisodeAction(kind="await_human")],
            ),
        )

    result = _run(tmp_path, adapter, provider)
    assert result.exit_code == EXIT_HUMAN_REVIEW
    assert "healing" in result.reason


def test_human_stop_preempts_pending_healing_allocation(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    action = HealingEpisodeAction(
        kind="allocate_attempt",
        allocation=HealingAttemptIntent(
            episode_id="ep1",
            attempt_id="ha1",
            attempt_number=1,
            operation_id="op1",
            source_batch_id="b1",
            pin_entry_baseline=True,
        ),
    )

    def provider() -> WorkflowStatus:
        return WorkflowStatus(
            phases=[],
            next_dispatch=[],
            terminal=Terminal(kind="stopped", reason="human stop"),
            healing_episode=HealingEpisodeSnapshot(
                state="active",
                stage="allocate",
                next_actions=[action],
            ),
        )

    class MustNotAllocate:
        def execute(self, action, ctx):  # noqa: ANN001, ANN201
            raise AssertionError("stop must preempt allocation")

    result = _run(tmp_path, adapter, provider, healing_executor=MustNotAllocate())
    assert result.exit_code == EXIT_STOPPED


def test_default_healing_executor_commits_baseline_then_allocation(tmp_path: Path) -> None:
    from assurance_agent.workflow.core.events import read_events

    change = tmp_path / "qa/changes/CH-1"
    change.mkdir(parents=True)
    ctx = PhaseContext("CH-1", change, tmp_path, {}, None)
    action = HealingEpisodeAction(
        kind="allocate_attempt",
        allocation=HealingAttemptIntent(
            episode_id="ep1",
            attempt_id="ha1",
            attempt_number=1,
            operation_id="op1",
            source_batch_id="b1",
            pin_entry_baseline=True,
        ),
    )
    result = DefaultHealingActionExecutor().execute(action, ctx)
    assert result.ok is True
    events = read_events(change)
    assert [e["type"] for e in events] == [
        "healing_entry_baseline_pinned",
        "healing_attempt_allocated",
    ]
    assert events[1]["operation_id"] == "op1"


def test_default_status_provider_forwards_scope(tmp_path: Path, monkeypatch) -> None:
    from assurance_agent.artifacts.models import WorkflowState
    from assurance_agent.workflow.driver import loop as loop_mod
    from assurance_agent.workflow.orchestration.schema import WorkflowSchema

    captured: dict[str, str] = {}
    monkeypatch.setattr(loop_mod, "read_state", lambda _change: WorkflowState())

    def fake_compute(schema, change, state, params, *, scope, healing_provider=None):  # noqa: ANN001
        captured["scope"] = scope
        return WorkflowStatus(phases=[], next_dispatch=[], terminal=Terminal(kind="completed"))

    monkeypatch.setattr(loop_mod, "compute_status", fake_compute)
    provider = loop_mod._DefaultStatusProvider(
        WorkflowSchema(schema_version="1", name="t"), tmp_path, {}, "execute"
    )
    provider()
    assert captured["scope"] == "execute"
