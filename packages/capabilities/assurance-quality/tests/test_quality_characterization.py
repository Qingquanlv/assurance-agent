from __future__ import annotations

from datetime import UTC, datetime


from assurance_quality.contracts.issues import AffectedSurface, FingerprintInputs
from assurance_quality.contracts.metrics import MetricScope
from assurance_quality.contracts.pr_metrics import ConstraintCoverageEvidence
from assurance_quality.operations.coverage import build_coverage_gaps
from assurance_quality.operations.identity import (
    ObservationIdentityInput,
    observation_id,
    problem_fingerprint,
    review_id,
)
from assurance_quality.operations.metrics import PrEvidenceBundle, build_metrics_document
from assurance_quality.operations.trace import project_trace
from assurance_quality.operations.trace import TraceOperationInput
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CASE_ID,
    CHANGE_ID,
    HEX_B,
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
    assert observation_id(payload) == "OBS-f690592fc607219e"


def test_problem_fingerprint_ignores_document_title() -> None:
    surface = AffectedSurface(kind="module", value="Menus Service")
    first = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=FingerprintInputs(surface="menus", symptom="boom"),
    )
    rewritten_title = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=FingerprintInputs(surface="rewritten-title", symptom="boom"),
    )
    changed_symptom = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=FingerprintInputs(surface="menus", symptom="crash"),
    )
    assert first.digest == rewritten_title.digest
    assert first.digest != changed_symptom.digest


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
    assert tuple(gap.kind for gap in new_gaps.gaps) == ("uncovered_required_case",)


def test_pr_constraint_value_is_materialized() -> None:
    constraint = ConstraintCoverageEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        declared=MetricScope.of(total=1, covered=1),
        value=1.0,
        source_digest="deadbeef",
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


def test_review_id_uses_closed_digest() -> None:
    assert review_id("PROB-1", 1) == "REV-d80c51678560d61f"
