"""Issue lifecycle fixtures for retro unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.artifacts.models.issues import (
    IssueOccurrence,
    OccurrenceAnalysis,
    ProblemFingerprint,
    ProvisionalAssessment,
)
from assurance_agent.workflow.issues.events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
)
from assurance_agent.workflow.issues.projection import dump_projection, project_problems


PROB_ID = "PROB-deadbeef12345678"
PROB_ID_B = "PROB-beefdeadcafef00d"
OCC_ID = "OCC-1111111111111111"


def sample_occurrence(*, change_id: str = "CH-1", batch_id: str = "B-001") -> IssueOccurrence:
    return IssueOccurrence(
        occurrence_id=OCC_ID,
        change_id=change_id,
        batch_id=batch_id,
        observation_ids=["OBS-1111111111111111"],
        problem_id=PROB_ID,
        provisional_assessment=ProvisionalAssessment(
            classification="product_bug",
            severity="high",
            authority="llm_provisional",
            root_cause_hypothesis="null pointer",
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest="sha256:aabbccdd",
            analyzer="aa-issue-analyzer",
            prompt_version="v1",
            candidate_digest="sha256:11223344",
        ),
    )


def write_change_occurrence_detected(
    change_dir: Path,
    *,
    change_id: str | None = None,
    event_id: str = "CIE-0001",
) -> str:
    """Append a valid occurrence_detected event; return its citable evidence id."""
    cid = change_id or change_dir.name
    event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": event_id,
            "idempotency_key": "CIK-0001",
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "change_id": cid,
            "batch_id": "B-001",
            "type": "occurrence_detected",
            "occurrence": sample_occurrence(change_id=cid).model_dump(mode="json"),
        }
    )
    issues = change_dir / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    (issues / "events.jsonl").write_text(
        json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return f"{cid}#issue-{event_id}"


def write_project_problem_lifecycle(project_root: Path, *, change_id: str = "CH-1") -> dict[str, str]:
    """Write project Problem events connected to ``change_id`` for retro aggregation tests."""
    detected = PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "PE-0001",
            "idempotency_key": "PIK-0001",
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "problem_id": PROB_ID,
            "expected_problem_version": 0,
            "type": "problem_detected",
            "occurrence_id": OCC_ID,
            "change_id": change_id,
            "batch_id": "B-001",
            "fingerprint": ProblemFingerprint(version="1", digest="sha256:" + "a" * 64).model_dump(
                mode="json"
            ),
            "title": "HTTP 500 on empty name",
            "classification": "product_bug",
            "severity": "high",
        }
    )
    resolved = PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 2,
            "event_id": "PE-0002",
            "idempotency_key": "PIK-0002",
            "ts": "2026-07-25T11:00:00Z",
            "evidence_digest": "sha256:bbccddee",
            "problem_id": PROB_ID,
            "expected_problem_version": 1,
            "type": "problem_resolved",
            "resolved_at": "2026-07-25T11:00:00Z",
            "change_id": change_id,
            "batch_id": "B-002",
            "disposition": "verified_fix",
            "verification_scope": ["TC-001"],
        }
    )
    regressed = PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 3,
            "event_id": "PE-0003",
            "idempotency_key": "PIK-0003",
            "ts": "2026-07-25T12:00:00Z",
            "evidence_digest": "sha256:ccddeeff",
            "problem_id": PROB_ID,
            "expected_problem_version": 2,
            "type": "problem_regressed",
            "occurrence_id": OCC_ID,
            "change_id": change_id,
        }
    )
    not_issue_detected = PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 4,
            "event_id": "PE-0004",
            "idempotency_key": "PIK-0004",
            "ts": "2026-07-25T13:00:00Z",
            "evidence_digest": "sha256:ddeeff00",
            "problem_id": PROB_ID_B,
            "expected_problem_version": 0,
            "type": "problem_detected",
            "occurrence_id": "OCC-bbbbbbbbbbbbbbbb",
            "change_id": change_id,
            "batch_id": "B-001",
            "fingerprint": ProblemFingerprint(version="1", digest="sha256:" + "b" * 64).model_dump(
                mode="json"
            ),
            "title": "Flaky locator",
            "classification": "test_bug",
            "severity": "medium",
        }
    )
    not_issue_marked = PROBLEM_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 5,
            "event_id": "PE-0005",
            "idempotency_key": "PIK-0005",
            "ts": "2026-07-25T14:00:00Z",
            "evidence_digest": "sha256:eff00112",
            "problem_id": PROB_ID_B,
            "expected_problem_version": 1,
            "type": "problem_marked_not_an_issue",
            "reason": "test selector drift",
            "evidence_refs": ["OCC-bbbbbbbbbbbbbbbb"],
        }
    )
    events = [detected, resolved, regressed, not_issue_detected, not_issue_marked]
    issues_dir = project_root / "qa" / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event.model_dump(mode="json"), separators=(",", ":")) for event in events)
        + "\n",
        encoding="utf-8",
    )
    projection = project_problems(events)  # type: ignore[arg-type]
    (issues_dir / "problems.json").write_bytes(dump_projection(projection))
    return {
        "occurrence": f"{change_id}#issue-CIE-0001",
        "detected": "project#problem-PE-0001",
        "resolved": "project#problem-PE-0002",
        "regressed": "project#problem-PE-0003",
        "not_an_issue": "project#problem-PE-0005",
    }
