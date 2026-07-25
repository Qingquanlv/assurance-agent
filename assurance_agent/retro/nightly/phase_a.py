from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.change_location import changes_root, resolve_change
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.archive_reader import (
    list_archived_changes,
    read_issue_evidence,
    resolve_change_dir,
)
from assurance_agent.retro.nightly.types import ChangeCandidate
from assurance_agent.retro.nightly.utils import list_dir_names
from assurance_agent.retro.types import EvidenceSource

IsTerminal = Callable[[Path, str], bool]


# Signal-bearing artifacts the aggregator reads from an archived snapshot. The
# coordinator files below cannot appear there: they are excluded from tree capture,
# so no write-set (and therefore no `aa-archive` run) can ever copy them.
_ARCHIVED_EVIDENCE_RELS = (
    "inspect/failure-analysis.json",
    "execution/execution-manifest.yaml",
    "review",
    "healing",
    "issues",
)


def has_required_evidence(change_dir: Path, source: EvidenceSource = "unarchived") -> bool:
    """Whether ``change_dir`` carries enough evidence for retro aggregation.

    An active change must expose its ledger and state projection — those are what
    the ledger-derived signals are built from. An archived change is a terminal
    snapshot by construction, so it qualifies on the evidence `aa-archive` does
    copy; requiring coordinator files there would reject every archived change.
    """
    if source == "archive":
        return any((change_dir / rel).exists() for rel in _ARCHIVED_EVIDENCE_RELS)
    if issue_evidence_error(change_dir) is not None:
        return False
    return (change_dir / "events.jsonl").exists() and (change_dir / "workflow-state.yaml").exists()


def issue_evidence_error(change_dir: Path) -> str | None:
    """Return a visible error when Change Issue JSONL exists but is malformed."""
    events_path = change_dir / "issues" / "events.jsonl"
    if not events_path.exists():
        return None
    _, _, error = read_issue_evidence(change_dir)
    return error


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
        issue_error = issue_evidence_error(change_dir)
        if issue_error is not None:
            incomplete.append(change_id)
            continue
        if not has_required_evidence(change_dir, source):
            incomplete.append(change_id)
            continue
        if not is_terminal(change_dir, change_id):
            continue
        candidates.append(ChangeCandidate(change_id=change_id, evidence_source=source, path=str(change_dir)))

    active_root = changes_root(sut)
    for change_id in list_dir_names(active_root):
        if change_id in consumed or any(c.change_id == change_id for c in candidates):
            continue
        change_dir = active_root / change_id
        issue_error = issue_evidence_error(change_dir)
        if issue_error is not None:
            incomplete.append(change_id)
            continue
        if not has_required_evidence(change_dir):
            incomplete.append(change_id)
            continue
        if not is_terminal(change_dir, change_id):
            continue
        candidates.append(
            ChangeCandidate(change_id=change_id, evidence_source="unarchived", path=str(change_dir))
        )
    return candidates, incomplete


def snapshot_unarchived_evidence(
    sut: Path, retro_id: str, change_id: str, *, dest_root: Path | None = None
) -> Path:
    """Copy an active change's evidence under ``qa/retro/<id>/evidence/<change_id>/``.

    ``dest_root`` (default: ``sut``) lets graph callers read host evidence while
    writing into their task workspace.
    """
    assert_path_segment_safe(retro_id, label="retro id")
    src = resolve_change(sut, change_id).path
    dest = (dest_root or sut) / "qa" / "retro" / retro_id / "evidence" / change_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "workflow-state.yaml"):
        if (src / name).exists():
            shutil.copy2(src / name, dest / name)
    for sub in ("inspect", "review", "healing", "issues"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True)
    return dest
