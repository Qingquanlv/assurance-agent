"""Department lifecycle bridge for generated fuzz tests."""

from __future__ import annotations

from typing import Any

_CREATE = "/api/v1/dept/create"
_DELETE = "/api/v1/dept/delete"
_LIST = "/api/v1/dept/list"


def fuzz_factory_make_dept(client: Any, headers: dict[str, str], name: str) -> Any | None:
    response = client.post(
        _CREATE,
        headers=headers,
        json={"name": name, "desc": "AA fuzz seed", "order": 0, "parent_id": 0},
    )
    assert response.status_code < 500
    listing = client.get(_LIST, headers=headers, params={"name": name})
    assert listing.status_code < 500
    body = listing.json()
    return _named_id(body.get("data", []), name) if isinstance(body, dict) else None


def fuzz_factory_cleanup_dept(client: Any, headers: dict[str, str], dept_id: Any) -> None:
    response = client.delete(_DELETE, headers=headers, params={"dept_id": dept_id})
    assert response.status_code < 500


def _named_id(value: Any, name: str) -> Any | None:
    if isinstance(value, dict):
        if value.get("name") == name:
            return value.get("id", value.get("dept_id"))
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


def _prefixed_ids(value: Any, prefix: str) -> list[Any]:
    if isinstance(value, list):
        return [identifier for child in value for identifier in _prefixed_ids(child, prefix)]
    if not isinstance(value, dict):
        return []
    identifiers = _prefixed_ids(value.get("children", []), prefix)
    identifier = value.get("id", value.get("dept_id"))
    if isinstance(value.get("name"), str) and value["name"].startswith(prefix) and identifier is not None:
        identifiers.append(identifier)
    return identifiers


def fuzz_cleanup_depts_by_prefix(client: Any, headers: dict[str, str], prefix: str) -> None:
    listing = client.get(_LIST, headers=headers)
    assert listing.status_code < 500
    body = listing.json()
    tree = body.get("data", []) if isinstance(body, dict) else []
    for identifier in _prefixed_ids(tree, prefix):
        response = client.delete(_DELETE, headers=headers, params={"dept_id": identifier})
        assert response.status_code < 500
