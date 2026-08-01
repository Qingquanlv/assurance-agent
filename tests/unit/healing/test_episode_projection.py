"""Normalized healing episode projection: legacy / v2 / mixed equivalence."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.healing.projection import (
    HealingProjectionIntegrityError,
    LEGACY_ALLOCATION,
    LEGACY_BASELINE,
    project_healing_episode,
    project_healing_episode_from_events,
)
from assurance_agent.workflow.healing.safety import derive_guard_context
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state
from tests.helpers_aa import write_aa_config

_ALLOWED_RAW_LEGACY_CONSUMERS = frozenset(
    {
        "assurance_agent/workflow/core/events.py",
        "assurance_agent/workflow/healing/projection.py",
    }
)


def _legacy_pair(change_dir: Path, *, episode: str = "ep1", op: str = "op1", n: int = 1) -> None:
    append_event_strict(
        change_dir,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": "baseline-sha",
            "entry_batch_id": "b1",
            "episode_id": episode,
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": episode,
            "attempt_id": f"ha-{n}",
            "attempt_number": n,
            "operation_id": op,
            "source_batch_id": "b1",
        },
    )


def _v2_allocation(
    change_dir: Path,
    *,
    episode: str = "ep1",
    op: str = "op1",
    n: int = 1,
    embedded: bool = True,
) -> None:
    append_event_strict(
        change_dir,
        {
            "source": "progression",
            "type": "healing_attempt_allocated_v2",
            "episode_id": episode,
            "attempt_id": f"ha-{n}",
            "attempt_number": n,
            "operation_id": op,
            "source_batch_id": "b1",
            "entry_batch_id": "b1",
            "baseline_sha256": "baseline-sha",
            "baseline_embedded": embedded,
        },
    )


def test_legacy_v2_and_mixed_project_equivalent_baseline_and_allocations(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    v2_dir = tmp_path / "v2"
    mixed_dir = tmp_path / "mixed"
    for path in (legacy_dir, v2_dir, mixed_dir):
        path.mkdir()

    _legacy_pair(legacy_dir, op="op1", n=1)
    _v2_allocation(v2_dir, op="op1", n=1, embedded=True)

    # Mixed: legacy first allocation + distinct v2 second allocation.
    _legacy_pair(mixed_dir, op="op1", n=1)
    _v2_allocation(mixed_dir, op="op2", n=2, embedded=False)

    legacy = project_healing_episode(legacy_dir)
    v2 = project_healing_episode(v2_dir)
    mixed = project_healing_episode(mixed_dir)

    assert legacy.baseline is not None and v2.baseline is not None
    assert legacy.baseline.episode_id == v2.baseline.episode_id == "ep1"
    assert legacy.baseline.artifact_sha256 == v2.baseline.artifact_sha256 == "baseline-sha"
    assert legacy.attempts_used == 1
    assert v2.attempts_used == 1
    assert legacy.allocations[0].operation_id == v2.allocations[0].operation_id == "op1"
    assert mixed.attempts_used == 2
    assert [item.operation_id for item in mixed.allocations] == ["op1", "op2"]


def test_duplicate_logical_key_dedupes_equivalent_payload(tmp_path: Path) -> None:
    change_dir = tmp_path / "dup"
    change_dir.mkdir()
    _legacy_pair(change_dir, op="op1", n=1)
    _v2_allocation(change_dir, op="op1", n=1, embedded=False)
    projection = project_healing_episode(change_dir)
    assert projection.attempts_used == 1
    assert projection.allocations[0].operation_id == "op1"


def test_conflicting_logical_key_is_integrity_failure(tmp_path: Path) -> None:
    change_dir = tmp_path / "conflict"
    change_dir.mkdir()
    _legacy_pair(change_dir, op="op1", n=1)
    append_event_strict(
        change_dir,
        {
            "source": "progression",
            "type": "healing_attempt_allocated_v2",
            "episode_id": "ep1",
            "attempt_id": "ha-other",
            "attempt_number": 1,
            "operation_id": "op1",
            "source_batch_id": "b1",
            "entry_batch_id": "b1",
            "baseline_sha256": "baseline-sha",
            "baseline_embedded": False,
        },
    )
    with pytest.raises(HealingProjectionIntegrityError, match="conflict"):
        project_healing_episode(change_dir)


def test_consumers_accept_legacy_v2_and_mixed(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    for name, seeder in (
        ("legacy", lambda d: _legacy_pair(d)),
        ("v2", lambda d: _v2_allocation(d, embedded=True)),
        (
            "mixed",
            lambda d: (_legacy_pair(d, op="op1"), _v2_allocation(d, op="op2", n=2, embedded=False)),
        ),
    ):
        change_dir = tmp_path / "qa" / "changes" / f"CH-{name}"
        change_dir.mkdir(parents=True)
        (change_dir / "healing").mkdir(parents=True)
        (change_dir / "healing" / "fix-proposal.json").write_text("{}", encoding="utf-8")
        seeder(change_dir)
        state = derive_healing_state(change_dir)
        assert state.episode_id == "ep1"
        assert state.attempts_used >= 1
        ctx = derive_guard_context(tmp_path, f"CH-{name}")
        assert ctx.source_batch_id == "b1"
        assert ctx.attempt_key is not None


def test_ast_consumer_set_allows_raw_legacy_names_only_in_codec_and_projector() -> None:
    root = Path("assurance_agent")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.as_posix()
        if rel in _ALLOWED_RAW_LEGACY_CONSUMERS:
            continue
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in {LEGACY_BASELINE, LEGACY_ALLOCATION}:
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {LEGACY_BASELINE, LEGACY_ALLOCATION}:
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_project_from_events_without_disk() -> None:
    events = [
        {
            "seq": 1,
            "type": "healing_attempt_allocated_v2",
            "episode_id": "ep1",
            "attempt_id": "ha-1",
            "attempt_number": 1,
            "operation_id": "op1",
            "source_batch_id": "b1",
            "entry_batch_id": "b1",
            "baseline_sha256": "baseline-sha",
            "baseline_embedded": True,
        }
    ]
    projection = project_healing_episode_from_events(events)
    assert projection.attempts_used == 1
    assert projection.baseline is not None
