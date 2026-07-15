from __future__ import annotations

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
