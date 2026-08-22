from __future__ import annotations

from datetime import UTC, datetime

from tests.phase4.conformance import execute_task

from assurance_kernel.artifacts.models.metrics import MetricEntry as LegacyMetricEntry
from assurance_kernel.artifacts.models.metrics import MetricsDocument as LegacyMetricsDocument
from assurance_kernel.artifacts.models.policy import MetricFloor
from assurance_kernel.artifacts.models.pr_metric_evidence import (
    ConstraintCoverageEvidence as LegacyConstraintCoverageEvidence,
)
from assurance_kernel.artifacts.models.trace import TraceProjectionV2 as LegacyTraceProjectionV2
from assurance_kernel.evidence.coverage_gaps import build_coverage_gaps as legacy_build_coverage_gaps
from assurance_kernel.evidence.issue_identity import (
    ObservationIdentityInput as LegacyObservationIdentityInput,
)
from assurance_kernel.evidence.issue_identity import _canonical_sha256, _digest_prefix
from assurance_kernel.evidence.issue_identity import observation_id as legacy_observation_id
from assurance_kernel.evidence.issue_identity import problem_fingerprint as legacy_problem_fingerprint
from assurance_kernel.evidence.metrics import aggregate_pr_metrics
from assurance_kernel.evidence.metrics_sufficiency import numeric_below_floor_shortboards
from assurance_kernel.evidence.risk_tier import RiskTierResolution
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


def test_observation_id_matches_legacy_canonical_formula() -> None:
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
    legacy = LegacyObservationIdentityInput(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        kind="test_failure",
        target="api",
        case_id=CASE_ID,
        source_artifact="execution/api-result.json",
        source_json_pointer="/results/0/message",
        signature="boom",
    )
    assert observation_id(payload) == legacy_observation_id(legacy)


def test_problem_fingerprint_ignores_document_title() -> None:
    from assurance_quality.contracts.issues import AffectedSurface, FingerprintInputs
    from assurance_kernel.artifacts.models.issues import (
        AffectedSurface as LegacySurface,
    )
    from assurance_kernel.artifacts.models.issues import (
        FingerprintInputs as LegacyInputs,
    )

    surface = AffectedSurface(kind="module", value="Menus Service")
    inputs = FingerprintInputs(surface="menus", symptom="boom")
    first = problem_fingerprint(affected_surface=surface, fingerprint_inputs=inputs)
    second = problem_fingerprint(affected_surface=surface, fingerprint_inputs=inputs)
    assert first.digest == second.digest
    legacy = legacy_problem_fingerprint(
        affected_surface=LegacySurface(kind="module", value="Menus Service"),
        fingerprint_inputs=LegacyInputs(surface="menus", symptom="boom"),
        title="pretty title",
    )
    assert first.digest == legacy.digest


async def test_issue_handler_digest_matches_legacy_observation_id() -> None:
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


async def test_coverage_gaps_match_legacy_fold_on_closed_projection() -> None:
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
    raw = projection.model_dump(mode="json")
    for row in raw["rows"]:
        for key in ("capability", "plan_id", "schema_id", "issue_ids", "evidence_refs"):
            row.pop(key, None)
    legacy = legacy_build_coverage_gaps(
        LegacyTraceProjectionV2.model_validate(raw),
        None,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
    )
    assert [gap.kind for gap in new_gaps.gaps] == [gap.kind for gap in legacy.gaps]
    assert [gap.locator.model_dump(mode="json") for gap in new_gaps.gaps] == [
        gap.locator.model_dump(mode="json") for gap in legacy.gaps
    ]
    # Intentional: V2 catalog fields (capability/plan/test/issue/evidence) change
    # the projection digest relative to the kernel V1/V2 row shape.


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


def test_pr_constraint_value_matches_kernel_aggregate() -> None:
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
    raw = constraint.model_dump(mode="json")
    raw.pop("source_digest", None)
    legacy = aggregate_pr_metrics(
        change_id=CHANGE_ID,
        computed_at=datetime(2026, 8, 22, tzinfo=UTC),
        policy_digest=HEX_B,
        risk=RiskTierResolution(tier="medium", lower_bound="medium", declared=None),
        coverage_diff=None,
        constraint_coverage=LegacyConstraintCoverageEvidence.model_validate(raw),
        auth_matrix=None,
        journey_coverage=None,
        perf_slack=None,
        expected_batch_id=BATCH_ID,
    )
    assert quality.metrics["constraint_coverage"].value == legacy.metrics["constraint_coverage"].value
    # Intentional: kernel marks missing PR keys collection_failed and binds
    # evidence to the collector filename; quality skips those keys and hashes
    # the evidence object (now including source_digest).


def test_nightly_below_floor_matches_kernel_shortboard() -> None:
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
    legacy_doc = LegacyMetricsDocument.of(
        risk=RiskTierResolution(tier="medium", lower_bound="medium", declared=None),
        change_id=CHANGE_ID,
        cadence="nightly",
        computed_at=datetime(2026, 8, 22, tzinfo=UTC),
        metrics={
            "mutation_score": LegacyMetricEntry(
                layer="backend",
                status="evaluated",
                value=0.5,
                evidence="mutation.json",
            )
        },
        policy_digest=HEX_B,
    )
    legacy_boards = {
        (board.code, board.metric)
        for board in numeric_below_floor_shortboards(
            legacy_doc, {"mutation_score": MetricFloor(target="value", min=1.0)}
        )
    }
    assert quality_boards == {("below_floor", "mutation_score")}
    assert quality_boards == legacy_boards
    # Intentional: quality uses a fixed 1.0 threshold; kernel uses policy floors.


def test_review_id_matches_kernel_digest_formula() -> None:
    expected = "REV-" + _digest_prefix(
        _canonical_sha256({"problem_id": "PROB-1", "expected_problem_version": 1})
    )
    assert review_id("PROB-1", 1) == expected
    assert expected == "REV-d80c51678560d61f"
