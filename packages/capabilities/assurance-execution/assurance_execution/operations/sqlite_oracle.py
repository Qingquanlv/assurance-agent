"""Independent read-only observations for the supported SQLite User binding."""

from __future__ import annotations

import sqlite3
import stat
import time
from pathlib import Path
from typing import Any


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
) -> dict[str, Any]:
    """Read committed User rows through a new read-only SQLite connection."""
    if timeout_s <= 0:
        return _failed("timeout", "oracle_budget_exhausted")
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
    deadline = time.monotonic() + timeout_s
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=timeout_s) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            rows = connection.execute(_USER_QUERY, (username, email)).fetchall()
    except sqlite3.OperationalError as error:
        message = str(error).lower()
        if "locked" in message or "busy" in message or "interrupted" in message:
            return _failed("timeout", "database_busy")
        return _failed("error", "database_query_failed")
    except sqlite3.DatabaseError:
        return _failed("error", "database_query_failed")
    try:
        after = path.stat()
    except OSError:
        return _failed("error", "database_file_changed")
    if _identity(before) != _identity(after):
        return _failed("error", "database_file_changed")
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
        "database_identity": _identity(after),
    }


def _strict_bool(value: object, field: str) -> bool | str:
    if type(value) is int and value in {0, 1}:
        return value == 1
    return f"invalid_boolean:{field}"


def _identity(details: Any) -> dict[str, int]:
    return {
        "device": int(details.st_dev),
        "inode": int(details.st_ino),
        "size": int(details.st_size),
        "mtime_ns": int(details.st_mtime_ns),
    }


def _failed(state: str, reason: str) -> dict[str, Any]:
    return {"state": state, "rows": [], "reason": reason, "database_identity": None}


__all__ = ["observe_user"]
