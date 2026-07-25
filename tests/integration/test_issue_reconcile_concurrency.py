"""Integration test: two Changes with the same exact fingerprint reconcile correctly.

Proves:
- One problem_detected and one problem_occurrence_linked in the project store
  (no duplicate Problem identity regardless of ordering)
- Two unique Occurrences, one per Change
- Correct final Problem version (2)
- Stable byte-identical rebuild after re-reading the project ledger
- Idempotency: replaying either reconcile operation does not duplicate events

The test uses sequential reconciliation simulating what would happen when each
Change holds the project:issue-registry exclusive lock in turn.  This is
equivalent to the concurrent scenario because the lock serialises the read-
compute-write cycle; the test asserts the correct outcome of that serialised
execution without depending on Task 4's ProjectResourceLockManager
(which is tested separately in test_synchronized_project_resources.py).

Multiprocessing variant (via multiprocessing module):
We run two independent reconciler calls in separate processes that operate on
different change directories but the same shared project root.  The first
process runs first (acquiring the lock conceptually); the second runs after
the first completes.  This proves idempotency + single Problem identity across
real process boundaries.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    Observation,
    ObservationDocument,
    ObservationSource,
    ProblemProjection,
)
from assurance_agent.workflow.issues.events import read_problem_events
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from assurance_agent.workflow.issues.projection import project_problems, dump_projection
from assurance_agent.workflow.issues.reconciler import plan_reconciliation


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_observation(obs_id: str, change_id: str, batch_id: str) -> Observation:
    return Observation(
        observation_id=obs_id,
        change_id=change_id,
        batch_id=batch_id,
        kind="test_failure",
        target="api",
        case_id="test-case-1",
        source=ObservationSource(
            artifact=f"execution/runs/{batch_id}/api-result.json",
            json_pointer="/cases/0",
        ),
        evidence_refs=[f"execution/runs/{batch_id}/api-result.json"],
        signature="GET /api/v1/users returned HTTP 500",
        observed_at="2026-07-25T10:00:00Z",
    )


def _make_candidate(candidate_id: str, obs_id: str) -> IssueCandidate:
    """Make a candidate with a fixed surface/symptom (same fingerprint for all calls)."""
    return IssueCandidate(
        candidate_id=candidate_id,
        observation_ids=[obs_id],
        proposed=IssueCandidateProposed(
            title="API endpoint returns 500",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="Unhandled exception in user endpoint",
        ),
        affected_surface=AffectedSurface(kind="endpoint", value="GET /api/v1/users"),
        fingerprint_inputs=FingerprintInputs(
            surface="endpoint",
            symptom="returns http 500",
        ),
        possible_problem_ids=[],
        confidence=0.9,
        recommended_action="investigate",
    )


def _do_one_reconcile(
    change_dir: Path,
    project_root: Path,
    change_id: str,
    batch_id: str,
    obs_id: str,
    candidate_id: str,
    evidence_digest: str = "sha256:" + "a" * 64,
) -> ProblemProjection:
    """Run a single reconcile cycle for one Change, return the resulting ProblemProjection."""
    observation = _make_observation(obs_id, change_id, batch_id)
    observations = ObservationDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        observations=[observation],
    )
    candidate = _make_candidate(candidate_id, obs_id)
    candidates_doc = IssueCandidateDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        evidence_bundle_digest=evidence_digest,
        candidates=[candidate],
    )

    # Load current project projection
    problems_path = project_root / "qa" / "issues" / "problems.json"
    if problems_path.is_file():
        projection = ProblemProjection.model_validate(
            json.loads(problems_path.read_text(encoding="utf-8"))
        )
    else:
        projection = ProblemProjection(
            schema_version="1.0",
            generated_at="1970-01-01T00:00:00Z",
            problems=[],
        )

    from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot

    change_snapshot = ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=change_id,
        authoritative_batch_id=batch_id,
        observations=[observation],
        occurrences=[],
        analysis_status=None,
        project_sync_status="completed",
        batches=[batch_id],
    )

    plan = plan_reconciliation(candidates_doc, observations, change_snapshot, projection)

    # Append to Change store
    change_store = ChangeIssueStore(change_dir)
    change_store.append_and_rebuild(list(plan.change_events))

    # Append to Project store
    if plan.problem_events:
        problem_store = ProjectProblemStore(project_root)
        new_proj, _ = problem_store.append_and_rebuild(list(plan.problem_events))
        return new_proj

    return projection


# ---------------------------------------------------------------------------
# Worker function for multiprocessing
# ---------------------------------------------------------------------------


def _worker_reconcile(
    result_queue: multiprocessing.Queue,
    change_dir: str,
    project_root: str,
    change_id: str,
    batch_id: str,
    obs_id: str,
    candidate_id: str,
) -> None:
    """Top-level function for multiprocessing.Process (must be picklable)."""
    try:
        projection = _do_one_reconcile(
            change_dir=Path(change_dir),
            project_root=Path(project_root),
            change_id=change_id,
            batch_id=batch_id,
            obs_id=obs_id,
            candidate_id=candidate_id,
        )
        result_queue.put(("ok", projection.model_dump(mode="json")))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


# ---------------------------------------------------------------------------
# Sequential simulation test (primary correctness proof)
# ---------------------------------------------------------------------------


def test_two_changes_same_fingerprint_sequential() -> None:
    """Sequential simulation: Change A then Change B with same fingerprint.

    Proves:
    - One problem in final projection (no duplicates)
    - Problem version = 2 (detected + linked)
    - Two unique occurrences
    - Project ledger has one problem_detected + one problem_occurrence_linked
    - Rebuild is stable (byte-identical)
    """
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        change_a_dir = Path(tmp) / "changes" / "CH-A"
        change_b_dir = Path(tmp) / "changes" / "CH-B"
        change_a_dir.mkdir(parents=True)
        change_b_dir.mkdir(parents=True)

        # --- Change A reconciles first (empty project store) ---
        proj_after_a = _do_one_reconcile(
            change_dir=change_a_dir,
            project_root=project_root,
            change_id="CH-A",
            batch_id="BATCH-A",
            obs_id="OBS-A-001",
            candidate_id="CAND-A-001",
        )

        # After Change A: one problem detected
        assert len(proj_after_a.problems) == 1
        prob_a = proj_after_a.problems[0]
        assert prob_a.status == "detected"
        assert prob_a.version == 1
        assert len(prob_a.occurrences) == 1

        # --- Change B reconciles second (sees problem from A) ---
        proj_after_b = _do_one_reconcile(
            change_dir=change_b_dir,
            project_root=project_root,
            change_id="CH-B",
            batch_id="BATCH-B",
            obs_id="OBS-B-001",
            candidate_id="CAND-B-001",
        )

        # After Change B: still ONE problem, now version 2
        assert len(proj_after_b.problems) == 1, (
            f"Expected 1 problem, got {len(proj_after_b.problems)}"
        )
        prob_b = proj_after_b.problems[0]
        assert prob_b.problem_id == prob_a.problem_id, "Problem identity must be stable"
        assert prob_b.version == 2, f"Expected version 2, got {prob_b.version}"
        assert len(prob_b.occurrences) == 2, (
            f"Expected 2 occurrences, got {len(prob_b.occurrences)}"
        )

        # The two occurrences must be distinct
        occ_a = prob_b.occurrences[0]
        occ_b = prob_b.occurrences[1]
        assert occ_a != occ_b, "Two reconcile cycles must produce distinct occurrence IDs"

        # Change A store: occurrence_detected
        change_a_events = read_problem_events(project_root / "qa" / "issues" / "events.jsonl")
        project_event_types = [e.type for e in change_a_events]
        assert "problem_detected" in project_event_types
        assert "problem_occurrence_linked" in project_event_types
        assert project_event_types.count("problem_detected") == 1
        assert project_event_types.count("problem_occurrence_linked") == 1


def test_stable_rebuild_after_sequential_reconcile() -> None:
    """Re-reading and replaying the project ledger produces byte-identical output."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        change_a_dir = Path(tmp) / "changes" / "CH-A"
        change_b_dir = Path(tmp) / "changes" / "CH-B"
        change_a_dir.mkdir(parents=True)
        change_b_dir.mkdir(parents=True)

        _do_one_reconcile(
            change_dir=change_a_dir,
            project_root=project_root,
            change_id="CH-A",
            batch_id="BATCH-A",
            obs_id="OBS-A-001",
            candidate_id="CAND-A-001",
        )
        _do_one_reconcile(
            change_dir=change_b_dir,
            project_root=project_root,
            change_id="CH-B",
            batch_id="BATCH-B",
            obs_id="OBS-B-001",
            candidate_id="CAND-B-001",
        )

        # Read and rebuild from ledger
        events_path = project_root / "qa" / "issues" / "events.jsonl"
        events = read_problem_events(events_path)
        rebuilt = project_problems(events)

        # Serialize and re-serialize: byte-identical
        bytes1 = dump_projection(rebuilt)
        bytes2 = dump_projection(rebuilt)
        assert bytes1 == bytes2, "dump_projection must be deterministic"

        # Rebuild from the store is consistent with the written file
        problems_path = project_root / "qa" / "issues" / "problems.json"
        on_disk = problems_path.read_bytes()
        assert on_disk == bytes1, "On-disk projection must match rebuild from events"


