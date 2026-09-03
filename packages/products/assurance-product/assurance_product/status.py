from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import thaw_json
from graph_engine.evidence.models import (
    ActivationRecord,
    AttemptRecord,
    GraphInstanceRecord,
    InvocationProjection,
)

from assurance_product.change_workspace import ChangeWorkspace, safe_change_id
from assurance_product.generated_merge import merge_generated
from assurance_product.models import (
    AdapterEvidenceRefV1,
    ApplyManifestFileV1,
    ApplyManifestV1,
    ApplyProjectionV1,
    ChangeProjectionV1,
    CoverageProgressV1,
    EffectStatusV1,
    GraphStatusV1,
    NodeStatusV1,
    PendingInterruptStatusV1,
    PublicationProjectionV1,
    PublishReceiptV1,
    StatusV1,
)

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_RECEIPT_NAME = "publish-receipt.json"
_STATUS_NAME = "status.json"
_EVENTS_NAME = "events.jsonl"
_MANIFEST_NAME = "apply-manifest.json"


class ArchiveError(GraphEngineError):
    """Raised when a published change cannot be archived."""


_GRAPH_STATES: dict[str, Literal["inactive", "running", "failed", "stopped", "interrupted", "completed"]] = {
    "running": "running",
    "completed": "completed",
    "failed": "failed",
}
_PRODUCT_STATUS: dict[str, Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]] = {
    "succeeded": "completed",
    "failed": "failed",
    "stopped": "stopped",
    "running": "running",
}


def project_status_fields(
    projection: InvocationProjection,
    *,
    root_input_digest: str | None = None,
    selected_test_families: tuple[str, ...] = (),
    change_id: str | None = None,
    apply_manifest_digest: str | None = None,
    apply_file_count: int = 0,
    publication_status: Literal["not_ready", "ready", "published", "drifted"] = "not_ready",
) -> dict[str, object]:
    if projection.status == "not_started" or projection.invocation_id is None:
        raise ValueError("cannot project status for an unstarted invocation")
    if projection.lock_digest is None or projection.entrypoint is None:
        raise ValueError("started projection requires complete invocation identity")
    families = selected_test_families or _selected_families(projection)
    status = _status_class(projection)
    resolved_change = change_id or _change_id(projection)
    change_state = _change_state(
        status,
        publication_status,
        entrypoint=projection.entrypoint or "",
    )
    return {
        "schema_version": "1",
        "invocation_id": projection.invocation_id,
        "lock_digest": projection.lock_digest,
        "root_input_digest": root_input_digest or _require_digest(root_input_digest),
        "status": status,
        "entrypoint": projection.entrypoint,
        "graph_hierarchy": tuple(_graph_status(item) for item in projection.graph_instances),
        "node_states": tuple(_node_status(item) for item in projection.activations),
        "selected_test_families": families,
        "coverage_progress": _coverage_progress(projection),
        "durable_effects": tuple(_effect_status(item) for item in projection.effects),
        "adapter_evidence": tuple(_adapter_evidence(projection)),
        "pending_interrupt": _pending_interrupt(projection),
        "terminal_reason": projection.terminal_reason,
        "change": ChangeProjectionV1(change_id=resolved_change, state=change_state),
        "apply": ApplyProjectionV1(manifest_digest=apply_manifest_digest, file_count=apply_file_count),
        "publication": PublicationProjectionV1(status=publication_status),
    }


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
    change_state = "achieved" if mapped == "completed" else mapped
    hierarchy, nodes, pending = _langgraph_snapshot_fields(invocation_id, snapshot)
    return StatusV1.model_validate(
        {
            "schema_version": "1",
            "invocation_id": invocation_id,
            "lock_digest": lock_digest,
            "root_input_digest": root_input_digest,
            "status": mapped,
            "entrypoint": entrypoint,
            "graph_hierarchy": hierarchy,
            "node_states": nodes,
            "selected_test_families": (),
            "coverage_progress": None,
            "durable_effects": (),
            "adapter_evidence": _journal_adapter_evidence(journal_events),
            "pending_interrupt": pending,
            "terminal_reason": None,
            "change": {"change_id": change_id, "state": change_state},
            "apply": {"manifest_digest": None, "file_count": 0},
            "publication": {"status": "not_ready"},
        }
    )


