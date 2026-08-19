"""Bounded durable revision views and exact manual plan-revision capture.

Transport lives under ``.graph-runtime/revision-views/<interrupt-id>/`` and is
never evidence. Accepted revisions are published only as immutable TreeStore
objects derived from exact allowlisted plan bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from assurance_kernel.exceptions import AaError
from assurance_kernel.identifiers import assert_path_segment_safe
from assurance_kernel.workflow.core.graph_events import (
    GraphInterruptedEvent,
    GraphResumedEvent,
    ManualPlanRevisionEvent,
    ResumeAnchor,
)
from assurance_kernel.workflow.core.progression import ProgressionTxn
from assurance_kernel.workflow.graph.compiler import canonical_digest
from assurance_kernel.workflow.graph.contracts import ResourcePath
from assurance_kernel.workflow.graph.models import ResumeCommand
from assurance_kernel.workflow.graph.workspace import TreeFileRevision, TreeStore

_REVISION_VIEWS_RELPATH = PurePosixPath(".graph-runtime") / "revision-views"
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_PREFIX_CONFLICT = "manual_plan_revision_prefix_conflict"


class ManualRevisionError(AaError):
    """Bounded revision-view inventory or candidate validation failure."""


@dataclass(frozen=True, slots=True)
class RevisionPathBaseline:
    logical_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RevisionViewBinding:
    interrupt_id: str
    owner_invocation_id: str
    base_tree_id: str
    view_relpath: str
    logical_paths: tuple[str, ...]
    baseline: tuple[RevisionPathBaseline, ...]


@dataclass(frozen=True, slots=True)
class GateEvidenceEpoch:
    source_gate_attempt_id: str
    source_gate_tree_id: str


@dataclass(frozen=True, slots=True)
class ManualRevisionTransition:
    revision: ManualPlanRevisionEvent
    resumes: tuple[GraphResumedEvent, ...]


def _after_revision_inventory_hook(view_root: Path) -> None:
    """Test seam between inventory validation and descriptor-bound reads."""
    del view_root


def _view_relpath_for(interrupt_id: str) -> str:
    assert_path_segment_safe(interrupt_id, label="interrupt id")
    return (_REVISION_VIEWS_RELPATH / interrupt_id).as_posix()


def _canonical_logical_paths(logical_paths: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    canonical: list[str] = []
    for raw in logical_paths:
        path = ResourcePath.parse(raw)
        if path.root != "change" or not path.pattern.startswith("plans/"):
            raise ManualRevisionError(f"revision view path must stay under change:plans/: {raw}")
        if path.pattern.endswith("/") or path.pattern == "plans":
            raise ManualRevisionError(f"revision view path must be a file: {raw}")
        key = f"{path.root}:{path.pattern}"
        if key in seen:
            raise ManualRevisionError(f"duplicate revision view path: {key}")
        seen.add(key)
        canonical.append(key)
    return tuple(sorted(canonical))


def _relative_for(logical_path: str) -> str:
    return ResourcePath.parse(logical_path).pattern


def _expected_inventory(logical_paths: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    files = frozenset(_relative_for(path) for path in logical_paths)
    dirs: set[str] = set()
    for rel in files:
        parts = PurePosixPath(rel).parts
        for index in range(1, len(parts)):
            dirs.add(PurePosixPath(*parts[:index]).as_posix())
    return files, frozenset(dirs)


def _open_dir_nofollow(parent_fd: int, name: str) -> int:
    return os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)


def _open_view_root(change_dir: Path, view_relpath: str) -> int:
    fd = os.open(change_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in PurePosixPath(view_relpath).parts:
            next_fd = _open_dir_nofollow(fd, component)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise ManualRevisionError(f"revision view root unavailable: {view_relpath}") from exc


def _inventory_revision_view(view_fd: int) -> tuple[frozenset[str], frozenset[str]]:
    files: set[str] = set()
    dirs: set[str] = set()

    def visit(dir_fd: int, prefix: str) -> None:
        try:
            names = os.listdir(dir_fd)
        except OSError as exc:
            raise ManualRevisionError("revision view inventory failed") from exc
        for name in names:
            rel = name if not prefix else f"{prefix}/{name}"
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise ManualRevisionError(f"revision view inventory failed: {rel}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise ManualRevisionError(f"symlink in revision view: {rel}")
            if stat.S_ISDIR(st.st_mode):
                dirs.add(rel)
                child_fd = _open_dir_nofollow(dir_fd, name)
                try:
                    visit(child_fd, rel)
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(st.st_mode):
                files.add(rel)
                continue
            raise ManualRevisionError(f"non-file in revision view: {rel}")

    visit(view_fd, "")
    return frozenset(files), frozenset(dirs)


def _assert_inventory_matches(
    files: frozenset[str],
    dirs: frozenset[str],
    logical_paths: tuple[str, ...],
) -> None:
    expected_files, expected_dirs = _expected_inventory(logical_paths)
    missing = expected_files - files
    extra = files - expected_files
    extra_dirs = dirs - expected_dirs
    if missing:
        raise ManualRevisionError(f"revision view inventory missing allowlisted path: {sorted(missing)[0]}")
    if extra:
        raise ManualRevisionError(f"revision view inventory has extra path: {sorted(extra)[0]}")
    if extra_dirs:
        raise ManualRevisionError(f"revision view inventory has extra directory: {sorted(extra_dirs)[0]}")


def _read_file_nofollow(parent_fd: int, name: str, *, rel: str) -> bytes:
    try:
        fd = os.open(name, _OPEN_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ManualRevisionError(f"symlink or unreadable path in revision view: {rel}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ManualRevisionError(f"non-file in revision view: {rel}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_relative_via_descriptors(view_fd: int, rel: str) -> bytes:
    parts = PurePosixPath(rel).parts
    if not parts:
        raise ManualRevisionError(f"empty revision view path: {rel}")
    fd = view_fd
    opened: list[int] = []
    try:
        for component in parts[:-1]:
            fd = _open_dir_nofollow(fd, component)
            opened.append(fd)
        return _read_file_nofollow(fd, parts[-1], rel=rel)
    except OSError as exc:
        raise ManualRevisionError(f"symlink or unreadable path in revision view: {rel}") from exc
    finally:
        for child_fd in reversed(opened):
            os.close(child_fd)


def _read_validated_revision_files(
    change_dir: Path,
    binding: RevisionViewBinding,
) -> Mapping[str, bytes]:
    logical_paths = _canonical_logical_paths(tuple(binding.logical_paths))
    if tuple(binding.logical_paths) != logical_paths:
        raise ManualRevisionError("revision view binding logical_paths must be canonical")

    view_root = change_dir / binding.view_relpath
    view_fd = _open_view_root(change_dir, binding.view_relpath)
    try:
        files, dirs = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files, dirs, logical_paths)
        _after_revision_inventory_hook(view_root)
        replacements: dict[str, bytes] = {}
        for logical_path in logical_paths:
            rel = _relative_for(logical_path)
            replacements[logical_path] = _read_relative_via_descriptors(view_fd, rel)
        files_after, dirs_after = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files_after, dirs_after, logical_paths)
        return replacements
    finally:
        os.close(view_fd)


def _baseline_for(
    store: TreeStore,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
) -> tuple[RevisionPathBaseline, ...]:
    items: list[RevisionPathBaseline] = []
    for logical_path in logical_paths:
        data = store.read_bytes(base_tree_id, logical_path)
        items.append(
            RevisionPathBaseline(
                logical_path=logical_path,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(items)


def _write_revision_files(
    *,
    change_dir: Path,
    store: TreeStore,
    base_tree_id: str,
    view_relpath: str,
    logical_paths: tuple[str, ...],
) -> None:
    view_root = change_dir / view_relpath
    if view_root.exists() or view_root.is_symlink():
        if view_root.is_symlink() or view_root.is_file():
            view_root.unlink()
        else:
            shutil.rmtree(view_root)
    view_root.mkdir(parents=True, exist_ok=False)
    for logical_path in logical_paths:
        rel = _relative_for(logical_path)
        target = view_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = store.read_bytes(base_tree_id, logical_path)
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)


def _write_or_validate_revision_view(
    *,
    change_dir: Path,
    store: TreeStore,
    interrupt_id: str,
    owner_invocation_id: str,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
    committed_binding: RevisionViewBinding | None,
) -> RevisionViewBinding:
    canonical_paths = _canonical_logical_paths(logical_paths)
    view_relpath = _view_relpath_for(interrupt_id)
    if committed_binding is None:
        _write_revision_files(
            change_dir=change_dir,
            store=store,
            base_tree_id=base_tree_id,
            view_relpath=view_relpath,
            logical_paths=canonical_paths,
        )
        return RevisionViewBinding(
            interrupt_id=interrupt_id,
            owner_invocation_id=owner_invocation_id,
            base_tree_id=base_tree_id,
            view_relpath=view_relpath,
            logical_paths=canonical_paths,
            baseline=_baseline_for(store, base_tree_id, canonical_paths),
        )

    expected = RevisionViewBinding(
        interrupt_id=interrupt_id,
        owner_invocation_id=owner_invocation_id,
        base_tree_id=base_tree_id,
        view_relpath=view_relpath,
        logical_paths=canonical_paths,
        baseline=_baseline_for(store, base_tree_id, canonical_paths),
    )
    if (
        committed_binding.interrupt_id != expected.interrupt_id
        or committed_binding.owner_invocation_id != expected.owner_invocation_id
        or committed_binding.base_tree_id != expected.base_tree_id
        or committed_binding.view_relpath != expected.view_relpath
        or committed_binding.logical_paths != expected.logical_paths
        or committed_binding.baseline != expected.baseline
    ):
        raise ManualRevisionError("committed revision view binding metadata mismatch")
    view_root = change_dir / committed_binding.view_relpath
    if not view_root.is_dir() or view_root.is_symlink():
        raise ManualRevisionError(f"committed revision view missing: {committed_binding.view_relpath}")
    # Preserve user edits; only confirm the path inventory still matches.
    view_fd = _open_view_root(change_dir, committed_binding.view_relpath)
    try:
        files, dirs = _inventory_revision_view(view_fd)
        _assert_inventory_matches(files, dirs, committed_binding.logical_paths)
    finally:
        os.close(view_fd)
    return committed_binding


def materialize_revision_view(
    *,
    change_dir: Path,
    store: TreeStore,
    interrupt_id: str,
    owner_invocation_id: str,
    base_tree_id: str,
    logical_paths: tuple[str, ...],
    committed_binding: RevisionViewBinding | None,
) -> RevisionViewBinding:
    """Materialize or validate the exact bounded transport view."""
    return _write_or_validate_revision_view(
        change_dir=change_dir,
        store=store,
        interrupt_id=interrupt_id,
        owner_invocation_id=owner_invocation_id,
        base_tree_id=base_tree_id,
        logical_paths=logical_paths,
        committed_binding=committed_binding,
    )


def capture_revision_candidate(
    *,
    change_dir: Path,
    store: TreeStore,
    binding: RevisionViewBinding,
) -> TreeFileRevision:
    """Validate the complete view inventory and publish exact replacements."""
    replacements = _read_validated_revision_files(change_dir, binding)
    return store.replace_tree_files(binding.base_tree_id, replacements)


def _anchor_ref(anchor: ResumeAnchor) -> str:
    return canonical_digest(anchor.model_dump(mode="json"))


def _resume_events_for(
    *,
    revision_transition_id: str,
    command_action: str,
    who: str,
    reason: str,
    audited_reads_sha256: Mapping[str, str],
    resume_anchors: tuple[ResumeAnchor, ...],
    source_gate_attempt_id: str | None,
    source_gate_tree_id: str | None,
) -> tuple[GraphResumedEvent, ...]:
    chain_length = len(resume_anchors)
    if chain_length < 1:
        raise ManualRevisionError("manual revision resume chain must be non-empty")
    resumes: list[GraphResumedEvent] = []
    parent_anchor_ref: str | None = None
    for ordinal, anchor in enumerate(resume_anchors):
        resumes.append(
            GraphResumedEvent(
                type="graph_resumed",
                invocation_id=anchor.invocation_id,
                checkpoint_ns=anchor.checkpoint_ns,
                interrupt_id=anchor.interrupt_id,
                action=command_action,
                reason=reason,
                who=who,
                audited_reads_sha256=dict(audited_reads_sha256) if ordinal == 0 else {},
                anchor=anchor,
                parent_anchor_ref=parent_anchor_ref,
                payload={},
                revision_transition_id=revision_transition_id,
                revision_ordinal=ordinal,
                revision_chain_length=chain_length,
                source_gate_attempt_id=source_gate_attempt_id,
                source_gate_tree_id=source_gate_tree_id,
            )
        )
        parent_anchor_ref = _anchor_ref(anchor)
    return tuple(resumes)


def build_manual_revision_transition(
    *,
    interrupted: GraphInterruptedEvent,
    command: ResumeCommand,
    revision: TreeFileRevision,
    pinned_definition_digests: Mapping[str, str],
    resume_anchors: tuple[ResumeAnchor, ...],
) -> ManualRevisionTransition:
    """Build a deterministic transition from committed inputs only (no FS paths)."""
    if command.action != "fix_and_proceed":
        raise ManualRevisionError("manual revision transition requires fix_and_proceed")
    if command.interrupt_id != interrupted.interrupt_id:
        raise ManualRevisionError("resume interrupt_id does not match interrupted event")
    if interrupted.revision_base_tree_id is None:
        raise ManualRevisionError("interrupted event lacks revision_base_tree_id")
    if interrupted.source_gate_attempt_id is None or interrupted.source_gate_tree_id is None:
        raise ManualRevisionError("manual revision requires a source gate evidence pair")
    if not resume_anchors:
        raise ManualRevisionError("manual revision resume anchors must be non-empty")
    if resume_anchors[-1].interrupt_id != interrupted.interrupt_id:
        raise ManualRevisionError("leaf resume anchor interrupt_id mismatch")

    logical_paths = [path.logical_path for path in revision.paths]
    before_sha256 = {path.logical_path: path.before_sha256 for path in revision.paths}
    after_sha256 = {path.logical_path: path.after_sha256 for path in revision.paths}
    identity = {
        "pinned_definition_digests": dict(sorted(pinned_definition_digests.items())),
        "interrupt_id": interrupted.interrupt_id,
        "action": command.action,
        "who": command.who,
        "reason": command.reason,
        "audited_reads_sha256": dict(sorted(interrupted.audited_reads_sha256.items())),
        "source_gate_attempt_id": interrupted.source_gate_attempt_id,
        "source_gate_tree_id": interrupted.source_gate_tree_id,
        "base_tree_id": interrupted.revision_base_tree_id,
        "target_tree_id": revision.target_tree_id,
        "logical_paths": logical_paths,
        "before_sha256": dict(sorted(before_sha256.items())),
        "after_sha256": dict(sorted(after_sha256.items())),
        "resume_anchors": [anchor.model_dump(mode="json") for anchor in resume_anchors],
    }
    revision_transition_id = canonical_digest(identity)
    owner_invocation_id = interrupted.revision_owner_invocation_id or interrupted.invocation_id
    owner_ns = next(
        (anchor.checkpoint_ns for anchor in resume_anchors if anchor.invocation_id == owner_invocation_id),
        interrupted.checkpoint_ns,
    )
    revision_event = ManualPlanRevisionEvent(
        type="manual_plan_revision",
        invocation_id=owner_invocation_id,
        checkpoint_ns=owner_ns,
        revision_transition_id=revision_transition_id,
        interrupt_id=interrupted.interrupt_id,
        action="fix_and_proceed",
        who=command.who,
        reason=command.reason,
        audited_reads_sha256=dict(interrupted.audited_reads_sha256),
        source_gate_attempt_id=interrupted.source_gate_attempt_id,
        source_gate_tree_id=interrupted.source_gate_tree_id,
        base_tree_id=interrupted.revision_base_tree_id,
        target_tree_id=revision.target_tree_id,
        logical_paths=logical_paths,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        resume_anchors=list(resume_anchors),
    )
    resumes = _resume_events_for(
        revision_transition_id=revision_transition_id,
        command_action=command.action,
        who=command.who,
        reason=command.reason,
        audited_reads_sha256=interrupted.audited_reads_sha256,
        resume_anchors=resume_anchors,
        source_gate_attempt_id=interrupted.source_gate_attempt_id,
        source_gate_tree_id=interrupted.source_gate_tree_id,
    )
    return ManualRevisionTransition(revision=revision_event, resumes=resumes)


def transition_from_committed_revision(revision: ManualPlanRevisionEvent) -> ManualRevisionTransition:
    """Reconstruct a transition solely from a committed manual_plan_revision event."""
    anchors = tuple(revision.resume_anchors)
    resumes = _resume_events_for(
        revision_transition_id=revision.revision_transition_id,
        command_action=revision.action,
        who=revision.who,
        reason=revision.reason,
        audited_reads_sha256=revision.audited_reads_sha256,
        resume_anchors=anchors,
        source_gate_attempt_id=revision.source_gate_attempt_id,
        source_gate_tree_id=revision.source_gate_tree_id,
    )
    return ManualRevisionTransition(revision=revision, resumes=resumes)


def _resume_payload(event: Mapping[str, object]) -> dict[str, object]:
    return {
        "revision_transition_id": event.get("revision_transition_id"),
        "revision_ordinal": event.get("revision_ordinal"),
        "revision_chain_length": event.get("revision_chain_length"),
        "invocation_id": event.get("invocation_id"),
        "checkpoint_ns": event.get("checkpoint_ns"),
        "interrupt_id": event.get("interrupt_id"),
        "action": event.get("action"),
        "reason": event.get("reason"),
        "who": event.get("who"),
        "audited_reads_sha256": event.get("audited_reads_sha256") or {},
        "anchor": event.get("anchor"),
        "parent_anchor_ref": event.get("parent_anchor_ref"),
        "source_gate_attempt_id": event.get("source_gate_attempt_id"),
        "source_gate_tree_id": event.get("source_gate_tree_id"),
    }


def validate_resume_prefix(
    events: Sequence[Mapping[str, object]],
    transition: ManualRevisionTransition,
) -> int:
    """Return the first missing resume ordinal or raise a prefix conflict."""
    expected = transition.resumes
    chain_length = len(expected)
    transition_id = transition.revision.revision_transition_id
    interrupt_id = transition.revision.interrupt_id
    observed: list[Mapping[str, object]] = []
    for event in events:
        if event.get("type") != "graph_resumed":
            continue
        event_transition = event.get("revision_transition_id")
        if event_transition is None:
            continue
        if event_transition != transition_id:
            if event.get("interrupt_id") == interrupt_id:
                raise ManualRevisionError(_PREFIX_CONFLICT)
            continue
        observed.append(event)

    if not observed:
        return 0

    seen_ordinals: set[int] = set()
    for index, event in enumerate(observed):
        ordinal = event.get("revision_ordinal")
        length = event.get("revision_chain_length")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ManualRevisionError(_PREFIX_CONFLICT)
        if length != chain_length:
            raise ManualRevisionError(_PREFIX_CONFLICT)
        if ordinal in seen_ordinals or ordinal != index or ordinal >= chain_length:
            raise ManualRevisionError(_PREFIX_CONFLICT)
        seen_ordinals.add(ordinal)
        expected_dump = expected[ordinal].model_dump(mode="json", exclude_none=True)
        if _resume_payload(event) != _resume_payload(expected_dump):
            raise ManualRevisionError(_PREFIX_CONFLICT)
    return len(observed)


def stage_missing_resume_suffix(
    *,
    txn: ProgressionTxn,
    transition: ManualRevisionTransition,
) -> int:
    """Validate the committed prefix and append only its missing suffix."""
    events = txn.read_events_strict()
    first_missing = validate_resume_prefix(events, transition)
    for resume in transition.resumes[first_missing:]:
        txn.append_strict(resume)
    return first_missing


def _graph_payload(event: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in event.items() if key not in {"seq", "ts"}}


def find_open_revision_transition(
    events: Sequence[Mapping[str, object]],
) -> ManualRevisionTransition | None:
    """Return the open committed revision whose resume chain is still incomplete."""
    open_transition: ManualRevisionTransition | None = None
    for event in events:
        if event.get("type") != "manual_plan_revision":
            continue
        revision = ManualPlanRevisionEvent.model_validate(_graph_payload(event))
        transition = transition_from_committed_revision(revision)
        missing = validate_resume_prefix(events, transition)
        if missing < len(transition.resumes):
            if open_transition is not None:
                raise ManualRevisionError(_PREFIX_CONFLICT)
            open_transition = transition
    return open_transition


def derive_revision_recovery_state(
    events: Sequence[Mapping[str, object]],
) -> Literal["revision_resume_recovery_pending"] | None:
    """Derive recovery_state from the strict global event stream."""
    try:
        open_transition = find_open_revision_transition(events)
    except ManualRevisionError:
        return "revision_resume_recovery_pending"
    if open_transition is None:
        return None
    return "revision_resume_recovery_pending"


def resolve_gate_evidence_epoch(
    *,
    events: Sequence[Mapping[str, object]],
    invocation_id: str,
    checkpoint: str,
    gate_ids: frozenset[str],
    checkpoint_gate_aliases: Mapping[str, str],
) -> GateEvidenceEpoch | None:
    """Resolve the successful gate attempt/tree pair for a checkpoint, if any."""
    gate_id = checkpoint if checkpoint in gate_ids else checkpoint_gate_aliases.get(checkpoint)
    if gate_id is None or gate_id not in gate_ids:
        return None

    current_tree_id: str | None = None
    latest_attempt_id: str | None = None
    latest_attempt_tree_id: str | None = None
    for event in events:
        if event.get("invocation_id") != invocation_id:
            continue
        event_type = event.get("type")
        if event_type == "graph_invocation_started":
            tree = event.get("root_tree_id")
            current_tree_id = tree if isinstance(tree, str) else current_tree_id
        elif event_type == "superstep_committed":
            tree = event.get("target_tree_id")
            current_tree_id = tree if isinstance(tree, str) else current_tree_id
        elif event_type == "manual_plan_revision":
            tree = event.get("target_tree_id")
            current_tree_id = tree if isinstance(tree, str) else current_tree_id
        elif event_type == "task_attempt_succeeded":
            report = event.get("gate_report")
            attempt_id = event.get("attempt_id")
            if (
                isinstance(report, Mapping)
                and report.get("gate_id") == gate_id
                and isinstance(attempt_id, str)
                and current_tree_id is not None
            ):
                # Bind the tree that was current when this gate attempt succeeded,
                # not a later superstep/manual-revision tree.
                latest_attempt_id = attempt_id
                latest_attempt_tree_id = current_tree_id
    if latest_attempt_id is None or latest_attempt_tree_id is None:
        return None
    return GateEvidenceEpoch(
        source_gate_attempt_id=latest_attempt_id,
        source_gate_tree_id=latest_attempt_tree_id,
    )


__all__ = [
    "GateEvidenceEpoch",
    "ManualRevisionError",
    "ManualRevisionTransition",
    "RevisionPathBaseline",
    "RevisionViewBinding",
    "build_manual_revision_transition",
    "capture_revision_candidate",
    "derive_revision_recovery_state",
    "find_open_revision_transition",
    "materialize_revision_view",
    "resolve_gate_evidence_epoch",
    "stage_missing_resume_suffix",
    "transition_from_committed_revision",
    "validate_resume_prefix",
]
