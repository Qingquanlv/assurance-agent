from __future__ import annotations

from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.retro.aggregator import build_retro_context
from assurance_agent.retro.proposals import validate_retro_proposals
from assurance_agent.retro.types import RetroProposal
from tests.unit.retro.archive_fixtures import make_archived_change


def _context(tmp_path: Path):
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    return build_retro_context(tmp_path, changes=["CH-1"], retro_id="retro-test")


def test_proposal_backfills_summary_and_body_from_skill_fields() -> None:
    """Regression: aa-retro emits `problem`/`proposed_change`; the Python model
    only had `summary`/`body`, so skill-authored proposals validated with an
    empty body and were dropped. Rich fields must backfill the legacy ones.
    """
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-001",
            "layer": "agent",
            "target": ".aa/memory/aa-api-codegen.md",
            "problem": "duplicate username returns HTTP 500",
            "proposed_change": "append validation rule",
            "evidence_ids": ["CH-1#F-1"],
            "apply_kind": "memory_append",
            "eval_suite": "workflow-api-codegen",
        }
    )
    assert proposal.summary == "duplicate username returns HTTP 500"
    assert proposal.body == "append validation rule"


def test_validate_accepts_proposal_citing_existing_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-001",
            "problem": "p",
            "proposed_change": "c",
            "evidence_ids": ["CH-1#F-1"],
            "apply_kind": "memory_append",
        }
    )
    assert validate_retro_proposals(context, [proposal]) == []


def test_validate_rejects_proposal_without_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = RetroProposal.model_validate(
        {"id": "RETRO-001", "proposed_change": "c", "apply_kind": "memory_append"}
    )
    errors = validate_retro_proposals(context, [proposal])
    assert any("cites no evidence_ids" in e for e in errors)


def test_validate_rejects_evidence_absent_from_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-001",
            "proposed_change": "c",
            "evidence_ids": ["CH-1#F-1", "CH-9#FAIL-404"],
            "apply_kind": "memory_append",
        }
    )
    errors = validate_retro_proposals(context, [proposal])
    assert any("absent from context" in e and "CH-9#FAIL-404" in e for e in errors)
