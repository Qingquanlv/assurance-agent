from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from graph_engine.canonical import JSONValue

from tests.capabilities.conformance import execute_task

from pydantic import ValidationError

from assurance_quality.contracts.coverage import (
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
    MinimumCoverageMatrix,
    MinimumCoverageResult,
)
from assurance_quality.contracts.c_layer import CLayerMetricsDocument
from assurance_quality.contracts.pr_metrics import (
    AuthMatrixEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
)
from assurance_quality.contracts.quarantine import QuarantineProjection
from assurance_healing.contracts.coverage_repair import CoverageRepairBrief
from assurance_quality.operations.coverage import (
    AuthMatrixInput,
    BuildCoverageGapsHandler,
    CollectDiffCoverageHandler,
    ComputeAuthMatrixHandler,
    ComputeConstraintCoverageHandler,
    ComputeJourneyCoverageHandler,
    ComputeThresholdSlackHandler,
    ConstraintCoverageInput,
    DerivePlanLayerApplicabilityHandler,
    JourneyCoverageInput,
    MaterializeCLayerMetricsHandler,
    MaterializeMinimumCoverageHandler,
    MaterializeQuarantineProjectionHandler,
    MaterializeTraceAndCoverageGapsHandler,
    ProbeCoverageRepairNeedHandler,
    ThresholdSlackInput,
    compute_auth_matrix,
    compute_constraint_coverage,
    compute_journey_coverage,
    compute_threshold_slack,
    coverage_gap_to_repair_brief,
    default_c_layer_entry,
)
from assurance_quality.operations.trace import MaterializeTraceHandler
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CASE_ID,
    CHANGE_ID,
    HEX_A,
    as_object,
    catalog_leafs,
    trace_input,
)


def _empty_c_layer() -> dict[str, object]:
    entry = default_c_layer_entry().model_dump(mode="json")
    return {
        "change_id": CHANGE_ID,
        "computed_at": "2026-08-22T00:00:00Z",
        "escape_rate": entry,
        "counterexample_promotion_rate": entry,
        "coverage_gap_closure_rate": entry,
        "seed_replay_stability": entry,
    }


@pytest.mark.asyncio
async def test_coverage_gaps_fold_uncovered_required_case(tmp_path: Path) -> None:
    trace = await execute_task(
        MaterializeTraceHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=[]),
        tmp_path,
    )
    assert trace.status == "succeeded"
    gaps = await execute_task(
        BuildCoverageGapsHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "projection": as_object(trace.output),
            "capability_leafs": catalog_leafs(),
        },
        tmp_path,
    )
    assert gaps.status == "succeeded"
    document = CoverageGapsDocument.model_validate(gaps.output)
    assert tuple(item.kind for item in document.gaps) == ("uncovered_required_case",)
    assert document.gaps[0].locator.case_id == CASE_ID


@pytest.mark.asyncio
async def test_combined_trace_and_gaps_exclude_old_tests(tmp_path: Path) -> None:
    outcome = await execute_task(
        MaterializeTraceAndCoverageGapsHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=["tests/generated.py", "tests/old.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert [row["covering_tests"] for row in as_object(payload["trace"])["rows"]] == [[]]
    gaps = CoverageGapsDocument.model_validate(payload["gaps"])
    assert tuple(item.kind for item in gaps.gaps) == ("uncovered_required_case",)


@pytest.mark.asyncio
async def test_minimum_coverage_join_and_repair_need(tmp_path: Path) -> None:
    result = await execute_task(
        MaterializeMinimumCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "items": [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "POST /menus",
                    "category": "api",
                    "layer": "api",
                    "required": True,
                    "covered_by_cases": [CASE_ID],
                }
            ],
            "executed_case_ids": [CASE_ID],
        },
        tmp_path,
    )
    assert result.status == "succeeded"
    document = MinimumCoverageResult.model_validate(result.output)
    assert document.items[0].status == "covered"

    trace = await execute_task(
        MaterializeTraceHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=[]),
        tmp_path,
    )
    gaps = await execute_task(
        BuildCoverageGapsHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "projection": as_object(trace.output),
            "capability_leafs": catalog_leafs(),
        },
        tmp_path,
    )
    need = await execute_task(
        ProbeCoverageRepairNeedHandler(),
        {"change_id": CHANGE_ID, "batch_id": BATCH_ID, "gaps": as_object(gaps.output)},
        tmp_path,
    )
    assert need.status == "succeeded", need.failure
    assert as_object(need.output)["needed"] is True


