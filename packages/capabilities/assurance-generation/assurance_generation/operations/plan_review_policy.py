"""Validate-only plan-review routing: route plus finding_ids, no host rewrite."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SCOPE_RELATIVE = "qa/results/codegen/{family}/reviews/epochs/{epoch}/finding-scope.json"


def finding_scope_path(family: str, coverage_epoch: int) -> str:
    return _SCOPE_RELATIVE.format(family=family, epoch=coverage_epoch)


def apply_plan_review_policy(
    payload: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    del previous
    updated = dict(payload)
    updated.pop("public_outcome", None)
    return updated


def load_finding_scope(root: Path, *, family: str, coverage_epoch: int) -> dict[str, Any] | None:
    path = root.joinpath(*finding_scope_path(family, coverage_epoch).split("/"))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    route = payload.get("route") or payload.get("decision")
    raw_ids = payload.get("finding_ids")
    if not isinstance(route, str) or not isinstance(raw_ids, list):
        return None
    ids = [item for item in raw_ids if isinstance(item, str) and item]
    return {"route": route, "finding_ids": ids}


def expected_finding_scope(
    *,
    family: str,
    coverage_epoch: int,
    change_id: str,
    route: str,
    finding_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "change_id": change_id,
        "coverage_epoch": coverage_epoch,
        "family": family,
        "route": route,
        "finding_ids": list(finding_ids),
    }


def write_finding_scope(
    root: Path,
    *,
    family: str,
    coverage_epoch: int,
    change_id: str,
    route: str,
    finding_ids: Sequence[str],
) -> str:
    relative = finding_scope_path(family, coverage_epoch)
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            expected_finding_scope(
                family=family,
                coverage_epoch=coverage_epoch,
                change_id=change_id,
                route=route,
                finding_ids=finding_ids,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return relative


def authenticate_finding_scope(
    root: Path,
    *,
    family: str,
    coverage_epoch: int,
    change_id: str,
    route: str,
    finding_ids: Sequence[str],
) -> str:
    relative = finding_scope_path(family, coverage_epoch)
    path = root.joinpath(*relative.split("/"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid finding-scope: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("finding-scope must be a JSON object")
    expected = expected_finding_scope(
        family=family,
        coverage_epoch=coverage_epoch,
        change_id=change_id,
        route=route,
        finding_ids=finding_ids,
    )
    if payload != expected:
        raise ValueError("finding-scope.json does not match the locked review")
    return relative


__all__ = [
    "apply_plan_review_policy",
    "authenticate_finding_scope",
    "expected_finding_scope",
    "finding_scope_path",
    "load_finding_scope",
    "write_finding_scope",
]
