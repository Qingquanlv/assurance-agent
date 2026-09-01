from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from graph_engine.boot.graph_revision import GraphRevision
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_product.change_workspace import ChangeWorkspace


class RevisionRegistryError(ValueError):
    """Raised when a GraphRevision cannot be stored or replayed."""


class RevisionRegistry:
    def __init__(self, workspace: ChangeWorkspace) -> None:
        self._root = workspace.paths.langgraph_leases / "revisions"

    def remember(self, revision: GraphRevision) -> GraphRevision:
        self._root.mkdir(mode=0o700, exist_ok=True)
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

    def _path(self, revision_id: str) -> Path:
        return self._root / f"{revision_id}.json"


__all__ = ["RevisionRegistry", "RevisionRegistryError"]
