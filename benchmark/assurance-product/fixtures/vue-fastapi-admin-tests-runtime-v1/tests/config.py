"""Unified, environment-addressable test runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _url(*names: str, default: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return default.rstrip("/")


def qa_sqlite_file() -> str | None:
    value = os.environ.get("QA_SQLITE_FILE") or os.environ.get("AA_SQLITE_PATH")
    if not value:
        return None
    return str(Path(value.split(os.pathsep, 1)[0]).expanduser().resolve())


@dataclass(frozen=True)
class QaSettings:
    base_url: str
    frontend_url: str
    admin_username: str
    admin_password: str
    dept_prefix: str
    qa_sqlite_file: str | None


def load_settings() -> QaSettings:
    return QaSettings(
        base_url=_url("BASE_URL", "API_BASE_URL", "AA_BASE_URL", default="http://127.0.0.1:9999"),
        frontend_url=_url("E2E_FRONTEND_URL", "FRONTEND_URL", default="http://127.0.0.1:3100"),
        admin_username=os.environ.get("QA_ADMIN_USERNAME", "admin"),
        admin_password=os.environ.get("QA_ADMIN_PASSWORD", "123456"),
        dept_prefix=os.environ.get("QA_DEPT_PREFIX", "qa-dept"),
        qa_sqlite_file=qa_sqlite_file(),
    )


settings = load_settings()
