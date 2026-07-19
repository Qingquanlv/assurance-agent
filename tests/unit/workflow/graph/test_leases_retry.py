"""Retry/backoff 决策、running-tasks lease registry 与 orphan 恢复分类。

覆盖：注入时钟的 wait→start 决策、policy∩contract 双重 retry 闸门、canonical
contract 的合约类错误立即失败、``max_attempts`` 含第一次、abandoned 计入
attempt、resume 不重置 attempts；并发 upsert 不丢记录、heartbeat 只推进匹配
attempt、stale attempt 不覆盖新记录、截断临时文件不替换有效文件；orphan 的
adopt/wait/abandon 分类、按 attempt_id 去重的 strict abandonment。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.contracts import ResourceClaims, load_execution_contracts
from assurance_agent.workflow.graph.leases import (
    AttemptDecision,
    LeaseError,
    LeaseRegistry,
    RunningTaskLease,
    SystemClock,
    abandon_running_attempt,
    classify_recovery,
    compute_next_retry_at,
    next_attempt_decision,
    recover_running_tasks,
    retry_delay_seconds,
)
from assurance_agent.workflow.graph.models import ExecutableTask, GraphProjection
from assurance_agent.workflow.graph.schema_v2 import BackoffDef, RetryPolicyDef, TimeoutPolicyDef

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
_ALL_KINDS: list[ErrorKind] = [
    "timeout", "transport", "rate_limit", "auth", "invalid_input",
    "invalid_output", "forbidden_write", "contract", "internal",
]


class FakeClock:
    """注入时钟：返回受控的 UTC 与 monotonic 值。"""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def _task(
    task_id: str = "task-a",
    *,
    max_attempts: int = 3,
    retry_on: list[ErrorKind] | None = None,
    retryable_errors: tuple[ErrorKind, ...] = ("timeout", "transport", "rate_limit"),
    backoff: BackoffDef | None = None,
) -> ExecutableTask:
    return ExecutableTask(
        task_id=task_id,
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        graph_id="main",
        node_id="node-a",
        structural_path="main",
        input={},
        input_sha256="in-1",
        contract_digest="cd-1",
        retryable_errors=retryable_errors,
        retry_policy=RetryPolicyDef(
            max_attempts=max_attempts,
            retry_on=retry_on if retry_on is not None else ["timeout", "transport", "rate_limit"],
            backoff=backoff or BackoffDef(),
        ),
        timeout_policy=TimeoutPolicyDef(run_seconds=60, heartbeat_seconds=10),
        target="skill:fix",
        resources=ResourceClaims(),
    )


def _started(inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": inv,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "gd-1",
        "contract_digests": {"skill:fix": "cd-1"},
        "params": {},
        "params_sha256": "ps-1",
        "root_tree_id": "tree-0",
        "max_parallel_tasks": 2,
        "checkpoint_ns": inv,
        "structural_path": "main",
    }


def _begin(
    task_id: str,
    attempt: int = 1,
    inv: str = "inv-1",
    lease_expires_at: str | None = None,
) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_started",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "node_id": "node-a",
        "input_sha256": "in-1",
        "graph_digest": "gd-1",
        "contract_digest": "cd-1",
        "attempt_number": attempt,
        "lease_expires_at": lease_expires_at or (T0 + timedelta(seconds=60)).isoformat(),
        "started_at": T0.isoformat(),
    }


def _failed(
    task_id: str,
    *,
    attempt: int = 1,
    error_kind: ErrorKind = "transport",
    next_retry_at: str | None = None,
    inv: str = "inv-1",
) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_failed",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "error_kind": error_kind,
        "message": "boom",
        "next_retry_at": next_retry_at,
    }


def _abandoned(task_id: str, *, attempt: int = 1, inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_abandoned",
        "invocation_id": inv,
        "checkpoint_ns": inv,
        "task_id": task_id,
        "attempt_id": f"{task_id}-a{attempt}",
        "reason": "lease expired",
        "abandoned_at": T0.isoformat(),
    }


def _append_all(change: Path, events: list[dict]) -> None:
    for event in events:
        append_event_strict(change, event)


def _project(change: Path, inv: str = "inv-1") -> GraphProjection:
    return fold_invocation_events(inv, read_events_strict(change))


def _lease(
    task_id: str,
    attempt_id: str | None = None,
    *,
    pid: int = 4242,
    host: str = "testhost",
    session_id: str | None = None,
    started_at: datetime = T0,
    last_heartbeat_at: datetime | None = None,
    lease_expires_at: datetime = T0 + timedelta(seconds=60),
) -> RunningTaskLease:
    return RunningTaskLease(
        task_id=task_id,
        attempt_id=attempt_id or f"{task_id}-a1",
        pid=pid,
        host=host,
        session_id=session_id,
        started_at=started_at.isoformat(),
        last_heartbeat_at=(last_heartbeat_at or started_at).isoformat(),
        lease_expires_at=lease_expires_at.isoformat(),
    )


class _FakeProbe:
    """三态存活性探针：按 pid/session 返回预设的 True/False/None。"""

    def __init__(
        self,
        pid_map: dict[int, bool | None] | None = None,
        session_map: dict[str, bool | None] | None = None,
    ) -> None:
        self._pid_map = pid_map or {}
        self._session_map = session_map or {}

    def pid_alive(self, pid: int, host: str) -> bool | None:
        return self._pid_map.get(pid)

    def session_alive(self, session_id: str) -> bool | None:
        return self._session_map.get(session_id)


# ---------------------------------------------------------------------------
# Step 1/2：retry 决策与确定性 backoff


def test_retry_decision_waits_for_persisted_backoff_then_starts(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    task = _task()
    fake_clock = FakeClock()
    retry_at = (T0 + timedelta(seconds=2)).isoformat()
    _append_all(change, [_started(), _begin(task.task_id), _failed(task.task_id, next_retry_at=retry_at)])
    failed_once = _project(change)

    decision = next_attempt_decision(task=task, projection=failed_once, now=fake_clock.now())
    assert decision.kind == "wait"
    assert decision.next_retry_at == failed_once.tasks[task.task_id].next_retry_at

    fake_clock.advance(2)
    decision = next_attempt_decision(task=task, projection=failed_once, now=fake_clock.now())
    assert decision.kind == "start"
    assert decision.attempt_number == 2


def test_fresh_task_starts_attempt_one(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started()])
    projection = _project(change)
    decision = next_attempt_decision(task=_task(), projection=projection, now=T0)
    assert decision == AttemptDecision(kind="start", attempt_number=1)


def test_error_retries_only_when_in_policy_and_contract(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    events = [_started()]
    cases: list[tuple[str, ErrorKind]] = [
        ("task-policy-only", "auth"),
        ("task-contract-only", "rate_limit"),
        ("task-both", "transport"),
    ]
    for task_id, kind in cases:
        events += [_begin(task_id), _failed(task_id, error_kind=kind)]
    _append_all(change, events)
    projection = _project(change)
    past = T0 - timedelta(seconds=1)

    # 在 policy.retry_on 但不在 contract.retryable_errors → 立即失败。
    policy_only = _task("task-policy-only", retry_on=["transport", "auth"], retryable_errors=("transport",))
    assert next_attempt_decision(task=policy_only, projection=projection, now=past).kind == "failed"
    # 在 contract.retryable_errors 但不在 policy.retry_on → 立即失败。
    contract_only = _task("task-contract-only", retry_on=["transport"], retryable_errors=("transport", "rate_limit"))
    assert next_attempt_decision(task=contract_only, projection=projection, now=past).kind == "failed"
    # 两边同时出现才可重试。
    both = _task("task-both", retry_on=["transport"], retryable_errors=("transport",))
    decision = next_attempt_decision(task=both, projection=projection, now=past)
    assert decision.kind == "start"
    assert decision.attempt_number == 2


def test_canonical_contract_violations_stop_immediately(tmp_path: Path) -> None:
    # packaged registry 的 canonical contract 不把合约类错误列为可重试。
    catalog = load_execution_contracts(tmp_path)
    violations = {"auth", "invalid_output", "forbidden_write", "contract"}
    assert catalog.contracts, "canonical registry must not be empty"
    for contract in catalog.contracts.values():
        assert not set(contract.retryable_errors) & violations

    change = tmp_path / "CH-1"
    change.mkdir()
    kinds: list[ErrorKind] = ["auth", "invalid_output", "forbidden_write", "contract"]
    events = [_started()]
    for kind in kinds:
        events += [_begin(f"task-{kind}"), _failed(f"task-{kind}", error_kind=kind)]
    _append_all(change, events)
    projection = _project(change)

    for kind in kinds:
        # canonical contract 的 retryable_errors 为空；即使 policy 全部放行也立即失败。
        task = _task(f"task-{kind}", retry_on=list(_ALL_KINDS), retryable_errors=())
        decision = next_attempt_decision(task=task, projection=projection, now=T0)
        assert decision.kind == "failed", kind


def test_max_attempts_includes_first_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), _begin("task-a"), _failed("task-a")])
    projection = _project(change)
    past = T0 - timedelta(seconds=1)

    one_shot = _task(max_attempts=1)
    decision = next_attempt_decision(task=one_shot, projection=projection, now=past)
    assert decision.kind == "exhausted"
    assert decision.attempt_number is None

    two_shot = _task(max_attempts=2)
    decision = next_attempt_decision(task=two_shot, projection=projection, now=past)
    assert decision.kind == "start"
    assert decision.attempt_number == 2


def test_abandoned_counts_as_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _begin("task-a"),
            _abandoned("task-a"),
            _begin("task-b"),
            _abandoned("task-b"),
        ],
    )
    projection = _project(change)
    assert projection.tasks["task-a"].attempts_used == 1

    retryable = _task("task-a", max_attempts=2)
    decision = next_attempt_decision(task=retryable, projection=projection, now=T0)
    assert decision.kind == "start"
    assert decision.attempt_number == 2

    one_shot = _task("task-b", max_attempts=1)
    assert next_attempt_decision(task=one_shot, projection=projection, now=T0).kind == "exhausted"


def test_resume_never_resets_attempts(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _begin("task-a", attempt=1),
            _failed("task-a", attempt=1, next_retry_at=(T0 + timedelta(seconds=1)).isoformat()),
            _begin("task-a", attempt=2),
            _failed("task-a", attempt=2, next_retry_at=(T0 + timedelta(seconds=2)).isoformat()),
        ],
    )
    # 两次独立 fold 模拟跨进程 resume：attempts 绝不重置。
    first = _project(change)
    second = _project(change)
    assert first.tasks["task-a"].attempts_used == 2
    assert second.tasks["task-a"].attempts_used == 2

    task = _task(max_attempts=3)
    decision = next_attempt_decision(
        task=task, projection=second, now=T0 + timedelta(seconds=5)
    )
    assert decision.kind == "start"
    assert decision.attempt_number == 3
    exhausted = _task(max_attempts=2)
    assert next_attempt_decision(task=exhausted, projection=second, now=T0).kind == "exhausted"


def test_retry_delay_seconds_progression_and_cap() -> None:
    policy = RetryPolicyDef(
        max_attempts=5,
        retry_on=["transport"],
        backoff=BackoffDef(initial_seconds=1, multiplier=2, max_seconds=5),
    )
    assert [retry_delay_seconds(policy, "task-a", n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 5, 5]


def test_retry_delay_jitter_is_structural_and_deterministic() -> None:
    policy = RetryPolicyDef(
        max_attempts=5,
        retry_on=["transport"],
        backoff=BackoffDef(initial_seconds=2, multiplier=2, max_seconds=8, jitter=True),
    )
    for attempt in (1, 2, 3, 10):
        capped = min(2 * (2 ** (attempt - 1)), 8)
        first = retry_delay_seconds(policy, "task-a", attempt)
        again = retry_delay_seconds(policy, "task-a", attempt)
        assert first == again  # 结构身份派生：无进程随机源，重启不漂移
        assert 0.5 * capped <= first <= capped

    zero = RetryPolicyDef(
        max_attempts=2,
        retry_on=["transport"],
        backoff=BackoffDef(initial_seconds=0, multiplier=1, max_seconds=0, jitter=True),
    )
    assert retry_delay_seconds(zero, "task-a", 1) == 0


def test_compute_next_retry_at_is_restart_stable() -> None:
    policy = RetryPolicyDef(
        max_attempts=3,
        retry_on=["transport"],
        backoff=BackoffDef(initial_seconds=2, multiplier=1, max_seconds=2, jitter=True),
    )
    first = compute_next_retry_at(policy, "task-a", 1, T0)
    again = compute_next_retry_at(policy, "task-a", 1, T0)
    assert first == again
    expected = T0 + timedelta(seconds=retry_delay_seconds(policy, "task-a", 1))
    assert datetime.fromisoformat(first) == expected


def test_system_clock_smoke() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is not None
    assert isinstance(clock.monotonic(), float)
    clock.sleep(0)


# ---------------------------------------------------------------------------
# Step 3/4：原子 lease 文件


def test_concurrent_upserts_retain_both_records(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    barrier = threading.Barrier(3)

    def writer(lease: RunningTaskLease) -> None:
        barrier.wait(timeout=5)
        for _ in range(25):
            registry.upsert(lease)

    threads = [
        threading.Thread(target=writer, args=(_lease("task-a"),)),
        threading.Thread(target=writer, args=(_lease("task-b"),)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    leases = registry.read_all()
    assert set(leases) == {"task-a", "task-b"}
    assert leases["task-a"].attempt_id == "task-a-a1"
    assert leases["task-b"].attempt_id == "task-b-a1"


def test_heartbeat_only_advances_the_matching_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    registry.upsert(_lease("task-a"))
    registry.upsert(_lease("task-b"))

    at = T0 + timedelta(seconds=10)
    expires = T0 + timedelta(seconds=70)
    updated = registry.heartbeat("task-a", "task-a-a1", at=at.isoformat(), lease_expires_at=expires.isoformat())
    assert updated is not None
    assert updated.last_heartbeat_at == at.isoformat()
    assert updated.lease_expires_at == expires.isoformat()

    leases = registry.read_all()
    # 同 task 的旧 attempt 与别的 task 都不被推进。
    assert leases["task-a"].last_heartbeat_at == at.isoformat()
    assert leases["task-b"].last_heartbeat_at == T0.isoformat()
    assert leases["task-b"].lease_expires_at == (T0 + timedelta(seconds=60)).isoformat()

    stale = registry.heartbeat("task-a", "task-a-a0", at=at.isoformat(), lease_expires_at=expires.isoformat())
    assert stale is None
    missing = registry.heartbeat("task-missing", "task-missing-a1", at=at.isoformat(), lease_expires_at=expires.isoformat())
    assert missing is None
    assert registry.read_all()["task-a"].lease_expires_at == expires.isoformat()


def test_stale_attempt_cannot_overwrite_newer_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    newer = _lease("task-a", "task-a-a2", started_at=T0 + timedelta(seconds=30))
    registry.upsert(newer)

    stale = _lease("task-a", "task-a-a1", started_at=T0)
    result = registry.upsert(stale)
    assert result.attempt_id == "task-a-a2"
    assert registry.read_all()["task-a"].attempt_id == "task-a-a2"

    # 同一 attempt 的幂等刷新仍然允许。
    refreshed = _lease(
        "task-a", "task-a-a2",
        started_at=T0 + timedelta(seconds=30),
        last_heartbeat_at=T0 + timedelta(seconds=40),
    )
    assert registry.upsert(refreshed).last_heartbeat_at == refreshed.last_heartbeat_at
    assert registry.read_all()["task-a"].last_heartbeat_at == refreshed.last_heartbeat_at


def test_truncated_temp_file_never_replaces_prior_valid_file(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    registry.upsert(_lease("task-a"))
    before = registry.path.read_bytes()

    (change / "running-tasks.json.tmp.99999").write_text('{"leases": [{"task_id": "task-a", "att', encoding="utf-8")
    (change / "running-tasks.json.tmp.4242").write_text("", encoding="utf-8")

    leases = registry.read_all()
    assert leases["task-a"].attempt_id == "task-a-a1"
    assert registry.path.read_bytes() == before

    registry.upsert(_lease("task-b"))
    assert set(registry.read_all()) == {"task-a", "task-b"}


def test_missing_file_reads_empty_and_corrupt_file_fails_closed(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    assert registry.read_all() == {}

    registry.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LeaseError):
        registry.read_all()


def test_remove_only_removes_the_matching_attempt(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    registry.upsert(_lease("task-a", "task-a-a2"))

    registry.remove("task-a", "task-a-a1")
    assert set(registry.read_all()) == {"task-a"}
    registry.remove("task-a", "task-a-a2")
    assert registry.read_all() == {}


def test_lease_file_is_deterministic_json(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    registry = LeaseRegistry(change)
    registry.upsert(_lease("task-b"))
    registry.upsert(_lease("task-a"))
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    assert [lease["task_id"] for lease in payload["leases"]] == ["task-a", "task-b"]


# ---------------------------------------------------------------------------
# Step 5：orphan 恢复分类与 abandonment


def test_classify_adopts_reconnectable_live_session() -> None:
    lease = _lease("task-a", session_id="sess-1")
    probe = _FakeProbe(session_map={"sess-1": True})
    action = classify_recovery(lease=lease, reconnect=True, probe=probe, now=T0)
    assert action.kind == "adopt"
    assert action.attempt_id == "task-a-a1"


def test_classify_abandons_when_pid_or_session_provably_dead() -> None:
    lease = _lease("task-a", pid=4242, session_id="sess-1")
    dead_pid = classify_recovery(
        lease=lease, reconnect=False, probe=_FakeProbe(pid_map={4242: False}), now=T0
    )
    assert dead_pid.kind == "abandon"
    assert "dead" in (dead_pid.reason or "")

    dead_session = classify_recovery(
        lease=lease,
        reconnect=False,
        probe=_FakeProbe(pid_map={4242: True}, session_map={"sess-1": False}),
        now=T0,
    )
    assert dead_session.kind == "abandon"


def test_classify_waits_when_liveness_unknown_and_lease_unexpired() -> None:
    lease = _lease("task-a", pid=4242)
    action = classify_recovery(
        lease=lease, reconnect=False, probe=_FakeProbe(pid_map={4242: None}), now=T0
    )
    assert action.kind == "wait"

    alive_but_not_reconnectable = classify_recovery(
        lease=lease, reconnect=False, probe=_FakeProbe(pid_map={4242: True}), now=T0
    )
    assert alive_but_not_reconnectable.kind == "wait"


def test_classify_abandons_when_lease_expired() -> None:
    expired = _lease("task-a", pid=4242, lease_expires_at=T0 - timedelta(seconds=1))
    now = T0
    unknown = classify_recovery(
        lease=expired, reconnect=False, probe=_FakeProbe(pid_map={4242: None}), now=now
    )
    assert unknown.kind == "abandon"
    assert unknown.reason == "lease expired"
    # 即使本机 pid 仍活，lease 到期也 abandon（lease 才是执行权凭证）。
    alive = classify_recovery(
        lease=expired, reconnect=False, probe=_FakeProbe(pid_map={4242: True}), now=now
    )
    assert alive.kind == "abandon"


def test_abandon_running_attempt_appends_strict_event_and_dedups(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), _begin("task-a")])

    appended = abandon_running_attempt(
        change,
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        task_id="task-a",
        attempt_id="task-a-a1",
        reason="lease expired",
        abandoned_at=T0.isoformat(),
    )
    assert appended is True
    projection = _project(change)
    assert projection.tasks["task-a"].status == "abandoned"

    again = abandon_running_attempt(
        change,
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        task_id="task-a",
        attempt_id="task-a-a1",
        reason="lease expired",
        abandoned_at=T0.isoformat(),
    )
    assert again is False
    stale = abandon_running_attempt(
        change,
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        task_id="task-a",
        attempt_id="task-a-a0",
        reason="lease expired",
        abandoned_at=T0.isoformat(),
    )
    assert stale is False

    abandoned_events = [
        e for e in read_events_strict(change) if e.get("type") == "task_attempt_abandoned"
    ]
    assert len(abandoned_events) == 1
    assert abandoned_events[0]["attempt_id"] == "task-a-a1"


def test_recover_running_tasks_classifies_and_feeds_retry_decision(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(change, [_started(), _begin("task-a"), _begin("task-b")])
    registry = LeaseRegistry(change)
    registry.upsert(_lease("task-a", pid=4242))
    registry.upsert(_lease("task-b", pid=4343))
    probe = _FakeProbe(pid_map={4242: False, 4343: True})
    now = T0 + timedelta(seconds=10)

    actions = recover_running_tasks(
        change,
        projection=_project(change),
        leases=registry.read_all(),
        reconnect_for=lambda task_id: False,
        probe=probe,
        now=now,
    )
    by_task = {action.task_id: action for action in actions}
    assert by_task["task-a"].kind == "abandon"
    assert by_task["task-b"].kind == "wait"

    updated = _project(change)
    assert updated.tasks["task-a"].status == "abandoned"
    assert updated.tasks["task-b"].status == "running"

    # 更新后的 attempt 计数喂给 next_attempt_decision（plan Step 5.5）。
    decision = next_attempt_decision(task=_task("task-a", max_attempts=2), projection=updated, now=now)
    assert decision.kind == "start"
    assert decision.attempt_number == 2
    exhausted = next_attempt_decision(task=_task("task-a", max_attempts=1), projection=updated, now=now)
    assert exhausted.kind == "exhausted"

    # running task 绝不重复执行。
    running = next_attempt_decision(task=_task("task-b"), projection=updated, now=now)
    assert running.kind == "wait"


def test_recover_falls_back_to_ledger_lease_when_file_lost(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    change.mkdir()
    _append_all(
        change,
        [
            _started(),
            _begin("task-a", lease_expires_at=(T0 + timedelta(seconds=10)).isoformat()),
            _begin("task-b", attempt=2, lease_expires_at=(T0 + timedelta(seconds=1000)).isoformat()),
        ],
    )
    registry = LeaseRegistry(change)
    # task-b 的 lease 文件残留旧 attempt：不是当前 attempt 的 liveness，按丢失处理。
    registry.upsert(_lease("task-b", "task-b-a1", lease_expires_at=T0 + timedelta(seconds=10)))
    now = T0 + timedelta(seconds=100)

    actions = recover_running_tasks(
        change,
        projection=_project(change),
        leases=registry.read_all(),
        reconnect_for=lambda task_id: False,
        probe=_FakeProbe(),
        now=now,
    )
    by_task = {action.task_id: action for action in actions}
    assert by_task["task-a"].kind == "abandon"  # ledger lease 已到期
    assert by_task["task-b"].kind == "wait"  # ledger lease 未到期，不重复执行

    projection = _project(change)
    assert projection.tasks["task-a"].status == "abandoned"
    assert projection.tasks["task-b"].status == "running"
