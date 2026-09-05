"""Run-owned department hierarchy for generated performance scenarios."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

_CREATE = "/api/v1/dept/create"
_DELETE = "/api/v1/dept/delete"
_LIST = "/api/v1/dept/list"
_MANIFEST = Path("/tmp/aa-perf-dept-seed.json")


@dataclass(frozen=True)
class DeptSeed:
    root_id: int | str | None
    child_id: int | str | None
    grandchild_id: int | str | None
    prefix: str

    def child_first(self) -> tuple[int | str, ...]:
        return tuple(
            value for value in (self.grandchild_id, self.child_id, self.root_id) if value is not None
        )


def _named_id(value: Any, name: str) -> int | str | None:
    if isinstance(value, Mapping):
        identifier = value.get("id", value.get("dept_id"))
        if value.get("name") == name and isinstance(identifier, (int, str)):
            return identifier
        for child in value.values():
            found = _named_id(child, name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _named_id(child, name)
            if found is not None:
                return found
    return None


def _create(client: Any, headers: dict[str, str], name: str, parent_id: int | str) -> int | str:
    with client.post(
        _CREATE,
        json={"name": name, "desc": "AA performance seed", "order": 0, "parent_id": parent_id},
        headers=headers,
        name="POST /api/v1/dept/create (setup)",
        catch_response=True,
    ) as response:
        if not 200 <= response.status_code < 300:
            response.failure(f"department seed creation returned HTTP {response.status_code}")
            raise RuntimeError("unable to seed performance department")
        response.success()
    with client.get(
        _LIST,
        headers=headers,
        name="GET /api/v1/dept/list (setup lookup)",
        catch_response=True,
    ) as response:
        body = response.json()
        identifier = _named_id(body.get("data", []) if isinstance(body, Mapping) else [], name)
        if identifier is None:
            response.failure("seeded department was absent from list")
            raise RuntimeError("unable to resolve performance department")
        response.success()
        return identifier


def _persist(seed: DeptSeed) -> None:
    _MANIFEST.write_text(json.dumps(asdict(seed), sort_keys=True), encoding="utf-8")


def setup(client: Any, headers: dict[str, str]) -> DeptSeed:
    prefix = f"perfdept{uuid4().hex[:8]}"
    seed = DeptSeed(None, None, None, prefix)
    try:
        root = _create(client, headers, f"{prefix}r", 0)
        seed = DeptSeed(root, None, None, prefix)
        _persist(seed)
        child = _create(client, headers, f"{prefix}c", root)
        seed = DeptSeed(root, child, None, prefix)
        _persist(seed)
        grandchild = _create(client, headers, f"{prefix}g", child)
        seed = DeptSeed(root, child, grandchild, prefix)
        _persist(seed)
        return seed
    except Exception:
        cleanup(client, headers, seed)
        raise


def _load() -> DeptSeed | None:
    try:
        value = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("prefix"), str):
        return None
    return DeptSeed(
        root_id=value.get("root_id"),
        child_id=value.get("child_id"),
        grandchild_id=value.get("grandchild_id"),
        prefix=value["prefix"],
    )


def cleanup(client: Any, headers: dict[str, str], seed: DeptSeed | None = None) -> None:
    owned = seed or _load()
    if owned is None:
        return
    failures: list[str] = []
    for identifier in owned.child_first():
        try:
            with client.delete(
                _DELETE,
                params={"dept_id": identifier},
                headers=headers,
                name="DELETE /api/v1/dept/delete (cleanup)",
                catch_response=True,
            ) as response:
                if 200 <= response.status_code < 300 or response.status_code == 404:
                    response.success()
                else:
                    failures.append(f"cleanup returned HTTP {response.status_code}")
        except Exception as error:
            failures.append(type(error).__name__)
    _MANIFEST.unlink(missing_ok=True)
    if failures:
        raise RuntimeError("; ".join(failures))