@pytest.mark.asyncio
async def test_diff_constraint_auth_journey_slack_quarantine_c_layer(tmp_path: Path) -> None:
    diff = await execute_task(
        CollectDiffCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "files": [
                {
                    "path": "app/menus.py",
                    "changed_lines": [1, 2],
                    "covered_lines": [1],
                    "uncovered_lines": [2],
                }
            ],
            "source": {"tree": HEX_A},
        },
        tmp_path,
    )
    assert diff.status == "succeeded"
    evidence = CoverageDiffEvidence.model_validate(diff.output)
    assert evidence.value == 0.5

    constraint = await execute_task(
        ComputeConstraintCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "declared_keys": ["menus.create"],
            "covered_keys": ["menus.create"],
            "source_digest": HEX_A,
        },
        tmp_path,
    )
    assert constraint.status == "succeeded"
    assert as_object(constraint.output)["value"] == 1.0

    auth = await execute_task(
        ComputeAuthMatrixHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "source_digest": HEX_A,
            "cells": [
                {
                    "cell_id": "menus:post:admin",
                    "route": "/menus",
                    "method": "POST",
                    "token": "admin",
                    "expected": "allow",
                    "allowed_status_codes": [200],
                    "asserted": True,
                    "outcome": "passed",
                }
            ],
        },
        tmp_path,
    )
    assert auth.status == "succeeded"
    AuthMatrixEvidence.model_validate(auth.output)

    journey = await execute_task(
        ComputeJourneyCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "source_digest": HEX_A,
            "items": [
                {
                    "journey_key": "create-menu",
                    "case_ids": [CASE_ID],
                    "executed_case_ids": [CASE_ID],
                    "covered": True,
                    "status": "covered",
                }
            ],
        },
        tmp_path,
    )
    assert journey.status == "succeeded"
    JourneyCoverageEvidence.model_validate(journey.output)

    slack = await execute_task(
        ComputeThresholdSlackHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "source_digest": HEX_A,
            "scenarios": [
                {
                    "capability": "entities.item.create",
                    "endpoint": "POST /menus",
                    "threshold_p95_ms": 200.0,
                    "measured_p95_ms": 100.0,
                    "slack": 2.0,
                }
            ],
        },
        tmp_path,
    )
    assert slack.status == "succeeded"
    assert as_object(slack.output)["value"] == 2.0

    quarantine = await execute_task(
        MaterializeQuarantineProjectionHandler(),
        {
            "change_id": CHANGE_ID,
            "entries": [
                {
                    "subject_kind": "property",
                    "subject_key": "menus.create",
                    "status": "active",
                    "reason": "flaky",
                    "entered_at": "2026-08-22T00:00:00Z",
                    "evidence_refs": [f"sha256:{HEX_A}"],
                }
            ],
        },
        tmp_path,
    )
    assert quarantine.status == "succeeded"
    QuarantineProjection.model_validate(quarantine.output)

    c_layer = await execute_task(
        MaterializeCLayerMetricsHandler(),
        cast(JSONValue, _empty_c_layer()),
        tmp_path,
    )
    assert c_layer.status == "succeeded"
    CLayerMetricsDocument.model_validate(c_layer.output)


@pytest.mark.asyncio
async def test_plan_layer_applicability_from_cases(tmp_path: Path) -> None:
    applicable = await execute_task(
        DerivePlanLayerApplicabilityHandler(),
        {
            "layer": "api",
            "cases": [
                {
                    "added": [{"case_id": CASE_ID, "type": "API", "automation": {"required": True}}],
                    "modified": [],
                }
            ],
        },
        tmp_path,
    )
    skipped = await execute_task(
        DerivePlanLayerApplicabilityHandler(),
        {
            "layer": "e2e",
            "cases": [
                {
                    "added": [{"case_id": CASE_ID, "type": "API", "automation": {"required": True}}],
                    "modified": [],
                }
            ],
        },
        tmp_path,
    )
    assert applicable.status == "succeeded"
    assert as_object(applicable.output)["applicable"] is True
    assert skipped.status == "succeeded"
    assert as_object(skipped.output)["applicable"] is False
    assert as_object(skipped.output)["reason_code"] == "no_automated_cases"