def test_idempotent_replay_does_not_duplicate_problems() -> None:
    """Replaying the same plan events twice does not duplicate ledger entries.

    The operation-level idempotency guarantee is:
    - plan_reconciliation(same inputs) → same idempotency_keys
    - store.append_and_rebuild(same events) → deduplicates by idempotency_key
    - Final ledger has exactly the same events as the first call

    We test this by:
    1. Building a ReconciliationPlan with fixed inputs
    2. Appending its events to both stores (first time)
    3. Appending the exact same events again (second time)
    4. Verifying no duplicate events were written
    """
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        change_dir = Path(tmp) / "changes" / "CH-IDEM"
        change_dir.mkdir(parents=True)

        change_id = "CH-IDEM"
        batch_id = "BATCH-IDEM"
        evidence_digest = "sha256:" + "a" * 64

        observation = _make_observation("OBS-IDEM-001", change_id, batch_id)
        observations = ObservationDocument(
            schema_version="1.0",
            change_id=change_id,
            batch_id=batch_id,
            observations=[observation],
        )
        candidate = _make_candidate("CAND-IDEM-001", "OBS-IDEM-001")
        candidates_doc = IssueCandidateDocument(
            schema_version="1.0",
            change_id=change_id,
            batch_id=batch_id,
            evidence_bundle_digest=evidence_digest,
            candidates=[candidate],
        )

        from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot

        change_snapshot = ChangeIssueSnapshot(
            schema_version="1.0",
            change_id=change_id,
            authoritative_batch_id=batch_id,
            observations=[observation],
            occurrences=[],
            analysis_status=None,
            project_sync_status="completed",
            batches=[batch_id],
        )
        empty_problems = ProblemProjection(
            schema_version="1.0",
            generated_at="1970-01-01T00:00:00Z",
            problems=[],
        )

        # Build the plan ONCE with fixed inputs (empty projection)
        plan = plan_reconciliation(candidates_doc, observations, change_snapshot, empty_problems)

        # First append to stores
        change_store = ChangeIssueStore(change_dir)
        change_store.append_and_rebuild(list(plan.change_events))

        problem_store = ProjectProblemStore(project_root)
        proj1, _ = problem_store.append_and_rebuild(list(plan.problem_events))

        # Second append with SAME events → idempotency_key deduplication → no-op
        change_store.append_and_rebuild(list(plan.change_events))
        proj2, _ = problem_store.append_and_rebuild(list(plan.problem_events))

        # Version unchanged: no new events were appended
        assert len(proj2.problems) == 1
        assert proj2.problems[0].version == proj1.problems[0].version, (
            "Second append of same events must not change problem version"
        )

        # Project events.jsonl must have exactly 1 event (problem_detected)
        events = read_problem_events(project_root / "qa" / "issues" / "events.jsonl")
        assert len(events) == 1
        assert events[0].type == "problem_detected"

        # Change events.jsonl: issue_analysis_completed + occurrence_detected = 2 events
        from assurance_agent.workflow.issues.events import read_change_issue_events
        change_events = read_change_issue_events(change_dir / "issues" / "events.jsonl")
        assert len(change_events) == 2  # analysis_completed + occurrence_detected


