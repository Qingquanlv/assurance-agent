"""TreeStore reads use the same bounded symlink namespace as materialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError


def _object_path(change_dir: Path, digest: str) -> Path:
    return change_dir / ".graph-runtime" / "objects" / digest[:2] / digest


def _write_object(change_dir: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = _object_path(change_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


def _write_tree(
    change_dir: Path,
    entries: list[tuple[str, str, bytes]],
) -> str:
    payload_entries: list[dict[str, object]] = []
    for path, kind, data in entries:
        payload_entries.append(
            {
                "executable": False,
                "kind": kind,
                "path": path,
                "sha256": _write_object(change_dir, data),
            }
        )
    raw = json.dumps(
        {
            "entries": sorted(payload_entries, key=lambda item: str(item["path"])),
            "kind": "tree",
            "roots": {"change": "qa/changes/CH-1", "project": ".", "repo": "."},
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _write_object(change_dir, raw)


def test_reads_follow_an_intermediate_directory_symlink_like_materialization(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    real_dir = project / ".aa" / "real-policy-dir"
    real_dir.mkdir(parents=True)
    payload = b'{"source":"captured"}\n'
    (real_dir / "policy.json").write_bytes(payload)
    (project / ".aa" / "policy-dir").symlink_to(real_dir.name, target_is_directory=True)
    store = TreeStore(change)
    tree_id = store.capture(project)

    materialized = tmp_path / "materialized"
    store.materialize(tree_id, materialized)

    assert store.read_bytes(tree_id, "project:.aa/policy-dir/policy.json") == payload
    assert store.read_json(tree_id, "project:.aa/policy-dir/policy.json").value == {"source": "captured"}
    assert (materialized / ".aa" / "policy-dir" / "policy.json").read_bytes() == payload


def test_terminal_directory_target_raises_is_a_directory_like_materialization(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    directory = project / ".aa" / "policy-dir"
    directory.mkdir(parents=True)
    (directory / "marker.txt").write_text("directory\n", encoding="utf-8")
    (project / ".aa" / "policy.yaml").symlink_to(directory.name, target_is_directory=True)
    store = TreeStore(change)
    tree_id = store.capture(project)
    materialized = tmp_path / "materialized"
    store.materialize(tree_id, materialized)

    with pytest.raises(IsADirectoryError):
        store.read_bytes(tree_id, "project:.aa/policy.yaml")
    with pytest.raises(IsADirectoryError):
        (materialized / ".aa" / "policy.yaml").read_bytes()


def test_symlink_cycle_fails_closed_with_a_domain_error(tmp_path: Path) -> None:
    change = tmp_path / "project" / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    tree_id = _write_tree(
        change,
        [
            (".aa/policy.yaml", "symlink", b"policy-a.yaml"),
            (".aa/policy-a.yaml", "symlink", b"policy-b.yaml"),
            (".aa/policy-b.yaml", "symlink", b"policy-a.yaml"),
        ],
    )

    with pytest.raises(WorkspaceError, match="cycle"):
        TreeStore(change).read_bytes(tree_id, "project:.aa/policy.yaml")


def test_revisiting_a_symlink_with_a_new_suffix_is_not_a_cycle(tmp_path: Path) -> None:
    change = tmp_path / "project" / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    tree_id = _write_tree(
        change,
        [
            ("a", "symlink", b"b"),
            ("b/x", "symlink", b"../a/y"),
            ("b/y", "file", b"resolved\n"),
        ],
    )

    assert TreeStore(change).read_bytes(tree_id, "project:a/x") == b"resolved\n"


def test_long_symlink_chain_stops_at_the_documented_hop_bound(tmp_path: Path) -> None:
    change = tmp_path / "project" / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    entries = [
        (f".aa/policy-{index}.yaml", "symlink", f"policy-{index + 1}.yaml".encode()) for index in range(50)
    ]
    entries.append((".aa/policy-50.yaml", "file", b"version: 1\n"))
    tree_id = _write_tree(change, entries)

    with pytest.raises(WorkspaceError, match="too many symlink hops"):
        TreeStore(change).read_bytes(tree_id, "project:.aa/policy-0.yaml")
