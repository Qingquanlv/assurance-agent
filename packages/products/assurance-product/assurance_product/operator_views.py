"""Typed task, run, and node projections for managed operator reads."""

from __future__ import annotations
from collections.abc import Sequence

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from graph_engine.plugin_api import FrozenModel

from assurance_product.bootstrap.status import read_bootstrap_status, read_run_manifest
from assurance_product.task_records import TaskRecordError, read_task, run_manifests

Lifecycle = Literal["preparing", "running", "completed", "failed", "stopped", "unknown"]
Freshness = Literal["fresh", "stale"]
AttemptState = Literal["pending", "running", "completed", "failed", "stopped", "skipped", "unknown"]
ExecutionKind = Literal["agent", "command"]
OutputKind = Literal["obligations", "cases", "tests", "report", "advisory", "retro", "execution", "artifact"]

_RUNNING_PHASES = frozenset({"opencode_ready", "compiled", "started", "running"})
_logger = logging.getLogger(__name__)


class RunOutputRefV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output_id: str = Field(min_length=1)
    kind: OutputKind
    logical_path: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    stored_path: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    activation_id: str = Field(min_length=1)
    attempt_key: str | None = None


class NodeAttemptV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    activation_id: str = Field(min_length=1)
    attempt_key: str | None = None
    order: int
    label: str = Field(min_length=1)
    execution_kind: ExecutionKind
    state: AttemptState
    session_id: str | None = None
    parent_session_id: str | None = None
    outputs: tuple[RunOutputRefV1, ...] = ()


class RunViewV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    task_id: str
    task_directory: str
    change_id: str
    run_number: int
    requirement: str
    test_families: tuple[str, ...]
    baseline: tuple[str, ...] = ()
    config_source: str
    opencode_endpoint: str
    root_session_id: str | None = None
    lifecycle: Lifecycle
    freshness: Freshness
    stoppable: bool
    nodes: tuple[NodeAttemptV1, ...] = ()
    closure: dict[str, object] | None = None


class HistoryIssueV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str | None = None
    reason: str


