from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.verification import VerifiedExecutionResultV1
from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError

from assurance_product.change_workspace import ChangeWorkspace, safe_change_id
from assurance_product.generated_merge import merge_generated
from assurance_product.models import (
    AdapterEvidenceRefV1,
    ApplyManifestFileV1,
    ApplyManifestV1,
    ExecutionGateRefV1,
    GraphStatusV1,
    NodeStatusV1,
    PendingInterruptStatusV1,
    PublishReceiptV1,
    QualityGateRefV1,
    StatusV1,
)
from assurance_product.verification import authenticate_verified_delivery

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_RECEIPT_NAME = "publish-receipt.json"
_STATUS_NAME = "status.json"
_MANIFEST_NAME = "apply-manifest.json"


class ArchiveError(GraphEngineError):
    """Raised when a published change cannot be archived."""


def render_status_from_langgraph(
    *,
    invocation_id: str,
    lock_digest: str,
    root_input_digest: str,
    entrypoint: str,
    change_id: str,
    status: str,
    snapshot: object | None = None,
    journal_events: Sequence[object] = (),
) -> StatusV1:
    mapped = "completed" if status in {"succeeded", "completed"} else status
    if mapped not in {"running", "blocked", "interrupted", "stopped", "failed", "completed"}:
        mapped = "failed"
    terminal_reason, full_achieved = _terminal_projection(entrypoint, snapshot)
    achieved = mapped == "completed" and (entrypoint != "full" or full_achieved)
    change_state = "achieved" if achieved else mapped
    if change_state == "completed":
        change_state = "stopped"
    active_hierarchy, active_nodes, pending = _langgraph_snapshot_fields(invocation_id, snapshot)
    journal_hierarchy, journal_nodes = _journal_attempt_fields(invocation_id, journal_events)
    execution_gate = _execution_gate_from_snapshot(snapshot)
    quality_gate = _quality_gate_from_snapshot(snapshot, change_id)
    return StatusV1.model_validate(
        {
            "schema_version": "1",
            "invocation_id": invocation_id,
            "lock_digest": lock_digest,
            "root_input_digest": root_input_digest,
            "status": mapped,
            "entrypoint": entrypoint,
            "graph_hierarchy": journal_hierarchy + active_hierarchy,
            "node_states": journal_nodes + active_nodes,
            "selected_test_families": _selected_test_families_from_snapshot(snapshot),
            "coverage_progress": _coverage_progress_from_snapshot(snapshot),
            "durable_effects": (),
            "adapter_evidence": _journal_adapter_evidence(invocation_id, journal_events),
            "execution_gate": execution_gate,
            "quality_gate": quality_gate,
            "pending_interrupt": pending,
            "terminal_reason": terminal_reason,
            "change": {"change_id": change_id, "state": change_state},
            "apply": {"manifest_digest": None, "file_count": 0},
            "publication": {"status": "not_ready"},
        }
    )


def load_persisted_status(workspace: ChangeWorkspace) -> StatusV1 | None:
    path = workspace.paths.change_root / _STATUS_NAME
    if not path.exists():
        return None
    try:
        return StatusV1.model_validate_json(_read_regular_file(path, _STATUS_NAME))
    except (OSError, ValueError) as error:
        raise ValueError("persisted status is invalid") from error


