from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.state import (
    complete_retro_stage,
    mark_consumed_change,
    read_state,
)


def test_mark_consumed_change_persists(tmp_path: Path) -> None:
    mark_consumed_change(
        tmp_path, change_id="CH-1", source="archive", consumed_at="2026-07-15T00:00:00Z", retro_id="retro-1"
    )
    state = read_state(tmp_path)
    assert state["consumed_changes"]["CH-1"]["retro_id"] == "retro-1"


def test_complete_retro_stage_sets_last_retro(tmp_path: Path) -> None:
    complete_retro_stage(tmp_path, "retro-9")
    assert read_state(tmp_path)["last_retro_id"] == "retro-9"


def test_read_state_migrates_legacy_list_shaped_consumed_changes(tmp_path: Path) -> None:
    """Regression: pre-1.2 `_state.json` wrote `consumed_changes` as a flat
    list of records instead of a dict keyed by change_id, which crashed
    `phase_a.enumerate_candidates`'s `.items()` call with
    `'list' object has no attribute 'items'`.
    """
    state_path = tmp_path / "qa" / "retro" / "_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "last_retro_id": "retro-old",
                "consumed_changes": [
                    {
                        "change_id": "RET-old-1",
                        "source": "unarchived",
                        "consumed_at": "2026-07-13T02:13:39.274Z",
                        "retro_id": "retro-old",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state = read_state(tmp_path)

    assert isinstance(state["consumed_changes"], dict)
    assert state["consumed_changes"]["RET-old-1"]["retro_id"] == "retro-old"
    assert state["consumed_changes"]["RET-old-1"]["terminal"] is True
