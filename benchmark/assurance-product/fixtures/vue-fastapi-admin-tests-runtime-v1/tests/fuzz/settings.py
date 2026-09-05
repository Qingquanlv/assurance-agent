"""Environment-only fuzz runtime settings."""

from __future__ import annotations

import os


def base_url() -> str:
    return (
        os.environ.get("AA_BASE_URL") or os.environ.get("API_BASE_URL") or "http://127.0.0.1:9999"
    ).rstrip("/")


def admin_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AA_ADMIN_USERNAME", "admin"),
        os.environ.get("AA_ADMIN_PASSWORD", "123456"),
    )
