"""Authority prefix / failure-authority truth tables (Task 9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.issue_events import (
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    ObservationRecordedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueCandidateDocument,
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    Observation,
    ObservationDocument,
    ObservationSource,
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
)
from assurance_agent.evidence.issue_replay import dump_projection, project_change_issues
from assurance_agent.evidence.trace_authority import (
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
