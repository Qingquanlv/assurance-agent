from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers_aa import write_aa_config

from assurance_agent.exceptions import AaError
from assurance_agent.retro.aggregator import build_retro_context
from assurance_agent.retro.proposals import read_proposals, validate_retro_proposals
from assurance_agent.retro.types import IssueDraftPayload, RetroProposal, memory_body_text
from tests.unit.retro.archive_fixtures import make_archived_change
from tests.unit.retro.proposal_fixtures import memory_proposal, memory_proposal_dict


def _context(tmp_path: Path):
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    return build_retro_context(tmp_path, changes=["CH-1"], retro_id="retro-test")


def test_proposal_backfills_summary_and_body_from_skill_fields() -> None:
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-001",
            "finding_kind": "prompt_rule",
            "apply_kind": "memory_append",
            "layer": "agent",
            "target": ".aa/memory/aa-api-codegen.md",
            "problem": "duplicate username returns HTTP 500",
            "proposed_change": "append validation rule",
            "payload": {"body": "append validation rule"},
            "evidence_ids": ["CH-1#F-1"],
            "eval_suite": "workflow-api-codegen",
        }
    )
    assert proposal.summary == "duplicate username returns HTTP 500"
    assert proposal.body == "append validation rule"


def test_validate_accepts_proposal_citing_existing_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = memory_proposal(
        id="RETRO-001",
        problem="p",
        proposed_change="c",
        payload={"body": "c"},
        evidence_ids=["CH-1#F-1"],
    )
    assert validate_retro_proposals(context, [proposal]) == []


def test_validate_rejects_proposal_without_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = memory_proposal(id="RETRO-001", evidence_ids=[])
    errors = validate_retro_proposals(context, [proposal])
    assert any("cites no evidence_ids" in e for e in errors)


def test_validate_rejects_evidence_absent_from_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    proposal = memory_proposal(
        id="RETRO-001",
        evidence_ids=["CH-1#F-1", "CH-9#FAIL-404"],
    )
    errors = validate_retro_proposals(context, [proposal])
    assert any("absent from context" in e and "CH-9#FAIL-404" in e for e in errors)


def test_retro_proposal_rejects_mismatched_finding_kind_and_apply_kind() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        memory_proposal(id="RETRO-001", finding_kind="workflow_bug", apply_kind="memory_append")


def _write_proposals(retro_dir: Path, entries: list[dict]) -> Path:
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "proposals.json").write_text(json.dumps({"proposals": entries}), encoding="utf-8")
    return retro_dir


def test_legacy_memory_proposal_derives_finding_kind_and_payload() -> None:
    """Pre-three-track proposals carry apply_kind + prose but no finding_kind/payload."""
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-001",
            "apply_kind": "memory_append",
            "layer": "agent",
            "target": ".aa/memory/aa-inspect.md",
            "problem": "fuzz failures misclassified",
            "proposed_change": "reparse the raw fuzz log before classifying",
            "evidence_ids": ["CH-1#F-1"],
            "eval_suite": "workflow-full",
        }
    )
    assert proposal.finding_kind == "prompt_rule"
    assert memory_body_text(proposal) == "reparse the raw fuzz log before classifying"


def test_legacy_issue_proposal_derives_payload_and_maps_risk_to_severity() -> None:
    proposal = RetroProposal.model_validate(
        {
            "id": "RETRO-008",
            "apply_kind": "issue_export",
            "target": "assurance_agent/workflow/inspect",
            "problem": "schema KeyError classified as unknown",
            "proposed_change": "register the local schema key before classifying",
            "risk": "low",
            "evidence_ids": ["CH-1#F-1"],
        }
    )
    assert proposal.finding_kind == "workflow_bug"
    assert isinstance(proposal.payload, IssueDraftPayload)
    assert proposal.payload.severity == "low"
    assert proposal.payload.target == "assurance_agent/workflow/inspect"
    assert proposal.payload.proposed_change == "register the local schema key before classifying"


