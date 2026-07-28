from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.improvements import (
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementSourceRefs,
)
from assurance_agent.artifacts.models.retro_v3 import (
    AffectedSurface,
    ContextSignalSet,
    DomainAnalysisStatus,
    DomainStatuses,
    ImprovementCandidateDocumentV3,
    ImprovementCandidateV3,
    IssuePatternSignal,
    RetroContextV3,
    RetroIntegrity,
    RetroSelectionSnapshot,
    RetroSourceDescriptor,
    RetroSourceManifestV3,
    RetroWindow,
)
from assurance_agent.workflow.retro_outputs import CandidateOutputError, complete_candidate_outputs
from assurance_agent.retro.candidates import (
    CandidateBatchInvalid,
    context_sha256,
    validate_candidate_document,
)


def _context() -> RetroContextV3:
    source = RetroSourceDescriptor(
        kind="change_issue_ledger",
        change_id="CH-1",
        sha256="sha256:issue",
        evidence_ids=("PROB-1", "OCC-1"),
    )
    signal = IssuePatternSignal(
        signal_id="SIG-1",
        summary="Repeated adapter registration gap",
        occurrence_count=2,
        recommended_change="Align the adapter registry with domain factories",
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-1",), occurrence_ids=("OCC-1",)),
        confidence="high",
        pattern_kind="workflow_gap",
        affected_surface=AffectedSurface(kind="api", value="department"),
        symptom="Generated adapter is absent from the registry",
    )
    ok = DomainAnalysisStatus(status="ok")
    return RetroContextV3(
        retro_id="retro-v3",
        generated_at="2026-07-27T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-1",)),
            change_ids=("CH-1",),
        ),
        source_manifest=RetroSourceManifestV3(
            issue_slice_sha256="sha256:issue-slice",
            workflow_slice_sha256="sha256:workflow-slice",
            eval_slice_sha256="sha256:eval-slice",
            issue_sources=(source,),
        ),
        integrity=RetroIntegrity(status="complete"),
        domain_status=DomainStatuses(issue=ok, workflow=ok, eval=ok),
        signals=ContextSignalSet(issue=(signal,)),
        signal_count=1,
    )


def _candidate(**updates: object) -> ImprovementCandidateV3:
    payload: dict[str, object] = {
        "candidate_id": "CAND-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "signal_ids": ["SIG-1"],
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": "assurance_agent/workflow",
        "rationale": "The same registry gap recurred",
        "proposed_change": "Register generated adapters from the domain factory catalog",
        "verification": {
            "suites": ["workflow-full"],
            "success_criteria": "Generated adapters resolve through the registry",
        },
        "risk": "low",
        "confidence": "high",
    }
    payload.update(updates)
    return ImprovementCandidateV3.model_validate(payload)


def _document(context: RetroContextV3, candidate: ImprovementCandidateV3) -> ImprovementCandidateDocumentV3:
    return ImprovementCandidateDocumentV3(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(candidate,),
    )


def test_v3_candidate_requires_resolvable_signal_and_source_refs() -> None:
    context = _context()
    candidate = _candidate(
        signal_ids=["SIG-MISSING"],
        source_refs={"problem_ids": ["PROB-MISSING"]},
    )

    with pytest.raises(CandidateBatchInvalid) as caught:
        validate_candidate_document(context, _document(context, candidate))

    assert {error.code for error in caught.value.errors} == {
        "unknown_signal_id",
        "unknown_source_ref",
    }


def test_v3_candidate_rejects_context_digest_mismatch() -> None:
    context = _context()
    document = _document(context, _candidate()).model_copy(update={"context_sha256": "sha256:wrong"})

    with pytest.raises(CandidateBatchInvalid) as caught:
        validate_candidate_document(context, document)

    assert {error.code for error in caught.value.errors} == {"context_digest_mismatch"}


def test_v3_context_rejects_a_v2_candidate_document_without_signal_provenance() -> None:
    context = _context()
    raw_candidate = _candidate().model_dump(mode="json")
    raw_candidate.pop("signal_ids")
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(ImprovementCandidate.model_validate(raw_candidate),),
    )

    with pytest.raises(CandidateBatchInvalid) as caught:
        validate_candidate_document(context, document)

    assert {error.code for error in caught.value.errors} == {"candidate_schema_mismatch"}


def test_v3_candidate_accepts_complete_traceability_chain() -> None:
    context = _context()
    validate_candidate_document(context, _document(context, _candidate()))


def test_candidate_completion_backfills_digest_from_exact_context_bytes(tmp_path: Path) -> None:
    context = _context()
    retro_dir = tmp_path / "qa" / "retro" / context.retro_id
    retro_dir.mkdir(parents=True)
    context_bytes = (
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (retro_dir / "context.json").write_bytes(context_bytes)
    draft = {
        "schema_version": "3",
        "retro_id": context.retro_id,
        "candidates": [_candidate().model_dump(mode="json")],
    }
    (retro_dir / "proposal-candidates.json").write_text(json.dumps(draft), encoding="utf-8")

    output = f"project:qa/retro/{context.retro_id}/proposal-candidates.json"
    complete_candidate_outputs(tmp_path, (output,))

    completed = ImprovementCandidateDocumentV3.model_validate_json(
        (retro_dir / "proposal-candidates.json").read_text()
    )
    assert completed.context_sha256 == context_sha256(context)


def test_candidate_completion_rejects_agent_authored_digest(tmp_path: Path) -> None:
    context = _context()
    retro_dir = tmp_path / "qa" / "retro" / context.retro_id
    retro_dir.mkdir(parents=True)
    (retro_dir / "context.json").write_text(context.model_dump_json(), encoding="utf-8")
    draft = {
        "schema_version": "3",
        "retro_id": context.retro_id,
        "context_sha256": "sha256:agent-authored",
        "candidates": [_candidate().model_dump(mode="json")],
    }
    (retro_dir / "proposal-candidates.json").write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(CandidateOutputError, match="invalid v3 Candidate"):
        complete_candidate_outputs(
            tmp_path,
            (f"project:qa/retro/{context.retro_id}/proposal-candidates.json",),
        )
