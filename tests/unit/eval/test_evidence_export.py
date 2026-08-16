"""Root event-slice and evidence-export closure tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.eval.evidence_export import (
    ExecutionEvidenceV1,
    RootEventSliceV1,
    export_root_execution_closure,
    replay_root_event_slice,
    select_root_event_slice,
    verify_export_manifest_closure,
)
from assurance_agent.workflow.core.events import append_event_strict
from tests.helpers_graph_v6 import v6_semantic_bindings


def _started(
    *,
    invocation_id: str,
    parent: str | None = None,
    schema: int = 6,
    supersedes: str | None = None,
    auth: str | None = None,
) -> dict:
    event = {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": invocation_id,
        "entrypoint": "execute",
        "graph_id": "g1",
        "graph_digest": "sha256:" + ("a" * 64),
        "event_schema_version": schema,
        "contract_digests": {"skill:x": "sha256:" + ("b" * 64)},
        "params": {},
        "params_sha256": "sha256:" + ("c" * 64),
        "root_tree_id": "tree-root",
        "max_parallel_tasks": 1,
        "checkpoint_ns": "ns",
        "structural_path": "/root",
        "parent_invocation_id": parent,
        "policy_digest": "sha256:" + ("d" * 64),
        "policy_origin": "packaged",
        "assurance_profile_digest": "sha256:" + ("f" * 64),
        **v6_semantic_bindings(),
    }
    if supersedes is not None:
        event["supersedes_invocation_id"] = supersedes
        event["replacement_authorization_id"] = auth
    return event


def _stage_v6_semantics(change_dir: Path) -> None:
    from assurance_agent.workflow.graph.definition_pinning import (
        commit_safety_semantics_snapshot_relpath,
        gate_semantics_snapshot_relpath,
        topology_semantics_snapshot_relpath,
    )
    from assurance_agent.workflow.graph.runtime_commit_safety import commit_safety_semantics_bytes
    from assurance_agent.workflow.graph.topology_semantics import topology_safety_semantics_bytes
    from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_bytes

    bindings = v6_semantic_bindings()
    for relpath_fn, object_id, data in (
        (gate_semantics_snapshot_relpath, bindings["gate_semantics_object_id"], gate_semantics_bytes()),
        (
            topology_semantics_snapshot_relpath,
            bindings["topology_safety_semantics_object_id"],
            topology_safety_semantics_bytes(),
        ),
        (
            commit_safety_semantics_snapshot_relpath,
            bindings["commit_safety_semantics_object_id"],
            commit_safety_semantics_bytes(),
        ),
    ):
        path = change_dir / relpath_fn(object_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _terminal(invocation_id: str) -> dict:
    return {
        "source": "graph",
        "type": "graph_completed",
        "invocation_id": invocation_id,
        "checkpoint_ns": "ns",
        "reason": "ok",
    }


def test_root_event_slice_interleaved_roots(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    # Two interleaved roots in one ledger.
    append_event_strict(change, _started(invocation_id="root-a"))
    append_event_strict(change, _started(invocation_id="root-b"))
    append_event_strict(change, _started(invocation_id="child-a", parent="root-a"))
    append_event_strict(change, _terminal("root-b"))
    append_event_strict(change, _terminal("child-a"))
    append_event_strict(change, _terminal("root-a"))

    from assurance_agent.workflow.core.events import read_events_strict

    events = read_events_strict(change)
    ledger = (change / "events.jsonl").read_bytes()
    slice_model = select_root_event_slice(
        root_invocation_id="root-a",
        source_events=events,
        source_ledger_sha256=sha256_bytes(ledger),
        source_ledger_size=len(ledger),
    )
    assert [item.export_seq for item in slice_model.events] == list(range(1, len(slice_model.events) + 1))
    source_seqs = [item.source_seq for item in slice_model.events]
    assert source_seqs == sorted(source_seqs)
    assert source_seqs != list(range(1, len(events) + 1))  # gaps permitted
    invs = {item.event.invocation_id for item in slice_model.events}  # type: ignore[attr-defined]
    assert invs == {"root-a", "child-a"}
    assert "root-b" not in invs
    replay_root_event_slice(
        slice_model=slice_model,
        source_events=events,
        source_ledger_sha256=sha256_bytes(ledger),
        source_ledger_size=len(ledger),
    )


def test_export_rejects_unrelated_sentinel_object(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    _stage_v6_semantics(change)
    append_event_strict(change, _started(invocation_id="root-a"))
    append_event_strict(change, _terminal("root-a"))
    # Plant an unrelated object in the store that must not be copied.
    sentinel = change / ".graph-runtime" / "objects" / "zz" / ("f" * 64)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("sentinel\n", encoding="utf-8")

    export_dir = tmp_path / "export"
    _slice, manifest, *_rest = export_root_execution_closure(
        change_dir=change,
        root_invocation_id="root-a",
        selected_layers=("api",),
        export_dir=export_dir,
    )
    assert all(obj.logical_id != ("f" * 64) for obj in manifest.objects)
    verify_export_manifest_closure(manifest=manifest, export_dir=export_dir)


def test_execution_evidence_rejects_extra_fields() -> None:
    with pytest.raises(Exception):
        ExecutionEvidenceV1.model_validate(
            {
                "schema_version": "1",
                "selected_layers": ["api"],
                "selection_normalizer_version": "selection_normalizer/v1",
                "write_policy_schema_version": "write_policy/v1",
                "change_id": "CH-1",
                "change_repo_path": "qa/changes/CH-1",
                "root_invocation_id": None,
                "extra_field": True,
            }
        )


def test_slice_sequence_corruption_fails(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    append_event_strict(change, _started(invocation_id="root-a"))
    append_event_strict(change, _terminal("root-a"))
    from assurance_agent.workflow.core.events import read_events_strict

    events = read_events_strict(change)
    ledger = (change / "events.jsonl").read_bytes()
    slice_model = select_root_event_slice(
        root_invocation_id="root-a",
        source_events=events,
        source_ledger_sha256=sha256_bytes(ledger),
        source_ledger_size=len(ledger),
    )
    corrupted = slice_model.model_copy(
        update={
            "events": list(reversed(slice_model.events)),
        }
    )
    with pytest.raises(Exception):
        RootEventSliceV1.model_validate(corrupted.model_dump(mode="json"))