def _langgraph_snapshot_fields(
    invocation_id: str, snapshot: object | None
) -> tuple[tuple[GraphStatusV1, ...], tuple[NodeStatusV1, ...], PendingInterruptStatusV1 | None]:
    if snapshot is None:
        return (), (), None
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    hierarchy = tuple(
        GraphStatusV1(
            graph_instance_id=f"{invocation_id}:active:{node}",
            graph_id=str(node),
            parent_graph_instance_id=None,
            state="running",
        )
        for node in nxt
    )
    nodes = tuple(
        NodeStatusV1(
            graph_instance_id=f"{invocation_id}:active:{node}",
            node_id=str(node),
            state="running",
            attempt=None,
            lease_state=None,
            failure_category=None,
            activity_reference_digest=None,
        )
        for node in nxt
    )
    interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
    pending = None
    if interrupts:
        first = interrupts[0]
        value = getattr(first, "value", first)
        node_id = getattr(first, "id", None)
        actions: tuple[str, ...] = ()
        reason = "system"
        if isinstance(value, Mapping):
            if not node_id:
                raw_node = value.get("node_id")
                if isinstance(raw_node, str) and raw_node:
                    node_id = raw_node
            raw_actions = value.get("actions")
            if isinstance(raw_actions, list | tuple):
                actions = tuple(str(item) for item in raw_actions)
            raw_reason = value.get("reason") or value.get("kind")
            if isinstance(raw_reason, str) and raw_reason:
                reason = raw_reason
        pending = PendingInterruptStatusV1(
            node_id=str(node_id or (nxt[0] if nxt else "interrupt")),
            actions=actions,
            reason_category=reason,
        )
    return hierarchy, nodes, pending


def _journal_adapter_evidence(
    invocation_id: str,
    events: Sequence[object],
) -> tuple[AdapterEvidenceRefV1, ...]:
    from graph_engine.attempts.events import (
        ActivityBound,
        ActivityTerminalObserved,
        AttemptOpened,
        AttemptTerminated,
    )

    opened: AttemptOpened | None = None
    pending: dict[str, AdapterEvidenceRefV1] = {}
    refs: list[AdapterEvidenceRefV1] = []
    for event in events:
        if isinstance(event, AttemptOpened):
            refs.extend(pending.values())
            pending = {}
            opened = event
            continue
        if opened is None or opened.invocation_id != invocation_id:
            continue
        if isinstance(event, ActivityBound) and isinstance(event.reference, Mapping):
            session_id = event.reference.get("session_id")
            if isinstance(session_id, str) and session_id:
                pending[event.activity_id] = AdapterEvidenceRefV1(
                    activation_id=event.activity_id,
                    activity_id=session_id,
                    reference_digest=event.reference_digest,
                    terminal_receipt_digest=None,
                )
        elif isinstance(event, ActivityTerminalObserved) and event.activity_id in pending:
            prior = pending[event.activity_id]
            pending[event.activity_id] = prior.model_copy(
                update={
                    "terminal_receipt_digest": event.source_receipt_digest or None,
                }
            )
        elif isinstance(event, AttemptTerminated):
            refs.extend(pending.values())
            pending = {}
            opened = None
    refs.extend(pending.values())
    return tuple(refs)


def _journal_attempt_fields(
    invocation_id: str,
    events: Sequence[object],
) -> tuple[tuple[GraphStatusV1, ...], tuple[NodeStatusV1, ...]]:
    from graph_engine.attempts.events import ActivityBound, AttemptOpened, AttemptTerminated

    opened: AttemptOpened | None = None
    activity_reference_digest: str | None = None
    projected: dict[str, tuple[GraphStatusV1, NodeStatusV1]] = {}
    precedence = {"failed": 1, "stopped": 2, "succeeded": 3}
    for event in events:
        if isinstance(event, AttemptOpened):
            opened = event
            activity_reference_digest = None
            continue
        if opened is None:
            continue
        if isinstance(event, ActivityBound):
            activity_reference_digest = event.reference_digest
            continue
        if not isinstance(event, AttemptTerminated):
            continue
        if opened.invocation_id == invocation_id:
            node_state, graph_state, failure = _attempt_terminal_state(event)
            semantic_node_id = opened.semantic_node_id
            graph_instance_id = f"{invocation_id}:attempt:{semantic_node_id}"
            candidate = (
                GraphStatusV1(
                    graph_instance_id=graph_instance_id,
                    graph_id=semantic_node_id,
                    parent_graph_instance_id=None,
                    state=graph_state,
                ),
                NodeStatusV1(
                    graph_instance_id=graph_instance_id,
                    node_id=f"{semantic_node_id}/finalize",
                    state=node_state,
                    attempt=None,
                    lease_state=None,
                    failure_category=failure,
                    activity_reference_digest=activity_reference_digest,
                ),
            )
            previous = projected.get(semantic_node_id)
            if previous is None or precedence[node_state] > precedence[previous[1].state]:
                projected[semantic_node_id] = candidate
        opened = None
        activity_reference_digest = None
    return (
        tuple(graph for graph, _node in projected.values()),
        tuple(node for _graph, node in projected.values()),
    )