def test_change_stores_have_correct_event_types() -> None:
    """Change A gets occurrence_detected; Change B gets occurrence_linked."""
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        change_a_dir = Path(tmp) / "changes" / "CH-A"
        change_b_dir = Path(tmp) / "changes" / "CH-B"
        change_a_dir.mkdir(parents=True)
        change_b_dir.mkdir(parents=True)

        from assurance_agent.workflow.issues.events import read_change_issue_events

        _do_one_reconcile(
            change_dir=change_a_dir,
            project_root=project_root,
            change_id="CH-A",
            batch_id="BATCH-A",
            obs_id="OBS-A-001",
            candidate_id="CAND-A-001",
        )
        _do_one_reconcile(
            change_dir=change_b_dir,
            project_root=project_root,
            change_id="CH-B",
            batch_id="BATCH-B",
            obs_id="OBS-B-001",
            candidate_id="CAND-B-001",
        )

        events_a = read_change_issue_events(change_a_dir / "issues" / "events.jsonl")
        events_b = read_change_issue_events(change_b_dir / "issues" / "events.jsonl")

        types_a = [e.type for e in events_a]
        types_b = [e.type for e in events_b]

        assert "issue_analysis_completed" in types_a
        assert "occurrence_detected" in types_a
        assert "occurrence_linked" not in types_a

        assert "issue_analysis_completed" in types_b
        assert "occurrence_linked" in types_b
        assert "occurrence_detected" not in types_b


