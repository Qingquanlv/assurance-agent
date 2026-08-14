"""Authority prefix / failure-authority truth tables (Tasks 9–10)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.issue_events import (
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    OccurrenceDetectedEvent,
    ObservationRecordedEvent,
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMergedEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemResolvedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueAnalysisStatus,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    IssueOccurrence,
    Observation,
    ObservationDocument,
    ObservationSource,
    OccurrenceAnalysis,
    ProblemProjection,
    ProvisionalAssessment,
)
from assurance_agent.artifacts.models.trace import TraceFailure, TraceSource
from assurance_agent.evidence.digests import (
    evidence_bundle_digest_v1,
    evidence_entry_digest_v1,
    raw_sha256,
)
from assurance_agent.evidence.issue_identity import (
    ObservationIdentityInput,
    candidate_document_digest,
    event_id,
    observation_id,
    occurrence_id as compute_occurrence_id,
    per_candidate_digest,
    problem_fingerprint,
    problem_id as compute_problem_id,
)
from assurance_agent.evidence.issue_replay import (
    dump_projection,
    project_change_issues,
    project_problems,
)
from assurance_agent.evidence.trace_authority import (
    AuthorityValidationError,
    ValidatedAuthorityPrefix,
    evaluate_reconciled_authority,
    validate_completed_authority,
    validate_failure_authority,
    validate_issue_authority_prefix,
)
from assurance_agent.evidence.verify import VERIFY_BLOCKING_GAP_CODES

CHANGE_ID = "CH-1"
BATCH_ID = "20260729-120000"
CASE_ID = "TC_DEPT_API_001"
TS = "2026-07-29T12:00:00Z"

FAILURE_SOURCE = "inspect/failure-analysis.json"
MANIFEST_SOURCE = "inspect/issue-evidence-manifest.json"
CANDIDATES_SOURCE = "inspect/issue-candidates.json"
OBSERVATIONS_SOURCE = "inspect/observations.json"
LEDGER_SOURCE = "issues/events.jsonl"
SNAPSHOT_SOURCE = "issues/snapshot.json"
RECONCILE_SOURCE = "inspect/issue-reconcile-status.json"
PROJECT_LEDGER_SOURCE = "qa/issues/events.jsonl"
PROJECT_PROBLEMS_SOURCE = "qa/issues/problems.json"
EXECUTION_ANCHOR = "execution/execution-manifest.yaml"
EXTRA_ENTRY = "execution/runs/extra.bin"

EXPECTED_SOURCE: dict[str, str] = {
    "manifest_missing": MANIFEST_SOURCE,
    "manifest_malformed": MANIFEST_SOURCE,
    "manifest_wrong_change": MANIFEST_SOURCE,
    "manifest_wrong_batch": MANIFEST_SOURCE,
    "manifest_entry_path_invalid": MANIFEST_SOURCE,
    "manifest_entry_duplicate": MANIFEST_SOURCE,
    "manifest_execution_anchor_missing": MANIFEST_SOURCE,
    "manifest_entry_digest_mismatch": MANIFEST_SOURCE,
    "manifest_bundle_digest_mismatch": MANIFEST_SOURCE,
    "candidates_missing": CANDIDATES_SOURCE,
    "candidates_malformed": CANDIDATES_SOURCE,
    "candidates_wrong_change": CANDIDATES_SOURCE,
    "candidates_wrong_batch": CANDIDATES_SOURCE,
    "candidates_evidence_mismatch": CANDIDATES_SOURCE,
    "observations_missing": OBSERVATIONS_SOURCE,
    "observations_malformed": OBSERVATIONS_SOURCE,
    "observations_wrong_change": OBSERVATIONS_SOURCE,
    "observations_wrong_batch": OBSERVATIONS_SOURCE,
    "observations_replay_mismatch": OBSERVATIONS_SOURCE,
    "ledger_missing": LEDGER_SOURCE,
    "ledger_malformed": LEDGER_SOURCE,
    "ledger_event_identity_mismatch": LEDGER_SOURCE,
}

EXPECTED_REASON: dict[str, str] = {
    "manifest_missing": "missing",
    "manifest_malformed": "malformed",
    "manifest_wrong_change": "change_id_mismatch",
    "manifest_wrong_batch": "batch_id_mismatch",
    "manifest_entry_path_invalid": "entry_path_invalid",
    "manifest_entry_duplicate": "entry_duplicate",
    "manifest_execution_anchor_missing": "execution_anchor_missing",
    "manifest_entry_digest_mismatch": "entry_digest_mismatch",
    "manifest_bundle_digest_mismatch": "bundle_digest_mismatch",
    "candidates_missing": "missing",
    "candidates_malformed": "malformed",
    "candidates_wrong_change": "change_id_mismatch",
    "candidates_wrong_batch": "batch_id_mismatch",
    "candidates_evidence_mismatch": "evidence_digest_mismatch",
    "observations_missing": "missing",
    "observations_malformed": "malformed",
    "observations_wrong_change": "change_id_mismatch",
    "observations_wrong_batch": "batch_id_mismatch",
    "observations_replay_mismatch": "observations_replay_mismatch",
    "ledger_missing": "ledger_missing",
    "ledger_malformed": "ledger_malformed",
    "ledger_event_identity_mismatch": "event_identity_mismatch",
}


def _project_root(change_dir: Path) -> Path:
    return change_dir.parents[2]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _failure_payload(failures: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "source_manifest": EXECUTION_ANCHOR,
        "inspection_status": "completed",
        "batch_id": BATCH_ID,
        "source_batch_id": BATCH_ID,
        "final_status": "FAIL",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": failures
        or [
            {
                "case_id": CASE_ID,
                "target": "api",
                "category": "assertion_failure",
                "fix_proposal_eligible": False,
                "severity": "high",
                "evidence": {
                    "result_file": "execution/runs/x/api-result.json",
                    "test_file": "tests/api/test_dept.py",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "",
                },
                "diagnosis": "diag",
                "recommended_action": "fix",
            }
        ],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }


def _write_execution_anchor(change_dir: Path) -> bytes:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": "2026-07-29T12:00:00+00:00",
        "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False).model_dump(),
        "result_files": {},
    }
    data = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
    _write_bytes(change_dir / EXECUTION_ANCHOR, data)
    return data


def _make_observation() -> Observation:
    source = ObservationSource(
        artifact="execution/runs/x/api-result.json",
        json_pointer="/cases/0",
    )
    signature = "GET /api/v1/dept returned HTTP 500"
    obs_id = observation_id(
        ObservationIdentityInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            kind="test_failure",
            target="api",
            case_id=CASE_ID,
            source_artifact=source.artifact,
            source_json_pointer=source.json_pointer,
            signature=signature,
        )
    )
    return Observation(
        observation_id=obs_id,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        kind="test_failure",
        target="api",
        case_id=CASE_ID,
        source=source,
        evidence_refs=["execution/runs/x/api-result.json"],
        signature=signature,
        observed_at=TS,
    )


def _empty_candidates(manifest_digest: str) -> IssueCandidateDocument:
    return IssueCandidateDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        evidence_bundle_digest=manifest_digest,
        candidates=[],
    )


def _append_event(path: Path, event: ChangeIssueEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write_ledger(change_dir: Path, events: list[ChangeIssueEvent]) -> None:
    path = change_dir / LEDGER_SOURCE
    if path.exists():
        path.unlink()
    for event in events:
        _append_event(path, event)


def _write_snapshot_from_events(change_dir: Path, events: list[ChangeIssueEvent]) -> None:
    snapshot = project_change_issues(tuple(events))
    path = change_dir / SNAPSHOT_SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_projection(snapshot))


def _write_reconcile_status(
    change_dir: Path,
    *,
    schema_version: str,
    status: str,
    evidence_bundle_digest: str,
    candidate_digest: str,
    occurrence_count: int | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "status": status,
        "evidence_bundle_digest": evidence_bundle_digest,
        "candidate_digest": candidate_digest,
    }
    if occurrence_count is not None:
        payload["occurrence_count"] = occurrence_count
    if error is not None:
        payload["error"] = error
    _write_json(change_dir / RECONCILE_SOURCE, payload)


def _seed_manifest_tree(change_dir: Path) -> tuple[str, IssueCandidateDocument, Observation]:
    """Write execution anchor + secret/utf8/binary entries and a matching manifest."""
    anchor_bytes = _write_execution_anchor(change_dir)
    secret = b'{"access_token":"abcdefghijklmnop"}'
    utf8 = "visible-utf8-内容\n".encode()
    binary = bytes(range(256))
    _write_bytes(change_dir / "execution/runs/secret.json", secret)
    _write_bytes(change_dir / "execution/runs/visible.txt", utf8)
    _write_bytes(change_dir / EXTRA_ENTRY, binary)

    entries = [
        IssueEvidenceManifestEntry(
            path=EXECUTION_ANCHOR,
            digest=evidence_entry_digest_v1(anchor_bytes),
        ),
        IssueEvidenceManifestEntry(
            path="execution/runs/secret.json",
            digest=evidence_entry_digest_v1(secret),
        ),
        IssueEvidenceManifestEntry(
            path="execution/runs/visible.txt",
            digest=evidence_entry_digest_v1(utf8),
        ),
        IssueEvidenceManifestEntry(
            path=EXTRA_ENTRY,
            digest=evidence_entry_digest_v1(binary),
        ),
    ]
    digest = evidence_bundle_digest_v1(entries)
    manifest = IssueEvidenceManifest(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        digest=digest,
        entries=entries,
    )
    _write_json(change_dir / MANIFEST_SOURCE, manifest.model_dump(mode="json"))
    candidates = _empty_candidates(digest)
    _write_json(change_dir / CANDIDATES_SOURCE, candidates.model_dump(mode="json"))
    observation = _make_observation()
    obs_doc = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[observation],
    )
    _write_json(change_dir / OBSERVATIONS_SOURCE, obs_doc.model_dump(mode="json"))
    return digest, candidates, observation


def _obs_recorded(observation: Observation, *, seq: int = 1) -> ObservationRecordedEvent:
    key = f"observation_recorded:{CHANGE_ID}:{BATCH_ID}:{observation.observation_id}"
    return ObservationRecordedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest="sha256:" + "e" * 64,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        type="observation_recorded",
        observation=observation,
    )


def _analysis_failed_event(
    *,
    evidence_digest: str,
    candidate_digest: str,
    candidate_count: int,
    seq: int,
) -> IssueAnalysisFailedEvent:
    status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        status="failed",
        evidence_bundle_digest=evidence_digest,
        candidate_count=candidate_count,
        reason="timeout",
        retryable=True,
        candidate_digest=candidate_digest,
    )
    key = f"issue_analysis_failed:{CHANGE_ID}:{BATCH_ID}:{evidence_digest}"
    return IssueAnalysisFailedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        type="issue_analysis_failed",
        analysis_status=status,
    )


def _analysis_completed_event(
    *,
    evidence_digest: str,
    candidate_digest: str,
    candidate_count: int,
    seq: int,
) -> IssueAnalysisCompletedEvent:
    status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        status="completed",
        evidence_bundle_digest=evidence_digest,
        candidate_count=candidate_count,
        candidate_digest=candidate_digest,
    )
    key = f"issue_analysis_completed:{CHANGE_ID}:{BATCH_ID}:{candidate_digest}"
    return IssueAnalysisCompletedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        type="issue_analysis_completed",
        analysis_status=status,
    )


def _pending_event(*, candidate_digest: str, evidence_digest: str, seq: int) -> ProjectSyncPendingEvent:
    key = f"project_sync_pending:{CHANGE_ID}:{BATCH_ID}:{candidate_digest}"
    return ProjectSyncPendingEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        type="project_sync_pending",
        candidate_digest=candidate_digest,
    )


def _source_map(result: Any) -> dict[str, TraceSource]:
    return {source.path: source for source in result.sources}


def _write_completed_authority_tree(change_dir: Path) -> tuple[str, str]:
    """Manifest→ledger prefix + completed snapshot + V2 completed reconcile."""
    digest, candidates, observation = _seed_manifest_tree(change_dir)
    c_digest = candidate_document_digest(candidates)
    events = [
        _obs_recorded(observation, seq=1),
        _analysis_completed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=0,
            seq=2,
        ),
    ]
    _write_ledger(change_dir, events)
    _write_snapshot_from_events(change_dir, events)
    _write_reconcile_status(
        change_dir,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest,
        candidate_digest=c_digest,
        occurrence_count=0,
    )
    return digest, c_digest


@pytest.fixture
def authority_tree(tmp_path: Path) -> Path:
    """Valid analysis-failed recovery tree (manifest→ledger prefix + failed snapshot)."""
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, candidates, observation = _seed_manifest_tree(change_dir)
    c_digest = candidate_document_digest(candidates)
    events = [
        _obs_recorded(observation, seq=1),
        _analysis_failed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=0,
            seq=2,
        ),
    ]
    _write_ledger(change_dir, events)
    _write_snapshot_from_events(change_dir, events)
    # Intentionally omit reconcile/project — analysis_failed must mask them.
    return change_dir


def mutate_failure(change_dir: Path, mutation: str) -> None:
    path = change_dir / FAILURE_SOURCE
    if mutation == "missing":
        path.unlink(missing_ok=True)
        return
    payload = _failure_payload()
    if mutation == "malformed":
        path.write_text("{not-json", encoding="utf-8")
        return
    if mutation == "wrong_change":
        payload["change_id"] = "CH-OTHER"
    elif mutation == "wrong_batch":
        payload["batch_id"] = "OTHER-BATCH"
    elif mutation == "wrong_source_batch":
        payload["source_batch_id"] = "OTHER-BATCH"
    elif mutation == "multi_identity":
        payload["change_id"] = "CH-OTHER"
        payload["batch_id"] = "OTHER-BATCH"
        payload["source_batch_id"] = "OTHER-SOURCE"
    else:
        raise AssertionError(mutation)
    _write_json(path, payload)


@pytest.mark.parametrize(
    ("mutation", "code", "detail"),
    [
        ("missing", "failure_analysis_missing", "reason=missing"),
        ("malformed", "failure_analysis_missing", "reason=malformed"),
        ("wrong_change", "failure_analysis_identity_mismatch", "reason=change_id_mismatch"),
        ("wrong_batch", "failure_analysis_identity_mismatch", "reason=batch_id_mismatch"),
        (
            "wrong_source_batch",
            "failure_analysis_identity_mismatch",
            "reason=source_batch_id_mismatch",
        ),
    ],
)
def test_failure_authority_has_one_canonical_gap(
    authority_tree: Path,
    mutation: str,
    code: str,
    detail: str,
) -> None:
    mutate_failure(authority_tree, mutation)
    result = validate_failure_authority(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.failures_by_case == {}
    assert [(gap.code, gap.detail) for gap in result.gaps] == [(code, detail)]
    assert len(result.gaps) == 1
    assert result.gaps[0].source == FAILURE_SOURCE
    assert result.gaps[0].batch_id == BATCH_ID


def test_failure_authority_identity_precedence(authority_tree: Path) -> None:
    mutate_failure(authority_tree, "multi_identity")
    result = validate_failure_authority(authority_tree, CHANGE_ID, BATCH_ID)
    assert [(gap.code, gap.detail) for gap in result.gaps] == [
        ("failure_analysis_identity_mismatch", "reason=change_id_mismatch")
    ]


def test_failure_authority_valid_groups_failures(authority_tree: Path) -> None:
    payload = _failure_payload(
        [
            {
                "case_id": "TC_B",
                "target": "api",
                "category": "locator_failure",
                "fix_proposal_eligible": False,
                "severity": "low",
                "evidence": {
                    "result_file": "execution/runs/x/api-result.json",
                    "test_file": "tests/api/test_dept.py",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "",
                },
                "diagnosis": "d",
                "recommended_action": "r",
            },
            {
                "case_id": "TC_A",
                "target": "api",
                "category": "environment_failure",
                "fix_proposal_eligible": False,
                "severity": "high",
                "evidence": {
                    "result_file": "execution/runs/x/api-result.json",
                    "test_file": "tests/api/test_dept.py",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "",
                },
                "diagnosis": "d",
                "recommended_action": "r",
            },
            {
                "case_id": "TC_A",
                "target": "api",
                "category": "assertion_failure",
                "fix_proposal_eligible": False,
                "severity": "medium",
                "evidence": {
                    "result_file": "execution/runs/x/api-result.json",
                    "test_file": "tests/api/test_dept.py",
                    "trace": "",
                    "screenshot": "",
                    "video": "",
                    "raw_log": "",
                    "log_excerpt": "",
                },
                "diagnosis": "d",
                "recommended_action": "r",
            },
        ]
    )
    _write_json(authority_tree / FAILURE_SOURCE, payload)
    result = validate_failure_authority(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.gaps == ()
    assert list(result.failures_by_case) == ["TC_A", "TC_B"]
    assert result.failures_by_case["TC_A"] == (
        TraceFailure(category="assertion_failure", severity="medium"),
        TraceFailure(category="environment_failure", severity="high"),
    )
    sources = _source_map(result)
    assert sources[FAILURE_SOURCE].exists is True
    assert sources[FAILURE_SOURCE].sha256 == raw_sha256((authority_tree / FAILURE_SOURCE).read_bytes())


def _load_manifest(change_dir: Path) -> dict[str, Any]:
    return json.loads((change_dir / MANIFEST_SOURCE).read_text(encoding="utf-8"))


def _save_manifest(change_dir: Path, payload: dict[str, Any]) -> None:
    _write_json(change_dir / MANIFEST_SOURCE, payload)


def mutate_prefix(change_dir: Path, mutation: str) -> None:
    if mutation == "manifest_missing":
        (change_dir / MANIFEST_SOURCE).unlink()
        return
    if mutation == "manifest_malformed":
        (change_dir / MANIFEST_SOURCE).write_text("{bad", encoding="utf-8")
        return
    if mutation == "manifest_wrong_change":
        payload = _load_manifest(change_dir)
        payload["change_id"] = "CH-OTHER"
        _save_manifest(change_dir, payload)
        return
    if mutation == "manifest_wrong_batch":
        payload = _load_manifest(change_dir)
        payload["batch_id"] = "OTHER"
        _save_manifest(change_dir, payload)
        return
    if mutation == "manifest_entry_path_invalid":
        payload = _load_manifest(change_dir)
        payload["entries"].append({"path": "../escape.txt", "digest": "sha256:" + "a" * 64})
        _save_manifest(change_dir, payload)
        return
    if mutation == "manifest_entry_duplicate":
        payload = _load_manifest(change_dir)
        payload["entries"].append(dict(payload["entries"][0]))
        # Keep declared digest in sync so duplicate wins over bundle mismatch.
        entries = [IssueEvidenceManifestEntry(path=e["path"], digest=e["digest"]) for e in payload["entries"]]
        # Duplicate path makes normalize/unique fail before rehash; leave digest as-is.
        _save_manifest(change_dir, payload)
        del entries
        return
    if mutation == "manifest_execution_anchor_missing":
        payload = _load_manifest(change_dir)
        payload["entries"] = [e for e in payload["entries"] if e["path"] != EXECUTION_ANCHOR]
        entries = [IssueEvidenceManifestEntry(path=e["path"], digest=e["digest"]) for e in payload["entries"]]
        payload["digest"] = evidence_bundle_digest_v1(entries)
        _save_manifest(change_dir, payload)
        return
    if mutation == "manifest_entry_digest_mismatch":
        payload = _load_manifest(change_dir)
        payload["entries"][1]["digest"] = "sha256:" + "f" * 64
        entries = [IssueEvidenceManifestEntry(path=e["path"], digest=e["digest"]) for e in payload["entries"]]
        payload["digest"] = evidence_bundle_digest_v1(entries)
        _save_manifest(change_dir, payload)
        return
    if mutation == "manifest_bundle_digest_mismatch":
        payload = _load_manifest(change_dir)
        payload["digest"] = "sha256:" + "c" * 64
        _save_manifest(change_dir, payload)
        return
    if mutation == "candidates_missing":
        (change_dir / CANDIDATES_SOURCE).unlink()
        return
    if mutation == "candidates_malformed":
        (change_dir / CANDIDATES_SOURCE).write_text("{bad", encoding="utf-8")
        return
    if mutation == "candidates_wrong_change":
        doc = json.loads((change_dir / CANDIDATES_SOURCE).read_text(encoding="utf-8"))
        doc["change_id"] = "CH-OTHER"
        _write_json(change_dir / CANDIDATES_SOURCE, doc)
        return
    if mutation == "candidates_wrong_batch":
        doc = json.loads((change_dir / CANDIDATES_SOURCE).read_text(encoding="utf-8"))
        doc["batch_id"] = "OTHER"
        _write_json(change_dir / CANDIDATES_SOURCE, doc)
        return
    if mutation == "candidates_evidence_mismatch":
        doc = json.loads((change_dir / CANDIDATES_SOURCE).read_text(encoding="utf-8"))
        doc["evidence_bundle_digest"] = "sha256:" + "d" * 64
        _write_json(change_dir / CANDIDATES_SOURCE, doc)
        return
    if mutation == "observations_missing":
        (change_dir / OBSERVATIONS_SOURCE).unlink()
        return
    if mutation == "observations_malformed":
        (change_dir / OBSERVATIONS_SOURCE).write_text("{bad", encoding="utf-8")
        return
    if mutation == "observations_wrong_change":
        doc = json.loads((change_dir / OBSERVATIONS_SOURCE).read_text(encoding="utf-8"))
        doc["change_id"] = "CH-OTHER"
        _write_json(change_dir / OBSERVATIONS_SOURCE, doc)
        return
    if mutation == "observations_wrong_batch":
        doc = json.loads((change_dir / OBSERVATIONS_SOURCE).read_text(encoding="utf-8"))
        doc["batch_id"] = "OTHER"
        _write_json(change_dir / OBSERVATIONS_SOURCE, doc)
        return
    if mutation == "observations_replay_mismatch":
        doc = json.loads((change_dir / OBSERVATIONS_SOURCE).read_text(encoding="utf-8"))
        doc["observations"] = []
        _write_json(change_dir / OBSERVATIONS_SOURCE, doc)
        return
    if mutation == "ledger_missing":
        (change_dir / LEDGER_SOURCE).unlink()
        return
    if mutation == "ledger_malformed":
        (change_dir / LEDGER_SOURCE).write_text("{bad\n", encoding="utf-8")
        return
    if mutation == "ledger_event_identity_mismatch":
        lines = (change_dir / LEDGER_SOURCE).read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["event_id"] = "EVT-deadbeefdeadbeef"
        lines[0] = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        (change_dir / LEDGER_SOURCE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    raise AssertionError(mutation)


@pytest.mark.parametrize("mutation", sorted(EXPECTED_SOURCE))
def test_prefix_unavailable_has_one_canonical_gap(authority_tree: Path, mutation: str) -> None:
    mutate_prefix(authority_tree, mutation)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert [(gap.code, gap.source, gap.batch_id, gap.detail) for gap in result.gaps] == [
        (
            "issue_reconciliation_unavailable",
            EXPECTED_SOURCE[mutation],
            BATCH_ID,
            f"reason={EXPECTED_REASON[mutation]}",
        )
    ]
    assert result.validated is None


def test_manifest_digest_tamper_rejects_even_with_downstream_rewritten(
    authority_tree: Path,
) -> None:
    payload = _load_manifest(authority_tree)
    payload["digest"] = "sha256:" + "e" * 64
    _save_manifest(authority_tree, payload)
    # Rewrite every downstream M reference to the tampered value.
    cand = json.loads((authority_tree / CANDIDATES_SOURCE).read_text(encoding="utf-8"))
    cand["evidence_bundle_digest"] = payload["digest"]
    _write_json(authority_tree / CANDIDATES_SOURCE, cand)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].detail == "reason=bundle_digest_mismatch"


def test_execution_anchor_rehash_and_entry_round_trips(authority_tree: Path) -> None:
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "analysis_failed"
    sources = _source_map(result)
    for rel in (
        EXECUTION_ANCHOR,
        "execution/runs/secret.json",
        "execution/runs/visible.txt",
        EXTRA_ENTRY,
    ):
        assert sources[rel].exists is True
        assert sources[rel].sha256 == raw_sha256((authority_tree / rel).read_bytes())


@pytest.mark.parametrize(
    ("source", "mutations", "expected_reason"),
    [
        (
            "manifest",
            ("manifest_wrong_change", "manifest_wrong_batch"),
            "change_id_mismatch",
        ),
        (
            "candidates",
            ("candidates_wrong_change", "candidates_evidence_mismatch"),
            "change_id_mismatch",
        ),
        (
            "observations",
            ("observations_wrong_batch", "observations_replay_mismatch"),
            "batch_id_mismatch",
        ),
        (
            "ledger",
            ("ledger_malformed",),  # single structural; identity covered elsewhere
            "ledger_malformed",
        ),
    ],
)
def test_source_local_precedence(
    authority_tree: Path,
    source: str,
    mutations: tuple[str, ...],
    expected_reason: str,
) -> None:
    del source
    for mutation in mutations:
        mutate_prefix(authority_tree, mutation)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].detail == f"reason={expected_reason}"


def test_snapshot_document_source_local_precedence(authority_tree: Path) -> None:
    """Snapshot document: change_id > authoritative_batch_id (> projection)."""
    snapshot = json.loads((authority_tree / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
    snapshot["change_id"] = "OTHER-CHANGE"
    snapshot["authoritative_batch_id"] = "OTHER-BATCH"
    # Also drift projection payload; change_id must still win.
    snapshot["observations"] = []
    _write_json(authority_tree / SNAPSHOT_SOURCE, snapshot)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert [(gap.code, gap.source, gap.detail) for gap in result.gaps] == [
        (
            "issues_snapshot_identity_mismatch",
            SNAPSHOT_SOURCE,
            "reason=change_id_mismatch",
        )
    ]


def test_snapshot_batch_precedes_projection_replay_mismatch(authority_tree: Path) -> None:
    snapshot = json.loads((authority_tree / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
    snapshot["authoritative_batch_id"] = "OTHER-BATCH"
    snapshot["observations"] = []
    _write_json(authority_tree / SNAPSHOT_SOURCE, snapshot)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].code == "issues_snapshot_identity_mismatch"
    assert result.gaps[0].detail == "reason=batch_id_mismatch"


def test_reconcile_status_source_local_precedence(tmp_path: Path) -> None:
    """Reconcile: change_id > evidence_digest_mismatch."""
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    _write_completed_authority_tree(change_dir)
    status = json.loads((change_dir / RECONCILE_SOURCE).read_text(encoding="utf-8"))
    status["change_id"] = "OTHER-CHANGE"
    status["evidence_bundle_digest"] = "sha256:" + "f" * 64
    _write_json(change_dir / RECONCILE_SOURCE, status)
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert [(gap.code, gap.source, gap.detail) for gap in result.gaps] == [
        (
            "issue_reconciliation_unavailable",
            RECONCILE_SOURCE,
            "reason=change_id_mismatch",
        )
    ]


def test_v1_reconcile_change_id_precedes_status_inconsistent(tmp_path: Path) -> None:
    """V1 must not short-circuit to status_inconsistent before identity checks."""
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, c_digest = _write_completed_authority_tree(change_dir)
    _write_reconcile_status(
        change_dir,
        schema_version="1.0",
        status="failed",
        evidence_bundle_digest=digest,
        candidate_digest=c_digest,
        error="conflict",
    )
    status = json.loads((change_dir / RECONCILE_SOURCE).read_text(encoding="utf-8"))
    status["change_id"] = "OTHER-CHANGE"
    _write_json(change_dir / RECONCILE_SOURCE, status)
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].source == RECONCILE_SOURCE
    assert result.gaps[0].code == "issue_reconciliation_unavailable"
    assert result.gaps[0].detail == "reason=change_id_mismatch"


def test_analysis_failed_missing_snapshot_uses_dedicated_code(authority_tree: Path) -> None:
    (authority_tree / SNAPSHOT_SOURCE).unlink()
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert [(gap.code, gap.source, gap.detail) for gap in result.gaps] == [
        ("issues_snapshot_missing", SNAPSHOT_SOURCE, "reason=missing")
    ]


def test_analysis_failed_snapshot_identity_uses_dedicated_code(authority_tree: Path) -> None:
    snapshot = json.loads((authority_tree / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
    snapshot["change_id"] = "OTHER-CHANGE"
    _write_json(authority_tree / SNAPSHOT_SOURCE, snapshot)
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert [(gap.code, gap.source, gap.detail) for gap in result.gaps] == [
        (
            "issues_snapshot_identity_mismatch",
            SNAPSHOT_SOURCE,
            "reason=change_id_mismatch",
        )
    ]


def test_cross_source_prefix_order_manifest_before_candidates(authority_tree: Path) -> None:
    mutate_prefix(authority_tree, "manifest_wrong_batch")
    mutate_prefix(authority_tree, "candidates_missing")
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.gaps[0].source == MANIFEST_SOURCE
    assert result.gaps[0].detail == "reason=batch_id_mismatch"


def test_cross_source_prefix_order_candidates_before_observations(authority_tree: Path) -> None:
    mutate_prefix(authority_tree, "candidates_evidence_mismatch")
    mutate_prefix(authority_tree, "observations_missing")
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.gaps[0].source == CANDIDATES_SOURCE


def test_cross_source_prefix_order_observations_before_ledger(authority_tree: Path) -> None:
    mutate_prefix(authority_tree, "observations_wrong_change")
    mutate_prefix(authority_tree, "ledger_missing")
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.gaps[0].source == OBSERVATIONS_SOURCE


def write_recovery_state(change_dir: Path, state: str) -> None:
    digest, candidates, observation = _seed_manifest_tree(change_dir)
    c_digest = candidate_document_digest(candidates)
    if state == "analysis_failed":
        events = [
            _obs_recorded(observation, seq=1),
            _analysis_failed_event(
                evidence_digest=digest,
                candidate_digest=c_digest,
                candidate_count=0,
                seq=2,
            ),
        ]
        _write_ledger(change_dir, events)
        _write_snapshot_from_events(change_dir, events)
        return
    if state == "reconcile_failed":
        events = [
            _obs_recorded(observation, seq=1),
            _analysis_completed_event(
                evidence_digest=digest,
                candidate_digest=c_digest,
                candidate_count=0,
                seq=2,
            ),
        ]
        _write_ledger(change_dir, events)
        # Stale/missing snapshot must be masked by reconcile_failed.
        (change_dir / SNAPSHOT_SOURCE).unlink(missing_ok=True)
        _write_reconcile_status(
            change_dir,
            schema_version="2.0",
            status="failed",
            evidence_bundle_digest=digest,
            candidate_digest=c_digest,
            error="conflict",
        )
        return
    if state == "project_sync_pending":
        events = [
            _obs_recorded(observation, seq=1),
            _analysis_completed_event(
                evidence_digest=digest,
                candidate_digest=c_digest,
                candidate_count=0,
                seq=2,
            ),
            _pending_event(candidate_digest=c_digest, evidence_digest=digest, seq=3),
        ]
        _write_ledger(change_dir, events)
        # Persist a snapshot with wrong authoritative batch — pending must mask it.
        snapshot = project_change_issues(tuple(events))  # type: ignore[arg-type]
        bad = snapshot.model_copy(update={"authoritative_batch_id": "OTHER-BATCH"})
        (change_dir / SNAPSHOT_SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (change_dir / SNAPSHOT_SOURCE).write_bytes(dump_projection(bad))
        _write_reconcile_status(
            change_dir,
            schema_version="2.0",
            status="pending",
            evidence_bundle_digest=digest,
            candidate_digest=c_digest,
        )
        return
    raise AssertionError(state)


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("analysis_failed", "issue_analysis_failed"),
        ("reconcile_failed", "issue_reconcile_failed"),
        ("project_sync_pending", "project_sync_pending"),
    ],
)
def test_recovery_prefix_returns_one_blocking_state(
    tmp_path: Path,
    state: str,
    expected_code: str,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    write_recovery_state(change_dir, state)
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == state
    assert [gap.code for gap in result.gaps] == [expected_code]
    assert result.validated is None


def test_analysis_failed_candidate_count_mismatch_is_unavailable(authority_tree: Path) -> None:
    snapshot = json.loads((authority_tree / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
    snapshot["analysis_status"]["candidate_count"] = 99
    # Keep ledger/event in sync for replay equality so count mismatch is the deciding fault.
    events = []
    for line in (authority_tree / LEDGER_SOURCE).read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "issue_analysis_failed":
            event["analysis_status"]["candidate_count"] = 99
        events.append(event)
    (authority_tree / LEDGER_SOURCE).write_text(
        "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for e in events)
        + "\n",
        encoding="utf-8",
    )
    _write_snapshot_from_events(
        authority_tree,
        [
            IssueAnalysisFailedEvent.model_validate(e)
            if e.get("type") == "issue_analysis_failed"
            else ObservationRecordedEvent.model_validate(e)
            for e in events
        ],
    )
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].code == "issue_reconciliation_unavailable"
    assert result.gaps[0].detail == "reason=candidate_count_mismatch"


def test_analysis_failed_masks_missing_reconcile_and_project(authority_tree: Path) -> None:
    assert not (authority_tree / RECONCILE_SOURCE).exists()
    assert not (_project_root(authority_tree) / PROJECT_LEDGER_SOURCE).exists()
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "analysis_failed"
    assert [gap.code for gap in result.gaps] == ["issue_analysis_failed"]
    sources = _source_map(result)
    assert sources[RECONCILE_SOURCE].exists is False
    assert sources[PROJECT_LEDGER_SOURCE].exists is False
    assert sources[PROJECT_PROBLEMS_SOURCE].exists is False


def test_reconcile_failed_masks_missing_snapshot_and_project(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    write_recovery_state(change_dir, "reconcile_failed")
    assert not (change_dir / SNAPSHOT_SOURCE).exists()
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "reconcile_failed"
    sources = _source_map(result)
    assert sources[SNAPSHOT_SOURCE].exists is False
    assert sources[PROJECT_LEDGER_SOURCE].exists is False


def test_pending_masks_snapshot_batch_mismatch_and_project(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    write_recovery_state(change_dir, "project_sync_pending")
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "project_sync_pending"
    sources = _source_map(result)
    assert sources[SNAPSHOT_SOURCE].exists is True
    assert sources[PROJECT_LEDGER_SOURCE].exists is False


def test_malformed_manifest_never_masked_by_recovery(authority_tree: Path) -> None:
    mutate_prefix(authority_tree, "manifest_malformed")
    result = validate_issue_authority_prefix(authority_tree, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].source == MANIFEST_SOURCE
    assert result.gaps[0].detail == "reason=malformed"


def test_v1_reconcile_status_is_unavailable(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, candidates, observation = _seed_manifest_tree(change_dir)
    c_digest = candidate_document_digest(candidates)
    events = [
        _obs_recorded(observation, seq=1),
        _analysis_completed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=0,
            seq=2,
        ),
    ]
    _write_ledger(change_dir, events)
    _write_snapshot_from_events(change_dir, events)
    _write_reconcile_status(
        change_dir,
        schema_version="1.0",
        status="failed",
        evidence_bundle_digest=digest,
        candidate_digest=c_digest,
        error="conflict",
    )
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].source == RECONCILE_SOURCE
    assert result.gaps[0].code == "issue_reconciliation_unavailable"


def test_pending_without_recovery_event_is_unavailable(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, candidates, observation = _seed_manifest_tree(change_dir)
    c_digest = candidate_document_digest(candidates)
    events = [
        _obs_recorded(observation, seq=1),
        _analysis_completed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=0,
            seq=2,
        ),
    ]
    _write_ledger(change_dir, events)
    _write_snapshot_from_events(change_dir, events)
    _write_reconcile_status(
        change_dir,
        schema_version="2.0",
        status="pending",
        evidence_bundle_digest=digest,
        candidate_digest=c_digest,
    )
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "unavailable"
    assert result.gaps[0].detail == "reason=recovery_event_missing"


def test_completed_prefix_returns_validated(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, c_digest = _write_completed_authority_tree(change_dir)
    result = validate_issue_authority_prefix(change_dir, CHANGE_ID, BATCH_ID)
    assert result.state == "completed"
    assert result.gaps == ()
    assert result.validated is not None
    assert result.validated.manifest_digest == digest
    assert result.validated.candidate_digest == c_digest


def test_new_authority_gap_codes_are_verify_blocking() -> None:
    for code in (
        "failure_analysis_identity_mismatch",
        "issues_snapshot_identity_mismatch",
        "issue_analysis_failed",
        "project_sync_pending",
        "issue_reconcile_failed",
        "issue_reconciliation_unavailable",
    ):
        assert code in VERIFY_BLOCKING_GAP_CODES


# ---------------------------------------------------------------------------
# Task 10 — completed occurrence set + project membership
# ---------------------------------------------------------------------------


def _change_dir(project_root: Path) -> Path:
    return project_root / "qa" / "changes" / CHANGE_ID


def _make_candidate(observation: Observation) -> IssueCandidate:
    return IssueCandidate(
        candidate_id="CAND-001",
        observation_ids=[observation.observation_id],
        proposed=IssueCandidateProposed(
            title="API endpoint returns 500",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="Unhandled exception in endpoint handler",
        ),
        affected_surface=AffectedSurface(kind="endpoint", value="GET /api/v1/dept"),
        fingerprint_inputs=FingerprintInputs(
            surface="endpoint",
            symptom="returns http 500",
            qualifiers=None,
        ),
        possible_problem_ids=[],
        confidence=0.85,
        recommended_action="investigate and fix",
    )


def _append_problem_event(path: Path, event: ProblemEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write_project_ledger(project_root: Path, events: Sequence[ProblemEvent]) -> None:
    path = project_root / PROJECT_LEDGER_SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.write_text("", encoding="utf-8")
    for event in events:
        _append_problem_event(path, event)


def _write_problems_projection(project_root: Path, events: Sequence[ProblemEvent]) -> None:
    projection = project_problems(tuple(events))
    path = project_root / PROJECT_PROBLEMS_SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_projection(projection))


def _occurrence_and_problem_events(
    *,
    candidate: IssueCandidate,
    evidence_digest: str,
    seq: int,
) -> tuple[OccurrenceDetectedEvent, ProblemDetectedEvent, str, str]:
    digest = per_candidate_digest(candidate)
    occ_id = compute_occurrence_id(CHANGE_ID, BATCH_ID, digest)
    fp = problem_fingerprint(
        affected_surface=candidate.affected_surface,
        fingerprint_inputs=candidate.fingerprint_inputs,
    )
    pid = compute_problem_id(fp)
    occurrence = IssueOccurrence(
        occurrence_id=occ_id,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observation_ids=list(candidate.observation_ids),
        problem_id=pid,
        provisional_assessment=ProvisionalAssessment(
            classification=candidate.proposed.classification,
            severity=candidate.proposed.severity,
            authority="llm_provisional",
            root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest=evidence_digest,
            analyzer="aa-issue-analyzer",
            prompt_version="1.0",
            candidate_digest=digest,
        ),
    )
    occ_key = f"occurrence_detected:{CHANGE_ID}:{BATCH_ID}:{digest}"
    occ_event = OccurrenceDetectedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(occ_key),
        idempotency_key=occ_key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        type="occurrence_detected",
        occurrence=occurrence,
    )
    det_key = f"problem_detected:{pid}:{CHANGE_ID}:{BATCH_ID}:{digest}"
    problem_event = ProblemDetectedEvent(
        schema_version="1.0",
        seq=1,
        event_id=event_id(det_key),
        idempotency_key=det_key,
        ts=TS,
        evidence_digest=evidence_digest,
        problem_id=pid,
        expected_problem_version=0,
        type="problem_detected",
        occurrence_id=occ_id,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        fingerprint=fp,
        title=candidate.proposed.title,
        classification=candidate.proposed.classification,
        severity=candidate.proposed.severity,
        root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
    )
    return occ_event, problem_event, occ_id, pid


def _write_completed_with_occurrence(
    project_root: Path,
    *,
    omit_optional_qualifiers: bool = False,
) -> Path:
    """Valid completed tree: one candidate, one occurrence, matching project membership."""
    change_dir = _change_dir(project_root)
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    digest, _empty, observation = _seed_manifest_tree(change_dir)
    candidate = _make_candidate(observation)
    candidates = IssueCandidateDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        evidence_bundle_digest=digest,
        candidates=[candidate],
    )
    authored_candidates = candidates.model_dump(mode="json")
    if omit_optional_qualifiers:
        del authored_candidates["candidates"][0]["fingerprint_inputs"]["qualifiers"]
    _write_json(change_dir / CANDIDATES_SOURCE, authored_candidates)
    c_digest = candidate_document_digest(authored_candidates)
    occ_event, problem_event, _occ_id, _pid = _occurrence_and_problem_events(
        candidate=candidate,
        evidence_digest=digest,
        seq=3,
    )
    events: list[ChangeIssueEvent] = [
        _obs_recorded(observation, seq=1),
        _analysis_completed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=1,
            seq=2,
        ),
        occ_event,
    ]
    _write_ledger(change_dir, events)
    _write_snapshot_from_events(change_dir, events)
    _write_reconcile_status(
        change_dir,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest,
        candidate_digest=c_digest,
        occurrence_count=1,
    )
    problem_events = [problem_event]
    _write_project_ledger(project_root, problem_events)
    _write_problems_projection(project_root, problem_events)
    return project_root


def _write_completed_genesis_empty(project_root: Path) -> Path:
    """Completed empty-candidate batch with true project genesis (both project files absent)."""
    change_dir = _change_dir(project_root)
    change_dir.mkdir(parents=True)
    _write_json(change_dir / FAILURE_SOURCE, _failure_payload())
    _write_completed_authority_tree(change_dir)
    assert not (project_root / PROJECT_LEDGER_SOURCE).exists()
    assert not (project_root / PROJECT_PROBLEMS_SOURCE).exists()
    return project_root


def completed_prefix(project_root: Path) -> ValidatedAuthorityPrefix:
    result = validate_issue_authority_prefix(_change_dir(project_root), CHANGE_ID, BATCH_ID)
    assert result.state == "completed"
    assert result.validated is not None
    return result.validated


@pytest.fixture
def completed_tree(tmp_path: Path) -> Path:
    return _write_completed_with_occurrence(tmp_path)


def mutate_completed_tree(project_root: Path, mutation: str) -> None:
    change_dir = _change_dir(project_root)
    if mutation == "candidate_count":
        # Taint only the frozen prefix path via snapshot analysis count after prefix load.
        # Disk mutation kept for project-side helpers; count taint applied in test via replace.
        return
    if mutation == "occurrence_count":
        status_path = change_dir / RECONCILE_SOURCE
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        payload["occurrence_count"] = int(payload["occurrence_count"]) + 1
        _write_json(status_path, payload)
        return
    if mutation == "missing_occurrence":
        events = [
            _obs_recorded(_make_observation(), seq=1),
            _analysis_completed_event(
                evidence_digest=json.loads((change_dir / CANDIDATES_SOURCE).read_text())[
                    "evidence_bundle_digest"
                ],
                candidate_digest=json.loads((change_dir / RECONCILE_SOURCE).read_text())["candidate_digest"],
                candidate_count=1,
                seq=2,
            ),
        ]
        # Keep counts at 1 but drop the occurrence event from the ledger/snapshot.
        _write_ledger(change_dir, events)
        _write_snapshot_from_events(change_dir, events)
        return
    if mutation == "duplicate_occurrence":
        snapshot = json.loads((change_dir / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        occ = snapshot["occurrences"][0]
        snapshot["occurrences"] = [occ, occ]
        _write_json(change_dir / SNAPSHOT_SOURCE, snapshot)
        return
    if mutation == "wrong_occurrence_identity":
        snapshot = json.loads((change_dir / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        snapshot["occurrences"][0]["analysis"]["candidate_digest"] = "sha256:" + "f" * 64
        _write_json(change_dir / SNAPSHOT_SOURCE, snapshot)
        return
    if mutation == "dangling_observation":
        snapshot = json.loads((change_dir / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        snapshot["occurrences"][0]["observation_ids"] = ["OBS-missing-dangling"]
        _write_json(change_dir / SNAPSHOT_SOURCE, snapshot)
        return
    if mutation in {"additional_occurrence", "wrong_problem_id", "wrong_evidence_digest"}:
        # Change-side checks use tainted prefix; disk left valid for project replay.
        return
    raise AssertionError(mutation)


def _taint_prefix(prefix: ValidatedAuthorityPrefix, mutation: str) -> ValidatedAuthorityPrefix:
    """Apply change-side mutations that must be visible through the frozen prefix."""
    if mutation == "candidate_count":
        status = prefix.replayed_snapshot.analysis_status
        assert status is not None
        tainted_status = status.model_copy(update={"candidate_count": status.candidate_count + 1})
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"analysis_status": tainted_status})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "occurrence_count":
        tainted_status = prefix.reconcile_status.model_copy(
            update={"occurrence_count": int(prefix.reconcile_status.occurrence_count or 0) + 1}
        )
        return replace(prefix, reconcile_status=tainted_status)
    if mutation == "missing_occurrence":
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": []})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "duplicate_occurrence":
        occ = prefix.replayed_snapshot.occurrences[0]
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [occ, occ]})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "wrong_occurrence_identity":
        occ = prefix.replayed_snapshot.occurrences[0]
        tainted_occ = occ.model_copy(
            update={"analysis": occ.analysis.model_copy(update={"candidate_digest": "sha256:" + "f" * 64})}
        )
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [tainted_occ]})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "dangling_observation":
        # Keep occurrence.observation_ids aligned with the candidate, but drop the observation.
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"observations": []})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "additional_occurrence":
        occ = prefix.replayed_snapshot.occurrences[0]
        extra = occ.model_copy(
            update={
                "occurrence_id": compute_occurrence_id(CHANGE_ID, BATCH_ID, "sha256:" + "b" * 64),
                "analysis": occ.analysis.model_copy(update={"candidate_digest": "sha256:" + "b" * 64}),
            }
        )
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [occ, extra]})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "wrong_problem_id":
        occ = prefix.replayed_snapshot.occurrences[0]
        tainted_occ = occ.model_copy(update={"problem_id": "PROB-wrong-problem-id"})
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [tainted_occ]})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    if mutation == "wrong_evidence_digest":
        occ = prefix.replayed_snapshot.occurrences[0]
        tainted_occ = occ.model_copy(
            update={
                "analysis": occ.analysis.model_copy(update={"evidence_bundle_digest": "sha256:" + "e" * 64})
            }
        )
        tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [tainted_occ]})
        return replace(prefix, replayed_snapshot=tainted_snapshot)
    raise AssertionError(mutation)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("candidate_count", "candidate_count_mismatch"),
        ("occurrence_count", "occurrence_count_mismatch"),
        ("missing_occurrence", "occurrence_set_mismatch"),
        ("duplicate_occurrence", "occurrence_set_mismatch"),
        ("wrong_occurrence_identity", "occurrence_identity_mismatch"),
        ("additional_occurrence", "occurrence_set_mismatch"),
        ("wrong_problem_id", "occurrence_identity_mismatch"),
        ("wrong_evidence_digest", "occurrence_identity_mismatch"),
        ("dangling_observation", "observation_reference_invalid"),
    ],
)
def test_completed_authority_rejects_current_set_mutations(
    completed_tree: Path,
    mutation: str,
    reason: str,
) -> None:
    prefix = completed_prefix(completed_tree)
    mutate_completed_tree(completed_tree, mutation)
    tainted = _taint_prefix(prefix, mutation)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(
            tainted,
            completed_tree,
            CHANGE_ID,
            BATCH_ID,
        )
    assert raised.value.reason == reason


def test_completed_authority_rejects_count_equal_but_different_occurrence_set(
    completed_tree: Path,
) -> None:
    prefix = completed_prefix(completed_tree)
    occ = prefix.replayed_snapshot.occurrences[0]
    other = occ.model_copy(
        update={
            "occurrence_id": compute_occurrence_id(CHANGE_ID, BATCH_ID, "sha256:" + "a" * 64),
            "analysis": occ.analysis.model_copy(update={"candidate_digest": "sha256:" + "a" * 64}),
        }
    )
    tainted_snapshot = prefix.replayed_snapshot.model_copy(update={"occurrences": [other]})
    tainted = replace(prefix, replayed_snapshot=tainted_snapshot)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(tainted, completed_tree, CHANGE_ID, BATCH_ID)
    assert raised.value.reason == "occurrence_set_mismatch"


def test_completed_genesis_empty_candidate_batch_passes(tmp_path: Path) -> None:
    project_root = _write_completed_genesis_empty(tmp_path)
    prefix = completed_prefix(project_root)
    completed = validate_completed_authority(prefix, project_root, CHANGE_ID, BATCH_ID)
    assert completed.expected_occurrence_ids == frozenset()
    assert completed.replayed_problems == ProblemProjection(
        schema_version="1.0",
        problems=[],
        generated_at="1970-01-01T00:00:00Z",
    )
    assert completed.problem_events == ()


def test_completed_authority_preserves_authored_candidate_digest_when_optional_field_omitted(
    tmp_path: Path,
) -> None:
    project_root = _write_completed_with_occurrence(tmp_path, omit_optional_qualifiers=True)

    prefix = completed_prefix(project_root)
    completed = validate_completed_authority(prefix, project_root, CHANGE_ID, BATCH_ID)

    authored = json.loads((_change_dir(project_root) / CANDIDATES_SOURCE).read_text(encoding="utf-8"))
    assert prefix.candidate_digest == candidate_document_digest(authored)
    assert completed.expected_occurrence_ids


@pytest.mark.parametrize(
    ("events_state", "problems_state", "with_occurrence", "source", "reason"),
    [
        ("missing", "present", False, PROJECT_LEDGER_SOURCE, "ledger_missing"),
        ("present", "missing", False, PROJECT_PROBLEMS_SOURCE, "missing"),
        ("missing", "missing", True, PROJECT_LEDGER_SOURCE, "ledger_missing"),
        ("malformed", "present", True, PROJECT_LEDGER_SOURCE, "ledger_malformed"),
        ("present", "malformed", True, PROJECT_PROBLEMS_SOURCE, "malformed"),
        ("present", "replay_mismatch", True, PROJECT_PROBLEMS_SOURCE, "projection_replay_mismatch"),
        ("present", "missing_membership", True, PROJECT_PROBLEMS_SOURCE, "problem_occurrence_mismatch"),
        ("event_identity", "present", True, PROJECT_LEDGER_SOURCE, "event_identity_mismatch"),
    ],
)
def test_completed_authority_rejects_project_and_genesis_faults(
    tmp_path: Path,
    events_state: str,
    problems_state: str,
    with_occurrence: bool,
    source: str,
    reason: str,
) -> None:
    if with_occurrence:
        project_root = _write_completed_with_occurrence(tmp_path)
    else:
        project_root = _write_completed_genesis_empty(tmp_path)
        # Seed a valid project pair so asymmetric missing/present cases are reachable.
        _write_project_ledger(project_root, [])
        _write_problems_projection(project_root, [])

    events_path = project_root / PROJECT_LEDGER_SOURCE
    problems_path = project_root / PROJECT_PROBLEMS_SOURCE

    if events_state == "missing":
        events_path.unlink(missing_ok=True)
    elif events_state == "malformed":
        events_path.write_text("{not-jsonl\n", encoding="utf-8")
    elif events_state == "event_identity":
        raw = events_path.read_text(encoding="utf-8").strip().splitlines()
        payload = json.loads(raw[0])
        payload["event_id"] = "EVT-forged000000001"
        events_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if problems_state == "missing":
        problems_path.unlink(missing_ok=True)
    elif problems_state == "malformed":
        problems_path.write_text("{not-json", encoding="utf-8")
    elif problems_state == "replay_mismatch":
        payload = json.loads(problems_path.read_text(encoding="utf-8"))
        payload["generated_at"] = "1999-01-01T00:00:00Z"
        _write_json(problems_path, payload)
    elif problems_state == "missing_membership":
        # Keep ledger↔projection equality, but point membership at a non-current OCC.
        raw_line = events_path.read_text(encoding="utf-8").strip().splitlines()[0]
        event_payload = json.loads(raw_line)
        event_payload["occurrence_id"] = "OCC-not-current"
        events_path.write_text(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rewritten = ProblemDetectedEvent.model_validate(event_payload)
        _write_problems_projection(project_root, [rewritten])
    elif problems_state == "present" and events_state == "missing" and not with_occurrence:
        # asymmetric: keep problems, drop events (already dropped above)
        if not problems_path.is_file():
            _write_problems_projection(project_root, [])

    prefix = completed_prefix(project_root)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(prefix, project_root, CHANGE_ID, BATCH_ID)
    assert raised.value.source == source
    assert raised.value.reason == reason


def test_completed_authority_accepts_valid_occurrence_membership(completed_tree: Path) -> None:
    prefix = completed_prefix(completed_tree)
    completed = validate_completed_authority(prefix, completed_tree, CHANGE_ID, BATCH_ID)
    assert len(completed.expected_occurrence_ids) == 1
    assert len(completed.problem_events) == 1
    assert len(completed.replayed_problems.problems) == 1
    assert completed.prefix is prefix


def _load_project_detected_event(project_root: Path) -> ProblemDetectedEvent:
    events_path = project_root / PROJECT_LEDGER_SOURCE
    raw_line = events_path.read_text(encoding="utf-8").strip().splitlines()[0]
    return ProblemDetectedEvent.model_validate(json.loads(raw_line))


def _evidence_refs_digest(evidence_refs: Sequence[str]) -> str:
    canonical = json.dumps(sorted(evidence_refs), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _problem_merged_event(
    *,
    problem_id: str,
    target_problem_id: str,
    evidence_refs: Sequence[str],
    expected_problem_version: int = 1,
    seq: int = 2,
) -> ProblemMergedEvent:
    evidence_digest = _evidence_refs_digest(evidence_refs)
    merge_key = f"review:merge:{problem_id}:{expected_problem_version}:{target_problem_id}:{evidence_digest}"
    return ProblemMergedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(merge_key),
        idempotency_key=merge_key,
        ts=TS,
        evidence_digest=evidence_digest,
        problem_id=problem_id,
        expected_problem_version=expected_problem_version,
        type="problem_merged",
        target_problem_id=target_problem_id,
        reason="test merge",
        evidence_refs=list(evidence_refs),
        resolved_at=TS,
    )


def test_completed_authority_rejects_invalid_alias_chain(completed_tree: Path) -> None:
    prefix = completed_prefix(completed_tree)
    occ_id = prefix.replayed_snapshot.occurrences[0].occurrence_id
    pid = prefix.replayed_snapshot.occurrences[0].problem_id
    detected = _load_project_detected_event(completed_tree)
    merge_event = _problem_merged_event(
        problem_id=pid,
        target_problem_id="PROB-missing-target",
        evidence_refs=[occ_id],
    )
    problem_events: list[ProblemEvent] = [detected, merge_event]
    _write_project_ledger(completed_tree, problem_events)
    _write_problems_projection(completed_tree, problem_events)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(prefix, completed_tree, CHANGE_ID, BATCH_ID)
    assert raised.value.source == PROJECT_PROBLEMS_SOURCE
    assert raised.value.reason == "problem_occurrence_mismatch"


def test_completed_authority_rejects_duplicate_project_membership(completed_tree: Path) -> None:
    prefix = completed_prefix(completed_tree)
    occ_id = prefix.replayed_snapshot.occurrences[0].occurrence_id
    pid = prefix.replayed_snapshot.occurrences[0].problem_id
    detected = _load_project_detected_event(completed_tree)
    link_key = f"problem_occurrence_linked:{pid}:{occ_id}:dup"
    link_event = ProblemOccurrenceLinkedEvent(
        schema_version="1.0",
        seq=2,
        event_id=event_id(link_key),
        idempotency_key=link_key,
        ts=TS,
        evidence_digest=prefix.manifest_digest,
        problem_id=pid,
        expected_problem_version=1,
        type="problem_occurrence_linked",
        occurrence_id=occ_id,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
    )
    problem_events: list[ProblemEvent] = [detected, link_event]
    _write_project_ledger(completed_tree, problem_events)
    _write_problems_projection(completed_tree, problem_events)
    with pytest.raises(AuthorityValidationError) as raised:
        validate_completed_authority(prefix, completed_tree, CHANGE_ID, BATCH_ID)
    assert raised.value.source == PROJECT_PROBLEMS_SOURCE
    assert raised.value.reason == "problem_occurrence_mismatch"


# ---------------------------------------------------------------------------
# Task 11 — historical cross-ledger links + merge ownership
# ---------------------------------------------------------------------------

BATCH_B0 = "20260728-120000"
CASE_API = "TC_API_001"


def change_dir(project_root: Path) -> Path:
    return _change_dir(project_root)


def _make_observation_for(
    *,
    batch_id: str,
    case_id: str = CASE_ID,
    signature: str | None = None,
) -> Observation:
    source = ObservationSource(
        artifact="execution/runs/x/api-result.json",
        json_pointer="/cases/0",
    )
    sig = signature or f"GET /api/v1/dept returned HTTP 500 ({batch_id}:{case_id})"
    obs_id = observation_id(
        ObservationIdentityInput(
            change_id=CHANGE_ID,
            batch_id=batch_id,
            kind="test_failure",
            target="api",
            case_id=case_id,
            source_artifact=source.artifact,
            source_json_pointer=source.json_pointer,
            signature=sig,
        )
    )
    return Observation(
        observation_id=obs_id,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        kind="test_failure",
        target="api",
        case_id=case_id,
        source=source,
        evidence_refs=["execution/runs/x/api-result.json"],
        signature=sig,
        observed_at=TS,
    )


def _obs_recorded_for(observation: Observation, *, seq: int) -> ObservationRecordedEvent:
    key = f"observation_recorded:{observation.change_id}:{observation.batch_id}:{observation.observation_id}"
    return ObservationRecordedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest="sha256:" + "e" * 64,
        change_id=observation.change_id,
        batch_id=observation.batch_id,
        type="observation_recorded",
        observation=observation,
    )


def _analysis_completed_for(
    *,
    batch_id: str,
    evidence_digest: str,
    candidate_digest: str,
    candidate_count: int,
    seq: int,
) -> IssueAnalysisCompletedEvent:
    status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=evidence_digest,
        candidate_count=candidate_count,
        candidate_digest=candidate_digest,
    )
    key = f"issue_analysis_completed:{CHANGE_ID}:{batch_id}:{candidate_digest}"
    return IssueAnalysisCompletedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(key),
        idempotency_key=key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        type="issue_analysis_completed",
        analysis_status=status,
    )


def _candidate_for(
    observation: Observation,
    *,
    candidate_id: str = "CAND-001",
    symptom: str = "returns http 500",
    surface_value: str = "GET /api/v1/dept",
) -> IssueCandidate:
    return IssueCandidate(
        candidate_id=candidate_id,
        observation_ids=[observation.observation_id],
        proposed=IssueCandidateProposed(
            title="API endpoint returns 500",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="Unhandled exception in endpoint handler",
        ),
        affected_surface=AffectedSurface(kind="endpoint", value=surface_value),
        fingerprint_inputs=FingerprintInputs(
            surface="endpoint",
            symptom=symptom,
            qualifiers=None,
        ),
        possible_problem_ids=[],
        confidence=0.85,
        recommended_action="investigate and fix",
    )


def _occurrence_event_for(
    *,
    candidate: IssueCandidate,
    batch_id: str,
    evidence_digest: str,
    seq: int,
) -> tuple[OccurrenceDetectedEvent, str, str, str]:
    digest = per_candidate_digest(candidate)
    occ_id = compute_occurrence_id(CHANGE_ID, batch_id, digest)
    fp = problem_fingerprint(
        affected_surface=candidate.affected_surface,
        fingerprint_inputs=candidate.fingerprint_inputs,
    )
    pid = compute_problem_id(fp)
    occurrence = IssueOccurrence(
        occurrence_id=occ_id,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        observation_ids=list(candidate.observation_ids),
        problem_id=pid,
        provisional_assessment=ProvisionalAssessment(
            classification=candidate.proposed.classification,
            severity=candidate.proposed.severity,
            authority="llm_provisional",
            root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest=evidence_digest,
            analyzer="aa-issue-analyzer",
            prompt_version="1.0",
            candidate_digest=digest,
        ),
    )
    occ_key = f"occurrence_detected:{CHANGE_ID}:{batch_id}:{digest}"
    occ_event = OccurrenceDetectedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(occ_key),
        idempotency_key=occ_key,
        ts=TS,
        evidence_digest=evidence_digest,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        type="occurrence_detected",
        occurrence=occurrence,
    )
    return occ_event, occ_id, pid, digest


def _problem_detected_for(
    *,
    candidate: IssueCandidate,
    occ_id: str,
    pid: str,
    batch_id: str,
    evidence_digest: str,
    seq: int,
) -> ProblemDetectedEvent:
    digest = per_candidate_digest(candidate)
    fp = problem_fingerprint(
        affected_surface=candidate.affected_surface,
        fingerprint_inputs=candidate.fingerprint_inputs,
    )
    det_key = f"problem_detected:{pid}:{CHANGE_ID}:{batch_id}:{digest}"
    return ProblemDetectedEvent(
        schema_version="1.0",
        seq=seq,
        event_id=event_id(det_key),
        idempotency_key=det_key,
        ts=TS,
        evidence_digest=evidence_digest,
        problem_id=pid,
        expected_problem_version=0,
        type="problem_detected",
        occurrence_id=occ_id,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        fingerprint=fp,
        title=candidate.proposed.title,
        classification=candidate.proposed.classification,
        severity=candidate.proposed.severity,
        root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
    )


def _read_change_events(change_dir: Path) -> list[ChangeIssueEvent]:
    from assurance_agent.evidence.issue_replay import read_change_issue_events_from_bytes

    return list(read_change_issue_events_from_bytes((change_dir / LEDGER_SOURCE).read_bytes()))


def _read_problem_events(project_root: Path) -> list[ProblemEvent]:
    from assurance_agent.evidence.issue_replay import read_problem_events_from_bytes

    return list(read_problem_events_from_bytes((project_root / PROJECT_LEDGER_SOURCE).read_bytes()))


def make_each_ledger_individually_replayable(project_root: Path) -> dict[str, str]:
    """B0 historical OCC + B1 empty completed current batch; both ledgers replay cleanly."""
    change = _change_dir(project_root)
    change.mkdir(parents=True, exist_ok=True)
    _write_json(change / FAILURE_SOURCE, _failure_payload())
    digest_b1, _empty, _ = _seed_manifest_tree(change)
    # Current B1 observations document is empty (no B1 observations in ledger).
    empty_obs = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[],
    )
    _write_json(change / OBSERVATIONS_SOURCE, empty_obs.model_dump(mode="json"))
    c_digest_b1 = candidate_document_digest(
        IssueCandidateDocument(
            schema_version="1.0",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            evidence_bundle_digest=digest_b1,
            candidates=[],
        )
    )

    obs_b0 = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API)
    cand_b0 = _candidate_for(obs_b0, symptom="returns http 500 b0")
    digest_b0 = "sha256:" + "a" * 64
    c_digest_b0 = "sha256:" + "b" * 64
    occ_event, occ_id, pid, _ = _occurrence_event_for(
        candidate=cand_b0,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=3,
    )
    change_events: list[ChangeIssueEvent] = [
        _obs_recorded_for(obs_b0, seq=1),
        _analysis_completed_for(
            batch_id=BATCH_B0,
            evidence_digest=digest_b0,
            candidate_digest=c_digest_b0,
            candidate_count=1,
            seq=2,
        ),
        occ_event,
        _analysis_completed_for(
            batch_id=BATCH_ID,
            evidence_digest=digest_b1,
            candidate_digest=c_digest_b1,
            candidate_count=0,
            seq=4,
        ),
    ]
    _write_ledger(change, change_events)
    _write_snapshot_from_events(change, change_events)
    _write_reconcile_status(
        change,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest_b1,
        candidate_digest=c_digest_b1,
        occurrence_count=0,
    )
    problem_event = _problem_detected_for(
        candidate=cand_b0,
        occ_id=occ_id,
        pid=pid,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=1,
    )
    _write_project_ledger(project_root, [problem_event])
    _write_problems_projection(project_root, [problem_event])
    return {"occ_id": occ_id, "problem_id": pid, "obs_b0": obs_b0.observation_id}


def point_historical_occurrence_at_current_observation(project_root: Path) -> None:
    """Keep both ledgers replayable, but point B0 OCC at a B1 observation."""
    change = _change_dir(project_root)
    obs_b1 = _make_observation_for(batch_id=BATCH_ID, case_id=CASE_API, signature="b1-current-obs")
    events = _read_change_events(change)
    rewritten: list[ChangeIssueEvent] = []
    for event in events:
        if isinstance(event, OccurrenceDetectedEvent) and event.batch_id == BATCH_B0:
            tainted_occ = event.occurrence.model_copy(update={"observation_ids": [obs_b1.observation_id]})
            rewritten.append(event.model_copy(update={"occurrence": tainted_occ}))
        else:
            rewritten.append(event)
    # Insert B1 observation so change ledger still replays; OCC→OBS batch mismatch remains.
    rewritten.insert(
        -1,
        _obs_recorded_for(obs_b1, seq=max(e.seq for e in rewritten) + 1),
    )
    # Fix seq uniqueness by rewriting seqs in order.
    seq_fixed: list[ChangeIssueEvent] = []
    for index, event in enumerate(rewritten, start=1):
        seq_fixed.append(event.model_copy(update={"seq": index}))
    _write_ledger(change, seq_fixed)
    _write_snapshot_from_events(change, seq_fixed)
    obs_doc = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[obs_b1],
    )
    _write_json(change / OBSERVATIONS_SOURCE, obs_doc.model_dump(mode="json"))


def test_valid_ledgers_with_cross_batch_historical_observation_are_unavailable(
    tmp_path: Path,
) -> None:
    make_each_ledger_individually_replayable(tmp_path)
    point_historical_occurrence_at_current_observation(tmp_path)
    decision = evaluate_reconciled_authority(
        tmp_path,
        change_dir(tmp_path),
        CHANGE_ID,
        BATCH_ID,
    )
    assert decision.open_problem_ids_by_case == {}
    assert [gap.detail for gap in decision.issue_gaps] == ["reason=observation_reference_invalid"]


@pytest.mark.parametrize(
    "mutation",
    [
        "historical_dangling_observation",
        "source_missing_occurrence",
        "membership_only_on_merge_target",
        "membership_on_source_and_target",
        "wrong_source_owner",
        "project_only_current_change_membership",
        "snapshot_only_cross_change_occurrence",
        "forged_historical_occurrence",
        "snapshot_duplicate_observation",
    ],
)
def test_cross_ledger_mismatch_clears_whole_problem_authority(
    tmp_path: Path,
    mutation: str,
) -> None:
    ids = make_each_ledger_individually_replayable(tmp_path)
    change = _change_dir(tmp_path)
    if mutation == "historical_dangling_observation":
        events = _read_change_events(change)
        rewritten: list[ChangeIssueEvent] = []
        for event in events:
            if isinstance(event, OccurrenceDetectedEvent) and event.batch_id == BATCH_B0:
                tainted = event.occurrence.model_copy(update={"observation_ids": ["OBS-missing-historical"]})
                rewritten.append(event.model_copy(update={"occurrence": tainted}))
            else:
                rewritten.append(event)
        _write_ledger(change, rewritten)
        _write_snapshot_from_events(change, rewritten)
    elif mutation == "source_missing_occurrence":
        events = _read_problem_events(tmp_path)
        detected = events[0]
        assert isinstance(detected, ProblemDetectedEvent)
        # Keep ledger↔projection equal but point membership away from OCC.
        rewritten_detected = detected.model_copy(update={"occurrence_id": "OCC-not-owned"})
        # Recompute identity keys for the rewritten event.
        det_key = f"problem_detected:{detected.problem_id}:{CHANGE_ID}:{BATCH_B0}:rewritten"
        rewritten_detected = rewritten_detected.model_copy(
            update={"event_id": event_id(det_key), "idempotency_key": det_key}
        )
        _write_project_ledger(tmp_path, [rewritten_detected])
        _write_problems_projection(tmp_path, [rewritten_detected])
    elif mutation == "membership_only_on_merge_target":
        events = _read_problem_events(tmp_path)
        detected = events[0]
        assert isinstance(detected, ProblemDetectedEvent)
        target_cand = _candidate_for(
            _make_observation_for(batch_id=BATCH_B0, case_id="TC_TARGET"),
            candidate_id="CAND-T",
            symptom="target symptom",
            surface_value="GET /api/v1/target",
        )
        target_fp = problem_fingerprint(
            affected_surface=target_cand.affected_surface,
            fingerprint_inputs=target_cand.fingerprint_inputs,
        )
        target_pid = compute_problem_id(target_fp)
        target_detected = ProblemDetectedEvent(
            schema_version="1.0",
            seq=2,
            event_id=event_id(f"problem_detected:{target_pid}:seed"),
            idempotency_key=f"problem_detected:{target_pid}:seed",
            ts=TS,
            evidence_digest=detected.evidence_digest,
            problem_id=target_pid,
            expected_problem_version=0,
            type="problem_detected",
            occurrence_id="OCC-target-seed",
            change_id="CH-OTHER",
            batch_id=BATCH_B0,
            fingerprint=target_fp,
            title="target",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="h",
        )
        # Move membership: source detected with placeholder, link OCC onto target only.
        source_seed = detected.model_copy(update={"occurrence_id": "OCC-source-seed"})
        source_key = f"problem_detected:{detected.problem_id}:seed"
        source_seed = source_seed.model_copy(
            update={"event_id": event_id(source_key), "idempotency_key": source_key}
        )
        link_key = f"problem_occurrence_linked:{target_pid}:{ids['occ_id']}"
        link = ProblemOccurrenceLinkedEvent(
            schema_version="1.0",
            seq=3,
            event_id=event_id(link_key),
            idempotency_key=link_key,
            ts=TS,
            evidence_digest=detected.evidence_digest,
            problem_id=target_pid,
            expected_problem_version=1,
            type="problem_occurrence_linked",
            occurrence_id=ids["occ_id"],
            change_id=CHANGE_ID,
            batch_id=BATCH_B0,
        )
        problem_events = [source_seed, target_detected, link]
        _write_project_ledger(tmp_path, problem_events)
        _write_problems_projection(tmp_path, problem_events)
    elif mutation == "membership_on_source_and_target":
        events = _read_problem_events(tmp_path)
        detected = events[0]
        assert isinstance(detected, ProblemDetectedEvent)
        target_fp = problem_fingerprint(
            affected_surface=AffectedSurface(kind="endpoint", value="GET /api/v1/other"),
            fingerprint_inputs=FingerprintInputs(
                surface="endpoint",
                symptom="other",
                qualifiers=None,
            ),
        )
        target_pid = compute_problem_id(target_fp)
        target_detected = ProblemDetectedEvent(
            schema_version="1.0",
            seq=2,
            event_id=event_id(f"problem_detected:{target_pid}:t"),
            idempotency_key=f"problem_detected:{target_pid}:t",
            ts=TS,
            evidence_digest=detected.evidence_digest,
            problem_id=target_pid,
            expected_problem_version=0,
            type="problem_detected",
            occurrence_id="OCC-target-only",
            change_id="CH-OTHER",
            batch_id=BATCH_B0,
            fingerprint=target_fp,
            title="target",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="h",
        )
        link_key = f"problem_occurrence_linked:{target_pid}:{ids['occ_id']}:dup"
        link = ProblemOccurrenceLinkedEvent(
            schema_version="1.0",
            seq=3,
            event_id=event_id(link_key),
            idempotency_key=link_key,
            ts=TS,
            evidence_digest=detected.evidence_digest,
            problem_id=target_pid,
            expected_problem_version=1,
            type="problem_occurrence_linked",
            occurrence_id=ids["occ_id"],
            change_id=CHANGE_ID,
            batch_id=BATCH_B0,
        )
        problem_events = [detected, target_detected, link]
        _write_project_ledger(tmp_path, problem_events)
        _write_problems_projection(tmp_path, problem_events)
    elif mutation == "wrong_source_owner":
        events = _read_change_events(change)
        rewritten = []
        for event in events:
            if isinstance(event, OccurrenceDetectedEvent) and event.batch_id == BATCH_B0:
                tainted = event.occurrence.model_copy(update={"problem_id": "PROB-wrong-owner"})
                rewritten.append(event.model_copy(update={"occurrence": tainted}))
            else:
                rewritten.append(event)
        _write_ledger(change, rewritten)
        _write_snapshot_from_events(change, rewritten)
    elif mutation == "project_only_current_change_membership":
        events = _read_problem_events(tmp_path)
        detected = events[0]
        assert isinstance(detected, ProblemDetectedEvent)
        link_key = f"problem_occurrence_linked:{detected.problem_id}:OCC-project-only"
        link = ProblemOccurrenceLinkedEvent(
            schema_version="1.0",
            seq=2,
            event_id=event_id(link_key),
            idempotency_key=link_key,
            ts=TS,
            evidence_digest=detected.evidence_digest,
            problem_id=detected.problem_id,
            expected_problem_version=1,
            type="problem_occurrence_linked",
            occurrence_id="OCC-project-only",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
        )
        problem_events = [detected, link]
        _write_project_ledger(tmp_path, problem_events)
        _write_problems_projection(tmp_path, problem_events)
    elif mutation == "snapshot_only_cross_change_occurrence":
        snapshot = json.loads((change / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        occ = dict(snapshot["occurrences"][0])
        occ["occurrence_id"] = "OCC-cross-change-only"
        occ["change_id"] = "CH-OTHER"
        snapshot["occurrences"] = [*snapshot["occurrences"], occ]
        _write_json(change / SNAPSHOT_SOURCE, snapshot)
    elif mutation == "forged_historical_occurrence":
        snapshot = json.loads((change / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        occ = dict(snapshot["occurrences"][0])
        occ["occurrence_id"] = "OCC-forged-historical"
        occ["batch_id"] = BATCH_B0
        snapshot["occurrences"] = [*snapshot["occurrences"], occ]
        _write_json(change / SNAPSHOT_SOURCE, snapshot)
    elif mutation == "snapshot_duplicate_observation":
        snapshot = json.loads((change / SNAPSHOT_SOURCE).read_text(encoding="utf-8"))
        obs = snapshot["observations"][0]
        snapshot["observations"] = [obs, obs]
        _write_json(change / SNAPSHOT_SOURCE, snapshot)
    else:
        raise AssertionError(mutation)

    decision = evaluate_reconciled_authority(tmp_path, change, CHANGE_ID, BATCH_ID)
    assert decision.open_problem_ids_by_case == {}
    assert len(decision.issue_gaps) == 1
    assert decision.issue_gaps[0].code == "issue_reconciliation_unavailable" or decision.issue_gaps[
        0
    ].code in {
        "issues_snapshot_missing",
        "issues_snapshot_identity_mismatch",
    }


@pytest.mark.parametrize(
    ("fault", "code", "source", "detail"),
    [
        (
            "ledger_missing",
            "issue_reconciliation_unavailable",
            PROJECT_LEDGER_SOURCE,
            "reason=ledger_missing",
        ),
        (
            "problems_missing",
            "problems_snapshot_missing",
            PROJECT_PROBLEMS_SOURCE,
            "reason=missing",
        ),
        (
            "membership_mismatch",
            "issue_reconciliation_unavailable",
            PROJECT_PROBLEMS_SOURCE,
            "reason=problem_occurrence_mismatch",
        ),
    ],
)
def test_evaluate_reconciled_maps_project_source_faults(
    tmp_path: Path,
    fault: str,
    code: str,
    source: str,
    detail: str,
) -> None:
    """Public gap mapping for the three Task 10 project-fault classes."""
    _write_completed_with_occurrence(tmp_path)
    events_path = tmp_path / PROJECT_LEDGER_SOURCE
    problems_path = tmp_path / PROJECT_PROBLEMS_SOURCE
    if fault == "ledger_missing":
        events_path.unlink()
    elif fault == "problems_missing":
        problems_path.unlink()
    elif fault == "membership_mismatch":
        raw = events_path.read_text(encoding="utf-8").strip().splitlines()[0]
        payload = json.loads(raw)
        payload["occurrence_id"] = "OCC-not-current"
        events_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_problems_projection(
            tmp_path,
            [ProblemDetectedEvent.model_validate(json.loads(events_path.read_text().strip()))],
        )
    decision = evaluate_reconciled_authority(tmp_path, _change_dir(tmp_path), CHANGE_ID, BATCH_ID)
    assert decision.open_problem_ids_by_case == {}
    if fault == "problems_missing":
        assert decision.issue_gaps == ()
        assert len(decision.project_gaps) == 1
        gap = decision.project_gaps[0]
    else:
        assert decision.project_gaps == ()
        assert len(decision.issue_gaps) == 1
        gap = decision.issue_gaps[0]
    assert gap.code == code
    assert gap.source == source
    assert gap.batch_id == BATCH_ID
    assert gap.detail == detail


def _write_merge_multi_batch_tree(project_root: Path) -> dict[str, str]:
    """B0: P owns O and T exists; merge P→T; B1 empty completed current batch."""
    change = _change_dir(project_root)
    change.mkdir(parents=True, exist_ok=True)
    _write_json(change / FAILURE_SOURCE, _failure_payload())
    digest_b1, _, _ = _seed_manifest_tree(change)
    empty_obs = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[],
    )
    _write_json(change / OBSERVATIONS_SOURCE, empty_obs.model_dump(mode="json"))
    c_digest_b1 = candidate_document_digest(
        IssueCandidateDocument(
            schema_version="1.0",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            evidence_bundle_digest=digest_b1,
            candidates=[],
        )
    )

    obs_p = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API, signature="obs-p")
    obs_t = _make_observation_for(batch_id=BATCH_B0, case_id="TC_TARGET", signature="obs-t")
    cand_p = _candidate_for(obs_p, candidate_id="CAND-P", symptom="source symptom")
    cand_t = _candidate_for(
        obs_t,
        candidate_id="CAND-T",
        symptom="target symptom",
        surface_value="GET /api/v1/target",
    )
    digest_b0 = "sha256:" + "c" * 64
    c_digest_b0 = "sha256:" + "d" * 64
    occ_p_event, occ_p, pid_p, _ = _occurrence_event_for(
        candidate=cand_p, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=4
    )
    occ_t_event, occ_t, pid_t, _ = _occurrence_event_for(
        candidate=cand_t, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=5
    )
    change_events: list[ChangeIssueEvent] = [
        _obs_recorded_for(obs_p, seq=1),
        _obs_recorded_for(obs_t, seq=2),
        _analysis_completed_for(
            batch_id=BATCH_B0,
            evidence_digest=digest_b0,
            candidate_digest=c_digest_b0,
            candidate_count=2,
            seq=3,
        ),
        occ_p_event,
        occ_t_event,
        _analysis_completed_for(
            batch_id=BATCH_ID,
            evidence_digest=digest_b1,
            candidate_digest=c_digest_b1,
            candidate_count=0,
            seq=6,
        ),
    ]
    _write_ledger(change, change_events)
    _write_snapshot_from_events(change, change_events)
    _write_reconcile_status(
        change,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest_b1,
        candidate_digest=c_digest_b1,
        occurrence_count=0,
    )
    detected_p = _problem_detected_for(
        candidate=cand_p,
        occ_id=occ_p,
        pid=pid_p,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=1,
    )
    detected_t = _problem_detected_for(
        candidate=cand_t,
        occ_id=occ_t,
        pid=pid_t,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=2,
    )
    merge = _problem_merged_event(
        problem_id=pid_p,
        target_problem_id=pid_t,
        evidence_refs=[occ_p],
        expected_problem_version=1,
        seq=3,
    )
    problem_events: list[ProblemEvent] = [detected_p, detected_t, merge]
    _write_project_ledger(project_root, problem_events)
    _write_problems_projection(project_root, problem_events)
    return {"occ_p": occ_p, "pid_p": pid_p, "pid_t": pid_t, "occ_t": occ_t}


def test_merge_source_ownership_links_terminal_canonical_id(tmp_path: Path) -> None:
    ids = _write_merge_multi_batch_tree(tmp_path)
    decision = evaluate_reconciled_authority(
        tmp_path,
        change_dir(tmp_path),
        CHANGE_ID,
        BATCH_ID,
    )
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case[CASE_API] == (ids["pid_t"],)
    # Source retains occurrence ownership after merge.
    problems = project_problems(tuple(_read_problem_events(tmp_path)))
    source = next(p for p in problems.problems if p.problem_id == ids["pid_p"])
    assert ids["occ_p"] in source.occurrences
    target = next(p for p in problems.problems if p.problem_id == ids["pid_t"])
    assert ids["occ_p"] not in target.occurrences


def test_historical_open_problem_survives_later_empty_batch(tmp_path: Path) -> None:
    ids = make_each_ledger_individually_replayable(tmp_path)
    decision = evaluate_reconciled_authority(tmp_path, change_dir(tmp_path), CHANGE_ID, BATCH_ID)
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case[CASE_API] == (ids["problem_id"],)


def test_closed_or_non_product_terminal_produces_no_historical_link(tmp_path: Path) -> None:
    ids = make_each_ledger_individually_replayable(tmp_path)
    detected = _read_problem_events(tmp_path)[0]
    assert isinstance(detected, ProblemDetectedEvent)
    resolve_key = f"problem_resolved:{ids['problem_id']}:{BATCH_ID}:{detected.evidence_digest}"
    resolved = ProblemResolvedEvent(
        schema_version="1.0",
        seq=2,
        event_id=event_id(resolve_key),
        idempotency_key=resolve_key,
        ts=TS,
        evidence_digest=detected.evidence_digest,
        problem_id=ids["problem_id"],
        expected_problem_version=1,
        type="problem_resolved",
        resolved_at=TS,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        disposition="fixed",
        verification_scope=["API"],
    )
    problem_events: list[ProblemEvent] = [detected, resolved]
    _write_project_ledger(tmp_path, problem_events)
    _write_problems_projection(tmp_path, problem_events)
    decision = evaluate_reconciled_authority(tmp_path, change_dir(tmp_path), CHANGE_ID, BATCH_ID)
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case == {}


def test_non_product_classification_produces_no_historical_link(tmp_path: Path) -> None:
    ids = make_each_ledger_individually_replayable(tmp_path)
    detected = _read_problem_events(tmp_path)[0]
    assert isinstance(detected, ProblemDetectedEvent)
    evidence_refs = [ids["occ_id"]]
    evidence_digest = _evidence_refs_digest(evidence_refs)
    confirm_key = f"review:confirm_assessment:{ids['problem_id']}:1:{evidence_digest}"
    confirmed = ProblemAssessmentConfirmedEvent(
        schema_version="1.0",
        seq=2,
        event_id=event_id(confirm_key),
        idempotency_key=confirm_key,
        ts=TS,
        evidence_digest=evidence_digest,
        problem_id=ids["problem_id"],
        expected_problem_version=1,
        type="problem_assessment_confirmed",
        classification="test_bug",
        severity="high",
        root_cause_hypothesis="test flake",
        reason="reclassified",
        evidence_refs=evidence_refs,
    )
    problem_events: list[ProblemEvent] = [detected, confirmed]
    _write_project_ledger(tmp_path, problem_events)
    _write_problems_projection(tmp_path, problem_events)
    decision = evaluate_reconciled_authority(tmp_path, change_dir(tmp_path), CHANGE_ID, BATCH_ID)
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case == {}


def test_canonical_fingerprint_deduplicates_open_problem_ids(tmp_path: Path) -> None:
    """Two same-case occurrences of one problem emit a single canonical ID."""
    change = _change_dir(tmp_path)
    change.mkdir(parents=True)
    _write_json(change / FAILURE_SOURCE, _failure_payload())
    digest_b1, _, _ = _seed_manifest_tree(change)
    empty_obs = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[],
    )
    _write_json(change / OBSERVATIONS_SOURCE, empty_obs.model_dump(mode="json"))
    c_digest_b1 = candidate_document_digest(
        IssueCandidateDocument(
            schema_version="1.0",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            evidence_bundle_digest=digest_b1,
            candidates=[],
        )
    )
    obs_a = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API, signature="dedup-a")
    obs_b = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API, signature="dedup-b")
    cand_a = _candidate_for(obs_a, candidate_id="CAND-A", symptom="dedup-shared")
    # Second occurrence intentionally reuses cand_a's fingerprint surface via same symptom
    # but a distinct candidate digest from a different observation set — link to same problem.
    cand_b = _candidate_for(obs_b, candidate_id="CAND-B", symptom="dedup-shared")
    digest_b0 = "sha256:" + "e" * 64
    c_digest_b0 = "sha256:" + "f" * 64
    occ_a_event, occ_a, pid_a, _ = _occurrence_event_for(
        candidate=cand_a, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=4
    )
    # Force second occurrence onto the same problem_id as the first (fingerprint match).
    occ_b_event, occ_b, pid_b, _ = _occurrence_event_for(
        candidate=cand_b, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=5
    )
    assert pid_b == pid_a  # same fingerprint inputs → same problem
    occ_b_event = occ_b_event.model_copy(
        update={
            "occurrence": occ_b_event.occurrence.model_copy(update={"problem_id": pid_a}),
            "seq": 5,
        }
    )
    change_events: list[ChangeIssueEvent] = [
        _obs_recorded_for(obs_a, seq=1),
        _obs_recorded_for(obs_b, seq=2),
        _analysis_completed_for(
            batch_id=BATCH_B0,
            evidence_digest=digest_b0,
            candidate_digest=c_digest_b0,
            candidate_count=2,
            seq=3,
        ),
        occ_a_event.model_copy(update={"seq": 4}),
        occ_b_event,
        _analysis_completed_for(
            batch_id=BATCH_ID,
            evidence_digest=digest_b1,
            candidate_digest=c_digest_b1,
            candidate_count=0,
            seq=6,
        ),
    ]
    _write_ledger(change, change_events)
    _write_snapshot_from_events(change, change_events)
    _write_reconcile_status(
        change,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest_b1,
        candidate_digest=c_digest_b1,
        occurrence_count=0,
    )
    detected = _problem_detected_for(
        candidate=cand_a,
        occ_id=occ_a,
        pid=pid_a,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=1,
    )
    link_key = f"problem_occurrence_linked:{pid_a}:{occ_b}"
    linked = ProblemOccurrenceLinkedEvent(
        schema_version="1.0",
        seq=2,
        event_id=event_id(link_key),
        idempotency_key=link_key,
        ts=TS,
        evidence_digest=digest_b0,
        problem_id=pid_a,
        expected_problem_version=1,
        type="problem_occurrence_linked",
        occurrence_id=occ_b,
        change_id=CHANGE_ID,
        batch_id=BATCH_B0,
    )
    _write_project_ledger(tmp_path, [detected, linked])
    _write_problems_projection(tmp_path, [detected, linked])
    decision = evaluate_reconciled_authority(tmp_path, change, CHANGE_ID, BATCH_ID)
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case[CASE_API] == (pid_a,)


def test_open_problem_ids_emitted_in_lexical_order(tmp_path: Path) -> None:
    change = _change_dir(tmp_path)
    change.mkdir(parents=True)
    _write_json(change / FAILURE_SOURCE, _failure_payload())
    digest_b1, _, _ = _seed_manifest_tree(change)
    empty_obs = ObservationDocument(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observations=[],
    )
    _write_json(change / OBSERVATIONS_SOURCE, empty_obs.model_dump(mode="json"))
    c_digest_b1 = candidate_document_digest(
        IssueCandidateDocument(
            schema_version="1.0",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            evidence_bundle_digest=digest_b1,
            candidates=[],
        )
    )
    obs_a = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API, signature="order-a")
    obs_b = _make_observation_for(batch_id=BATCH_B0, case_id=CASE_API, signature="order-b")
    cand_a = _candidate_for(obs_a, candidate_id="CAND-A", symptom="order-a")
    cand_b = _candidate_for(
        obs_b,
        candidate_id="CAND-B",
        symptom="order-b",
        surface_value="GET /api/v1/other",
    )
    digest_b0 = "sha256:" + "e" * 64
    c_digest_b0 = "sha256:" + "f" * 64
    occ_a_event, occ_a, pid_a, _ = _occurrence_event_for(
        candidate=cand_a, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=4
    )
    occ_b_event, occ_b, pid_b, _ = _occurrence_event_for(
        candidate=cand_b, batch_id=BATCH_B0, evidence_digest=digest_b0, seq=5
    )
    change_events: list[ChangeIssueEvent] = [
        _obs_recorded_for(obs_a, seq=1),
        _obs_recorded_for(obs_b, seq=2),
        _analysis_completed_for(
            batch_id=BATCH_B0,
            evidence_digest=digest_b0,
            candidate_digest=c_digest_b0,
            candidate_count=2,
            seq=3,
        ),
        occ_a_event.model_copy(update={"seq": 4}),
        occ_b_event.model_copy(update={"seq": 5}),
        _analysis_completed_for(
            batch_id=BATCH_ID,
            evidence_digest=digest_b1,
            candidate_digest=c_digest_b1,
            candidate_count=0,
            seq=6,
        ),
    ]
    _write_ledger(change, change_events)
    _write_snapshot_from_events(change, change_events)
    _write_reconcile_status(
        change,
        schema_version="2.0",
        status="completed",
        evidence_bundle_digest=digest_b1,
        candidate_digest=c_digest_b1,
        occurrence_count=0,
    )
    detected_a = _problem_detected_for(
        candidate=cand_a,
        occ_id=occ_a,
        pid=pid_a,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=1,
    )
    detected_b = _problem_detected_for(
        candidate=cand_b,
        occ_id=occ_b,
        pid=pid_b,
        batch_id=BATCH_B0,
        evidence_digest=digest_b0,
        seq=2,
    )
    _write_project_ledger(tmp_path, [detected_a, detected_b])
    _write_problems_projection(tmp_path, [detected_a, detected_b])
    decision = evaluate_reconciled_authority(tmp_path, change, CHANGE_ID, BATCH_ID)
    assert decision.issue_gaps == ()
    assert decision.open_problem_ids_by_case[CASE_API] == tuple(sorted((pid_a, pid_b)))


def test_alias_cycle_and_missing_target_fail_historical_authority(tmp_path: Path) -> None:
    ids = make_each_ledger_individually_replayable(tmp_path)
    detected = _read_problem_events(tmp_path)[0]
    assert isinstance(detected, ProblemDetectedEvent)
    merge = _problem_merged_event(
        problem_id=ids["problem_id"],
        target_problem_id="PROB-missing-target",
        evidence_refs=[ids["occ_id"]],
    )
    problem_events: list[ProblemEvent] = [detected, merge]
    _write_project_ledger(tmp_path, problem_events)
    _write_problems_projection(tmp_path, problem_events)
    decision = evaluate_reconciled_authority(tmp_path, change_dir(tmp_path), CHANGE_ID, BATCH_ID)
    assert decision.open_problem_ids_by_case == {}
    assert [gap.detail for gap in decision.issue_gaps] == ["reason=problem_occurrence_mismatch"]


def test_recovery_keeps_independent_problems_snapshot_project_gap(authority_tree: Path) -> None:
    project_root = _project_root(authority_tree)
    (project_root / "qa" / "issues").mkdir(parents=True, exist_ok=True)
    (project_root / PROJECT_PROBLEMS_SOURCE).write_text("{not-json", encoding="utf-8")
    decision = evaluate_reconciled_authority(
        project_root,
        authority_tree,
        CHANGE_ID,
        BATCH_ID,
    )
    assert decision.open_problem_ids_by_case == {}
    assert len(decision.issue_gaps) == 1
    assert decision.issue_gaps[0].code == "issue_analysis_failed"
    assert len(decision.project_gaps) == 1
    assert decision.project_gaps[0].code == "problems_snapshot_missing"
    assert decision.project_gaps[0].detail == "reason=malformed"
    assert decision.project_gaps[0].batch_id == BATCH_ID


def test_recovery_missing_problems_projection_is_independent_project_gap(
    authority_tree: Path,
) -> None:
    project_root = _project_root(authority_tree)
    (project_root / "qa" / "issues").mkdir(parents=True, exist_ok=True)
    # Present empty project ledger so missing problems is non-genesis.
    _write_project_ledger(project_root, [])
    decision = evaluate_reconciled_authority(
        project_root,
        authority_tree,
        CHANGE_ID,
        BATCH_ID,
    )
    assert decision.open_problem_ids_by_case == {}
    assert len(decision.issue_gaps) == 1
    assert decision.issue_gaps[0].code == "issue_analysis_failed"
    assert len(decision.project_gaps) == 1
    assert decision.project_gaps[0].code == "problems_snapshot_missing"
    assert decision.project_gaps[0].detail == "reason=missing"
