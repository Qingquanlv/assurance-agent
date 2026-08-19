"""Checkpoint snapshot cache. Ledger remains the authority."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_kernel.artifacts.paths import WORKFLOW_STATE_REL
from assurance_kernel.workflow.core.events import read_events_strict
from assurance_kernel.workflow.core.progression import transaction
from assurance_kernel.workflow.graph.import_validation import (
    CheckpointImportError,
    ValidatedImport,
    parse_import_manifest,
    state_values_for_import,
    validate_import,
)
from assurance_kernel.workflow.graph.ledger_fold import (
    derive_graph_state,
    fold_invocation_events,
    latest_root_invocation_id,
    project_invocation,
    project_workflow_state,
    render_workflow_state_yaml,
)
from assurance_kernel.workflow.graph.models import GraphProjection

CHECKPOINT_DIR_RELPATH = ".graph-runtime/checkpoints"


def checkpoint_snapshot_relpath(projection: GraphProjection) -> str:
    name = projection.latest_checkpoint_id or f"bootstrap-{projection.invocation_id}"
    return f"{CHECKPOINT_DIR_RELPATH}/{name}.json"


def dump_checkpoint_snapshot(projection: GraphProjection) -> bytes:
    payload = json.dumps(
        projection.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (payload + "\n").encode("utf-8")


class CheckpointStore:
    """checkpoint snapshot 缓存：snapshot 仅是性能缓存，ledger 才是权威。"""

    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir

    @property
    def change_dir(self) -> Path:
        return self._change_dir

    def write(self, projection: GraphProjection) -> Path:
        rel = checkpoint_snapshot_relpath(projection)
        with transaction(self._change_dir) as txn:
            txn.write_file(rel, dump_checkpoint_snapshot(projection))
        return self._change_dir / rel

    def read_latest(self, invocation_id: str) -> GraphProjection:
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        return projection

    def project(self, invocation_id: str) -> GraphProjection:
        """ledger 权威投影，并修复落后/损坏的 checkpoint 与 workflow-state 缓存。"""
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        self._repair_workflow_state(projection)
        return projection

    def latest_root_invocation(self, entrypoint: str | None = None) -> str | None:
        """严格 ledger 中最近一次无 parent 的 root ``graph_invocation_started``。

        ``entrypoint`` 非空时只看该 entrypoint 的 invocation：同一 change 上
        standalone entrypoint（``archive``/``retro``）与主 ``full`` 图各自独立成
        invocation，不该互相当成「已在跑/已完成」。
        """
        return latest_root_invocation_id(read_events_strict(self._change_dir), entrypoint)

    def _repair_workflow_state(self, projection: GraphProjection) -> None:
        """缺失或损坏的 ``workflow-state.yaml`` 只能从 ledger 投影重建，绝不反向推断。"""
        path = self._change_dir / WORKFLOW_STATE_REL
        expected = render_workflow_state_yaml(projection)
        try:
            current = path.read_bytes()
        except OSError:
            current = b""
        if current == expected:
            return
        with transaction(self._change_dir) as txn:
            txn.set_workflow_state_projection(expected)

    def _snapshot_matches(self, projection: GraphProjection) -> bool:
        """仅当 snapshot 的 event_seq 与 digest 三元组和 ledger 投影一致时接受。"""
        path = self._change_dir / checkpoint_snapshot_relpath(projection)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        try:
            cached = GraphProjection.model_validate(raw)
        except ValidationError:
            return False
        return (
            cached.invocation_id == projection.invocation_id
            and cached.event_seq == projection.event_seq
            and cached.graph_digest == projection.graph_digest
            and cached.contract_digests == projection.contract_digests
            and cached.params == projection.params
        )


__all__ = [
    "CHECKPOINT_DIR_RELPATH",
    "CheckpointImportError",
    "CheckpointStore",
    "ValidatedImport",
    "checkpoint_snapshot_relpath",
    "derive_graph_state",
    "dump_checkpoint_snapshot",
    "fold_invocation_events",
    "latest_root_invocation_id",
    "parse_import_manifest",
    "project_invocation",
    "project_workflow_state",
    "render_workflow_state_yaml",
    "state_values_for_import",
    "validate_import",
]
