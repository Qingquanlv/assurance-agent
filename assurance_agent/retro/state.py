from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.types import EvidenceSource


def _state_path(project_root: Path) -> Path:
    return project_root / "qa" / "retro" / "_state.json"


def _coerce_consumed_changes(raw: object) -> dict:
    """Normalize `consumed_changes` to the dict-keyed-by-change_id schema.

    Older schema_version (<=1.1) wrote `consumed_changes` as a flat list of
    per-change records (no `terminal` flag, keyed implicitly by list order).
    Downstream readers (`phase_a.enumerate_candidates`, `complete_retro_stage`)
    require a dict keyed by `change_id` with a `terminal` flag. Coerce here so
    stale on-disk state files from before the schema change don't crash
    `aa retro nightly collect` with `'list' object has no attribute 'items'`.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        coerced: dict = {}
        for record in raw:
            if not isinstance(record, dict):
                continue
            change_id = record.get("change_id")
            if not change_id:
                continue
            coerced[change_id] = {k: v for k, v in record.items() if k != "change_id"}
            coerced[change_id].setdefault("terminal", True)
        return coerced
    return {}


def read_state(project_root: Path) -> dict:
    path = _state_path(project_root)
    if not path.exists():
        return {"last_retro_id": None, "consumed_changes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_retro_id": None, "consumed_changes": {}}
    data.setdefault("last_retro_id", None)
    data["consumed_changes"] = _coerce_consumed_changes(data.get("consumed_changes"))
    return data


def write_state(project_root: Path, state: dict) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_consumed_change(
    project_root: Path, *, change_id: str, source: EvidenceSource, consumed_at: str, retro_id: str
) -> None:
    state = read_state(project_root)
    state["consumed_changes"][change_id] = {
        "source": source,
        "consumed_at": consumed_at,
        "retro_id": retro_id,
        "terminal": False,
    }
    write_state(project_root, state)


def complete_retro_stage(project_root: Path, retro_id: str) -> None:
    state = read_state(project_root)
    state["last_retro_id"] = retro_id
    for record in state["consumed_changes"].values():
        if record.get("retro_id") == retro_id:
            record["terminal"] = True
    write_state(project_root, state)