def _langgraph_snapshot_fields(
    invocation_id: str, snapshot: object | None
) -> tuple[tuple[GraphStatusV1, ...], tuple[NodeStatusV1, ...], PendingInterruptStatusV1 | None]:
    if snapshot is None:
        return (), (), None
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    hierarchy = tuple(
        GraphStatusV1(
            graph_instance_id=invocation_id,
            graph_id=str(node),
            parent_graph_instance_id=None,
            state="running",
        )
        for node in nxt
    )
    nodes = tuple(
        NodeStatusV1(
            graph_instance_id=invocation_id,
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


def _journal_adapter_evidence(events: Sequence[object]) -> tuple[AdapterEvidenceRefV1, ...]:
    refs: list[AdapterEvidenceRefV1] = []
    for event in events:
        digest = getattr(event, "receipt_digest", None) or getattr(event, "envelope_digest", None)
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        refs.append(
            AdapterEvidenceRefV1(
                activation_id=str(getattr(event, "generation", "journal")),
                activity_id=str(getattr(event, "ordinal", "0")),
                reference_digest=digest,
                terminal_receipt_digest=digest if "Completion" in type(event).__name__ else None,
            )
        )
    return tuple(refs)


def render_status(
    projection: InvocationProjection,
    *,
    root_input_digest: str | None = None,
    selected_test_families: tuple[str, ...] = (),
    change_id: str | None = None,
    apply_manifest_digest: str | None = None,
    apply_file_count: int = 0,
    publication_status: Literal["not_ready", "ready", "published", "drifted"] = "not_ready",
) -> StatusV1:
    return StatusV1.model_validate(
        project_status_fields(
            projection,
            root_input_digest=root_input_digest,
            selected_test_families=selected_test_families,
            change_id=change_id,
            apply_manifest_digest=apply_manifest_digest,
            apply_file_count=apply_file_count,
            publication_status=publication_status,
        )
    )


def write_runtime_projections(
    workspace: ChangeWorkspace,
    projection: InvocationProjection,
    envelopes: Sequence[object],
    *,
    root_input_digest: str,
) -> StatusV1:
    status = render_status(
        projection,
        root_input_digest=root_input_digest,
        change_id=workspace.paths.change_root.name,
    )
    _write_canonical_json(workspace.paths.change_root / _STATUS_NAME, status.model_dump(mode="json"))
    lines = []
    for envelope in envelopes:
        dump = getattr(envelope, "model_dump", None)
        if dump is None:
            raise ValueError("ledger envelope is not serializable")
        lines.append(json.dumps(dump(mode="json"), sort_keys=True, separators=(",", ":")))
    payload = ("\n".join(lines) + "\n") if lines else ""
    _write_text(workspace.paths.change_root / _EVENTS_NAME, payload)
    return status


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
    _require_execution_gate(project, change_id)
    _require_quality_gate(project, change_id)
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


def _change_state(
    status: Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"],
    publication_status: Literal["not_ready", "ready", "published", "drifted"],
    *,
    entrypoint: str = "",
) -> Literal["running", "blocked", "interrupted", "stopped", "failed", "achieved"]:
    if status == "completed" and (entrypoint == "full" or publication_status in {"ready", "published"}):
        return "achieved"
    if status == "blocked":
        return "blocked"
    if status == "interrupted":
        return "interrupted"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return "running"


def _require_digest(value: str | None) -> str:
    del value
    raise ValueError("status projection requires a root-input digest")


def _change_id(projection: InvocationProjection) -> str:
    for graph in projection.graph_instances:
        if graph.parent_graph_instance_id is not None:
            continue
        payload = thaw_json(graph.input)
        if isinstance(payload, Mapping):
            change_id = payload.get("change_id")
            if isinstance(change_id, str) and change_id:
                return change_id
    raise ValueError("status projection requires a change_id")


def _require_execution_gate(project: Path, change_id: str) -> None:
    path = project / "qa" / "changes" / change_id / "execution" / "execute-result.json"
    payload = _read_json_object(path, "execution evidence")
    status = payload.get("status")
    if status != "passed":
        raise ValueError(f"execution gate failed: {status!r}")


def _require_quality_gate(project: Path, change_id: str) -> None:
    inspect_path = project / "qa" / "changes" / change_id / "inspect" / "inspection.json"
    report_path = project / "qa" / "changes" / change_id / "report" / "report.md"
    payload = _read_json_object(inspect_path, "quality evidence")
    coverage = payload.get("coverage")
    state = payload.get("coverage_state")
    if not isinstance(state, str) and isinstance(coverage, Mapping):
        state = coverage.get("coverage_state")
    decision_pass = isinstance(coverage, Mapping) and coverage.get("decision") is True
    if state == "satisfied" or (state is None and decision_pass):
        pass
    else:
        raise ValueError("quality gate failed")
    if not report_path.is_file() or report_path.is_symlink():
        raise ValueError("quality gate failed: report is missing")


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


def _status_class(
    projection: InvocationProjection,
) -> Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]:
    if projection.pending_interrupt is not None:
        return "interrupted"
    return _PRODUCT_STATUS.get(projection.status, "running")


def _graph_status(graph: GraphInstanceRecord) -> GraphStatusV1:
    return GraphStatusV1(
        graph_instance_id=graph.graph_instance_id,
        graph_id=graph.graph_id,
        parent_graph_instance_id=graph.parent_graph_instance_id,
        state=_GRAPH_STATES.get(graph.status, "running"),
    )


def _node_status(activation: ActivationRecord) -> NodeStatusV1:
    last = activation.attempts[-1] if activation.attempts else None
    return NodeStatusV1(
        graph_instance_id=activation.graph_instance_id,
        node_id=activation.node_id,
        state=_node_state(activation, last),
        attempt=None if last is None else last.attempt,
        lease_state=None if last is None else last.status,
        failure_category=_failure_category(activation, last),
        activity_reference_digest=(
            None if last is None or last.activity is None else last.activity.reference_digest
        ),
    )


def _node_state(
    activation: ActivationRecord,
    last: AttemptRecord | None,
) -> Literal[
    "inactive",
    "ready",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "stopped",
    "interrupted",
    "skipped",
]:
    if activation.status == "completed":
        return "succeeded"
    if activation.status == "failed":
        return "failed"
    if activation.status == "interrupted":
        return "interrupted"
    if activation.status == "stopped":
        return "stopped"
    if last is not None and last.attempt > 1 and last.status == "running":
        return "retrying"
    if last is not None and last.status == "running":
        return "running"
    return "running"


def _failure_category(activation: ActivationRecord, last: AttemptRecord | None) -> str | None:
    if last is not None and last.failure is not None:
        return last.failure.kind
    if activation.failure is not None:
        return activation.failure.kind
    return None


def _effect_status(effect: object) -> EffectStatusV1:
    receipt = getattr(effect, "receipt", None)
    receipt_digest = None if receipt is None else canonical_digest(cast(JSONValue, thaw_json(receipt)))
    return EffectStatusV1(
        effect_id=str(getattr(effect, "effect_id")),
        kind=str(getattr(effect, "kind")),
        state=str(getattr(effect, "status")),
        receipt_digest=receipt_digest,
    )


def _adapter_evidence(projection: InvocationProjection) -> tuple[AdapterEvidenceRefV1, ...]:
    refs: list[AdapterEvidenceRefV1] = []
    for activation in projection.activations:
        for attempt in activation.attempts:
            activity = attempt.activity
            if activity is None or activity.reference_digest is None:
                continue
            refs.append(
                AdapterEvidenceRefV1(
                    activation_id=activation.activation_id,
                    activity_id=activity.activity_id,
                    reference_digest=activity.reference_digest,
                    terminal_receipt_digest=activity.terminal_proof_digest,
                )
            )
    return tuple(refs)


def _pending_interrupt(projection: InvocationProjection) -> PendingInterruptStatusV1 | None:
    pending = projection.pending_interrupt
    if pending is None:
        return None
    activation = next(item for item in projection.activations if item.activation_id == pending.activation_id)
    return PendingInterruptStatusV1(
        node_id=activation.node_id,
        actions=pending.actions,
        reason_category=pending.reason,
    )


def _selected_families(projection: InvocationProjection) -> tuple[str, ...]:
    for graph in projection.graph_instances:
        if graph.parent_graph_instance_id is not None:
            continue
        payload = thaw_json(graph.input)
        if not isinstance(payload, Mapping):
            continue
        families = payload.get("selected_test_families")
        if isinstance(families, list | tuple):
            return tuple(str(item) for item in families)
    return ()


def _coverage_progress(projection: InvocationProjection) -> CoverageProgressV1 | None:
    for activation in projection.activations:
        payload = thaw_json(activation.output)
        if payload is None and activation.attempts:
            payload = thaw_json(activation.attempts[-1].output)
        if not isinstance(payload, Mapping):
            continue
        coverage = payload.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        if not {"measured", "rounds_used", "rounds_budget", "decision"}.issubset(coverage):
            continue
        decision = coverage["decision"]
        return CoverageProgressV1(
            round=int(coverage["rounds_used"]),
            maximum_rounds=int(coverage["rounds_budget"]),
            measured=coverage["measured"],
            decision="pass" if decision is True else ("continue" if decision is False else str(decision)),
        )
    return None
