"""Run-owned department hierarchy for generated performance scenarios."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

_CREATE = "/api/v1/dept/create"
_DELETE = "/api/v1/dept/delete"
_LIST = "/api/v1/dept/list"
_MANIFEST = Path(__file__).resolve().parent / ".manifests" / "dept_seed.json"


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


def _nodes(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _named_node(value: Any, name: str) -> Mapping[str, Any]:
    matches = [node for node in _nodes(value) if node.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one department named {name!r}, found {len(matches)}")
    identifier = matches[0].get("id", matches[0].get("dept_id"))
    if not isinstance(identifier, (int, str)):
        raise RuntimeError(f"department {name!r} has no identifier")
    return matches[0]


def _response_data(response: Any, operation: str) -> list[Any]:
    if not 200 <= response.status_code < 300:
        response.failure(f"{operation} returned HTTP {response.status_code}")
        raise RuntimeError(f"{operation} failed")
    body = response.json()
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(body, Mapping) or body.get("code") != 200 or not isinstance(data, list):
        response.failure(f"{operation} returned an invalid success envelope")
        raise RuntimeError(f"{operation} returned an invalid success envelope")
    return data


def _create(
    client: Any,
    headers: dict[str, str],
    *,
    name: str,
    desc: str,
    order: int,
    parent_id: int | str,
) -> int | str:
    with client.post(
        _CREATE,
        json={"name": name, "desc": desc, "order": order, "parent_id": parent_id},
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
        data = _response_data(response, "department seed lookup")
        node = _named_node(data, name)
        identifier = node.get("id", node.get("dept_id"))
        if node.get("parent_id") != parent_id or node.get("order") != order:
            response.failure("seeded department fields did not match the request")
            raise RuntimeError("unable to validate performance department")
        response.success()
        assert isinstance(identifier, (int, str))
        return identifier


def _persist(seed: DeptSeed) -> None:
    _MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST.write_text(json.dumps(asdict(seed), sort_keys=True), encoding="utf-8")


def _validate_chain(client: Any, headers: dict[str, str], seed: DeptSeed) -> None:
    with client.get(
        _LIST,
        headers=headers,
        name="GET /api/v1/dept/list (setup validation)",
        catch_response=True,
    ) as response:
        data = _response_data(response, "department seed validation")
        root = _named_node(data, f"{seed.prefix}r")
        child = _named_node(data, f"{seed.prefix}c")
        grandchild = _named_node(data, f"{seed.prefix}g")
        expected = (
            (root, seed.root_id, 0, 0),
            (child, seed.child_id, seed.root_id, 1),
            (grandchild, seed.grandchild_id, seed.child_id, 2),
        )
        for node, identifier, parent_id, order in expected:
            actual_id = node.get("id", node.get("dept_id"))
            if actual_id != identifier or node.get("parent_id") != parent_id or node.get("order") != order:
                response.failure("department seed chain fields did not match")
                raise RuntimeError("unable to validate performance department chain")
        root_children = root.get("children")
        child_children = child.get("children")
        if not isinstance(root_children, list) or child not in root_children:
            response.failure("department seed child was not nested under root")
            raise RuntimeError("unable to validate performance department chain")
        if not isinstance(child_children, list) or grandchild not in child_children:
            response.failure("department seed grandchild was not nested under child")
            raise RuntimeError("unable to validate performance department chain")
        response.success()


def setup(client: Any, headers: dict[str, str]) -> DeptSeed:
    prefix = f"p{uuid4().hex[:8]}"
    seed = DeptSeed(None, None, None, prefix)
    try:
        root = _create(
            client,
            headers,
            name=f"{prefix}r",
            desc="performance root",
            order=0,
            parent_id=0,
        )
        seed = DeptSeed(root, None, None, prefix)
        _persist(seed)
        child = _create(
            client,
            headers,
            name=f"{prefix}c",
            desc="performance child",
            order=1,
            parent_id=root,
        )
        seed = DeptSeed(root, child, None, prefix)
        _persist(seed)
        grandchild = _create(
            client,
            headers,
            name=f"{prefix}g",
            desc="performance grandchild",
            order=2,
            parent_id=child,
        )
        seed = DeptSeed(root, child, grandchild, prefix)
        _persist(seed)
        _validate_chain(client, headers, seed)
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