def test_legacy_adapter_does_not_override_explicit_payload() -> None:
    proposal = memory_proposal(id="RETRO-001", payload={"body": "explicit body"}, proposed_change="prose")
    assert memory_body_text(proposal) == "explicit body"


def test_read_proposals_rescues_legacy_shape_from_disk(tmp_path: Path) -> None:
    retro_dir = _write_proposals(
        tmp_path / "retro-legacy",
        [
            {
                "id": "RETRO-001",
                "apply_kind": "memory_append",
                "target": ".aa/memory/aa-inspect.md",
                "problem": "p",
                "proposed_change": "append a rule",
                "evidence_ids": ["CH-1#F-1"],
            }
        ],
    )
    proposals = read_proposals(retro_dir)
    assert [p.id for p in proposals] == ["RETRO-001"]
    assert proposals[0].finding_kind == "prompt_rule"


def test_read_proposals_fails_loud_on_unroutable_entry(tmp_path: Path) -> None:
    """An unroutable apply_kind must not make the whole run silently disappear."""
    retro_dir = _write_proposals(
        tmp_path / "retro-bad",
        [
            memory_proposal_dict(id="RETRO-001"),
            {"id": "RETRO-006", "apply_kind": "contract_field", "problem": "p"},
        ],
    )
    with pytest.raises(AaError) as err:
        read_proposals(retro_dir)
    assert "RETRO-006" in str(err.value)


def test_read_proposals_non_strict_keeps_routable_entries(tmp_path: Path) -> None:
    retro_dir = _write_proposals(
        tmp_path / "retro-mixed",
        [
            memory_proposal_dict(id="RETRO-001"),
            {"id": "RETRO-006", "apply_kind": "contract_field", "problem": "p"},
        ],
    )
    assert [p.id for p in read_proposals(retro_dir, strict=False)] == ["RETRO-001"]


def test_accept_proposals_rewrites_legacy_shape_to_canonical(tmp_path: Path) -> None:
    """Write gate persists finding_kind/payload so later reads need no shim."""
    from assurance_agent.retro.proposals import accept_proposals

    retro_dir = _write_proposals(
        tmp_path / "retro-accept",
        [
            {
                "id": "RETRO-001",
                "apply_kind": "memory_append",
                "target": ".aa/memory/aa-inspect.md",
                "problem": "misclassified fuzz failures",
                "proposed_change": "reparse the raw fuzz log before classifying",
                "evidence_ids": ["CH-1#F-1"],
                "eval_suite": "workflow-full",
            }
        ],
    )
    # Preserve envelope keys other than proposals.
    doc = json.loads((retro_dir / "proposals.json").read_text(encoding="utf-8"))
    doc["retro_id"] = "retro-accept"
    (retro_dir / "proposals.json").write_text(json.dumps(doc), encoding="utf-8")

    proposals = accept_proposals(retro_dir)
    assert [p.id for p in proposals] == ["RETRO-001"]

    rewritten = json.loads((retro_dir / "proposals.json").read_text(encoding="utf-8"))
    assert rewritten["retro_id"] == "retro-accept"
    entry = rewritten["proposals"][0]
    assert entry["finding_kind"] == "prompt_rule"
    assert entry["payload"] == {"body": "reparse the raw fuzz log before classifying"}
    # Canonical dump is readable without the before-validator shim.
    assert "finding_kind" in entry and "payload" in entry


def test_accept_proposals_fails_loud_on_unroutable_entry(tmp_path: Path) -> None:
    from assurance_agent.retro.proposals import accept_proposals

    retro_dir = _write_proposals(
        tmp_path / "retro-bad-accept",
        [{"id": "RETRO-006", "apply_kind": "contract_field", "problem": "p"}],
    )
    with pytest.raises(AaError) as err:
        accept_proposals(retro_dir)
    assert "RETRO-006" in str(err.value)
