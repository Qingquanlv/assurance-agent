"""Environment-addressable settings for the generic QA test runtime."""

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
    qa_sqlite_file: str | None


def load_settings() -> QaSettings:
    return QaSettings(
        base_url=_url("AA_BASE_URL", "API_BASE_URL", "BASE_URL", default="http://127.0.0.1:9999"),
        frontend_url=_url("E2E_FRONTEND_URL", "FRONTEND_URL", default="http://127.0.0.1:3100"),
        qa_sqlite_file=qa_sqlite_file(),
    )


settings = load_settings()