def _attempt_terminal_state(
    event: object,
) -> tuple[
    Literal["succeeded", "failed", "stopped"],
    Literal["completed", "failed", "stopped"],
    str | None,
]:
    resolution_kind = getattr(event, "resolution_kind", "")
    if resolution_kind == "committed":
        return "succeeded", "completed", None
    if resolution_kind == "rejected":
        return "stopped", "stopped", "rejected"
    failure = getattr(event, "failure_kind", None) or resolution_kind or "unknown"
    return "failed", "failed", str(failure)


def _terminal_projection(entrypoint: str, snapshot: object | None) -> tuple[str | None, bool]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return None, False
    raw = values.get("terminal")
    if not isinstance(raw, Mapping):
        return None, False
    status = raw.get("status")
    reason = raw.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str) or not reason:
        return None, False
    return reason, entrypoint == "full" and status == "completed" and reason == "achieved"


def _coverage_progress_from_snapshot(snapshot: object | None) -> Mapping[str, object] | None:
    from assurance_quality.contracts.assessment import InspectionOutcomeV1

    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping) or not values.get("coverage_state"):
        return None
    try:
        inspection = InspectionOutcomeV1.model_validate(values.get("inspection_outcome"))
        budgets = values.get("budgets")
        budget = budgets.get("coverage_rounds") if isinstance(budgets, Mapping) else None
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError("coverage budget must be a non-negative int")
        if values.get("coverage_epoch") != inspection.coverage_epoch:
            raise ValueError("coverage epoch must match the current inspection")
        if values["coverage_state"] != inspection.coverage_state:
            raise ValueError("coverage state must match the current inspection")
    except ValueError as error:
        raise ValueError("terminal checkpoint coverage progress is incomplete") from error
    return {
        "round": inspection.coverage_epoch,
        "maximum_rounds": budget,
        "measured": None,
        "decision": inspection.coverage_state,
    }


def _selected_test_families_from_snapshot(snapshot: object | None) -> tuple[str, ...]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return ()
    raw = values.get("selected_test_families")
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple) or any(not isinstance(item, str) for item in raw):
        raise ValueError("terminal checkpoint selected test families are invalid")
    families = tuple(raw)
    allowed = ("api", "e2e", "fuzz", "performance")
    if len(families) != len(set(families)) or tuple(item for item in allowed if item in families) != families:
        raise ValueError("terminal checkpoint selected test families are not canonical")
    return families


def finalize_achieved(
    project_root: Path,
    change_id: str,
    families: tuple[str, ...],
    *,
    invocation: Mapping[str, object] | StatusV1,
) -> StatusV1:
    project = Path(project_root)
    _require_terminal_full_success(invocation)
    merged = merge_generated(project, change_id, families)
    execution_gate = _require_execution_gate(project, change_id, invocation)
    quality_gate = _require_quality_gate(project, change_id, invocation, execution_gate)
    invocation_id = (
        invocation.invocation_id if isinstance(invocation, StatusV1) else invocation.get("invocation_id")
    )
    if not isinstance(invocation_id, str):
        raise ValueError("terminal invocation identity is missing")
    authenticate_verified_delivery(
        project,
        change_id,
        invocation_id,
        execution_gate=execution_gate,
        quality_gate=quality_gate,
    )
    manifest = _apply_manifest(project, change_id, merged)
    status = _achieved_status(invocation, change_id, manifest)
    change_root = project / "qa" / "changes" / change_id
    _write_canonical_json(change_root / "apply-manifest.json", manifest.model_dump(mode="json"))
    _write_canonical_json(change_root / "status.json", status.model_dump(mode="json"))
    return status