class HistoryViewV1(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    runs: tuple[RunViewV1, ...] = ()
    issues: tuple[HistoryIssueV1, ...] = ()
    truncated: bool = False


class ProjectionError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def capabilities() -> dict[str, object]:
    return {
        "schema_version": "1",
        "existing_task_runs": True,
        "task_records": True,
        "node_sessions": True,
        "historical_outputs": True,
    }


def nodes_from_records(
    records: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> tuple[NodeAttemptV1, ...]:
    nodes: list[NodeAttemptV1] = []
    for record in records:
        kind = record.get("execution_kind")
        if kind not in ("agent", "command"):
            raise ProjectionError("invalid_input", "attempt execution kind is unknown")
        reference = record.get("activity_reference")
        session_id = None
        parent_session_id = None
        if kind == "agent" and isinstance(reference, dict):
            session = reference.get("session_id")
            parent = reference.get("parent_session_id")
            session_id = session if isinstance(session, str) and session else None
            parent_session_id = parent if isinstance(parent, str) and parent else None
        payload = {key: value for key, value in record.items() if key != "activity_reference"}
        nodes.append(
            NodeAttemptV1.model_validate(
                {
                    **payload,
                    "session_id": session_id,
                    "parent_session_id": parent_session_id,
                    "outputs": record.get("outputs", ()),
                }
            )
        )
    return tuple(sorted(nodes, key=lambda node: (node.order, node.attempt_key or "")))


def read_run_view(task_directory: Path, change_id: str) -> RunViewV1:
    task = task_directory.resolve()
    try:
        definition = read_task(task)
    except TaskRecordError as error:
        raise ProjectionError(error.kind, str(error)) from error
    if definition is None:
        raise ProjectionError("not_configured", "task is not configured")
    run_dir = task / ".aa" / "runs" / change_id
    if not run_dir.is_dir():
        raise ProjectionError("unknown_run", f"unknown run: {change_id}")
    try:
        manifest = read_run_manifest(run_dir)
        status = read_bootstrap_status(run_dir)
    except (OSError, ValueError) as error:
        raise ProjectionError("recovery", "run record is unreadable") from error
    if manifest.get("task_id") != definition.task_id:
        raise ProjectionError("invalid_input", "invalid task identity")
    lifecycle, freshness = _lifecycle(status.phase, status.exit_code)
    if status.change_id != change_id:
        lifecycle, freshness = "unknown", "stale"
    if (task / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3").is_file():
        publish_run_attempts(task, change_id)
    try:
        nodes = nodes_from_records(_attempt_records(run_dir))
    except (ProjectionError, ValidationError) as error:
        raise ProjectionError("invalid_input", str(error)) from error
    number = manifest.get("run_number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ProjectionError("invalid_input", "run number is missing")
    baseline = manifest.get("baseline")
    families = definition.test_families
    return RunViewV1(
        task_id=definition.task_id,
        task_directory=str(task),
        change_id=change_id,
        run_number=number,
        requirement=definition.requirement,
        test_families=families,
        baseline=tuple(item for item in baseline if isinstance(item, str))
        if isinstance(baseline, list)
        else (),
        config_source=str(manifest.get("config_source") or ""),
        opencode_endpoint=str(manifest.get("requested_opencode_endpoint") or ""),
        root_session_id=status.root_session_id,
        lifecycle=lifecycle,
        freshness=freshness,
        stoppable=lifecycle in {"preparing", "running"},
        nodes=_with_preserved_outputs(run_dir, nodes),
        closure=_closure(run_dir),
    )


def _with_preserved_outputs(run_dir: Path, nodes: tuple[NodeAttemptV1, ...]) -> tuple[NodeAttemptV1, ...]:
    from assurance_product.run_history import RunHistoryError, preserved_refs

    try:
        refs = preserved_refs(run_dir)
    except RunHistoryError as error:
        raise ProjectionError(error.kind, str(error)) from error
    if not refs:
        return nodes
    attached: list[NodeAttemptV1] = []
    for node in nodes:
        matched = tuple(
            ref
            for ref in refs
            if ref.node_id == node.node_id
            and ref.activation_id == node.activation_id
            and ref.attempt_key == node.attempt_key
        )
        attached.append(node.model_copy(update={"outputs": matched}))
    return tuple(attached)


def read_task_history(task_directory: Path) -> HistoryViewV1:
    task = task_directory.resolve()
    runs: list[RunViewV1] = []
    issues: list[HistoryIssueV1] = []
    try:
        manifests = run_manifests(task)
    except TaskRecordError as error:
        return HistoryViewV1(issues=(HistoryIssueV1(change_id=None, reason=str(error)),))
    for manifest in manifests:
        change_id = manifest.get("change_id")
        if (
            not isinstance(change_id, str)
            or not manifest.get("task_id")
            or not isinstance(manifest.get("run_number"), int)
        ):
            issues.append(
                HistoryIssueV1(
                    change_id=change_id if isinstance(change_id, str) else None,
                    reason="legacy run is missing managed identity",
                )
            )
            continue
        try:
            runs.append(read_run_view(task, change_id))
        except ProjectionError as error:
            issues.append(HistoryIssueV1(change_id=change_id, reason=str(error)))
    runs.sort(key=lambda run: run.run_number)
    return HistoryViewV1(runs=tuple(runs), issues=tuple(issues), truncated=False)


def _lifecycle(phase: str, exit_code: int | None) -> tuple[Lifecycle, Freshness]:
    if phase == "preparing":
        return "preparing", "fresh"
    if phase in _RUNNING_PHASES:
        return "running", "fresh"
    if phase == "terminal":
        if exit_code == 0:
            return "completed", "fresh"
        if exit_code == 20:
            return "stopped", "fresh"
        if exit_code is None:
            return "unknown", "stale"
        return "failed", "fresh"
    return "unknown", "stale"


def publish_run_attempts(task_directory: Path, change_id: str) -> None:
    import asyncio

    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from assurance_product.task_records import _managed_run

    task = task_directory.resolve()
    try:
        definition = read_task(task)
        if definition is None:
            raise TaskRecordError("not_configured", "task is not configured")
        run_dir, _status = _managed_run(definition, change_id)
        current_id = change_id
        status_path = task / "qa" / "status.json"
        if status_path.exists():
            current_id = json.loads(status_path.read_bytes())["change"]["change_id"]
            _managed_run(definition, current_id)
        workspace = ChangeWorkspace.open(task, current_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProjectionError("recovery", str(error)) from error

    async def _read() -> tuple[object, ...]:
        async with open_sqlite_checkpointer(workspace) as backend:
            return await SqliteAttemptCheckpointStore(backend).read_checkpoints()

    records = asyncio.run(_read())
    try:
        write_attempt_projection(run_dir, records, change_id)
    except (OSError, ValueError) as error:
        _logger.warning("run_attempt_projection_failed: %s", error)


def write_attempt_projection(run_dir: Path, records: Sequence[object], invocation_id: str) -> None:
    from assurance_product.run_history import publish_attempt_outputs, write_projection_bytes
    from graph_engine.attempts.orchestration.checkpoint import AttemptCheckpoint

    rows: list[dict[str, object]] = []
    order = 0
    for snapshot in records:
        if not isinstance(snapshot, AttemptCheckpoint):
            raise ProjectionError("invalid_input", "Attempt checkpoint record is unreadable")
        digest = snapshot.attempt_key.digest
        if snapshot.invocation_id != invocation_id:
            continue
        reference = snapshot.activity_reference if isinstance(snapshot.activity_reference, dict) else {}
        session = reference.get("session_id")
        session_id = session if isinstance(session, str) and session else None
        parent = reference.get("parent_session_id")
        parent_session_id = parent if isinstance(parent, str) and parent else None
        recorded_attempt = reference.get("attempt_key")
        attempt_key = recorded_attempt if isinstance(recorded_attempt, str) and recorded_attempt else None
        terminal = snapshot.terminal
        if terminal is not None:
            state = {
                "committed": "completed",
                "permanent": "failed",
                "retryable": "failed",
                "rejected": "stopped",
            }.get(terminal.resolution_kind, "unknown")
        elif snapshot.activity_state == "bound":
            state = "running"
        else:
            state = "pending"
        activation_id = snapshot.activity_id
        node_id = snapshot.semantic_node_id
        if (
            not isinstance(activation_id, str)
            or not activation_id
            or not isinstance(node_id, str)
            or not node_id
        ):
            continue
        order += 1
        rows.append(
            {
                "node_id": node_id,
                "activation_id": activation_id,
                "attempt_key": attempt_key,
                "order": order,
                "label": node_id,
                "execution_kind": "agent" if session_id else "command",
                "state": state,
                "activity_reference": {"session_id": session_id, "parent_session_id": parent_session_id},
            }
        )
        if terminal is not None and terminal.resolution_kind == "committed":
            publish_attempt_outputs(run_dir, digest, invocation_id, node_id, activation_id, attempt_key)
    if not rows:
        return
    nodes_from_records(rows)
    path = run_dir / "attempts.json"
    write_projection_bytes(path, json.dumps(rows).encode("utf-8"))


def _attempt_records(run_dir: Path) -> list[dict[str, object]]:
    path = run_dir / "attempts.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ProjectionError("invalid_input", "attempt records are unreadable")
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProjectionError("invalid_input", "attempt records are unreadable")
        records.append(item)
    return records


def _closure(run_dir: Path) -> dict[str, object] | None:
    path = run_dir / "closure.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return {str(key): value for key, value in raw.items()}
