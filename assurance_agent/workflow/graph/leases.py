"""Task attempt 的 retry/backoff 决策、running-tasks lease registry 与恢复分类。

retry 决策是纯函数：jitter 由 ``sha256(task_id:attempt_number)`` 派生（结构性
身份，绝不使用进程随机源），进程重启不会移动已持久化的 ``next_retry_at``。
``max_attempts`` 包含第一次 attempt；错误只有在 ``task.retry_policy.retry_on``
与 ``task.retryable_errors``（execution contract）**同时**出现时才可自动重试；
abandoned 消耗一次 attempt（started 事件已计数），不看错误 kind，只看剩余
attempt budget。

coordinator 所有的 ``running-tasks.json`` 只承载 liveness（pid/host/session/
heartbeat/lease expiry）：不进 strict ledger，永远不改变预算或 task 成功投影；
丢失后恢复退回 ledger 中 ``task_attempt_started.lease_expires_at`` 判断。写入
走「进程本地锁 + ``.progression.lock`` fcntl 锁 + 临时 sibling 文件 fsync 后
``os.replace``」，截断的临时文件永远不会替换上一份有效文件。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import TaskAttemptAbandonedEvent
from assurance_agent.workflow.core.progression import LOCK_FILENAME, transaction
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.models import ExecutableTask, GraphProjection
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef

RUNNING_TASKS_FILENAME = "running-tasks.json"
_LOCK_TIMEOUT_S = 0.5
_LOCK_POLL_S = 0.01

# 进程本地锁按 resolved change_dir 路径归档（与 progression 同协议）。
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LeaseError(AaError):
    """lease registry 基础设施失败：锁超时、文件损坏或读写错误。"""


# ---------------------------------------------------------------------------
# 注入时钟与确定性 backoff


class Clock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class AttemptDecision(BaseModel):
    """一次 retry 调度的纯决策：scheduler 据此 start/wait 或终止 task。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["start", "wait", "exhausted", "failed"]
    attempt_number: int | None = None
    next_retry_at: str | None = None
    reason: str | None = None