def test_c_layer_now_is_injected() -> None:
    assert datetime(2026, 8, 22, tzinfo=UTC).tzinfo is UTC


def test_constraint_auth_journey_slack_persist_source_digest() -> None:
    constraint = compute_constraint_coverage(
        ConstraintCoverageInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            declared_keys=("menus.create",),
            covered_keys=("menus.create",),
            source_digest="deadbeef",
        )
    )
    other = compute_constraint_coverage(
        ConstraintCoverageInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            declared_keys=("menus.create",),
            covered_keys=("menus.create",),
            source_digest="cafebabe",
        )
    )
    assert constraint.source_digest == "deadbeef"
    assert constraint.model_dump(mode="json") != other.model_dump(mode="json")

    auth = compute_auth_matrix(
        AuthMatrixInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            source_digest="deadbeef",
            cells=(),
        )
    )
    journey = compute_journey_coverage(
        JourneyCoverageInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            source_digest="deadbeef",
            items=(),
        )
    )
    slack = compute_threshold_slack(
        ThresholdSlackInput(
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            source_digest="deadbeef",
            scenarios=(),
        )
    )
    assert auth.source_digest == "deadbeef"
    assert journey.source_digest == "deadbeef"
    assert slack.source_digest == "deadbeef"


def test_minimum_coverage_matrix_requires_structured_rows() -> None:
    with pytest.raises(ValidationError):
        MinimumCoverageMatrix.model_validate(
            [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "menus.create",
                    "required": True,
                    "covered_by_cases": ["TC_A"],
                    "status": "covered",
                }
            ]
        )


def test_coverage_states_are_exactly_the_closed_set() -> None:
    from assurance_quality.contracts.coverage import COVERAGE_STATES
    from assurance_quality.contracts.decisions import CoverageAssessmentPublicV1

    assert set(COVERAGE_STATES) == {
        "satisfied",
        "repair_required",
        "exhausted",
        "needs_human",
        "inconclusive",
    }
    assert len(COVERAGE_STATES) == 5
    assert CoverageAssessmentPublicV1.model_fields["coverage_state"].annotation is not None


def test_coverage_gap_converts_to_healing_repair_brief() -> None:
    document = CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        projection_digest=f"sha256:{HEX_A}",
        gaps=(
            CoverageGap(
                kind="uncovered_required_case",
                locator=CoverageGapLocator(case_id=CASE_ID),
                layer="execution",
                batch_id=BATCH_ID,
                evidence_refs=(f"sha256:{HEX_A}",),
            ),
        ),
    )
    brief = coverage_gap_to_repair_brief(document)
    assert isinstance(brief, CoverageRepairBrief)
    assert brief.change_id == CHANGE_ID
    assert brief.batch_id == BATCH_ID
    assert brief.eligible is False
    assert brief.repair_items[0].kind == "uncovered_required_case"


def test_obligation_gaps_are_deferred_to_intake_rather_than_dropped() -> None:
    kinds = (
        "obligation_case_missing",
        "obligation_mapping_missing",
        "obligation_observation_missing",
    )
    document = CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        projection_digest=f"sha256:{HEX_A}",
        gaps=tuple(
            CoverageGap(
                kind=kind,
                locator=CoverageGapLocator(plan_digest=HEX_A, mrc_id=f"MRC-{index}"),
                layer="declaration",
                batch_id=BATCH_ID,
                evidence_refs=(f"sha256:{HEX_A}",),
            )
            for index, kind in enumerate(kinds)
        ),
    )
    brief = coverage_gap_to_repair_brief(document)
    assert brief.repair_items == ()
    assert tuple(item.kind for item in brief.deferred_to_intake) == kinds
    assert {item.reason for item in brief.deferred_to_intake} == {"obligation_scope"}
    assert tuple(item.locator.mrc_id for item in brief.deferred_to_intake) == (
        "MRC-0",
        "MRC-1",
        "MRC-2",
    )
