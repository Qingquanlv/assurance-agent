"""E2E sync-safe bridge to shared dept domain factories (subprocess transport)."""

from __future__ import annotations

from typing import Any

import httpx

from tests.api.adapters.isolated_worker import run_isolated
from tests.helpers.dept_assertions import find_dept_in_tree
from tests.testdata.domain.dept import CLEANUP_DEPT, MAKE_DEPT


def make_dept(**kwargs):
    return run_isolated(MAKE_DEPT, **kwargs)


def cleanup_dept(dept_id: int):
    return run_isolated(CLEANUP_DEPT, dept_id=dept_id)


def _resolve_dept_id_by_name(
    client: httpx.Client, headers: dict[str, str], name: str
) -> int | None:
    resp = client.get("/api/v1/dept/list", headers=headers, params={"name": name})
    if resp.status_code != 200:
        raise RuntimeError(f"dept list failed: HTTP {resp.status_code} {resp.text}")
    item = find_dept_in_tree(resp.json().get("data") or [], name=name)
    return item["id"] if item else None


def e2e_cleanup_dept(
    client: httpx.Client, headers: dict[str, str], dept_id: int
) -> dict[str, Any]:
    resp = client.delete(f"/api/v1/dept/delete?dept_id={dept_id}", headers=headers)
    if resp.status_code >= 500:
        raise RuntimeError(f"dept delete failed: HTTP {resp.status_code} {resp.text}")
    body = resp.json()
    return {
        "dept_id": dept_id,
        "deleted": resp.status_code == 200 and body.get("code") == 200,
    }


def e2e_cleanup_dept_by_name(
    client: httpx.Client, headers: dict[str, str], name: str
) -> dict[str, Any]:
    dept_id = _resolve_dept_id_by_name(client, headers, name)
    if dept_id is None:
        return {"name": name, "deleted": False}
    return e2e_cleanup_dept(client, headers, dept_id)
