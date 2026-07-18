from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def generate_retro_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"retro-{stamp}"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_dir_names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
