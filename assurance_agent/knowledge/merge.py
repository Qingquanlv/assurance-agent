"""Pure merge helpers for L2 proposal → L1 promotion (spec C5)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

PROPOSAL_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "based_on_l1_version",
        "mode",
        "discovered_candidates",
        "needs_review",
        "promotion_checklist",
    }
)

EXPORT_ENVELOPE_FIELDS = frozenset(
    {
        "source_proposal_id",
        "source_sha256",
        "exported_at",
    }
)


@dataclass(frozen=True)
class PromoteConflict:
    key: str
    l1_value: Any
    proposal_value: Any


@dataclass(frozen=True)
class MergeResult:
    merged: dict[str, Any]
    conflicts: list[PromoteConflict]
    merged_keys: list[str]
    changed: bool


def strip_proposal_metadata(proposal: dict[str, Any]) -> dict[str, Any]:
    skip = PROPOSAL_METADATA_FIELDS | EXPORT_ENVELOPE_FIELDS
    return {key: copy.deepcopy(value) for key, value in proposal.items() if key not in skip}


def collect_leaf_entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect dotted leaf paths → leaf mapping values."""
    leaves: dict[str, dict[str, Any]] = {}

    for name, leaf in (data.get("accounts") or {}).items():
        if isinstance(leaf, dict):
            leaves[f"accounts.{name}"] = leaf

    for name, leaf in (data.get("auth") or {}).items():
        if isinstance(leaf, dict):
            leaves[f"auth.{name}"] = leaf

    for name, leaf in (data.get("entities") or {}).items():
        if isinstance(leaf, dict):
            leaves[f"entities.{name}"] = leaf

    for name, leaf in (data.get("auth_matrix") or {}).items():
        if isinstance(leaf, dict):
            leaves[f"auth_matrix.{name}"] = leaf

    capabilities = data.get("capabilities") or {}
    for module, names in (capabilities.get("domain_factories") or {}).items():
        if not isinstance(names, dict):
            continue
        for name, leaf in names.items():
            if isinstance(leaf, dict):
                leaves[f"capabilities.domain_factories.{module}.{name}"] = leaf

    adapters = capabilities.get("adapters") or {}
    if isinstance(adapters, dict):
        for layer, modules in adapters.items():
            if not isinstance(modules, dict):
                continue
            for module, names in modules.items():
                if not isinstance(names, dict):
                    continue
                for name, leaf in names.items():
                    if isinstance(leaf, dict):
                        leaves[f"capabilities.adapters.{layer}.{module}.{name}"] = leaf

    for name, leaf in (capabilities.get("cleanup") or {}).items():
        if isinstance(leaf, dict):
            leaves[f"capabilities.cleanup.{name}"] = leaf

    return leaves


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _set_nested(root: dict[str, Any], dotted: str, value: dict[str, Any]) -> None:
    parts = dotted.split(".")
    cur: dict[str, Any] = root
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = copy.deepcopy(value)


def merge_l2_into_l1(l1: dict[str, Any], proposal: dict[str, Any], *, force: bool = False) -> MergeResult:
    """Merge stripped L2 content into an L1 dict by dotted leaf key."""
    merged = copy.deepcopy(l1)
    l1_leaves = collect_leaf_entries(merged)
    proposal_leaves = collect_leaf_entries(strip_proposal_metadata(proposal))

    conflicts: list[PromoteConflict] = []
    merged_keys: list[str] = []

    for key, proposal_leaf in sorted(proposal_leaves.items()):
        existing = l1_leaves.get(key)
        if existing is None:
            _set_nested(merged, key, proposal_leaf)
            merged_keys.append(key)
            l1_leaves[key] = proposal_leaf
            continue
        if _canonical_json(existing) == _canonical_json(proposal_leaf):
            continue
        if force:
            _set_nested(merged, key, proposal_leaf)
            merged_keys.append(key)
            l1_leaves[key] = proposal_leaf
        else:
            conflicts.append(PromoteConflict(key=key, l1_value=existing, proposal_value=proposal_leaf))

    changed = bool(merged_keys)
    return MergeResult(merged=merged, conflicts=conflicts, merged_keys=merged_keys, changed=changed)
