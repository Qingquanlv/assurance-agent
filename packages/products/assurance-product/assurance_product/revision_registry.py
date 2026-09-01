from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

from graph_engine.boot.graph_revision import GraphRevision
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import RuntimeKind


class RevisionRegistryError(ValueError):
    """Raised when a GraphRevision cannot be stored, replayed, or retired."""


class RevisionRegistry:
    def __init__(self, workspace: ChangeWorkspace) -> None:
        self._workspace = workspace
        self._root = workspace.paths.langgraph_leases / "revisions"
        self._bindings = self._root / "bindings"

    def remember(self, revision: GraphRevision) -> GraphRevision:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(revision.revision_id)
        encoded = canonical_json_bytes(cast(JSONValue, revision.model_dump(mode="json"))) + b"\n"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RevisionRegistryError("revision record must be a regular file")
            if path.read_bytes() != encoded:
                raise RevisionRegistryError("graph revision is immutable")
            return revision
        pending = path.with_name(f".{path.name}.pending")
        pending.write_bytes(encoded)
        os.replace(pending, path)
        return revision

    def get(self, revision_id: str) -> GraphRevision:
        path = self._path(revision_id)
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision record is missing")
        payload = json.loads(path.read_bytes())
        revision = GraphRevision(
            revision_id=payload["revision_id"],
            product_lock_digest=payload["product_lock_digest"],
            wheel_source_digests=payload["wheel_source_digests"],
            factory_symbols=tuple(payload["factory_symbols"]),
            state_schema_versions=payload["state_schema_versions"],
            langgraph_version=payload["langgraph_version"],
            checkpoint_contract_version=payload["checkpoint_contract_version"],
        )
        if revision.revision_id != revision.canonical_revision_id():
            raise RevisionRegistryError("stored graph revision is not canonical")
        return revision

    def bind(self, invocation_id: str, *, runtime: RuntimeKind, revision_id: str) -> None:
        self._bindings.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload: dict[str, str] = {
            "invocation_id": invocation_id,
            "runtime": runtime,
            "revision_id": revision_id,
        }
        encoded = canonical_json_bytes(cast(JSONValue, payload)) + b"\n"
        path = self._bindings / f"{invocation_id}.json"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RevisionRegistryError("revision binding must be a regular file")
            if path.read_bytes() != encoded:
                raise RevisionRegistryError("invocation revision binding is immutable")
            return
        pending = path.with_name(f".{path.name}.pending")
        pending.write_bytes(encoded)
        os.replace(pending, path)

    def revision_for(self, invocation_id: str) -> tuple[RuntimeKind, str]:
        path = self._bindings / f"{invocation_id}.json"
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision binding is missing")
        payload = json.loads(path.read_bytes())
        return cast(RuntimeKind, payload["runtime"]), str(payload["revision_id"])

    def active_counts(self) -> Mapping[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        if not self._bindings.is_dir():
            return MappingProxyType(counts)
        for path in self._bindings.iterdir():
            if path.name.startswith(".") or not path.name.endswith(".json"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            payload = json.loads(path.read_bytes())
            key = (str(payload["runtime"]), str(payload["revision_id"]))
            counts[key] = counts.get(key, 0) + 1
        return MappingProxyType(counts)

    def retire(self, revision_id: str) -> None:
        counts = self.active_counts()
        if any(revision == revision_id and count > 0 for (_, revision), count in counts.items()):
            raise RevisionRegistryError("cannot retire revision while a resumable Invocation exists")
        path = self._path(revision_id)
        if path.is_symlink() or not path.is_file():
            raise RevisionRegistryError("revision record is missing")
        path.unlink()

    def _path(self, revision_id: str) -> Path:
        return self._root / f"{revision_id}.json"


def assert_recorded_revision(
    workspace: ChangeWorkspace,
    invocation_id: str,
    current: GraphRevision,
) -> GraphRevision:
    registry = RevisionRegistry(workspace)
    runtime, revision_id = registry.revision_for(invocation_id)
    if runtime != "langgraph-v1":
        return current
    recorded = registry.get(revision_id)
    if recorded.revision_id == current.revision_id:
        return recorded
    raise RevisionRegistryError(
        "required artifact: "
        f"graph revision {recorded.revision_id} "
        f"product lock {recorded.product_lock_digest}"
    )


__all__ = [
    "RevisionRegistry",
    "RevisionRegistryError",
    "assert_recorded_revision",
]
