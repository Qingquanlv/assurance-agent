"""Unified runtime settings for API, E2E, Fuzz, and Performance tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _first_env(*names: str, default: str) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.rstrip("/"), f"env:{name}"
    return default.rstrip("/"), "project-default"


def qa_sqlite_file() -> str | None:
    value = os.getenv("QA_SQLITE_FILE")
    return value if value else None


def candidate_sqlite_paths() -> list[str]:
    """Candidate SQLite files: explicit QA_SQLITE_FILE then app TORTOISE_ORM default."""
    paths: list[str] = []
    explicit = qa_sqlite_file()
    if explicit:
        paths.append(str(Path(explicit).expanduser().resolve()))
    try:
        from app.settings import TORTOISE_ORM

        file_path = TORTOISE_ORM["connections"]["sqlite"]["credentials"]["file_path"]
        resolved = str(Path(file_path).expanduser().resolve())
        if resolved not in paths:
            paths.append(resolved)
    except (ImportError, KeyError, TypeError):
        pass
    return paths


@dataclass(frozen=True)
class QaSettings:
    base_url: str
    base_url_source: str
    frontend_url: str
    frontend_url_source: str
    admin_username: str
    admin_password: str
    user_prefix: str
    role_prefix: str
    dept_prefix: str
    menu_prefix: str
    api_prefix: str
    qa_sqlite_file: str | None


def load_settings() -> QaSettings:
    base_url, base_url_source = _first_env(
        "BASE_URL", "API_BASE_URL", default="http://127.0.0.1:9999"
    )
    frontend_url, frontend_url_source = _first_env(
        "E2E_FRONTEND_URL", "FRONTEND_URL", default="http://127.0.0.1:3100"
    )
    return QaSettings(
        base_url=base_url,
        base_url_source=base_url_source,
        frontend_url=frontend_url,
        frontend_url_source=frontend_url_source,
        admin_username=os.getenv("QA_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("QA_ADMIN_PASSWORD", "123456"),
        user_prefix=os.getenv("QA_USER_PREFIX", "qa-user"),
        role_prefix=os.getenv("QA_ROLE_PREFIX", "qa-role"),
        dept_prefix=os.getenv("QA_DEPT_PREFIX", "qa-dept"),
        menu_prefix=os.getenv("QA_MENU_PREFIX", "qa-menu"),
        api_prefix=os.getenv("QA_API_PREFIX", "qa-api"),
        qa_sqlite_file=qa_sqlite_file(),
    )


settings = load_settings()
