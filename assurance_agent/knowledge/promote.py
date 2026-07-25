"""Promotion orchestration for L2 → L1 merge (spec C5)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.change_location import resolve_change
from assurance_agent.exceptions import AaError
from assurance_agent.knowledge.io import load_l1_document, write_l1_document
from assurance_agent.knowledge.merge import PromoteConflict, collect_leaf_entries, merge_l2_into_l1
from assurance_agent.knowledge.validate import (
    L1_REL_PATH,
    _collect_change_proposals,
    _load_yaml,
    validate_proposal,
)

L1_PATH = L1_REL_PATH


class KnowledgePromoteError(AaError):
    pass


def l1_sha256(project_root: Path) -> str:
    """Return the SHA-256 digest of the current L1 data-knowledge document."""
    path = project_root / L1_PATH
    if not path.is_file():
        raise KnowledgePromoteError(f"{L1_PATH} not found under {project_root}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_l1_sha256(project_root: Path, expected: str) -> None:
    """Raise when the live L1 digest does not match ``expected``."""
    actual = l1_sha256(project_root)
    if actual != expected:
        raise KnowledgePromoteError(
            f"L1 digest mismatch: expected {expected}, found {actual}"
        )


def validate_improvement_knowledge_proposal(path: Path) -> None:
    """Validate an Improvement-exported L2 proposal via existing semantic rules."""
    result = validate_proposal(path, rel=path.name)
    if not result.ok:
        detail = "; ".join(result.errors)
        raise KnowledgePromoteError(f"invalid L2 proposal {path}: {detail}")


@dataclass(frozen=True)
class PromoteOutcome:
    changed: bool
    merged_keys: list[str]
    conflicts_path: Path | None
    conflicts: list[PromoteConflict]


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _apply_leaf(document: CommentedMap | dict[str, Any], dotted: str, leaf: dict[str, Any]) -> None:
    parts = dotted.split(".")
    cur: CommentedMap | dict[str, Any] = document
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = CommentedMap()
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = CommentedMap(leaf)


def _load_and_validate_proposal(path: Path, *, rel: str | None = None) -> dict[str, Any]:
    result = validate_proposal(path, rel=rel)
    if not result.ok:
        detail = "; ".join(result.errors)
        raise KnowledgePromoteError(f"invalid proposal {path}: {detail}")
    raw = _load_yaml(path)
    if not isinstance(raw, dict):
        raise KnowledgePromoteError(f"proposal must be a mapping: {path}")
    from assurance_agent.knowledge.merge import EXPORT_ENVELOPE_FIELDS

    for key in EXPORT_ENVELOPE_FIELDS:
        raw.pop(key, None)
    return raw


def _validate_l1_dict(l1: dict[str, Any]) -> None:
    DataKnowledge.model_validate(l1)


def _write_conflicts(path: Path, conflicts: list[PromoteConflict]) -> None:
    payload = [{"key": c.key, "l1_value": c.l1_value, "proposal_value": c.proposal_value} for c in conflicts]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def promote_knowledge(
    project_root: Path,
    *,
    change_id: str | None = None,
    proposal_path: Path | None = None,
    yes: bool = False,
    force: bool = False,
) -> PromoteOutcome:
    if (change_id is None) == (proposal_path is None):
        raise KnowledgePromoteError("exactly one of --change or --from is required")

    project_root = project_root.resolve()
    l1_path = project_root / L1_PATH
    if not l1_path.is_file():
        raise KnowledgePromoteError(f"{L1_PATH} not found under {project_root}")

    proposal_files: list[tuple[str, Path]] = []
    conflicts_path: Path

    if proposal_path is not None:
        proposal_path = proposal_path.resolve()
        rel = None
        try:
            rel = proposal_path.relative_to(project_root).as_posix()
        except ValueError:
            rel = proposal_path.name
        proposal_files.append((rel, proposal_path))
        conflicts_path = proposal_path.parent / "promote-conflicts.json"
    else:
        change_dir = resolve_change(project_root, change_id or "").path
        collected = _collect_change_proposals(change_dir)
        if not collected:
            raise KnowledgePromoteError("no proposal files found under plans/")
        proposal_files = collected
        conflicts_path = change_dir / "plans" / "promote-conflicts.json"

    l1_doc = load_l1_document(l1_path)
    if not isinstance(l1_doc, dict):
        raise KnowledgePromoteError(f"{L1_PATH} must contain a mapping at the top level")

    working = _to_plain(l1_doc)
    all_conflicts: list[PromoteConflict] = []
    merged_keys: list[str] = []
    changed = False

    for rel, path in proposal_files:
        proposal = _load_and_validate_proposal(path, rel=rel)
        result = merge_l2_into_l1(working, proposal, force=force)
        working = result.merged
        merged_keys.extend(result.merged_keys)
        all_conflicts.extend(result.conflicts)
        changed = changed or result.changed

    if all_conflicts and not force:
        _write_conflicts(conflicts_path, all_conflicts)
        raise KnowledgePromoteError(
            f"{len(all_conflicts)} conflict(s) written to {conflicts_path}; re-run with --force to override"
        )

    if conflicts_path.is_file() and not all_conflicts:
        conflicts_path.unlink()

    if not changed:
        return PromoteOutcome(
            changed=False,
            merged_keys=[],
            conflicts_path=None,
            conflicts=[],
        )

    if not yes:
        raise KnowledgePromoteError("promotion would modify L1; re-run with --yes to apply")

    merged_leaves = collect_leaf_entries(working)
    for key in merged_keys:
        _apply_leaf(l1_doc, key, merged_leaves[key])

    preview = _to_plain(l1_doc)
    _validate_l1_dict(preview)
    write_l1_document(l1_path, l1_doc)
    return PromoteOutcome(
        changed=True,
        merged_keys=merged_keys,
        conflicts_path=conflicts_path if all_conflicts else None,
        conflicts=all_conflicts,
    )
