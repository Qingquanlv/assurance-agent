"""Pure merge of a knowledge delta into an L1 mapping."""

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

_LEAF_ROOTS = ("accounts", "auth", "entities", "auth_matrix")


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
    leaves: dict[str, dict[str, Any]] = {}
    for root in _LEAF_ROOTS:
        block = data.get(root) or {}
        if isinstance(block, dict):
            for name, leaf in block.items():
                if isinstance(leaf, dict):
                    leaves[f"{root}.{name}"] = leaf
    capabilities = data.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        return leaves
    factories = capabilities.get("domain_factories") or {}
    if isinstance(factories, dict):
        for module, names in factories.items():
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
    cleanup = capabilities.get("cleanup") or {}
    if isinstance(cleanup, dict):
        for name, leaf in cleanup.items():
            if isinstance(leaf, dict):
                leaves[f"capabilities.cleanup.{name}"] = leaf
    return leaves


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _set_nested(root: dict[str, Any], dotted: str, value: dict[str, Any]) -> None:
    parts = dotted.split(".")
    cursor: dict[str, Any] = root
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = copy.deepcopy(value)


def merge_l2_into_l1(
    l1: dict[str, Any],
    proposal: dict[str, Any],
    *,
    force: bool = False,
) -> MergeResult:
    merged = copy.deepcopy(l1)
    l1_leaves = collect_leaf_entries(merged)
    proposal_leaves = collect_leaf_entries(strip_proposal_metadata(proposal))
    conflicts: list[PromoteConflict] = []
    merged_keys: list[str] = []
    for key, proposal_leaf in sorted(proposal_leaves.items()):
        proposal_leaf = _strip_none(proposal_leaf)
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
    return MergeResult(
        merged=merged,
        conflicts=conflicts,
        merged_keys=merged_keys,
        changed=bool(merged_keys),
    )
