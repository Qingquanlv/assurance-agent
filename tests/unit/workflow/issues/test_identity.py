import hashlib
import json

import pytest

from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    ProblemFingerprint,
    ProblemFingerprintPreimage,
)
from assurance_agent.workflow.issues.identity import (
    DIGEST_PREFIX_LENGTH,
    ObservationIdentityInput,
    candidate_document_digest,
    fingerprint_digest_for_version,
    occurrence_id,
    observation_id,
    problem_fingerprint,
    problem_id,
    reconciliation_idempotency_key,
)


def _canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _observation_input(**overrides: object) -> ObservationIdentityInput:
    base = {
        "change_id": "RET-dept-management",
        "batch_id": "20260725-124844",
        "kind": "test_failure",
        "target": "api",
        "case_id": "API-DEPT-NEG-001",
        "source_artifact": "execution/runs/20260725-124844/api-result.json",
        "source_json_pointer": "/cases/3",
        "signature": "http_500_on_empty_department_name",
    }
    merged: dict[str, str] = {**base, **{k: str(v) for k, v in overrides.items()}}
    return ObservationIdentityInput(**merged)


def test_observation_id_is_deterministic_and_prefixed() -> None:
    obs_input = _observation_input()
    first = observation_id(obs_input)
    second = observation_id(obs_input)

    assert first == second
    assert first.startswith("OBS-")
    assert len(first) == len("OBS-") + DIGEST_PREFIX_LENGTH


def test_observation_id_ignores_whitespace_in_signature() -> None:
    base = _observation_input(signature="http_500_on_empty_department_name")
    spaced = _observation_input(signature="  http_500_on_empty_department_name  ")

    assert observation_id(base) == observation_id(spaced)


def test_observation_id_changes_with_source_location() -> None:
    left = _observation_input(source_json_pointer="/cases/3")
    right = _observation_input(source_json_pointer="/cases/4")

    assert observation_id(left) != observation_id(right)


def test_occurrence_and_reconciliation_keys_are_stable() -> None:
    change_id = "RET-dept-management"
    batch_id = "20260725-124844"
    candidate_digest = "sha256:candidate"

    occ = occurrence_id(change_id, batch_id, candidate_digest)
    key = reconciliation_idempotency_key(change_id, batch_id, candidate_digest)

    assert occ.startswith("OCC-")
    assert len(occ) == len("OCC-") + DIGEST_PREFIX_LENGTH
    assert key == _canonical_sha256(
        {
            "change_id": change_id,
            "batch_id": batch_id,
            "candidate_digest": candidate_digest,
        }
    )


def test_problem_fingerprint_ignores_title_root_cause_confidence_severity_change_id() -> None:
    surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["negative_case"],
    )

    baseline = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
    )
    noisy = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
        title="Different title",
        root_cause_hypothesis="Different root cause",
        confidence=0.12,
        severity="low",
        change_id="OTHER-change",
    )

    assert baseline == noisy
    assert baseline.digest.startswith("sha256:")
    assert baseline.version == "1"


def test_problem_fingerprint_changes_with_surface_kind_value_symptom_qualifiers_version() -> None:
    base_surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    base_inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["negative_case"],
    )
    baseline = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=base_inputs,
    )

    other_kind = problem_fingerprint(
        affected_surface=AffectedSurface(kind="module", value="dept_service"),
        fingerprint_inputs=FingerprintInputs(
            surface="dept_service",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["negative_case"],
        ),
    )
    other_value = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept/other"),
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept/other",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["negative_case"],
        ),
    )
    other_symptom = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_502",
            qualifiers=["negative_case"],
        ),
    )
    other_qualifiers = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["positive_case"],
        ),
    )
    other_version_digest = fingerprint_digest_for_version(
        affected_surface=base_surface,
        fingerprint_inputs=base_inputs,
        version="2",
    )

    assert baseline != other_kind
    assert baseline != other_value
    assert baseline != other_symptom
    assert baseline != other_qualifiers
    assert baseline.digest.removeprefix("sha256:") != other_version_digest


