"""Lane C declaration proposal builder, writer, and intake adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.coverage_gaps import (
    CoverageGap,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.declarations import (
    DeclarationProposal,
    DeclarationProposalReceipt,
)
from assurance_agent.evidence.declarations import (
    build_declaration_proposal_from_counterexample,
    build_declaration_proposal_from_gaps,
    validate_declaration_intake,
    write_declaration_proposal,
)


def _gap(
    *,
    kind: str = "constraint_without_property",
    constraint_key: str | None = "auth.max_attempts",
    case_id: str | None = None,
    layer: str = "declaration",
) -> CoverageGap:
    locator: dict[str, str] = {}
    if case_id is not None:
        locator["case_id"] = case_id
    if constraint_key is not None:
        locator["constraint_key"] = constraint_key
    return CoverageGap.model_validate(
        {
            "kind": kind,
            "locator": locator,
            "layer": layer,
            "batch_id": "20260805-120000",
            "evidence_refs": ("sha256:projdeadbeef",),
        }
    )


def _gaps_doc(*gaps: CoverageGap) -> CoverageGapsDocument:
    return CoverageGapsDocument(
        schema_version="1",
        change_id="CH-DECL-001",
        batch_id="20260805-120000",
        projection_digest="sha256:projdeadbeef",
        gaps=gaps,
    )


def test_builder_from_coverage_gap_is_deterministic() -> None:
    doc = _gaps_doc(_gap())
    a = build_declaration_proposal_from_gaps(doc)
    b = build_declaration_proposal_from_gaps(doc)
    assert a == b
    assert a.status == "draft"
    assert a.change_id == "CH-DECL-001"
    assert a.fingerprint
    assert a.improvement_id.startswith("IMP-")
    assert len(a.case_drafts) == 1
    assert a.case_drafts[0].status == "draft"
    assert a.case_drafts[0].layer in {"api", "e2e", "fuzz", "performance"}
    assert a.evidence_refs[0].kind == "coverage_gap"
    assert a.evidence_refs[0].digest == "sha256:projdeadbeef"
    assert "constraint_key=auth.max_attempts" in a.evidence_refs[0].locator


def test_builder_from_gaps_skips_execution_layer() -> None:
    """Lane C templates only declaration-layer gaps (Lane B owns execution)."""
    doc = _gaps_doc(
        _gap(layer="execution", kind="uncovered_required_case", case_id="TC_API_001", constraint_key=None),
        _gap(layer="declaration"),
    )
    proposal = build_declaration_proposal_from_gaps(doc)
    assert len(proposal.case_drafts) == 1
    assert all(ref.kind == "coverage_gap" for ref in proposal.evidence_refs)
    assert "TC_API_001" not in {c.case_id for c in proposal.case_drafts}


def test_builder_from_counterexample_refs_digest_not_content() -> None:
    proposal = build_declaration_proposal_from_counterexample(
        counterexample_id="CE-AUTH-001",
        digest="sha256:cecontentdigest",
        change_id="CH-CE-001",
        path="discovery/counterexamples/CE-AUTH-001.yaml",
        source_improvement_id="IMP-CE-SRC",
    )
    assert proposal.status == "draft"
    assert proposal.change_id == "CH-CE-001"
    assert proposal.source_improvement_id == "IMP-CE-SRC"
    assert len(proposal.case_drafts) == 1
    assert proposal.case_drafts[0].status == "draft"
    assert "CE_AUTH_001" in proposal.case_drafts[0].case_id
    ref = proposal.evidence_refs[0]
    assert ref.kind == "counterexample"
    assert ref.digest == "sha256:cecontentdigest"
    assert ref.locator == "counterexample_id=CE-AUTH-001"
    assert ref.path == "discovery/counterexamples/CE-AUTH-001.yaml"
    # Identity only — never embeds CE body fields.
    dumped = proposal.model_dump(mode="json")
    assert "actions" not in dumped
    assert "observed" not in dumped


def test_write_round_trip_yaml(tmp_path: Path) -> None:
    proposal = build_declaration_proposal_from_gaps(_gaps_doc(_gap()))
    path = write_declaration_proposal(tmp_path, proposal)
    assert (
        path == tmp_path / "qa" / "improvements" / "declarations" / f"{proposal.improvement_id}.proposal.yaml"
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    reloaded = DeclarationProposal.model_validate(raw)
    assert reloaded == proposal


def test_write_returns_receipt_digest(tmp_path: Path) -> None:
    from assurance_agent.evidence.declarations import write_declaration_proposal_receipt

    proposal = build_declaration_proposal_from_gaps(_gaps_doc(_gap()))
    got = write_declaration_proposal_receipt(tmp_path, proposal)
    assert isinstance(got, DeclarationProposalReceipt)
    assert got.improvement_id == proposal.improvement_id
    assert got.status == "draft"
    assert got.path.endswith(".proposal.yaml")
    assert got.digest.startswith("sha256:")
    assert (tmp_path / got.path).is_file()


def test_intake_adapter_accepts_valid_draft_proposal() -> None:
    proposal = build_declaration_proposal_from_gaps(_gaps_doc(_gap()))
    verdict = validate_declaration_intake(proposal)
    assert verdict.ok is True
    assert verdict.errors == ()


def test_intake_adapter_rejects_active_case_entries() -> None:
    raw = {
        "schema_version": "1",
        "improvement_id": "IMP-DECL-ACTIVE",
        "status": "draft",
        "case_drafts": [
            {
                "case_id": "TC_API_ACTIVE",
                "title": "Should stay draft",
                "layer": "api",
                "status": "active",
            }
        ],
        "evidence_refs": [
            {
                "kind": "escape",
                "locator": "issue_fingerprint=ISSUE-ESC-1",
                "digest": "sha256:escapefingerprint",
            }
        ],
        "fingerprint": "b" * 64,
    }
    verdict = validate_declaration_intake(raw)
    assert verdict.ok is False
    assert any("draft" in err.lower() or "active" in err.lower() for err in verdict.errors)


def test_intake_accepts_escape_evidence_fingerprint_locator() -> None:
    """Smoke: declaration evidence kind ``escape`` still accepts fingerprint identity."""
    raw = {
        "schema_version": "1",
        "improvement_id": "IMP-DECL-ESCAPE",
        "status": "draft",
        "case_drafts": [
            {
                "case_id": "TC_API_ESCAPE_001",
                "title": "Regression from confirmed escape",
                "layer": "api",
                "status": "draft",
            }
        ],
        "evidence_refs": [
            {
                "kind": "escape",
                "locator": "issue_fingerprint=sha256:fingerprint",
                "digest": "sha256:escapefingerprint",
            }
        ],
        "fingerprint": "c" * 64,
    }
    verdict = validate_declaration_intake(raw)
    assert verdict.ok is True
    assert verdict.errors == ()


def test_intake_adapter_rejects_empty_digest() -> None:
    raw = {
        "schema_version": "1",
        "improvement_id": "IMP-DECL-EMPTY",
        "status": "draft",
        "case_drafts": [
            {
                "case_id": "TC_API_EMPTY",
                "title": "Missing digest",
                "layer": "api",
                "status": "draft",
            }
        ],
        "evidence_refs": [
            {
                "kind": "mutation_survivor",
                "locator": "mutant_id=M-1",
                "digest": "   ",
            }
        ],
        "fingerprint": "c" * 64,
    }
    verdict = validate_declaration_intake(raw)
    assert verdict.ok is False
    assert any("digest" in err.lower() for err in verdict.errors)


def test_builders_do_not_touch_metric_key() -> None:
    """Lane C builders must not import or mutate MetricKey / metrics surfaces."""
    import assurance_agent.evidence.declarations as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "from assurance_agent.artifacts.models.metrics" not in source
    assert "import MetricKey" not in source
    assert "MetricsDocument" not in source
    assert "metrics.json" not in source


def test_builder_requires_declaration_gap_or_raises() -> None:
    doc = _gaps_doc(
        _gap(layer="execution", kind="uncovered_required_case", case_id="TC_API_001", constraint_key=None),
    )
    with pytest.raises(ValueError, match="declaration"):
        build_declaration_proposal_from_gaps(doc)