def archive_published(project_root: Path, change_id: str) -> dict[str, object]:
    try:
        workspace = ChangeWorkspace.open(Path(project_root).resolve(), safe_change_id(change_id))
    except ValueError as error:
        archived = _existing_archive_without_change(Path(project_root).resolve(), change_id)
        if archived is not None:
            return archived
        raise ArchiveError(str(error)) from error
    status = _read_archive_status(workspace)
    if status.change.state != "achieved" or status.change.change_id != workspace.paths.change_root.name:
        raise ArchiveError("cannot archive a change that is not achieved")
    if status.publication.status != "published":
        raise ArchiveError("cannot archive before publish")
    _authenticate_publish_receipt(workspace)
    archive_root = _relocate_change(workspace)
    return {
        "change_id": workspace.paths.change_root.name,
        "archive_root": f"qa/archive/{archive_root.name}",
    }


def _existing_archive_without_change(project: Path, change_id: str) -> dict[str, object] | None:
    try:
        resolved_id = safe_change_id(change_id)
    except ValueError:
        return None
    destination = project / "qa" / "archive" / resolved_id
    source = project / "qa" / "changes" / resolved_id
    if source.exists() or destination.is_symlink() or not destination.is_dir():
        return None
    return {
        "change_id": resolved_id,
        "archive_root": f"qa/archive/{resolved_id}",
    }


def _read_archive_status(workspace: ChangeWorkspace) -> StatusV1:
    path = workspace.paths.change_root / _STATUS_NAME
    try:
        return StatusV1.model_validate_json(_read_regular_file(path, _STATUS_NAME))
    except FileNotFoundError as error:
        raise ArchiveError("achieved status is missing") from error
    except (OSError, ValueError) as error:
        raise ArchiveError("achieved status is invalid") from error


def _authenticate_publish_receipt(workspace: ChangeWorkspace) -> PublishReceiptV1:
    path = workspace.paths.change_root / _RECEIPT_NAME
    if not path.exists():
        raise ArchiveError("publish receipt is missing")
    try:
        receipt = PublishReceiptV1.model_validate_json(_read_regular_file(path, _RECEIPT_NAME))
        manifest = ApplyManifestV1.model_validate_json(
            _read_regular_file(workspace.paths.apply_manifest, _MANIFEST_NAME)
        )
    except FileNotFoundError as error:
        raise ArchiveError("publish receipt is missing") from error
    except (OSError, ValueError) as error:
        raise ArchiveError("publish receipt is invalid") from error
    if receipt.change_id != workspace.paths.change_root.name or receipt.change_id != manifest.change_id:
        raise ArchiveError("publish receipt change_id does not match the change")
    if receipt.manifest_digest != manifest.digest:
        raise ArchiveError("publish receipt does not match the apply manifest")
    receipt_files = {
        (item.target_path, item.source_path, item.source_sha256, item.baseline_sha256)
        for item in receipt.files
    }
    manifest_files = {
        (item.target_path, item.source_path, item.source_sha256, item.baseline_sha256)
        for item in manifest.files
    }
    if receipt_files != manifest_files:
        raise ArchiveError("publish receipt does not match the apply manifest")
    source_digest = canonical_digest({item.target_path: item.source_sha256 for item in receipt.files})
    if receipt.source_digest != source_digest or receipt.final_digest != source_digest:
        raise ArchiveError("publish receipt does not match the apply manifest")
    for item in receipt.files:
        target = workspace.paths.project_root / item.target_path
        if target.is_symlink() or not target.is_file():
            raise ArchiveError("publish receipt is tampered")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        recorded = item.final_sha256.removeprefix("sha256:")
        if actual != recorded:
            raise ArchiveError("publish receipt is tampered")
    return receipt


