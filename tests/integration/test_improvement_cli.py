"""Integration tests for ``aa improvement list|show`` (project ledger only)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from tests.helpers_aa import write_aa_config

IMP_ID = "IMP-ABC"


def _seed_improvement(project: Path) -> None:
    store = ProjectImprovementStore(project)
    event = IMPROVEMENT_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "IMPEVT-PROP",
            "idempotency_key": "IDEM-PROP",
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": "a" * 64,
            "fingerprint_version": "1",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {"problem_ids": ["PROB-1"]},
            "target": "assurance_agent/workflow/inspect",
            "rationale": "Repeated truncation",
            "proposed_change": "Preserve pytest E lines",
            "verification": {
                "suites": ["workflow-full"],
                "success_criteria": "No truncation",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": "retro-1",
            "candidate_id": "IMP-CAND-1",
            "context_sha256": "c" * 64,
            "candidate_batch_digest": "d" * 64,
        }
    )
    store.append_and_rebuild([event])


def test_improvement_list_and_show_read_project_projection() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project = Path(fs)
        write_aa_config(project)
        _seed_improvement(project)

        listed = runner.invoke(main, ["improvement", "list", "--json"])
        shown = runner.invoke(main, ["improvement", "show", "--id", IMP_ID, "--json"])
        assert listed.exit_code == shown.exit_code == 0, (listed.output, shown.output)
        listed_payload = json.loads(listed.output)
        assert any(item["improvement_id"] == IMP_ID for item in listed_payload["improvements"])
        shown_payload = json.loads(shown.output)
        assert shown_payload["improvement_id"] == IMP_ID
        assert "events" in shown_payload
        assert shown_payload["events"][0]["type"] == "improvement_proposed"


def test_improvement_list_filters_by_state_kind_delivery() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project = Path(fs)
        write_aa_config(project)
        _seed_improvement(project)

        match = runner.invoke(
            main,
            [
                "improvement",
                "list",
                "--json",
                "--state",
                "proposed",
                "--kind",
                "workflow_improvement",
                "--delivery",
                "change_draft",
            ],
        )
        miss = runner.invoke(main, ["improvement", "list", "--json", "--state", "approved"])
        assert match.exit_code == miss.exit_code == 0
        assert len(json.loads(match.output)["improvements"]) == 1
        assert json.loads(miss.output)["improvements"] == []


def test_improvement_commands_do_not_walk_qa_retro() -> None:
    """list/show must succeed from the project ledger even with no qa/retro tree."""
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project = Path(fs)
        write_aa_config(project)
        _seed_improvement(project)
        assert not (project / "qa" / "retro").exists()
        result = runner.invoke(main, ["improvement", "list", "--json"])
        assert result.exit_code == 0, result.output
        assert IMP_ID in result.output
