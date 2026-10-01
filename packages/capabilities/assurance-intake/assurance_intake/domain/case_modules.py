"""Derive locked case.yaml paths from the Explore impact inventory.

The operator no longer names case modules. Explore names them on each row
(`case_module`) or the kernel projects one from `affected_behavior`. One
requirement can imply several modules; each distinct module becomes one
`qa/cases/<module>/case.yaml`.
"""

from __future__ import annotations

import re

from assurance_intake.contracts.impact import AffectedBehaviorV1, ChangeImpactInventoryV1

_METHOD = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+", re.IGNORECASE)
_VERSION = re.compile(r"^v\d+$", re.IGNORECASE)
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def infer_case_delta_paths(inventory: ChangeImpactInventoryV1) -> tuple[str, ...]:
    """Return sorted exact `qa/cases/<module>/case.yaml` paths implied by inventory."""

    rows = inventory.actionable_rows()
    modules: set[str] = set()
    for row in rows:
        modules.add(row.case_module or module_from_behavior(row.affected_behavior))
    if not modules:
        raise ValueError("impact inventory does not imply any case module")
    return tuple(sorted(f"qa/cases/{module}/case.yaml" for module in modules))


def module_from_behavior(behavior: AffectedBehaviorV1) -> str:
    key = behavior.key.strip()
    if behavior.kind == "api":
        return _module_from_api_key(key)
    if behavior.kind == "data_constraint":
        return _module_from_constraint_key(key)
    return _slug(key)


def _module_from_api_key(key: str) -> str:
    path = _METHOD.sub("", key).strip()
    parts = [part for part in path.split("/") if part]
    if parts and parts[0].lower() == "api":
        parts = parts[1:]
    if parts and _VERSION.fullmatch(parts[0]):
        parts = parts[1:]
    if not parts:
        raise ValueError(f"cannot derive case module from API key: {key}")
    return _slug(parts[0])


def _module_from_constraint_key(key: str) -> str:
    parts = [part for part in key.split(".") if part]
    if parts and parts[0] == "entities" and len(parts) > 1:
        return _slug(parts[1])
    if not parts:
        raise ValueError(f"cannot derive case module from constraint key: {key}")
    return _slug(parts[0])


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().replace(" ", "-")).strip("-").lower()
    if _SLUG.fullmatch(cleaned) is None:
        raise ValueError(f"cannot derive case module from {value!r}")
    return cleaned


__all__ = ["infer_case_delta_paths", "module_from_behavior"]
