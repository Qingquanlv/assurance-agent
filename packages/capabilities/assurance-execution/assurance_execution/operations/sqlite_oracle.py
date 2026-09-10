"""Independent read-only observations for the supported SQLite User binding."""

from __future__ import annotations

import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from assurance_execution.contracts.verification import SqliteFileIdentityV1


_USER_QUERY = """\
SELECT username, email, is_active, is_superuser, dept_id
FROM "user"
WHERE username = ? OR email = ?
LIMIT 2
"""


def observe_user(
    db_file: Path,
    username: str,
    email: str,
    timeout_s: float = 2.0,
    *,
    expected_identity: SqliteFileIdentityV1 | None = None,
) -> dict[str, Any]:
    """Read committed User rows through a new read-only SQLite connection."""
    deadline = time.monotonic() + timeout_s
    if isinstance(timeout_s, bool) or not 0 < timeout_s <= 10:
        raise ValueError("oracle timeout must be greater than zero and at most 10 seconds")
    supplied = Path(db_file)
    if supplied.is_symlink():
        return _failed("error", "database_path_is_symlink")
    try:
        path = supplied.resolve(strict=True)
        before = path.stat()
    except FileNotFoundError:
        return _failed("error", "database_not_found")
    except OSError:
        return _failed("error", "database_unreadable")
    if not stat.S_ISREG(before.st_mode):
        return _failed("error", "database_not_regular")
    before_identity = _identity(path, before)
    if expected_identity is not None and before_identity != expected_identity.model_dump(mode="json"):
        return _failed("error", "database_identity_mismatch")
    remaining = _remaining(deadline)
    if remaining is None:
        return _failed("timeout", "oracle_budget_exhausted")
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=remaining) as connection:
            remaining = _remaining(deadline)
            if remaining is None:
                return _failed("timeout", "oracle_budget_exhausted")
            try:
                opened = path.stat()
            except OSError:
                return _failed("error", "database_file_changed")
            if _remaining(deadline) is None:
                return _failed("timeout", "oracle_budget_exhausted")
            opened_identity = _identity(path, opened)
            if opened_identity != before_identity:
                return _failed("error", "database_file_changed")
            if expected_identity is not None and opened_identity != expected_identity.model_dump(mode="json"):
                return _failed("error", "database_identity_mismatch")
            connection.execute("PRAGMA query_only = ON")
            connection.execute(f"PRAGMA busy_timeout = {max(1, int(remaining * 1000))}")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            rows = _oracle_select(connection, username, email)
    except sqlite3.OperationalError as error:
        message = str(error).lower()
        if "locked" in message or "busy" in message or "interrupted" in message:
            return _failed("timeout", "database_busy")
        return _failed("error", "database_query_failed")
    except sqlite3.DatabaseError:
        return _failed("error", "database_query_failed")
    if _remaining(deadline) is None:
        return _failed("timeout", "oracle_budget_exhausted")
    try:
        after = path.stat()
    except OSError:
        return _failed("error", "database_file_changed")
    after_identity = _identity(path, after)
    if before_identity != after_identity:
        return _failed("error", "database_file_changed")
    if expected_identity is not None and after_identity != expected_identity.model_dump(mode="json"):
        return _failed("error", "database_identity_mismatch")
    if _remaining(deadline) is None:
        return _failed("timeout", "oracle_budget_exhausted")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        active = _strict_bool(row[2], "is_active")
        if isinstance(active, str):
            return _failed("error", active)
        superuser = _strict_bool(row[3], "is_superuser")
        if isinstance(superuser, str):
            return _failed("error", superuser)
        normalized.append(
            {
                "username": row[0],
                "email": row[1],
                "is_active": active,
                "is_superuser": superuser,
                "dept_id": row[4],
            }
        )
    return {
        "state": "observed",
        "rows": normalized,
        "reason": None,
        "database_identity": after_identity,
        "database_metadata": _metadata(after),
    }


def _oracle_tracer():
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": "oracle"}))
    return provider.get_tracer("assurance.execution.oracle")


def _oracle_select(connection: sqlite3.Connection, username: str, email: str) -> list[tuple[Any, ...]]:
    try:
        tracer = _oracle_tracer()
    except (ImportError, AttributeError):
        return connection.execute(_USER_QUERY, (username, email)).fetchall()
    with tracer.start_as_current_span("oracle.user.select") as span:
        span.set_attribute("aa.role", "oracle")
        span.set_attribute("aa.db.table", "user")
        span.set_attribute("aa.db.operation", "SELECT")
        return connection.execute(_USER_QUERY, (username, email)).fetchall()


def _strict_bool(value: object, field: str) -> bool | str:
    if type(value) is int and value in {0, 1}:
        return value == 1
    return f"invalid_boolean:{field}"


def _identity(path: Path, details: Any) -> dict[str, str | int]:
    return {
        "path": str(path),
        "device": int(details.st_dev),
        "inode": int(details.st_ino),
    }


def _metadata(details: Any) -> dict[str, int]:
    return {
        "size": int(details.st_size),
        "mtime_ns": int(details.st_mtime_ns),
    }


def _remaining(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    return remaining if remaining > 0 else None


def _failed(state: str, reason: str) -> dict[str, Any]:
    return {
        "state": state,
        "rows": [],
        "reason": reason,
        "database_identity": None,
        "database_metadata": None,
    }


__all__ = ["observe_user"]