def test_problem_fingerprint_normalizes_endpoint_module_and_symptom_tokens() -> None:
    left = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="post  /api/v1/dept/"),
        fingerprint_inputs=FingerprintInputs(
            surface="post  /api/v1/dept/",
            symptom="Invalid Empty Name Returns 500",
        ),
    )
    right = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept"),
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_500",
        ),
    )

    assert left == right


def test_problem_fingerprint_rejects_empty_normalized_values() -> None:
    with pytest.raises(ValueError, match="symptom"):
        problem_fingerprint(
            affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept"),
            fingerprint_inputs=FingerprintInputs(surface="POST /api/v1/dept", symptom="   "),
        )

    with pytest.raises(ValueError, match="surface"):
        problem_fingerprint(
            affected_surface=AffectedSurface(kind="module", value="   "),
            fingerprint_inputs=FingerprintInputs(surface="   ", symptom="missing_module"),
        )


def test_problem_id_uses_fingerprint_digest_prefix() -> None:
    fingerprint = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
    derived = problem_id(fingerprint)

    assert derived == f"PROB-{'a' * DIGEST_PREFIX_LENGTH}"


def test_problem_fingerprint_canonical_key_order_is_stable() -> None:
    surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["b", "a"],
    )
    fingerprint = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
    )

    expected_digest = "sha256:" + _canonical_sha256(
        {
            "version": "1",
            "surface_kind": "endpoint",
            "surface_identity": "POST /api/v1/dept",
            "symptom": "invalid_empty_name_returns_500",
            "qualifiers": ["a", "b"],
        }
    )
    assert fingerprint.digest == expected_digest


def test_problem_fingerprint_exposes_verified_canonical_preimage_for_future_reuse() -> None:
    fingerprint = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="post  /api/v1/dept/"),
        fingerprint_inputs=FingerprintInputs(
            surface="ignored duplicate surface",
            symptom="Closure Not Rebuilt On Reparent",
            qualifiers=["reparent", "dept closure"],
        ),
    )

    assert fingerprint.preimage == ProblemFingerprintPreimage(
        version="1",
        surface_kind="endpoint",
        surface_identity="POST /api/v1/dept",
        symptom="closure_not_rebuilt_on_reparent",
        qualifiers=["dept_closure", "reparent"],
    )


def test_problem_fingerprint_rejects_preimage_that_does_not_match_digest() -> None:
    with pytest.raises(ValueError, match="preimage"):
        ProblemFingerprint(
            version="1",
            digest="sha256:" + "a" * 64,
            preimage=ProblemFingerprintPreimage(
                version="1",
                surface_kind="endpoint",
                surface_identity="POST /api/v1/dept",
                symptom="http_500",
                qualifiers=[],
            ),
        )