def retry_delay_seconds(policy: RetryPolicyDef, task_id: str, attempt_number: int) -> float:
    """第 ``attempt_number`` 次 attempt 失败后的等待秒数（确定性，可跨进程重放）。

    jitter 从 ``sha256(task_id:attempt_number)`` 派生，取值 ``[0.5*capped, capped]``；
    同一 (policy, task_id, attempt_number) 在任何进程里都得到同一结果，因此
    resume 后 ``next_retry_at`` 不漂移。
    """
    raw = policy.backoff.initial_seconds * (policy.backoff.multiplier ** max(0, attempt_number - 1))
    capped = min(raw, policy.backoff.max_seconds)
    if not policy.backoff.jitter or capped == 0:
        return capped
    digest = hashlib.sha256(f"{task_id}:{attempt_number}".encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return capped * (0.5 + 0.5 * fraction)


def compute_next_retry_at(
    policy: RetryPolicyDef,
    task_id: str,
    attempt_number: int,
    now: datetime,
) -> str:
    """失败事件的 ``next_retry_at``：``now + retry_delay_seconds`` 的 ISO 串。"""
    delay = retry_delay_seconds(policy, task_id, attempt_number)
    return (now + timedelta(seconds=delay)).isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def next_attempt_decision(
    *,
    task: ExecutableTask,
    projection: GraphProjection,
    now: datetime,
) -> AttemptDecision:
    """对一个 pending/failed/abandoned task 决定下一次 attempt。

    错误 kind 必须同时在 ``retry_policy.retry_on`` 与 contract 的
    ``retryable_errors`` 中才可重试，否则立即 ``failed``；``max_attempts``
    包含第一次 attempt，用尽即 ``exhausted``（普通 resume 绝不重置）；
    已持久化的 ``next_retry_at`` 未到则 ``wait``，绝不因进程重启提前重试。
    """
    proj = projection.tasks.get(task.task_id)
    if proj is None or proj.status == "pending":
        return AttemptDecision(kind="start", attempt_number=1)
    if proj.status == "running":
        # 未到期 attempt 绝不重复执行；orphan 由恢复分类（adopt/wait/abandon）处理。
        return AttemptDecision(kind="wait", reason="attempt still running; recovery classification owns it")
    if proj.status in ("succeeded", "interrupted"):
        raise ValueError(
            f"next_attempt_decision requires a pending/failed/abandoned task; "
            f"{task.task_id} is {proj.status}"
        )
    if proj.status == "abandoned":
        # abandoned 消耗一次 attempt（started 已计入 attempts_used），只看剩余 budget。
        if proj.attempts_used >= task.retry_policy.max_attempts:
            return AttemptDecision(
                kind="exhausted",
                reason=(
                    f"attempt budget exhausted after {proj.attempts_used} attempt(s) "
                    f"(max_attempts {task.retry_policy.max_attempts} includes the first attempt)"
                ),
            )
        return AttemptDecision(kind="start", attempt_number=proj.attempts_used + 1)

    # status == "failed"
    error_kind = proj.error_kind
    retryable = (
        error_kind is not None
        and error_kind in task.retry_policy.retry_on
        and error_kind in task.retryable_errors
    )
    if not retryable:
        return AttemptDecision(
            kind="failed",
            reason=f"error kind {error_kind!r} is not retryable for task {task.task_id}",
        )
    if proj.attempts_used >= task.retry_policy.max_attempts:
        return AttemptDecision(
            kind="exhausted",
            reason=(
                f"attempt budget exhausted after {proj.attempts_used} attempt(s) "
                f"(max_attempts {task.retry_policy.max_attempts} includes the first attempt)"
            ),
        )
    if proj.next_retry_at is not None and now < _parse_ts(proj.next_retry_at):
        return AttemptDecision(kind="wait", next_retry_at=proj.next_retry_at)
    return AttemptDecision(kind="start", attempt_number=proj.attempts_used + 1)


# ---------------------------------------------------------------------------
# coordinator 所有的 running-tasks lease registry（仅 liveness）


class RunningTaskLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    attempt_id: str
    pid: int
    host: str
    session_id: str | None
    started_at: str
    last_heartbeat_at: str
    lease_expires_at: str


class RunningTasksSnapshot(BaseModel):
    """``running-tasks.json`` 的磁盘格式：按 task_id 排序的 lease 列表。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    leases: tuple[RunningTaskLease, ...] = ()


def new_lease(
    *,
    task_id: str,
    attempt_id: str,
    session_id: str | None,
    started_at: str,
    lease_expires_at: str,
) -> RunningTaskLease:
    """构造当前进程/主机的 lease；heartbeat 起点等于 started_at。"""
    return RunningTaskLease(
        task_id=task_id,
        attempt_id=attempt_id,
        pid=os.getpid(),
        host=socket.gethostname(),
        session_id=session_id,
        started_at=started_at,
        last_heartbeat_at=started_at,
        lease_expires_at=lease_expires_at,
    )


def _thread_lock_for(change_dir: Path) -> threading.Lock:
    key = str(change_dir.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _lease_lock(change_dir: Path) -> Iterator[None]:
    """进程本地锁 + ``.progression.lock`` fcntl 排他锁（与 strict 事务互斥）。

    绝不在 progression transaction 内调用 registry 方法：fcntl flock 不跨
    open file description 重入，嵌套只会在锁超时后失败。
    """
    change_dir.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(change_dir)
    if not thread_lock.acquire(timeout=_LOCK_TIMEOUT_S):
        raise LeaseError(f"lease thread lock timeout for {change_dir}")
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(change_dir / LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LeaseError(f"fcntl lock timeout for {change_dir}") from None
                time.sleep(_LOCK_POLL_S)
        yield
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        thread_lock.release()


def _read_snapshot(change_dir: Path) -> RunningTasksSnapshot:
    path = change_dir / RUNNING_TASKS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RunningTasksSnapshot()
    except OSError as exc:
        raise LeaseError(f"cannot read {path}: {exc}") from exc
    try:
        return RunningTasksSnapshot.model_validate_json(raw)
    except ValidationError as exc:
        raise LeaseError(f"corrupt {path}: {exc}") from exc


def _write_snapshot(change_dir: Path, leases: Mapping[str, RunningTaskLease]) -> None:
    ordered = tuple(leases[task_id] for task_id in sorted(leases))
    payload = RunningTasksSnapshot(leases=ordered).model_dump(mode="json")
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path = change_dir / RUNNING_TASKS_FILENAME
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise LeaseError(f"cannot write {path}: {exc}") from exc


class LeaseRegistry:
    """``running-tasks.json`` 的唯一写方（coordinator）；只更新 liveness。

    heartbeat 只精确推进匹配的 (task_id, attempt_id)，scheduler 为每个运行中
    future 起一个 heartbeat 线程周期性调用它；它不能把 failed task 变成成功，
    也不触碰预算与成功投影。
    """

    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir

    @property
    def path(self) -> Path:
        return self._change_dir / RUNNING_TASKS_FILENAME

    def read_all(self) -> dict[str, RunningTaskLease]:
        with _lease_lock(self._change_dir):
            snapshot = _read_snapshot(self._change_dir)
        return {lease.task_id: lease for lease in snapshot.leases}

    def upsert(self, lease: RunningTaskLease) -> RunningTaskLease:
        """登记/刷新 lease；``started_at`` 更旧的 stale attempt 不得覆盖较新记录。"""
        with _lease_lock(self._change_dir):
            snapshot = _read_snapshot(self._change_dir)
            leases = {item.task_id: item for item in snapshot.leases}
            existing = leases.get(lease.task_id)
            if (
                existing is not None
                and existing.attempt_id != lease.attempt_id
                and _parse_ts(lease.started_at) < _parse_ts(existing.started_at)
            ):
                return existing
            leases[lease.task_id] = lease
            _write_snapshot(self._change_dir, leases)
            return lease

    def heartbeat(
        self,
        task_id: str,
        attempt_id: str,
        *,
        at: str,
        lease_expires_at: str,
    ) -> RunningTaskLease | None:
        """只推进精确匹配 (task_id, attempt_id) 的 lease；不匹配一律不动，返回 None。"""
        with _lease_lock(self._change_dir):
            snapshot = _read_snapshot(self._change_dir)
            leases = {item.task_id: item for item in snapshot.leases}
            existing = leases.get(task_id)
            if existing is None or existing.attempt_id != attempt_id:
                return None
            updated = existing.model_copy(
                update={"last_heartbeat_at": at, "lease_expires_at": lease_expires_at}
            )
            leases[task_id] = updated
            _write_snapshot(self._change_dir, leases)
            return updated

    def remove(self, task_id: str, attempt_id: str) -> None:
        """attempt 结算后摘除 lease；attempt_id 不匹配则不动（防误删新一代 attempt）。"""
        with _lease_lock(self._change_dir):
            snapshot = _read_snapshot(self._change_dir)
            leases = {item.task_id: item for item in snapshot.leases}
            existing = leases.get(task_id)
            if existing is None or existing.attempt_id != attempt_id:
                return
            del leases[task_id]
            _write_snapshot(self._change_dir, leases)


# ---------------------------------------------------------------------------
# orphan 恢复分类与 abandonment


class LivenessProbe(Protocol):
    """attempt 存活性探针；三态：True/False 确定，None 表示无法判定。"""

    def pid_alive(self, pid: int, host: str) -> bool | None:
        raise NotImplementedError

    def session_alive(self, session_id: str) -> bool | None:
        raise NotImplementedError


def _pid_alive(pid: int) -> bool:
    # 与 v1 driver_state.is_pid_alive 同一探针语义；v2 lease 独立实现，不复用 v1 文件。
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SystemLivenessProbe:
    """本机 PID 探针；跨主机 PID 与进程外 session 一律返回未知（None）。"""

    def pid_alive(self, pid: int, host: str) -> bool | None:
        if host != socket.gethostname():
            return None
        return _pid_alive(pid)

    def session_alive(self, session_id: str) -> bool | None:
        return None


class RecoveryAction(BaseModel):
    """对一个投影为 running 的 orphan attempt 的恢复分类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["adopt", "wait", "abandon"]
    task_id: str
    attempt_id: str
    reason: str | None = None


def classify_recovery(
    *,
    lease: RunningTaskLease,
    reconnect: bool,
    probe: LivenessProbe,
    now: datetime,
) -> RecoveryAction:
    """纯函数恢复分类：adopt（可重连且存活）→ abandon（证明死亡）→ wait（未到期）→ abandon（到期）。"""
    session_status = probe.session_alive(lease.session_id) if lease.session_id is not None else None
    if reconnect and session_status is True:
        return RecoveryAction(
            kind="adopt",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            reason="contract supports reconnect and session is alive",
        )
    if probe.pid_alive(lease.pid, lease.host) is False or session_status is False:
        return RecoveryAction(
            kind="abandon",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            reason="same-host pid/session provably dead",
        )
    if now < _parse_ts(lease.lease_expires_at):
        return RecoveryAction(
            kind="wait",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            reason="liveness unknown and lease unexpired; do not duplicate execution",
        )
    return RecoveryAction(
        kind="abandon",
        task_id=lease.task_id,
        attempt_id=lease.attempt_id,
        reason="lease expired",
    )


def abandon_running_attempt(
    change_dir: Path,
    *,
    invocation_id: str,
    checkpoint_ns: str,
    task_id: str,
    attempt_id: str,
    reason: str,
    abandoned_at: str,
) -> bool:
    """经 progression transaction 严格追加 ``task_attempt_abandoned``，按 attempt_id 去重。

    事务内重读 ledger 投影：仅当该 task 仍以同一 ``attempt_id`` 处于 running
    时才追加；重复或并发恢复不会产生第二条 abandonment。返回是否实际写入。
    """
    with transaction(change_dir) as txn:
        projection = fold_invocation_events(invocation_id, read_events_strict(change_dir))
        task = projection.tasks.get(task_id)
        if task is None or task.status != "running" or task.latest_attempt_id != attempt_id:
            return False
        txn.append_strict(
            TaskAttemptAbandonedEvent(
                type="task_attempt_abandoned",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                task_id=task_id,
                attempt_id=attempt_id,
                reason=reason,
                abandoned_at=abandoned_at,
            )
        )
    return True


def recover_running_tasks(
    change_dir: Path,
    *,
    projection: GraphProjection,
    leases: Mapping[str, RunningTaskLease],
    reconnect_for: Callable[[str], bool],
    probe: LivenessProbe,
    now: datetime,
) -> tuple[RecoveryAction, ...]:
    """对每个投影为 running 的 task 分类恢复；abandon 经 transaction 落 ledger 并去重。

    lease 文件缺失时退回 ledger 中 ``task_attempt_started.lease_expires_at``
    （liveness 未知）：未到期 ``wait``，到期 ``abandon``。abandon 之后调用方用
    更新后的投影喂 ``next_attempt_decision`` 决定是否创建新 attempt。
    """
    actions: list[RecoveryAction] = []
    for task_id in sorted(projection.tasks):
        task = projection.tasks[task_id]
        if task.status != "running" or task.latest_attempt_id is None:
            continue
        lease = leases.get(task_id)
        if lease is not None and lease.attempt_id != task.latest_attempt_id:
            lease = None  # 旧 attempt 的残留 lease 不是当前 attempt 的 liveness
        if lease is None:
            expires = task.lease_expires_at
            if expires is not None and now < _parse_ts(expires):
                action = RecoveryAction(
                    kind="wait",
                    task_id=task_id,
                    attempt_id=task.latest_attempt_id,
                    reason="lease file lost; ledger lease unexpired",
                )
            else:
                action = RecoveryAction(
                    kind="abandon",
                    task_id=task_id,
                    attempt_id=task.latest_attempt_id,
                    reason="lease file lost and ledger lease expired",
                )
        else:
            action = classify_recovery(
                lease=lease,
                reconnect=reconnect_for(task_id),
                probe=probe,
                now=now,
            )
        if action.kind == "abandon":
            abandon_running_attempt(
                change_dir,
                invocation_id=projection.invocation_id,
                checkpoint_ns=projection.checkpoint_ns,
                task_id=action.task_id,
                attempt_id=action.attempt_id,
                reason=action.reason or "abandoned",
                abandoned_at=now.isoformat(),
            )
        actions.append(action)
    return tuple(actions)


@contextmanager
def heartbeat_while(
    registry: LeaseRegistry,
    *,
    task_id: str,
    attempt_id: str,
    clock: Clock,
    heartbeat_seconds: float,
    lease_extension_seconds: float,
) -> Iterator[None]:
    """为运行中的 attempt 起一个只更新 liveness 的 heartbeat 线程。

    绝不能把 failed task 变成成功，也不触碰预算与成功投影；只推进匹配的
    ``(task_id, attempt_id)`` lease 到期时刻。退出 context 时停止线程。
    """
    stop = threading.Event()
    interval = max(0.05, float(heartbeat_seconds))
    extension = max(interval, float(lease_extension_seconds))

    def _loop() -> None:
        while not stop.wait(interval):
            now = clock.now()
            registry.heartbeat(
                task_id,
                attempt_id,
                at=now.isoformat(),
                lease_expires_at=(now + timedelta(seconds=extension)).isoformat(),
            )

    thread = threading.Thread(
        target=_loop,
        name=f"lease-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval + 1.0)


__all__ = [
    "RUNNING_TASKS_FILENAME",
    "AttemptDecision",
    "Clock",
    "LeaseError",
    "LeaseRegistry",
    "LivenessProbe",
    "RecoveryAction",
    "RunningTaskLease",
    "RunningTasksSnapshot",
    "SystemClock",
    "SystemLivenessProbe",
    "abandon_running_attempt",
    "classify_recovery",
    "compute_next_retry_at",
    "heartbeat_while",
    "new_lease",
    "next_attempt_decision",
    "recover_running_tasks",
    "retry_delay_seconds",
]