# ---------------------------------------------------------------------------
# Multiprocessing test: real cross-process isolation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocessing fork not supported on Windows in test context",
)
def test_two_processes_same_fingerprint_single_problem_identity() -> None:
    """Real cross-process scenario: two Changes in different processes, same fingerprint.

    Uses sequential process execution (P1 then P2) to simulate the behaviour
    with the project:issue-registry exclusive lock held in turn by each process.
    Proves:
    - Exactly one Problem in the final projection
    - Problem version == 2
    - Two distinct occurrences
    """
    ctx = multiprocessing.get_context("fork")

    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_root.mkdir()
        change_a_dir = Path(tmp) / "changes" / "CH-P1"
        change_b_dir = Path(tmp) / "changes" / "CH-P2"
        change_a_dir.mkdir(parents=True)
        change_b_dir.mkdir(parents=True)

        q: multiprocessing.Queue = ctx.Queue()

        # Process 1: empty project store → problem_detected
        p1 = ctx.Process(
            target=_worker_reconcile,
            args=(
                q,
                str(change_a_dir),
                str(project_root),
                "CH-P1",
                "BATCH-P1",
                "OBS-P1-001",
                "CAND-P1-001",
            ),
        )
        p1.start()
        p1.join(timeout=30)
        assert p1.exitcode == 0, f"Process 1 exited with code {p1.exitcode}"

        result1_status, result1_data = q.get(timeout=5)
        assert result1_status == "ok", f"Process 1 failed: {result1_data}"
        proj1 = ProblemProjection.model_validate(result1_data)
        assert len(proj1.problems) == 1
        assert proj1.problems[0].version == 1

        # Process 2: project store has one problem → problem_occurrence_linked
        p2 = ctx.Process(
            target=_worker_reconcile,
            args=(
                q,
                str(change_b_dir),
                str(project_root),
                "CH-P2",
                "BATCH-P2",
                "OBS-P2-001",
                "CAND-P2-001",
            ),
        )
        p2.start()
        p2.join(timeout=30)
        assert p2.exitcode == 0, f"Process 2 exited with code {p2.exitcode}"

        result2_status, result2_data = q.get(timeout=5)
        assert result2_status == "ok", f"Process 2 failed: {result2_data}"
        proj2 = ProblemProjection.model_validate(result2_data)

        # Final assertions
        assert len(proj2.problems) == 1, (
            f"Expected exactly 1 problem, got {len(proj2.problems)}"
        )
        final_problem = proj2.problems[0]
        assert final_problem.problem_id == proj1.problems[0].problem_id, (
            "Problem identity must be the same across both processes"
        )
        assert final_problem.version == 2, (
            f"Expected version 2, got {final_problem.version}"
        )
        assert len(final_problem.occurrences) == 2, (
            f"Expected 2 occurrences, got {len(final_problem.occurrences)}"
        )
        assert final_problem.occurrences[0] != final_problem.occurrences[1], (
            "Two distinct occurrence IDs are required"
        )

        # Project ledger integrity: one problem_detected + one problem_occurrence_linked
        events = read_problem_events(project_root / "qa" / "issues" / "events.jsonl")
        event_types = [e.type for e in events]
        assert event_types.count("problem_detected") == 1
        assert event_types.count("problem_occurrence_linked") == 1
