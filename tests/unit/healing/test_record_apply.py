import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.core.events import EventWriteError, append_event_strict, read_events
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    derive_guard_context,
    record_apply_summary,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_healing_episode(change_dir: Path, *, source_batch: str = "20260101-000000") -> None:
    append_event_strict(
        change_dir,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": "x",
            "entry_batch_id": source_batch,
            "episode_id": "e1",
        },
    )
    append_event_strict(
        change_dir,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": "e1",
            "attempt_id": "a1",
            "attempt_number": 1,
            "operation_id": "op1",
            "source_batch_id": source_batch,
        },
    )


def _manifest(change_dir: Path, files: dict[str, str], aggregate: str) -> None:
    _write(
        change_dir / "execution" / "execution-manifest.yaml",
        yaml.safe_dump(
            {
                "batch_id": "20260101-000000",
                "tests_tree_sha256": aggregate,
                "test_files_sha256": files,
                "product_tree_sha256": "p0",
                "final_status": "PASS",
                "result_files": {},
            }
        ),
    )


def _proposal(change_dir: Path) -> None:
    _write(
        change_dir / "healing" / "fix-proposal.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "summary": {"eligible_count": 1},
                "proposals": [
                    {
                        "proposal_id": "FIX-001",
                        "target": "api",
                        "eligible": True,
                        "files_to_modify": ["tests/api/test_menu.py"],
                    }
                ],
            }
        ),
    )


def test_record_apply_rejects_without_active_allocation(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _proposal(change_dir)
    with pytest.raises(HealingGuardError, match="no active healing allocation"):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])


def test_record_apply_rejects_file_outside_authorized_proposals(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _write(tmp_path / "tests" / "api" / "test_menu.py", "v1\n")
    baseline = hash_test_tree(tmp_path)
    _manifest(change_dir, baseline.files, baseline.aggregate)
    _seed_healing_episode(change_dir)
    _proposal(change_dir)
    _write(tmp_path / "tests" / "api" / "other.py", "tampered\n")
    with pytest.raises(HealingGuardError, match="outside authorized proposals"):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])


def test_record_apply_writes_summary_and_frozen_event_atomically(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _write(tmp_path / "tests" / "api" / "test_menu.py", "v1\n")
    baseline = hash_test_tree(tmp_path)
    _manifest(change_dir, baseline.files, baseline.aggregate)
    _seed_healing_episode(change_dir, source_batch="20260101-000000")
    _proposal(change_dir)
    _write(tmp_path / "tests" / "api" / "test_menu.py", "v2\n")

    result = record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])
    assert Path(result.json_path).is_file()
    assert Path(result.md_path).is_file()

    events = read_events(change_dir)
    apply = next(e for e in events if e.get("type") == "heal_record_apply")
    proposal_sha = derive_guard_context(tmp_path, "CH-1").proposal_sha256
    assert apply["target"] == "api"
    assert apply["proposal_sha256"] == proposal_sha
    assert apply["source_batch_id"] == "20260101-000000"
    assert apply["attempt_key"] == f"{proposal_sha}:20260101-000000"
    assert apply["summary_sha256"] == result.summary_sha256
    assert apply["files_modified"] == ["tests/api/test_menu.py"]


def test_record_apply_event_failure_restores_both_summary_files(tmp_path: Path, monkeypatch) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    _write(tmp_path / "tests" / "api" / "test_menu.py", "v1\n")
    baseline = hash_test_tree(tmp_path)
    _manifest(change_dir, baseline.files, baseline.aggregate)
    _seed_healing_episode(change_dir)
    _proposal(change_dir)
    _write(tmp_path / "tests" / "api" / "test_menu.py", "v2\n")

    json_path = change_dir / "healing" / "api-apply-summary.json"
    md_path = change_dir / "healing" / "api-apply-summary.md"

    def fail_append(*_args, **_kwargs) -> None:
        raise EventWriteError("simulated event failure")

    monkeypatch.setattr(
        "assurance_agent.workflow.healing.safety.append_event_strict",
        fail_append,
    )
    with pytest.raises(EventWriteError):
        record_apply_summary(tmp_path, "CH-1", "api", ["FIX-001"])
    assert not json_path.exists()
    assert not md_path.exists()


def test_guard_context_uses_m3_projection_attempt_count(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    append_event_strict(
        change_dir,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": "x",
            "entry_batch_id": "b1",
            "episode_id": "e1",
        },
    )
    allocation = {
        "source": "progression",
        "type": "healing_attempt_allocated",
        "episode_id": "e1",
        "attempt_id": "a1",
        "attempt_number": 1,
        "operation_id": "op1",
        "source_batch_id": "b1",
    }
    append_event_strict(change_dir, allocation)
    append_event_strict(change_dir, allocation)
    context = derive_guard_context(tmp_path, "CH-1")
    assert context.snapshot.attempts_used == 1
