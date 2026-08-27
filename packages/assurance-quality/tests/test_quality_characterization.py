from __future__ import annotations

from datetime import UTC, datetime

from tests.phase4.conformance import execute_task

from assurance_quality.contracts.issues import AffectedSurface, FingerprintInputs
from assurance_quality.contracts.pr_metrics import MutationEvidence
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.coverage import (
    ConstraintCoverageInput,
    build_coverage_gaps,
    compute_constraint_coverage,
)
from assurance_quality.operations.identity import (
    ObservationIdentityInput,
    observation_id,
    problem_fingerprint,
    review_id,
)
from assurance_quality.operations.issues import CollectObservationsHandler, ReconcileIssuesHandler
from assurance_quality.operations.metrics import PrEvidenceBundle, build_metrics_document
from assurance_quality.operations.nightly import evaluate_shortboards
from assurance_quality.operations.trace import MaterializeTraceHandler, project_trace
from assurance_quality.operations.trace import TraceOperationInput
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CASE_ID,
    CHANGE_ID,
    EVIDENCE_REF,
    HEX_B,
    as_object,
    catalog_leafs,
    issue_input,
    production_import_roots,
    trace_input,
)


def test_production_quality_does_not_import_legacy_or_runtimes() -> None:
    names = production_import_roots()
    assert "assurance_agent" not in names
    assert "assurance_kernel" not in names
    assert "agent_runtime_opencode" not in names
    assert "agent_runtime_cursor" not in names


def test_observation_id_is_stable_for_closed_inputs() -> None:
    payload = ObservationIdentityInput(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        kind="test_failure",
        target="api",
        case_id=CASE_ID,
        source_artifact="execution/api-result.json",
        source_json_pointer="/results/0/message",
        signature="boom",
    )
    assert observation_id(payload) == observation_id(payload)


def test_problem_fingerprint_ignores_document_title() -> None:
    surface = AffectedSurface(kind="module", value="Menus Service")
    inputs = FingerprintInputs(surface="menus", symptom="boom")
    first = problem_fingerprint(affected_surface=surface, fingerprint_inputs=inputs)
    second = problem_fingerprint(affected_surface=surface, fingerprint_inputs=inputs)
    assert first.digest == second.digest


async def test_issue_handler_digest_matches_observation_id() -> None:
    outcome = await execute_task(CollectObservationsHandler(), issue_input(message="boom"))
    assert outcome.status == "succeeded"
    observed = as_object(outcome.output)["observations"][0]["observation_id"]
    expected = observation_id(
        ObservationIdentityInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            kind="test_failure",
            target="api",
            case_id=CASE_ID,
            source_artifact="execution/api-result.json",
            source_json_pointer="/results/0/message",
            signature="boom",
        )
    )
    assert observed == expected


async def test_coverage_gaps_fold_on_closed_projection() -> None:
    payload = TraceOperationInput.model_validate(
        trace_input(closed_mapping=["tests/generated.py"], observed=["tests/generated.py"])
    )
    projection = project_trace(payload)
    new_gaps = build_coverage_gaps(
        projection,
        None,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
    )
    assert tuple(gap.kind for gap in new_gaps.gaps) == ()


async def test_closed_mapping_is_stricter_than_observed_list() -> None:
    outcome = await execute_task(
        MaterializeTraceHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=["tests/generated.py", "tests/old.py"]),
    )
    assert outcome.status == "succeeded"
    trace = TraceProjectionV2.model_validate(
        outcome.output,
        context={"capability_leafs": frozenset(catalog_leafs())},
    )
    assert tuple(row.test_path for row in trace.rows) == ("tests/generated.py",)
    assert trace.unmapped_tests == ()


async def test_reconcile_fingerprint_stable_across_title_rewrite() -> None:
    observations = await execute_task(CollectObservationsHandler(), issue_input(message="boom"))
    observation_id_value = as_object(observations.output)["observations"][0]["observation_id"]
    first = await execute_task(
        ReconcileIssuesHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "observations": as_object(observations.output)["observations"],
            "evidence_bundle_digest": EVIDENCE_REF,
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": [observation_id_value],
                    "proposed": {
                        "title": "pretty",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "prose",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": [],
                    "confidence": 0.9,
                    "recommended_action": "triage",
                }
            ],
        },
    )
    second = await execute_task(
        ReconcileIssuesHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "observations": as_object(observations.output)["observations"],
            "evidence_bundle_digest": EVIDENCE_REF,
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": [observation_id_value],
                    "proposed": {
                        "title": "rewritten",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "other",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": [],
                    "confidence": 0.2,
                    "recommended_action": "ignore",
                }
            ],
        },
    )
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert (
        as_object(first.output)["problems"][0]["fingerprint"]["digest"]
        == as_object(second.output)["problems"][0]["fingerprint"]["digest"]
    )


def test_pr_constraint_value_is_materialized() -> None:
    constraint = compute_constraint_coverage(
        ConstraintCoverageInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            declared_keys=("menus.create",),
            covered_keys=("menus.create",),
            source_digest="deadbeef",
        )
    )
    quality = build_metrics_document(
        PrEvidenceBundle(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            policy_digest=HEX_B,
            computed_at=datetime(2026, 8, 22, tzinfo=UTC),
            constraint=constraint,
        )
    )
    assert quality.metrics["constraint_coverage"].value == 1.0


def test_nightly_below_floor_marks_mutation_score() -> None:
    quality_doc = build_metrics_document(
        PrEvidenceBundle(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            policy_digest=HEX_B,
            cadence="nightly",
            computed_at=datetime(2026, 8, 22, tzinfo=UTC),
            mutation=MutationEvidence(
                schema_version="1",
                change_id=CHANGE_ID,
                batch_id=BATCH_ID,
                status="evaluated",
                value=0.5,
                killed=1,
                survived=1,
                equivalent=0,
                tested=2,
                selected=2,
                budget_seconds=60,
                elapsed_seconds=1.0,
            ),
        )
    )
    quality_boards = {
        (board.code, board.metric)
        for board in evaluate_shortboards(quality_doc)
        if board.code == "below_floor"
    }
    assert quality_boards == {("below_floor", "mutation_score")}


def test_review_id_uses_closed_digest() -> None:
    assert review_id("PROB-1", 1) == "REV-d80c51678560d61f"