def test_candidate_document_digest_hashes_authored_json_without_inserting_defaults() -> None:
    authored = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "B-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [
            {
                "candidate_id": "CAND-1",
                "observation_ids": ["OBS-1"],
                "proposed": {
                    "title": "Endpoint fails",
                    "classification": "product_bug",
                    "severity": "high",
                    "root_cause_hypothesis": "Unhandled input",
                },
                "affected_surface": {"kind": "endpoint", "value": "POST /api/items"},
                "fingerprint_inputs": {"surface": "POST /api/items", "symptom": "http_500"},
                "possible_problem_ids": [],
                "confidence": 1,
                "recommended_action": "investigate",
            }
        ],
    }
    expected = (
        "sha256:"
        + hashlib.sha256(
            (json.dumps(authored, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
    )

    assert candidate_document_digest(authored) == expected


def test_event_id_matches_existing_sha256_prefix() -> None:
    from assurance_agent.evidence.issue_identity import event_id

    key = "project_sync_pending:CH-1:B1:sha256:candidate"
    assert event_id(key) == "EVT-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def test_per_candidate_digest_ignores_key_order_and_whitespace_formatting() -> None:
    from assurance_agent.evidence.issue_identity import per_candidate_digest

    candidate = {
        "candidate_id": "CAND-1",
        "observation_ids": ["OBS-1"],
        "proposed": {
            "title": "Endpoint fails",
            "classification": "product_bug",
            "severity": "high",
            "root_cause_hypothesis": "Unhandled input",
        },
        "affected_surface": {"kind": "endpoint", "value": "POST /api/items"},
        "fingerprint_inputs": {"surface": "POST /api/items", "symptom": "http_500"},
        "possible_problem_ids": [],
        "confidence": 1,
        "recommended_action": "investigate",
    }
    reordered = {k: candidate[k] for k in reversed(list(candidate))}
    spaced = json.dumps(candidate, indent=2, sort_keys=False)
    assert per_candidate_digest(candidate) == per_candidate_digest(reordered)
    assert per_candidate_digest(candidate) == per_candidate_digest(json.loads(spaced))
    changed = {**candidate, "candidate_id": "CAND-2"}
    assert per_candidate_digest(candidate) != per_candidate_digest(changed)


def test_recomputable_issue_event_idempotency_key_formats() -> None:
    from assurance_agent.artifacts.models.issue_events import (
        ChangeIssueEvent,
        IssueAnalysisCompletedEvent,
        IssueAnalysisFailedEvent,
        ObservationRecordedEvent,
        OccurrenceDetectedEvent,
        OccurrenceLinkedEvent,
        ProblemAssessmentConfirmedEvent,
        ProblemDetectedEvent,
        ProblemEvent,
        ProblemMarkedNotAnIssueEvent,
        ProblemMergedEvent,
        ProblemMergeSuggestedEvent,
        ProblemOccurrenceLinkedEvent,
        ProblemRegressedEvent,
        ProblemReopenedEvent,
        ProblemResolvedEvent,
        ProblemRiskAcceptedEvent,
        ProblemVerificationRequestedEvent,
        ProblemWorkStartedEvent,
        ProjectSyncPendingEvent,
    )
    from assurance_agent.artifacts.models.issues import (
        IssueAnalysisStatus,
        IssueOccurrence,
        Observation,
        ObservationSource,
        OccurrenceAnalysis,
        ProblemFingerprint,
        ProvisionalAssessment,
    )
    from assurance_agent.evidence.issue_identity import (
        ObservationIdentityInput,
        event_id,
        observation_id,
        occurrence_id,
        problem_id,
        recomputable_issue_event_idempotency_key,
    )

    obs_id = observation_id(
        ObservationIdentityInput(
            change_id="CH-1",
            batch_id="B1",
            kind="test_failure",
            target="api",
            case_id=None,
            source_artifact="execution/runs/B1/api-result.json",
            source_json_pointer="/cases/0",
            signature="http_500",
        )
    )
    observation = Observation(
        observation_id=obs_id,
        change_id="CH-1",
        batch_id="B1",
        kind="test_failure",
        target="api",
        case_id=None,
        source=ObservationSource(
            artifact="execution/runs/B1/api-result.json",
            json_pointer="/cases/0",
        ),
        evidence_refs=["execution/runs/B1/api-result.json"],
        signature="http_500",
        observed_at="2026-07-25T10:00:00Z",
    )
    digest = "sha256:candidate"
    fingerprint = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
    pid = problem_id(fingerprint)
    occ = IssueOccurrence(
        occurrence_id=occurrence_id("CH-1", "B1", digest),
        change_id="CH-1",
        batch_id="B1",
        observation_ids=[obs_id],
        problem_id=pid,
        provisional_assessment=ProvisionalAssessment(
            classification="product_bug",
            severity="high",
            authority="llm_provisional",
            root_cause_hypothesis="null",
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest="sha256:e",
            analyzer="aa-issue-analyzer",
            prompt_version="v1",
            candidate_digest=digest,
        ),
    )

    def _change_envelope(key: str, **extra: object) -> dict:
        return {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": event_id(key),
            "idempotency_key": key,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:e",
            "change_id": "CH-1",
            "batch_id": "B1",
            **extra,
        }

    def _problem_envelope(key: str, **extra: object) -> dict:
        return {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": event_id(key),
            "idempotency_key": key,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:e",
            "problem_id": pid,
            "expected_problem_version": 1,
            **extra,
        }

    obs_key = f"observation_recorded:CH-1:B1:{obs_id}"
    assert (
        recomputable_issue_event_idempotency_key(
            ObservationRecordedEvent(
                **_change_envelope(obs_key, type="observation_recorded", observation=observation)
            )
        )
        == obs_key
    )

    completed_key = f"issue_analysis_completed:CH-1:B1:{digest}"
    completed_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id="CH-1",
        batch_id="B1",
        status="completed",
        evidence_bundle_digest="sha256:e",
        candidate_count=1,
        candidate_digest=digest,
    )
    assert (
        recomputable_issue_event_idempotency_key(
            IssueAnalysisCompletedEvent(
                **_change_envelope(
                    completed_key,
                    type="issue_analysis_completed",
                    analysis_status=completed_status,
                )
            )
        )
        == completed_key
    )
    # Completed without persisted candidate digest is not recomputable.
    legacy_completed = IssueAnalysisCompletedEvent(
        **_change_envelope(
            "legacy-completed",
            type="issue_analysis_completed",
            analysis_status=IssueAnalysisStatus(
                schema_version="1.0",
                change_id="CH-1",
                batch_id="B1",
                status="completed",
                evidence_bundle_digest="sha256:e",
                candidate_count=0,
                candidate_digest=None,
            ),
        )
    )
    assert recomputable_issue_event_idempotency_key(legacy_completed) is None

    failed_key = "issue_analysis_failed:CH-1:B1:sha256:e"
    failed_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id="CH-1",
        batch_id="B1",
        status="failed",
        evidence_bundle_digest="sha256:e",
        candidate_count=0,
        reason="timeout",
        retryable=True,
    )
    assert (
        recomputable_issue_event_idempotency_key(
            IssueAnalysisFailedEvent(
                **_change_envelope(failed_key, type="issue_analysis_failed", analysis_status=failed_status)
            )
        )
        == failed_key
    )

    detected_key = f"occurrence_detected:CH-1:B1:{digest}"
    assert (
        recomputable_issue_event_idempotency_key(
            OccurrenceDetectedEvent(
                **_change_envelope(detected_key, type="occurrence_detected", occurrence=occ)
            )
        )
        == detected_key
    )
    linked_key = f"occurrence_linked:CH-1:B1:{digest}"
    assert (
        recomputable_issue_event_idempotency_key(
            OccurrenceLinkedEvent(**_change_envelope(linked_key, type="occurrence_linked", occurrence=occ))
        )
        == linked_key
    )

    sync_key = f"project_sync_pending:CH-1:B1:{digest}"
    assert (
        recomputable_issue_event_idempotency_key(
            ProjectSyncPendingEvent(
                **_change_envelope(sync_key, type="project_sync_pending", candidate_digest=digest)
            )
        )
        == sync_key
    )

    resolved_key = f"problem_resolved:{pid}:B4:sha256:e"
    assert (
        recomputable_issue_event_idempotency_key(
            ProblemResolvedEvent(
                **_problem_envelope(
                    resolved_key,
                    type="problem_resolved",
                    expected_problem_version=4,
                    resolved_at="2026-07-25T12:00:00Z",
                    change_id="CH-2",
                    batch_id="B4",
                    disposition="fixed",
                    verification_scope=["API-1"],
                )
            )
        )
        == resolved_key
    )

    review_cases: list[tuple[ChangeIssueEvent | ProblemEvent, str]] = [
        (
            ProblemAssessmentConfirmedEvent(
                **_problem_envelope(
                    f"review:confirm_assessment:{pid}:1:sha256:e",
                    type="problem_assessment_confirmed",
                    classification="product_bug",
                    severity="high",
                    reason="ok",
                    evidence_refs=["r1"],
                )
            ),
            f"review:confirm_assessment:{pid}:1:sha256:e",
        ),
        (
            ProblemMarkedNotAnIssueEvent(
                **_problem_envelope(
                    f"review:mark_not_an_issue:{pid}:1:sha256:e",
                    type="problem_marked_not_an_issue",
                    reason="noise",
                    evidence_refs=["r1"],
                )
            ),
            f"review:mark_not_an_issue:{pid}:1:sha256:e",
        ),
        (
            ProblemRiskAcceptedEvent(
                **_problem_envelope(
                    f"review:accept_risk:{pid}:1:sha256:e",
                    type="problem_risk_accepted",
                    reason="accepted",
                    evidence_refs=["r1"],
                )
            ),
            f"review:accept_risk:{pid}:1:sha256:e",
        ),
        (
            ProblemWorkStartedEvent(
                **_problem_envelope(
                    f"review:start_work:{pid}:1:sha256:e",
                    type="problem_work_started",
                    reason="start",
                    evidence_refs=["r1"],
                )
            ),
            f"review:start_work:{pid}:1:sha256:e",
        ),
        (
            ProblemReopenedEvent(
                **_problem_envelope(
                    f"review:reopen:{pid}:1:sha256:e",
                    type="problem_reopened",
                    reason="reopen",
                    evidence_refs=["r1"],
                )
            ),
            f"review:reopen:{pid}:1:sha256:e",
        ),
        (
            ProblemMergedEvent(
                **_problem_envelope(
                    f"review:merge:{pid}:1:PROB-bbbbbbbbbbbbbbbb:sha256:e",
                    type="problem_merged",
                    target_problem_id="PROB-bbbbbbbbbbbbbbbb",
                    reason="dup",
                    evidence_refs=["r1"],
                    resolved_at="2026-07-25T12:00:00Z",
                )
            ),
            f"review:merge:{pid}:1:PROB-bbbbbbbbbbbbbbbb:sha256:e",
        ),
        (
            ProblemVerificationRequestedEvent(
                **_problem_envelope(
                    f"review:submit_resolution:{pid}:1:CH-2:B3:sha256:e",
                    type="problem_verification_requested",
                    verification_scope=["API-1"],
                    linked_fix_disposition="PR-1",
                    change_id="CH-2",
                    batch_id="B3",
                )
            ),
            f"review:submit_resolution:{pid}:1:CH-2:B3:sha256:e",
        ),
    ]
    for event, expected_key in review_cases:
        assert recomputable_issue_event_idempotency_key(event) == expected_key

    # Legacy / incomplete preimage arms return None.
    assert (
        recomputable_issue_event_idempotency_key(
            ProblemDetectedEvent(
                **_problem_envelope(
                    "problem_detected:legacy",
                    type="problem_detected",
                    expected_problem_version=0,
                    occurrence_id=occ.occurrence_id,
                    change_id="CH-1",
                    batch_id="B1",
                    fingerprint=fingerprint,
                    title="T",
                    classification="product_bug",
                    severity="high",
                )
            )
        )
        is None
    )
    assert (
        recomputable_issue_event_idempotency_key(
            ProblemOccurrenceLinkedEvent(
                **_problem_envelope(
                    "problem_linked:legacy",
                    type="problem_occurrence_linked",
                    occurrence_id=occ.occurrence_id,
                    change_id="CH-1",
                    batch_id="B1",
                )
            )
        )
        is None
    )
    assert (
        recomputable_issue_event_idempotency_key(
            ProblemRegressedEvent(
                **_problem_envelope(
                    "problem_regressed:legacy",
                    type="problem_regressed",
                    occurrence_id=occ.occurrence_id,
                    change_id="CH-1",
                )
            )
        )
        is None
    )
    merge = ProblemMergeSuggestedEvent(
        **_problem_envelope(
            "legacy-merge",
            type="problem_merge_suggested",
            source_occurrence_id="OCC-1",
            source_change_id="CH-1",
            target_problem_id="PROB-bbbbbbbbbbbbbbbb",
            reason="possible match",
        )
    )
    assert recomputable_issue_event_idempotency_key(merge) is None


def test_no_workflow_imports_under_evidence() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "packages/assurance-kernel/assurance_kernel/evidence"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (
                    node.module.startswith("assurance_agent.workflow")
                    or node.module.startswith("assurance_kernel.workflow")
                )
            ):
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("assurance_agent.workflow") or alias.name.startswith(
                        "assurance_kernel.workflow"
                    ):
                        offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_no_local_event_id_helpers_remain_under_workflow_issues() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "assurance_agent" / "workflow" / "issues"
    writers = ("reconciler.py", "review.py", "operations.py")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_event_id":
                raise AssertionError(f"local _event_id remains in {path}")
        if path.name in writers:
            imported_event_id = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.endswith("issue_identity") or node.module.endswith("identity"):
                        if any(alias.name == "event_id" for alias in node.names):
                            imported_event_id = True
            assert imported_event_id, f"{path.name} must import foundational event_id"
