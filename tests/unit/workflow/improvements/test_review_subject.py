"""Immutable Improvement Review Subject invariants."""

from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.artifacts.models.retro_v3 import (
    ConfirmedEscapeSignal,
    DomainAnalysisStatus,
    ReopenedCoverageGapSignal,
    RetroSourceDescriptor,
)
from assurance_agent.workflow.improvements.review_subject import (
    ImprovementReviewSubjectError,
    build_review_subject,
    publish_review_subject,
)
from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context


def test_subject_digest_is_stable_for_the_same_semantic_input() -> None:
    context = _context()
    _subject, digest, data = build_review_subject(_candidate(), context, improvement_id="IMP-1")

    # Ledger version and review attempt are not builder inputs, so rebuilding
    # the same frozen Candidate/Context cannot perturb content identity.
    _rebuilt, rebuilt_digest, rebuilt_data = build_review_subject(
        _candidate(), context, improvement_id="IMP-1"
    )

    assert rebuilt_digest == digest
    assert rebuilt_data == data


def test_subject_rejects_unresolvable_source_before_publication() -> None:
    candidate = _candidate(source_refs={"problem_ids": ["PROB-MISSING"]})

    with pytest.raises(ImprovementReviewSubjectError, match="unresolvable source refs"):
        build_review_subject(candidate, _context(), improvement_id="IMP-1")


def test_subject_collapses_historical_exact_duplicate_signal_ids() -> None:
    context = _context()
    signal = context.signals.issue[0]
    duplicate_context = context.model_copy(
        update={
            "signals": context.signals.model_copy(update={"issue": (signal, signal)}),
            "signal_count": 2,
        }
    )

    subject, _digest, _data = build_review_subject(_candidate(), duplicate_context, improvement_id="IMP-1")

    assert subject.signal_evidence == (signal,)


def test_subject_rejects_conflicting_payloads_for_the_same_signal_id() -> None:
    context = _context()
    signal = context.signals.issue[0]
    conflicting = signal.model_copy(update={"summary": "Conflicting analyzer claim"})
    conflicting_context = context.model_copy(
        update={
            "signals": context.signals.model_copy(update={"issue": (signal, conflicting)}),
            "signal_count": 2,
        }
    )

    with pytest.raises(ImprovementReviewSubjectError, match="conflicting signal_id.*SIG-1"):
        build_review_subject(_candidate(), conflicting_context, improvement_id="IMP-1")


@pytest.mark.parametrize("domain", ["discovery", "coverage_gap"])
def test_subject_resolves_optional_domain_signals_and_narrows_their_sources(domain: str) -> None:
    context = _context()
    ok = DomainAnalysisStatus(status="ok")
    if domain == "discovery":
        signal = ConfirmedEscapeSignal(
            signal_id="ESCAPE-PROB-1",
            summary="Confirmed product escape",
            occurrence_count=1,
            recommended_change="Capture the missed obligation.",
            source_refs=ImprovementSourceRefs(problem_ids=("PROB-ESC-1",)),
            confidence="high",
            problem_id="PROB-ESC-1",
            change_id="CH-1",
            missed_obligation_ids=("OBL-1",),
        )
        source = RetroSourceDescriptor(
            kind="discovery_projection",
            change_id="CH-1",
            sha256="sha256:discovery",
            evidence_ids=("PROB-ESC-1",),
        )
        candidate = _candidate(
            signal_ids=[signal.signal_id],
            source_refs={"problem_ids": ["PROB-ESC-1"]},
        )
        manifest = context.source_manifest.model_copy(
            update={
                "discovery_slice_sha256": "sha256:discovery-slice",
                "discovery_sources": (source,),
            }
        )
        statuses = context.domain_status.model_copy(update={"discovery": ok})
        signals = context.signals.model_copy(update={"issue": (), "discovery": (signal,)})
    else:
        signal = ReopenedCoverageGapSignal(
            signal_id="REOPEN-CASE-1",
            summary="Coverage gap reopened",
            occurrence_count=1,
            recommended_change="Add a durable regression case.",
            source_refs=ImprovementSourceRefs(workflow_evidence_ids=("GAP-CASE-1",)),
            confidence="high",
            gap_kind="uncovered_required_case",
            locator_fingerprint="uncovered_required_case|CASE-1|||",
            change_id="CH-1",
            case_id="CASE-1",
        )
        source = RetroSourceDescriptor(
            kind="coverage_gap_projection",
            change_id="CH-1",
            sha256="sha256:coverage-gap",
            evidence_ids=("GAP-CASE-1",),
        )
        candidate = _candidate(
            signal_ids=[signal.signal_id],
            source_refs={"workflow_evidence_ids": ["GAP-CASE-1"]},
        )
        manifest = context.source_manifest.model_copy(
            update={
                "coverage_gap_slice_sha256": "sha256:coverage-gap-slice",
                "coverage_gap_sources": (source,),
            }
        )
        statuses = context.domain_status.model_copy(update={"coverage_gap": ok})
        signals = context.signals.model_copy(update={"issue": (), "coverage_gap": (signal,)})

    optional_context = context.model_copy(
        update={
            "source_manifest": manifest,
            "domain_status": statuses,
            "signals": signals,
            "signal_count": 1,
        }
    )

    subject, _digest, _data = build_review_subject(candidate, optional_context, improvement_id="IMP-1")

    assert subject.signal_evidence == (signal,)
    assert getattr(subject.source_manifest, f"{domain}_slice_sha256") == getattr(
        manifest, f"{domain}_slice_sha256"
    )
    assert getattr(subject.source_manifest, f"{domain}_sources") == (source,)


def test_publish_is_immutable_and_idempotent(tmp_path) -> None:
    _subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")

    path = publish_review_subject(tmp_path, digest, data)
    assert publish_review_subject(tmp_path, digest, data) == path
    assert path.read_bytes() == data

    with pytest.raises(ImprovementReviewSubjectError, match="do not match"):
        publish_review_subject(tmp_path, digest, data + b" ")

    assert sha256_bytes(path.read_bytes()) == digest


def test_publish_writes_multiline_semantically_equal_agent_projection(tmp_path) -> None:
    subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")

    canonical_path = publish_review_subject(tmp_path, digest, data)
    agent_path = canonical_path.parent / "agent" / canonical_path.name

    assert agent_path.is_file()
    assert agent_path.read_text(encoding="utf-8").count("\n") > 2
    assert json.loads(agent_path.read_text(encoding="utf-8")) == subject.model_dump(mode="json")
    assert canonical_path.read_bytes() == data


def test_publish_preflights_agent_projection_conflict_before_writing_canonical(tmp_path) -> None:
    _subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")
    agent_path = tmp_path / "qa" / "improvements" / "review-subjects" / "agent" / f"{digest}.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text('{"conflict":true}\n', encoding="utf-8")

    with pytest.raises(ImprovementReviewSubjectError, match="agent review subject conflict"):
        publish_review_subject(tmp_path, digest, data)

    assert not (agent_path.parent.parent / f"{digest}.json").exists()