def _relocate_change(workspace: ChangeWorkspace) -> Path:
    source = workspace.paths.change_root
    archive_parent = _ensure_archive_parent(workspace.paths.project_root)
    destination = archive_parent / source.name
    if destination.exists():
        return _finish_committed_archive(source, destination)
    if _same_filesystem(source, archive_parent):
        os.rename(source, destination)
        _fsync_directory(archive_parent)
        return destination
    temporary = archive_parent / f".{source.name}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_verified_tree(source, temporary)
        os.rename(temporary, destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    try:
        _fsync_directory(archive_parent)
    except OSError:
        pass
    return _finish_committed_archive(source, destination)


def _finish_committed_archive(source: Path, destination: Path) -> Path:
    if destination.is_symlink() or not destination.is_dir():
        raise ArchiveError("archive destination already exists")
    if not source.exists():
        return destination
    if _tree_file_bytes(source) != _tree_file_bytes(destination):
        raise ArchiveError("archive destination already exists")
    shutil.rmtree(source)
    return destination


def _tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {relative: content for relative, content, _mode in _collect_regular_files(root)}


def _ensure_archive_parent(project: Path) -> Path:
    parent = project / "qa" / "archive"
    parent.mkdir(parents=True, exist_ok=True)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArchiveError("qa/archive is not a real directory")
    return parent


def _same_filesystem(source: Path, destination_parent: Path) -> bool:
    return source.stat().st_dev == destination_parent.stat().st_dev


def _copy_verified_tree(source: Path, destination: Path) -> None:
    files = _collect_regular_files(source)
    destination.mkdir(parents=False, exist_ok=False)
    created_dirs = {destination}
    for relative, content, mode in files:
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent not in created_dirs:
            created_dirs.add(target.parent)
        _write_regular_file(target, content, mode)
    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    copied = _collect_regular_files(destination)
    if [(relative, content) for relative, content, _mode in copied] != [
        (relative, content) for relative, content, _mode in files
    ]:
        raise ArchiveError("archive copy verification failed")


def _collect_regular_files(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    collected: list[tuple[str, bytes, int]] = []

    def walk(directory: Path) -> None:
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArchiveError("archive path is not a real directory")
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for entry in children:
            path = Path(entry.path)
            child = path.lstat()
            if stat.S_ISLNK(child.st_mode):
                raise ArchiveError(f"symlink is not allowed: {path.name}")
            if stat.S_ISDIR(child.st_mode):
                walk(path)
                continue
            _reject_irregular(child, path.name)
            collected.append(
                (path.relative_to(root).as_posix(), path.read_bytes(), stat.S_IMODE(child.st_mode))
            )

    walk(root)
    return tuple(collected)


def _write_regular_file(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, _FILE_WRITE_FLAGS, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise ArchiveError(f"failed to write {path.name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, label: str) -> bytes:
    info = path.lstat()
    _reject_irregular(info, label)
    return path.read_bytes()


def _reject_irregular(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise ArchiveError(f"symlink is not allowed: {label}")
    if not stat.S_ISREG(info.st_mode):
        raise ArchiveError(f"path is not a regular file: {label}")
    if info.st_nlink != 1:
        raise ArchiveError(f"hard link is not allowed: {label}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_terminal_full_success(invocation: Mapping[str, object] | StatusV1) -> None:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    if payload.get("entrypoint") != "full":
        raise ValueError("achieved requires the full entrypoint")
    if payload.get("status") != "completed":
        raise ValueError("achieved requires terminal workflow success")
    if payload.get("pending_interrupt") is not None:
        raise ValueError("achieved requires no pending interrupt")
    reason = payload.get("terminal_reason")
    if isinstance(reason, str) and reason in {"failed", "exhausted", "not_achieved"}:
        raise ValueError(f"achieved rejects {reason} terminal state")
    node_states = payload.get("node_states")
    if isinstance(node_states, tuple | list):
        for item in node_states:
            if not isinstance(item, Mapping):
                continue
            status = item.get("status")
            node_id = item.get("node_id")
            if status in {"failed", "exhausted"} or node_id in {"failed", "exhausted", "not-achieved"}:
                raise ValueError("achieved rejects failed or exhausted state")


def _execution_gate_from_snapshot(snapshot: object | None) -> ExecutionGateRefV1 | None:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return None
    names = (
        "execution_semantic_node_id",
        "batch_id",
        "execution_evidence",
        "execution_digest",
    )
    present = tuple(name for name in names if name in values)
    if not any(
        values.get(name) for name in ("execution_semantic_node_id", "execution_evidence", "execution_digest")
    ):
        return None
    if len(present) != len(names):
        raise ValueError("terminal execution checkpoint identity is incomplete")
    if values.get("validation_profile") in {"api_db.v1", "api_db_trace.v1"}:
        try:
            evidence = VerifiedExecutionResultV1.model_validate(values["execution_evidence"])
            cycle = VerifiedExecutionCycleResultV1.model_validate(values.get("execution_result"))
        except ValueError as error:
            raise ValueError("terminal verified execution checkpoint is invalid") from error
        document = evidence.model_dump(mode="json")
        digest = canonical_digest(cast(JSONValue, document))
        if (
            values["batch_id"] != evidence.batch_id
            or values["execution_digest"] != digest
            or cycle.batch_id != evidence.batch_id
            or cycle.validation_profile != evidence.validation_profile
            or cycle.execution_id != evidence.execution_id
            or cycle.coverage_epoch != evidence.coverage_epoch
            or cycle.repair_round != evidence.repair_round
            or cycle.attempt_key != evidence.attempt_key
            or cycle.receipt.receipt_digest is None
        ):
            raise ValueError("terminal verified execution checkpoint identity drifted")
        return ExecutionGateRefV1(
            semantic_node_id=values["execution_semantic_node_id"],
            batch_id=evidence.batch_id,
            execution_digest=digest,
            validation_profile=evidence.validation_profile,
            execution_receipt_id=cycle.receipt.receipt_id,
            execution_receipt_digest=cycle.receipt.receipt_digest,
        )
    evidence, document = _validated_execution_evidence(
        values["execution_evidence"],
        label="terminal execution checkpoint evidence",
    )
    digest = canonical_digest(cast(JSONValue, document))
    if values["batch_id"] != evidence.batch_id or values["execution_digest"] != digest:
        raise ValueError("terminal execution checkpoint evidence identity drifted")
    return ExecutionGateRefV1.model_validate(
        {
            "semantic_node_id": values["execution_semantic_node_id"],
            "batch_id": evidence.batch_id,
            "execution_digest": digest,
        }
    )


def _diagnostic_report_outcome(values: Mapping[str, object]) -> Mapping[str, object] | None:
    from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
    from graph_engine.attempts.resolutions import ReceiptRef

    if (
        not values.get("inspection_outcome")
        or not values.get("report_refs")
        or not values.get("report_receipt")
    ):
        return None
    refs = values["report_refs"]
    if not isinstance(refs, (list, tuple)):
        return None
    try:
        inspection = InspectionOutcomeV1.model_validate(values["inspection_outcome"])
        return ReportOutcomeV1(
            change_id=inspection.change_id,
            coverage_epoch=inspection.coverage_epoch,
            batch_id=inspection.batch_id,
            inspection_receipt=inspection.inspection_receipt,
            plan_digest=inspection.plan_digest,
            plan_ref=inspection.plan_ref,
            report_refs=tuple(refs),
            report_receipt=ReceiptRef.model_validate(values["report_receipt"]),
        ).model_dump(mode="json")
    except (TypeError, ValueError):
        return None


def _quality_gate_from_snapshot(
    snapshot: object | None,
    change_id: str,
) -> QualityGateRefV1 | None:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return None
    inspection = values.get("inspection_outcome")
    if not isinstance(inspection, Mapping) or not inspection:
        return None
    raw_report = values.get("report_outcome")
    report = raw_report if isinstance(raw_report, Mapping) and raw_report else None
    report = report or _diagnostic_report_outcome(values)
    try:
        payload: dict[str, object] = {"inspection": inspection}
        if report:
            payload["report"] = report
        gate = QualityGateRefV1.model_validate(payload)
    except ValueError as error:
        raise ValueError("terminal quality checkpoint is invalid") from error
    if gate.inspection.change_id != change_id:
        raise ValueError("terminal quality checkpoint identity drifted")
    if values.get("batch_id") != gate.inspection.batch_id:
        raise ValueError("terminal quality checkpoint batch drifted")
    if values.get("coverage_epoch", 0) != gate.inspection.coverage_epoch:
        raise ValueError("terminal quality checkpoint epoch drifted")
    return gate


def _execution_gate_from_invocation(
    invocation: Mapping[str, object] | StatusV1,
) -> ExecutionGateRefV1:
    raw = invocation.execution_gate if isinstance(invocation, StatusV1) else invocation.get("execution_gate")
    if raw is None:
        raise ValueError("execution gate reference is missing")
    try:
        return raw if isinstance(raw, ExecutionGateRefV1) else ExecutionGateRefV1.model_validate(raw)
    except ValueError as error:
        raise ValueError("execution gate reference is invalid") from error


def _validated_execution_evidence(
    raw: object,
    *,
    label: str,
) -> tuple[ExecutionEvidenceV1, dict[str, object]]:
    try:
        evidence = ExecutionEvidenceV1.model_validate(raw)
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if evidence.executed_at is None:
        raise ValueError(f"{label} is missing executed_at")
    mapping = cast(JSONValue, evidence.mapping.model_dump(mode="json"))
    receipt = cast(JSONValue, evidence.receipt.model_dump(mode="json"))
    if evidence.mapping_digest != canonical_digest(mapping):
        raise ValueError(f"{label} mapping digest drifted")
    if evidence.receipt_digest != canonical_digest(receipt):
        raise ValueError(f"{label} receipt digest drifted")
    counts = {
        "passed": sum(item.status == "passed" for item in evidence.results),
        "failed": sum(item.status == "failed" for item in evidence.results),
        "skipped": sum(item.status == "skipped" for item in evidence.results),
    }
    receipt_total = evidence.receipt.passed + evidence.receipt.failed + evidence.receipt.skipped
    if (
        evidence.receipt.collected < len(evidence.results)
        or receipt_total != evidence.receipt.collected
        or evidence.receipt.passed < counts["passed"]
        or evidence.receipt.failed < counts["failed"]
        or evidence.receipt.skipped < counts["skipped"]
    ):
        raise ValueError(f"{label} receipt counts drifted")
    expected_status = (
        "failed"
        if evidence.receipt.exit_code != 0
        or evidence.receipt.failed != 0
        or any(item.status == "failed" for item in evidence.results)
        else "passed"
    )
    if evidence.status != expected_status:
        raise ValueError(f"{label} status drifted")
    return evidence, cast(dict[str, object], evidence.model_dump(mode="json"))


def _require_execution_gate(
    project: Path,
    change_id: str,
    invocation: Mapping[str, object] | StatusV1,
) -> ExecutionGateRefV1:
    gate = _execution_gate_from_invocation(invocation)
    filename = {
        "execution.execute": "execute-result.json",
        "execution.run": "run-result.json",
    }[gate.semantic_node_id]
    execution_root = project / "qa" / "changes" / change_id / "execution"
    payload = _read_json_object(execution_root / filename, "execution evidence")
    if gate.validation_profile is not None:
        try:
            verified = VerifiedExecutionResultV1.model_validate(payload)
        except ValueError as error:
            raise ValueError("verified execution evidence is invalid") from error
        document = verified.model_dump(mode="json")
        if (
            verified.change_id != change_id
            or verified.batch_id != gate.batch_id
            or verified.validation_profile != gate.validation_profile
        ):
            raise ValueError("verified execution evidence identity drifted")
        if canonical_digest(cast(JSONValue, document)) != gate.execution_digest:
            raise ValueError("verified execution evidence digest drifted")
        if verified.completion_status != "collected":
            raise ValueError("verified execution gate is incomplete")
        return gate
    evidence, document = _validated_execution_evidence(payload, label="execution evidence")
    if evidence.change_id != change_id or evidence.batch_id != gate.batch_id:
        raise ValueError("execution evidence identity drifted")
    if canonical_digest(cast(JSONValue, document)) != gate.execution_digest:
        raise ValueError("execution evidence digest drifted")
    if gate.semantic_node_id == "execution.run":
        initial_payload = _read_json_object(
            execution_root / "execute-result.json",
            "initial execution evidence",
        )
        initial, _document = _validated_execution_evidence(
            initial_payload,
            label="initial execution evidence",
        )
        if initial.change_id != change_id or initial.status != "failed":
            raise ValueError("execution rerun is not preceded by failed initial evidence")
        if (
            initial.mapping != evidence.mapping
            or initial.selected_targets != evidence.selected_targets
            or initial.runner_profile_digest != evidence.runner_profile_digest
        ):
            raise ValueError("execution rerun selection drifted")
    if evidence.status != "passed":
        raise ValueError(f"execution gate failed: {evidence.status!r}")
    return gate


def _quality_gate_from_invocation(
    invocation: Mapping[str, object] | StatusV1,
) -> QualityGateRefV1:
    raw = invocation.quality_gate if isinstance(invocation, StatusV1) else invocation.get("quality_gate")
    if raw is None:
        raise ValueError("quality gate reference is missing")
    try:
        return raw if isinstance(raw, QualityGateRefV1) else QualityGateRefV1.model_validate(raw)
    except ValueError as error:
        raise ValueError("quality gate reference is invalid") from error


def _require_quality_gate(
    project: Path,
    change_id: str,
    invocation: Mapping[str, object] | StatusV1,
    execution_gate: ExecutionGateRefV1,
) -> QualityGateRefV1:
    reference = _quality_gate_from_invocation(invocation)
    inspection = reference.inspection
    if reference.report is None:
        raise ValueError("quality gate failed")
    if inspection.change_id != change_id or inspection.batch_id != execution_gate.batch_id:
        raise ValueError("quality inspection identity drifted")
    if inspection.disposition != "satisfied" or inspection.coverage_state != "satisfied":
        raise ValueError("quality gate failed")
    filename = (
        "run-result.json" if execution_gate.semantic_node_id == "execution.run" else "execute-result.json"
    )
    execution_path = f"qa/changes/{change_id}/execution/{filename}"
    if not any(ref.path == execution_path for ref in inspection.assessment_refs):
        raise ValueError("quality inspection execution reference is missing")
    refs = (
        *inspection.reviewed_case.preparation_refs,
        *inspection.reviewed_case.case_refs,
        inspection.reviewed_case.review_ref,
        inspection.mapping_ref,
        *inspection.assessment_refs,
        *reference.report.report_refs,
    )
    for ref in refs:
        path = project.joinpath(*ref.path.split("/"))
        _reject_symlink_components(project, path)
        try:
            content = _read_regular_file(path, "quality evidence")
        except OSError as error:
            raise ValueError(f"quality evidence is missing: {ref.path}") from error
        if hashlib.sha256(content).hexdigest() != ref.digest:
            raise ValueError(f"quality evidence digest drifted: {ref.path}")
    return reference


def _reject_symlink_components(root: Path, target: Path) -> None:
    path = root
    for part in target.relative_to(root).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("quality evidence path is a symlink")


def _apply_manifest(project: Path, change_id: str, merged: object) -> ApplyManifestV1:
    files = []
    for item in getattr(merged, "files"):
        target = project.joinpath(*str(item.target_path).split("/"))
        baseline = None
        if target.is_file() and not target.is_symlink() and target.stat().st_nlink == 1:
            baseline = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        files.append(
            ApplyManifestFileV1(
                target_path=item.target_path,
                source_path=item.staged_path,
                source_sha256=item.sha256,
                baseline_sha256=baseline,
                mode=item.mode,
                operation=item.operation,
            )
        )
    ordered = tuple(sorted(files, key=lambda item: item.target_path))
    return ApplyManifestV1(
        schema_version="1",
        change_id=change_id,
        digest=str(getattr(merged, "digest")),
        files=ordered,
    )


def _achieved_status(
    invocation: Mapping[str, object] | StatusV1,
    change_id: str,
    manifest: ApplyManifestV1,
) -> StatusV1:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    payload["change"] = {"change_id": change_id, "state": "achieved"}
    payload["apply"] = {"manifest_digest": manifest.digest, "file_count": len(manifest.files)}
    payload["publication"] = {"status": "ready"}
    return StatusV1.model_validate(payload)


def _read_json_object(path: Path, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} is invalid")
    return payload


def _write_canonical_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
