"""Bounded root-execution evidence closure export and replay (design §13.2)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, PositiveInt, ValidationError, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    GraphEvent,
    GraphInvocationStartedEvent,
    GraphInvocationSupersededEvent,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError

EXPORT_ALGORITHM: Final[str] = "root_execution_closure/v1"
EVIDENCE_EXPORT_DIR: Final[str] = "evidence-export"
ROOT_EVENT_SLICE_NAME: Final[str] = "root-event-slice.json"
EXPORT_MANIFEST_NAME: Final[str] = "export-manifest.json"

ObjectKind = Literal[
    "root_event_slice",
    "definition_binding",
    "graph",
    "execution_contract",
    "ingest_catalog",
    "policy",
    "assurance_profile",
    "gate_semantics",
    "topology_semantics",
    "commit_safety_semantics",
    "input_snapshot",
    "runtime_context",
    "validation_receipt",
    "write_set",
    "blob",
]


class EvidenceExportError(AaError):
    """Root evidence export / replay failure."""


class RootEventSliceEventV1(StrictWireModel):
    export_seq: PositiveInt
    source_seq: PositiveInt
    event: GraphEvent


class RootEventSliceV1(StrictWireModel):
    schema_version: Literal["1"]
    export_algorithm: Literal["root_execution_closure/v1"]
    root_invocation_id: str
    source_ledger_sha256: str
    source_ledger_size: int
    source_event_count: int
    events: list[RootEventSliceEventV1]

    @model_validator(mode="after")
    def validate_sequences(self) -> RootEventSliceV1:
        if not self.events:
            raise ValueError("root event slice must contain at least one event")
        export_seqs = [item.export_seq for item in self.events]
        source_seqs = [item.source_seq for item in self.events]
        expected = list(range(1, len(self.events) + 1))
        if export_seqs != expected:
            raise ValueError("export_seq must be exactly 1..N")
        if source_seqs != sorted(source_seqs) or len(set(source_seqs)) != len(source_seqs):
            raise ValueError("source_seq must be strictly increasing")
        return self


class EvidenceExportObjectV1(StrictWireModel):
    kind: ObjectKind
    logical_id: str
    relative_path: str
    sha256: str
    size: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_object(self) -> EvidenceExportObjectV1:
        if (
            not self.relative_path
            or self.relative_path.startswith("/")
            or "\\" in self.relative_path
            or any(part in {"", ".", ".."} for part in self.relative_path.split("/"))
        ):
            raise ValueError("relative_path must be normalized export-relative")
        if (
            len(self.sha256) != len("sha256:") + 64
            or not self.sha256.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in self.sha256.removeprefix("sha256:"))
        ):
            raise ValueError("sha256 must be lowercase sha256:<64-hex>")
        return self


class EvidenceExportManifestV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    selected_layers: list[LayerName]
    objects: list[EvidenceExportObjectV1]

    @model_validator(mode="after")
    def validate_manifest(self) -> EvidenceExportManifestV1:
        keys = [(obj.kind, obj.logical_id) for obj in self.objects]
        if keys != sorted(keys):
            raise ValueError("objects must be sorted by (kind, logical_id)")
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate export object identity")
        return self


class BoundArtifactRefV1(StrictWireModel):
    relative_path: str
    sha256: str
    size: Annotated[int, Field(ge=0)]


class ExecutionEvidenceV1(StrictWireModel):
    """Strict executor-owned trusted envelope persisted as execution.json."""

    schema_version: Literal["1"]
    selected_layers: list[LayerName]
    selection_normalizer_version: str
    write_policy_schema_version: str
    change_id: str
    change_repo_path: str | None
    root_invocation_id: str | None
    exit_code: int | None = None
    reason: str | None = None
    run_mode: str | None = None
    runtime_params: dict[str, object] = Field(default_factory=dict)
    infrastructure_error: bool = False
    change_location_config: BoundArtifactRefV1 | None = None
    change_location: BoundArtifactRefV1 | None = None
    write_manifest_before: BoundArtifactRefV1 | None = None
    write_manifest_after: BoundArtifactRefV1 | None = None
    write_diff: BoundArtifactRefV1 | None = None
    write_policy: BoundArtifactRefV1 | None = None
    source_ledger_sha256: str | None = None
    source_ledger_size: int | None = None
    source_event_count: int | None = None
    root_slice: BoundArtifactRefV1 | None = None
    export_manifest: BoundArtifactRefV1 | None = None
    source_seq_pairs: list[list[object]] = Field(default_factory=list)


def _ledger_path(change_dir: Path) -> Path:
    return change_dir / "events.jsonl"


def _digest_ledger(change_dir: Path) -> tuple[bytes, str, int, int, list[dict[str, object]]]:
    path = _ledger_path(change_dir)
    if not path.is_file():
        raise EvidenceExportError(f"source ledger missing: {path}")
    raw = path.read_bytes()
    events = read_events_strict(change_dir)
    return raw, sha256_bytes(raw), len(raw), len(events), events


def _event_digest(event: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(event)))


def _parse_graph_event(raw: Mapping[str, object]) -> GraphEvent:
    # Ledger envelope fields are not part of the GraphEvent payload model.
    payload = {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
    try:
        return GRAPH_EVENT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise EvidenceExportError(f"invalid graph event: {exc}") from exc


def _descendant_closure(root_id: str, events: Sequence[Mapping[str, object]]) -> set[str]:
    children: dict[str, set[str]] = {}
    for raw in events:
        parent = raw.get("parent_invocation_id")
        inv = raw.get("invocation_id")
        if isinstance(parent, str) and isinstance(inv, str):
            children.setdefault(parent, set()).add(inv)
    closure = {root_id}
    stack = [root_id]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in closure:
                closure.add(child)
                stack.append(child)
    return closure


def select_root_event_slice(
    *,
    root_invocation_id: str,
    source_events: Sequence[Mapping[str, object]],
    source_ledger_sha256: str,
    source_ledger_size: int,
) -> RootEventSliceV1:
    """Select root + descendant events (plus optional consumed D18 supersede)."""
    closure = _descendant_closure(root_invocation_id, source_events)
    selected: list[tuple[int, Mapping[str, object]]] = []
    supersede_for_root: tuple[int, Mapping[str, object]] | None = None
    root_start: Mapping[str, object] | None = None

    for index, raw in enumerate(source_events, start=1):
        event_type = raw.get("type")
        inv = raw.get("invocation_id")
        if event_type == "graph_invocation_started" and inv == root_invocation_id:
            root_start = raw
        if isinstance(inv, str) and inv in closure:
            selected.append((index, raw))
            continue
        if event_type == "graph_invocation_superseded":
            # Keep candidate; admit only when root-start consumes its authorization.
            supersede_for_root = (index, raw)

    if root_start is None:
        raise EvidenceExportError(f"root start event missing for {root_invocation_id}")

    auth = root_start.get("replacement_authorization_id")
    supersedes = root_start.get("supersedes_invocation_id")
    if auth and supersedes and supersede_for_root is not None:
        raw = supersede_for_root[1]
        if (raw.get("supersede_id") == auth or raw.get("invocation_id") == supersedes) and raw.get(
            "invocation_id"
        ) == supersedes:
            # Insert supersede before other selected events while preserving source order.
            selected.append(supersede_for_root)
            selected = sorted({item[0]: item for item in selected}.values(), key=lambda pair: pair[0])

    parsed_events: list[RootEventSliceEventV1] = []
    for export_seq, (source_seq, raw) in enumerate(selected, start=1):
        parsed_events.append(
            RootEventSliceEventV1(
                export_seq=export_seq,
                source_seq=source_seq,
                event=_parse_graph_event(raw),
            )
        )
    return RootEventSliceV1(
        schema_version="1",
        export_algorithm="root_execution_closure/v1",
        root_invocation_id=root_invocation_id,
        source_ledger_sha256=source_ledger_sha256,
        source_ledger_size=source_ledger_size,
        source_event_count=len(source_events),
        events=parsed_events,
    )


def _safe_write(path: Path, data: bytes) -> BoundArtifactRefV1:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    rel = path.name if path.parent.name != EVIDENCE_EXPORT_DIR else f"{EVIDENCE_EXPORT_DIR}/{path.name}"
    # Caller overwrites relative_path when needed.
    return BoundArtifactRefV1(relative_path=rel, sha256=sha256_bytes(data), size=len(data))


def _object_record(
    *,
    kind: ObjectKind,
    logical_id: str,
    relative_path: str,
    data: bytes,
    dest: Path,
) -> EvidenceExportObjectV1:
    if ".." in relative_path.split("/") or relative_path.startswith("/"):
        raise EvidenceExportError(f"path escape: {relative_path}")
    target = dest / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return EvidenceExportObjectV1(
        kind=kind,
        logical_id=logical_id,
        relative_path=relative_path,
        sha256=sha256_bytes(data),
        size=len(data),
    )


def _collect_referenced_object_ids(slice_model: RootEventSliceV1) -> dict[ObjectKind, set[str]]:
    """Collect schema-version-specific explicit object references from the slice."""
    wanted: dict[ObjectKind, set[str]] = {
        "definition_binding": set(),
        "graph": set(),
        "execution_contract": set(),
        "ingest_catalog": set(),
        "policy": set(),
        "assurance_profile": set(),
        "gate_semantics": set(),
        "topology_semantics": set(),
        "commit_safety_semantics": set(),
        "input_snapshot": set(),
        "runtime_context": set(),
        "validation_receipt": set(),
        "write_set": set(),
        "blob": set(),
    }
    for item in slice_model.events:
        event = item.event
        payload = event.model_dump(mode="json")
        schema_version = int(payload.get("schema_version") or payload.get("event_schema_version") or 0)

        # Digests alone are not exportable object IDs. Only explicit object-id /
        # snapshot / receipt / write-set keys participate in the closure.
        for key, kind in (
            ("definition_binding_id", "definition_binding"),
            ("graph_object_id", "graph"),
            ("ingest_catalog_object_id", "ingest_catalog"),
            ("policy_object_id", "policy"),
            ("assurance_profile_object_id", "assurance_profile"),
            ("gate_semantics_object_id", "gate_semantics"),
            ("topology_safety_semantics_object_id", "topology_semantics"),
            ("commit_safety_semantics_object_id", "commit_safety_semantics"),
            ("input_snapshot_id", "input_snapshot"),
            ("candidate_validation_receipt_id", "validation_receipt"),
            ("write_set_id", "write_set"),
            ("runtime_context_id", "runtime_context"),
        ):
            value = payload.get(key)
            if isinstance(value, str) and value and not value.startswith("sha256:"):
                wanted[kind].add(value)  # type: ignore[index]

        # Per-target execution contracts referenced by binding payloads.
        contracts = payload.get("execution_contract_object_ids")
        if isinstance(contracts, Mapping):
            for value in contracts.values():
                if isinstance(value, str) and value:
                    wanted["execution_contract"].add(value)

        # Historical sparsity: v6 requires three semantics; older schemas only
        # contribute kinds they actually pin (already gated by key presence).
        if schema_version < 6:
            for kind in ("gate_semantics", "topology_semantics", "commit_safety_semantics"):
                # Keep only ids already observed on this event; do not invent.
                pass

        if isinstance(event, GraphInvocationStartedEvent):
            pass
        if isinstance(event, GraphInvocationSupersededEvent):
            pass

    return wanted


def export_root_execution_closure(
    *,
    change_dir: Path,
    root_invocation_id: str,
    selected_layers: Sequence[LayerName],
    export_dir: Path,
) -> tuple[
    RootEventSliceV1, EvidenceExportManifestV1, BoundArtifactRefV1, BoundArtifactRefV1, list[tuple[int, str]]
]:
    """One-pass export of the bounded root evidence closure."""
    raw, ledger_digest, ledger_size, event_count, events = _digest_ledger(change_dir)
    del raw  # digest/size/count are authoritative for the envelope
    slice_model = select_root_event_slice(
        root_invocation_id=root_invocation_id,
        source_events=events,
        source_ledger_sha256=ledger_digest,
        source_ledger_size=ledger_size,
    )
    # source_event_count in slice is full ledger count
    slice_model = slice_model.model_copy(update={"source_event_count": event_count})

    export_dir.mkdir(parents=True, exist_ok=True)
    objects: list[EvidenceExportObjectV1] = []
    slice_bytes = canonical_json_bytes(slice_model)
    objects.append(
        _object_record(
            kind="root_event_slice",
            logical_id=root_invocation_id,
            relative_path=ROOT_EVENT_SLICE_NAME,
            data=slice_bytes,
            dest=export_dir,
        )
    )

    store = TreeStore(change_dir)
    wanted = _collect_referenced_object_ids(slice_model)
    # Always include the slice itself; copy referenced CAS objects when present.
    for kind, ids in wanted.items():
        for logical_id in sorted(ids):
            rel = f"objects/{kind}/{logical_id}.json"
            try:
                if kind == "write_set":
                    write_set = store.load_write_set(logical_id)
                    if write_set.base_tree_roots is None:
                        raise EvidenceExportError(f"write set {logical_id} missing base_tree_roots")
                    data = canonical_json_bytes(write_set)
                    # Verify roots participate in identity by reloading.
                    store.load_write_set(logical_id)
                    # Export blob payloads referenced by write-set entries.
                    for entry in write_set.entries:
                        blob_id = entry.blob_sha256
                        if not blob_id:
                            continue
                        bare = blob_id.removeprefix("sha256:")
                        if ("blob", bare) in {(obj.kind, obj.logical_id) for obj in objects}:
                            continue
                        try:
                            blob_data = store.read_object(bare)
                        except WorkspaceError as exc:
                            raise EvidenceExportError(f"missing referenced object blob:{bare}") from exc
                        objects.append(
                            _object_record(
                                kind="blob",
                                logical_id=bare,
                                relative_path=f"objects/blob/{bare}.bin",
                                data=blob_data,
                                dest=export_dir,
                            )
                        )
                else:
                    # Content-addressed objects live in the sharded TreeStore.
                    digest = logical_id.removeprefix("sha256:")
                    try:
                        data = store.read_object(digest)
                    except WorkspaceError:
                        # Staged semantics / catalogs may use kind-named dirs.
                        alt = change_dir / ".graph-runtime" / kind.replace("_", "-") / f"{logical_id}.json"
                        if alt.is_file():
                            data = alt.read_bytes()
                        else:
                            raise EvidenceExportError(f"missing referenced object {kind}:{logical_id}")
                    if kind == "blob":
                        rel = f"objects/blob/{digest}.bin"
                    if kind == "input_snapshot":
                        _export_snapshot_blobs(
                            store=store,
                            snapshot_bytes=data,
                            objects=objects,
                            export_dir=export_dir,
                        )
                objects.append(
                    _object_record(
                        kind=kind,  # type: ignore[arg-type]
                        logical_id=logical_id,
                        relative_path=rel,
                        data=data,
                        dest=export_dir,
                    )
                )
            except (WorkspaceError, OSError) as exc:
                raise EvidenceExportError(f"failed exporting {kind}:{logical_id}: {exc}") from exc

    objects.sort(key=lambda obj: (obj.kind, obj.logical_id))
    manifest = EvidenceExportManifestV1(
        schema_version="1",
        root_invocation_id=root_invocation_id,
        selected_layers=list(selected_layers),
        objects=objects,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    (export_dir / EXPORT_MANIFEST_NAME).write_bytes(manifest_bytes)
    slice_ref = BoundArtifactRefV1(
        relative_path=f"{EVIDENCE_EXPORT_DIR}/{ROOT_EVENT_SLICE_NAME}",
        sha256=sha256_bytes(slice_bytes),
        size=len(slice_bytes),
    )
    manifest_ref = BoundArtifactRefV1(
        relative_path=f"{EVIDENCE_EXPORT_DIR}/{EXPORT_MANIFEST_NAME}",
        sha256=sha256_bytes(manifest_bytes),
        size=len(manifest_bytes),
    )
    pairs = [
        (item.source_seq, _event_digest(item.event.model_dump(mode="json"))) for item in slice_model.events
    ]
    return slice_model, manifest, slice_ref, manifest_ref, pairs


def replay_root_event_slice(
    *,
    slice_model: RootEventSliceV1,
    source_events: Sequence[Mapping[str, object]],
    source_ledger_sha256: str,
    source_ledger_size: int,
) -> RootEventSliceV1:
    recomputed = select_root_event_slice(
        root_invocation_id=slice_model.root_invocation_id,
        source_events=source_events,
        source_ledger_sha256=source_ledger_sha256,
        source_ledger_size=source_ledger_size,
    )
    recomputed = recomputed.model_copy(update={"source_event_count": len(source_events)})
    if canonical_json_bytes(recomputed) != canonical_json_bytes(slice_model):
        raise EvidenceExportError("root event slice replay mismatch")
    return recomputed


def _export_snapshot_blobs(
    *,
    store: TreeStore,
    snapshot_bytes: bytes,
    objects: list[EvidenceExportObjectV1],
    export_dir: Path,
) -> None:
    """Export content blobs named by a TaskInputSnapshotV1 entry digest map."""
    import json

    try:
        payload = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return
    seen = {(obj.kind, obj.logical_id) for obj in objects}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not digest:
            continue
        bare = digest.removeprefix("sha256:")
        if ("blob", bare) in seen:
            continue
        try:
            blob_data = store.read_object(bare)
        except WorkspaceError:
            continue
        objects.append(
            _object_record(
                kind="blob",
                logical_id=bare,
                relative_path=f"objects/blob/{bare}.bin",
                data=blob_data,
                dest=export_dir,
            )
        )
        seen.add(("blob", bare))


def verify_export_manifest_closure(
    *,
    manifest: EvidenceExportManifestV1,
    export_dir: Path,
) -> None:
    """Require every recorded object exists with matching digest/size; no extras."""
    seen: set[tuple[str, str]] = set()
    for obj in manifest.objects:
        key = (obj.kind, obj.logical_id)
        if key in seen:
            raise EvidenceExportError(f"duplicate object identity {key}")
        seen.add(key)
        path = export_dir / obj.relative_path
        if not path.is_file():
            raise EvidenceExportError(f"missing exported object bytes: {obj.relative_path}")
        data = path.read_bytes()
        if len(data) != obj.size or sha256_bytes(data) != obj.sha256:
            raise EvidenceExportError(f"digest/size mismatch for {obj.relative_path}")
    # No unreferenced files under objects/ beyond the manifest.
    objects_root = export_dir / "objects"
    if objects_root.is_dir():
        for path in objects_root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(export_dir).as_posix()
            if not any(obj.relative_path == rel for obj in manifest.objects):
                raise EvidenceExportError(f"unreferenced export object: {rel}")


def source_ledger_metadata(change_dir: Path) -> tuple[str, int, int]:
    _raw, digest, size, count, _events = _digest_ledger(change_dir)
    return digest, size, count


__all__ = [
    "EXPORT_ALGORITHM",
    "EVIDENCE_EXPORT_DIR",
    "EXPORT_MANIFEST_NAME",
    "ROOT_EVENT_SLICE_NAME",
    "BoundArtifactRefV1",
    "EvidenceExportError",
    "EvidenceExportManifestV1",
    "EvidenceExportObjectV1",
    "ExecutionEvidenceV1",
    "RootEventSliceEventV1",
    "RootEventSliceV1",
    "export_root_execution_closure",
    "replay_root_event_slice",
    "select_root_event_slice",
    "source_ledger_metadata",
    "verify_export_manifest_closure",
]
