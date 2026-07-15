from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.change_location import resolve_change
from assurance_agent.config import load_config
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.archive_reader import list_archived_changes, resolve_change_dir
from assurance_agent.retro.nightly.types import ChangeCandidate
from assurance_agent.retro.nightly.utils import list_dir_names

IsTerminal = Callable[[Path, str], bool]


def has_required_evidence(change_dir: Path) -> bool:
    return (change_dir / "events.jsonl").exists() and (change_dir / "workflow-state.yaml").exists()


def enumerate_candidates(
    sut: Path,
    state: dict,
    *,
    is_terminal: IsTerminal,
) -> tuple[list[ChangeCandidate], list[str]]:
    consumed = {cid for cid, rec in state.get("consumed_changes", {}).items() if rec.get("terminal")}
    candidates: list[ChangeCandidate] = []
    incomplete: list[str] = []

    for change_id in list_archived_changes(sut):
        if change_id in consumed:
            continue
        resolved = resolve_change_dir(sut, change_id)
        if resolved is None:
            continue
        change_dir, source = resolved
        if not has_required_evidence(change_dir):
            incomplete.append(change_id)
            continue
        if not is_terminal(change_dir, change_id):
            continue
        candidates.append(ChangeCandidate(change_id=change_id, evidence_source=source, path=str(change_dir)))

    config = load_config(sut)
    rel = config.qa.changes
    rel = rel[2:] if rel.startswith("./") else rel
    changes_root = sut / rel
    for change_id in list_dir_names(changes_root):
        if change_id in consumed or any(c.change_id == change_id for c in candidates):
            continue
        change_dir = changes_root / change_id
        if not has_required_evidence(change_dir):
            incomplete.append(change_id)
            continue
        if not is_terminal(change_dir, change_id):
            continue
        candidates.append(
            ChangeCandidate(change_id=change_id, evidence_source="unarchived", path=str(change_dir))
        )
    return candidates, incomplete


def snapshot_unarchived_evidence(sut: Path, retro_id: str, change_id: str) -> Path:
    assert_path_segment_safe(retro_id, label="retro id")
    src = resolve_change(sut, change_id).path
    dest = sut / "qa" / "retro" / retro_id / "evidence" / change_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "workflow-state.yaml"):
        if (src / name).exists():
            shutil.copy2(src / name, dest / name)
    for sub in ("inspect", "review", "healing"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True)
    return dest
