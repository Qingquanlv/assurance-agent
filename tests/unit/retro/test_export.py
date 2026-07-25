from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.retro.export import (
    ExportConflictError,
    ExportIneligibleError,
    export_proposal,
    export_proposals,
    proposal_export_sha256,
)
from assurance_agent.retro.promotions import (
    application_event,
    proposal_exported_event,
    proposal_states,
    read_promotion_events,
    review_decision_event,
)
from assurance_agent.retro.projection import project_retro_show
from tests.unit.retro.proposal_fixtures import issue_proposal, knowledge_proposal, memory_proposal


def _write_proposals(retro_dir: Path, proposals: list[dict]) -> None:
    retro_dir.mkdir(parents=True, exist_ok=True)
    retro_dir.joinpath("proposals.json").write_text(
        json.dumps({"proposals": proposals}, indent=2),
        encoding="utf-8",
    )


def test_export_issue_draft_writes_yaml_and_event(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    proposal = issue_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])

    outcome = export_proposal(retro_dir, proposal, state="proposed")
    assert outcome.action == "exported"
    draft = yaml.safe_load(outcome.target_path.read_text(encoding="utf-8"))
    assert draft["id"] == "P-ISSUE"
    assert draft["finding_kind"] == "workflow_bug"
    assert draft["source_sha256"] == proposal_export_sha256(proposal)
    events = read_promotion_events(retro_dir)
    assert events[-1]["type"] == "proposal_exported"
    assert proposal_states(events)["P-ISSUE"] == "exported"


def test_export_same_hash_is_noop_even_when_exported(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    proposal = issue_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])
    first = export_proposal(retro_dir, proposal, state="proposed")
    second = export_proposal(retro_dir, proposal, state="exported")
    assert first.action == "exported"
    assert second.action == "noop"
    assert len(read_promotion_events(retro_dir)) == 1


def test_export_hash_conflict_requires_overwrite(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    proposal = issue_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])
    export_proposal(retro_dir, proposal, state="proposed")
    changed = issue_proposal(
        payload={
            "title": "changed title",
            "target": "assurance_agent/workflow/report/failure_classifier.py",
            "severity": "low",
            "evidence_ids": ["CH-1#F-1"],
            "proposed_change": "y",
        }
    )
    with pytest.raises(ExportConflictError):
        export_proposal(retro_dir, changed, state="needs_rework")
    outcome = export_proposal(retro_dir, changed, state="needs_rework", overwrite=True)
    assert outcome.action == "exported"


def test_export_rejects_terminal_states(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    proposal = issue_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])
    with pytest.raises(ExportIneligibleError):
        export_proposal(retro_dir, proposal, state="rejected")


def test_export_knowledge_delta_writes_l2_proposal(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    proposal = knowledge_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])
    outcomes = export_proposals(retro_dir, apply_kind="knowledge_delta")
    assert len(outcomes) == 1
    draft = yaml.safe_load(outcomes[0].target_path.read_text(encoding="utf-8"))
    assert draft["mode"] == "delta"
    assert "api_admin_token" in draft["auth"]


def test_projection_includes_export_metadata(tmp_path: Path) -> None:
    root = tmp_path
    retro_id = "retro-x"
    retro_dir = root / "qa" / "retro" / retro_id
    proposal = issue_proposal()
    _write_proposals(retro_dir, [proposal.model_dump(mode="json")])
    export_proposal(retro_dir, proposal, state="proposed")
    payload = project_retro_show(root, retro_id)
    exported = next(item for item in payload["proposals"] if item["id"] == "P-ISSUE")
    assert exported["state"] == "exported"
    assert exported["export_target_path"] == "issue-drafts/P-ISSUE.yaml"
    assert exported["export_source_sha256"]


def test_export_skips_memory_append_proposals(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-x"
    _write_proposals(
        retro_dir,
        [memory_proposal().model_dump(mode="json"), issue_proposal().model_dump(mode="json")],
    )
    outcomes = export_proposals(retro_dir, apply_kind="issue_export")
    assert [item.proposal_id for item in outcomes] == ["P-ISSUE"]


def test_proposal_states_folds_export_event() -> None:
    events = [
        review_decision_event("P-1", decision="needs_rework", actor="h"),
        proposal_exported_event("P-1", actor="aa", target="issue-drafts/P-1.yaml", source_sha256="abc"),
        application_event("P-2", result="applied", actor="aa"),
    ]
    states = proposal_states(events)
    assert states["P-1"] == "exported"
    assert states["P-2"] == "applied"
