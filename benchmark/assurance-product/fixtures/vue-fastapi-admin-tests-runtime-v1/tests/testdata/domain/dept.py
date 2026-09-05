"""Department data lifecycle helpers against the managed backend."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from tests.config import settings


def _backend_url() -> str:
    return settings.base_url


def _login_token() -> str:
    configured = os.environ.get("E2E_API_TOKEN") or os.environ.get("API_ADMIN_TOKEN")
    if configured:
        return configured
    request = Request(
        f"{_backend_url()}/api/v1/base/access_token",
        data=json.dumps({"username": settings.admin_username, "password": settings.admin_password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        body = json.load(response)
    token = body.get("data", {}).get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("admin login response did not contain data.access_token")
    return token


def _request(method: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
    request = Request(
        f"{_backend_url()}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"token": _login_token(), "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode()
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"department fixture request failed: {error.code} {detail}") from error
    body = json.loads(raw) if raw else {}
    if isinstance(body, dict) and body.get("code") not in (None, 200):
        raise RuntimeError(f"department fixture request was rejected: {body}")
    return body.get("data") if isinstance(body, dict) else body


def unique_dept_name() -> str:
    return f"QAD{uuid4().hex[:12]}"


def _find(value: Any, name: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get("name") == name and value.get("id", value.get("dept_id")) is not None:
            return value
        for child in value.values():
            found = _find(child, name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find(child, name)
            if found is not None:
                return found
    return None


async def make_dept(
    *, name: str | None = None, description: str = "QA department", order: int = 100
) -> dict[str, Any]:
    dept_name = name or unique_dept_name()
    await asyncio.to_thread(
        _request,
        "POST",
        "/api/v1/dept/create",
        payload={"name": dept_name, "desc": description, "order": order, "parent_id": 0},
    )
    listing = await asyncio.to_thread(
        _request,
        "GET",
        f"/api/v1/dept/list?{urlencode({'name': dept_name})}",
    )
    record = _find(listing, dept_name)
    if record is None:
        raise RuntimeError(f"created department {dept_name!r} was not found")
    return {**record, "id": record.get("id", record.get("dept_id"))}


async def cleanup_dept(dept: int | str | dict[str, Any]) -> None:
    identifier = dept.get("id", dept.get("dept_id")) if isinstance(dept, dict) else dept
    if identifier is None:
        return
    try:
        await asyncio.to_thread(
            _request,
            "DELETE",
            f"/api/v1/dept/delete?{urlencode({'dept_id': identifier})}",
        )
    except RuntimeError as error:
        if "404" not in str(error):
            raise


def get_dept_closure_rows(dept_id: int | str | None = None) -> list[dict[str, int]]:
    database = settings.qa_sqlite_file
    if database is None:
        raise RuntimeError("QA_SQLITE_FILE or AA_SQLITE_PATH is required for closure-row reads")
    query = "SELECT ancestor, descendant, level FROM deptclosure"
    parameters: tuple[int | str, ...] = ()
    if dept_id is not None:
        query += " WHERE ancestor = ? OR descendant = ?"
        parameters = (dept_id, dept_id)
    query += " ORDER BY ancestor, descendant, level"
    with sqlite3.connect(Path(database)) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [
        {"ancestor": int(ancestor), "descendant": int(descendant), "level": int(level)}
        for ancestor, descendant, level in rows
    ]
