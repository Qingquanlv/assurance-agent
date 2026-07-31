"""Specialty report v2/v3 model invariants and integrity derivation."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Literal, cast, get_args

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.artifacts.models.policy import (
    CoverageFloor,
    EvidenceSufficiency,
    FuzzPolicy,
    HealingPolicy,
    Policy,
)
from assurance_agent.artifacts.models.trace import TraceExecution, TraceProjection, TraceRow
from assurance_agent.artifacts.policy import policy_digest
from assurance_agent.eval.specialty_models import (
    CompleteLayerRow,
    CompleteTraceabilityEvidenceV3,
    CoverageSummary,
    IncompleteLayerRow,
    IncompleteTraceabilityEvidenceV3,
    LegacySpecialtyReportV1,
    NotSelectedLayerRow,
    NotWiredLayerRow,
    ProjectionOverview,
    SpecialtyReportV2,
    SpecialtyReportV3,
    TraceCollectionFailureReason,
    TraceCommandStatus,
    TracePhaseEvidence,
    TraceabilityEvidenceV3,
    VerifyDiagnostics,
    build_capability_replay_v2,
    load_specialty_report,
    load_specialty_report_document,
)
from assurance_agent.eval.specialty_render import render_specialty_sections
from assurance_agent.evidence.layer_summary import join_layer_sufficiency
from assurance_agent.evidence.sufficiency import evaluate_sufficiency
from tests.helpers_aa import AWARE_NOW


def _check_summary(*, check_id: str, status: str = "pass", finding_count: int = 0) -> dict[str, object]:
    return {"check_id": check_id, "status": status, "finding_count": finding_count}


def _mechanical(*, status: str = "pass", finding_count: int = 0) -> dict[str, object]:
    checks = [
        _check_summary(check_id=check_id, status=status, finding_count=0) for check_id in PLAN_CHECK_IDS
    ]
    if status == "fail" and finding_count:
        checks[1] = _check_summary(check_id="shared_factory", status="fail", finding_count=finding_count)
    return {"status": status, "finding_count": finding_count, "checks": checks}


def _scenario(action: str, *, verdict: str = "pass") -> dict[str, object]:
    return {
        "action": action,
        "policy_digest": f"digest-{action}",
        "verdict": verdict,
        "route": verdict if verdict in {"pass", "reject", "needs_human_review", "skip"} else "pass",
        "matched_rule": "pass_when",
        "reason": "ok",
        "missing_capabilities": [],
        "policy_effect": "no_failed_checks",
    }


def _complete_row(
    layer: str,
    *,
    case_type: str,
    applicability: str = "applicable",
    inapplicable: bool = False,
) -> dict[str, object]:
    if inapplicable:
        return {
            "layer": layer,
            "case_type": case_type,
            "status": "complete",
            "reason_code": None,
            "applicability": "not_applicable",
            "gate_id": f"{layer}-plan-review-gate",
            "review_artifact": "review/api-plan-review.json" if layer == "api" else "review/plan-review.json",
            "checks_artifact": f"review/{layer}-plan-checks.json",
            "capabilities": None,
            "mechanical_checks": {
                "status": "pass",
                "finding_count": 0,
                "checks": [
                    _check_summary(check_id=check_id, status="not_applicable", finding_count=0)
                    for check_id in PLAN_CHECK_IDS
                ],
            },
            "mechanical_execution_contract_digest": f"mech-{layer}",
            "evidence_digests": {
                "review": None,
                "checks": f"checks-{layer}",
                "data_knowledge": None,
            },
            "scenarios": [
                _scenario("warn", verdict="skip"),
                _scenario("block", verdict="skip"),
                _scenario("require_human", verdict="skip"),
            ],
        }
    return {
        "layer": layer,
        "case_type": case_type,
        "status": "complete",
        "reason_code": None,
        "applicability": applicability,
        "gate_id": f"{layer}-plan-review-gate",
        "review_artifact": "review/api-plan-review.json" if layer == "api" else "review/plan-review.json",
        "checks_artifact": f"review/{layer}-plan-checks.json",
        "capabilities": {"required": ["auth.api_admin_token"], "missing": []},
        "mechanical_checks": _mechanical(status="pass"),
        "mechanical_execution_contract_digest": f"mech-{layer}",
        "evidence_digests": {
            "review": f"review-{layer}",
            "checks": f"checks-{layer}",
            "data_knowledge": f"l1-{layer}",
        },
        "scenarios": [
            _scenario("warn"),
            _scenario("block", verdict="reject"),
            _scenario("require_human", verdict="needs_human_review"),
        ],
    }


def _definition_binding() -> dict[str, object]:
    return {
        "root_invocation_id": "inv-root",
        "assurance_invocation_id": "inv-assurance",
        "graph_digest": "graph-digest",
        "gate_definition_source": "pinned_schema",
        "baseline_policy_digest": "policy-digest",
        "policy_source": "pinned_runtime_snapshot",
        "policy_origin": "project",
        "gate_semantics_digest": "semantics-digest",
        "assurance_profile_digest": "profile-digest",
    }


def _four_complete_rows() -> list[dict[str, object]]:
    return [
        _complete_row("api", case_type="API"),
        _complete_row("e2e", case_type="E2E"),
        {"layer": "fuzz", "case_type": "Fuzz", "status": "not_wired", "reason_code": None},
        {"layer": "performance", "case_type": "Performance", "status": "not_selected", "reason_code": None},
    ]


def test_complete_row_requires_plan_check_ids_in_order() -> None:
    row = CompleteLayerRow.model_validate(_complete_row("api", case_type="API"))
    assert [item.check_id for item in row.mechanical_checks.checks] == list(PLAN_CHECK_IDS)


def test_complete_row_requires_three_scenarios_in_action_order() -> None:
    row = CompleteLayerRow.model_validate(_complete_row("api", case_type="API"))
    assert [item.action for item in row.scenarios] == ["warn", "block", "require_human"]


def test_inapplicable_complete_row_requires_null_capabilities_and_skip_scenarios() -> None:
    row = CompleteLayerRow.model_validate(_complete_row("api", case_type="API", inapplicable=True))
    assert row.capabilities is None
    assert row.evidence_digests.review is None
    assert row.evidence_digests.data_knowledge is None
    assert row.evidence_digests.checks == "checks-api"
    assert all(item.verdict == "skip" for item in row.scenarios)


def test_mechanical_aggregate_reconciles_finding_count() -> None:
    payload: dict[str, object] = _complete_row("api", case_type="API")
    mechanical = payload["mechanical_checks"]
    assert isinstance(mechanical, dict)
    mechanical["finding_count"] = 99
    with pytest.raises(ValidationError, match="finding_count"):
        CompleteLayerRow.model_validate(payload)


def test_build_capability_replay_v2_derives_complete_integrity() -> None:
    replay = build_capability_replay_v2(
        definition_binding=_definition_binding(),
        rows=_four_complete_rows(),
    )
    assert replay.semantics == "counterfactual_plan_check_actions/v2"
    assert replay.integrity == "complete"
    assert [row.layer for row in replay.rows] == list(LAYER_NAMES)


def test_loader_and_renderer_accept_frozen_semantics_v1_unchanged() -> None:
    capability = build_capability_replay_v2(
        definition_binding=_definition_binding(),
        rows=_four_complete_rows(),
        semantics="counterfactual_plan_check_actions/v1",
    )
    assert capability.semantics == "counterfactual_plan_check_actions/v1"
    payload = {
        "schema_version": "2",
        "change_id": "CH-FROZEN-V1",
        "capability_contract_policy": capability.model_dump(mode="json"),
        "traceability_evidence": {
            "command_status": {"trace_exit": 0, "verify_exit": 0},
            "execution_projection": {
                "phase": "execution",
                "batch_id": "b",
                "integrity": "complete",
                "row_count": 0,
                "source_count": 0,
                "gap_count": 0,
                "unmapped_test_count": 0,
            },
            "reconciled_projection": {
                "phase": "reconciled",
                "batch_id": "b",
                "integrity": "complete",
                "row_count": 0,
                "source_count": 0,
                "gap_count": 0,
                "unmapped_test_count": 0,
                "failure_row_count": 0,
                "failure_link_count": 0,
                "open_problem_row_count": 0,
                "open_problem_link_count": 0,
                "unique_open_problem_count": 0,
            },
            "sufficiency": {"sufficient_count": 0, "insufficient_count": 0, "reason_counts": {}},
            "coverage": {"status": "PASS", "line": 0.0, "branch": 0.0, "final_status": "PASS"},
            "verify": {
                "phase": "verify",
                "verdict": "pass",
                "policy_digest": "p",
                "projection_digest": "d",
                "blocking_gap_count": 0,
                "open_problem_count": 0,
                "reported_insufficient_count": 0,
                "observed_insufficient_count": 0,
            },
        },
    }
    report = load_specialty_report(payload)
    assert isinstance(report, SpecialtyReportV2)
    assert report.capability_contract_policy.semantics == "counterfactual_plan_check_actions/v1"
    rendered = render_specialty_sections([report])
    assert "| `CH-FROZEN-V1` | fuzz | not_wired | not_wired | not_wired |" in rendered
    assert "counterfactual_plan_check_actions/v1" in rendered


def test_build_capability_replay_v2_rejects_false_complete_claim() -> None:
    rows = _four_complete_rows()
    rows[0] = {
        "layer": "api",
        "case_type": "API",
        "status": "incomplete",
        "reason_code": "gate_evidence_unbound",
    }
    with pytest.raises(ValidationError, match="integrity"):
        build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows, integrity="complete")


def test_build_capability_replay_v2_forces_incomplete_when_selected_wired_row_incomplete() -> None:
    rows = _four_complete_rows()
    rows[0] = {
        "layer": "api",
        "case_type": "API",
        "status": "incomplete",
        "reason_code": "gate_evidence_unbound",
    }
    replay = build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows)
    assert replay.integrity == "incomplete"


def test_build_capability_replay_v2_rejects_unknown_layer() -> None:
    rows = _four_complete_rows()
    rows[0]["layer"] = "unknown"
    with pytest.raises(ValidationError, match="layer"):
        build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows)


def test_build_capability_replay_v2_rejects_reordered_layers() -> None:
    rows = _four_complete_rows()
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ValidationError, match="order"):
        build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows)


def test_build_capability_replay_v2_rejects_duplicate_actions() -> None:
    payload = _complete_row("api", case_type="API")
    payload["scenarios"] = [_scenario("warn"), _scenario("warn"), _scenario("require_human")]
    with pytest.raises(ValidationError, match="scenarios"):
        CompleteLayerRow.model_validate(payload)


def test_not_selected_and_not_wired_rows_are_minimal() -> None:
    NotSelectedLayerRow.model_validate(
        {"layer": "performance", "case_type": "Performance", "status": "not_selected", "reason_code": None}
    )
    NotWiredLayerRow.model_validate(
        {"layer": "fuzz", "case_type": "Fuzz", "status": "not_wired", "reason_code": None}
    )


def test_incomplete_row_requires_reason_code() -> None:
    with pytest.raises(ValidationError):
        IncompleteLayerRow.model_validate(
            {"layer": "api", "case_type": "API", "status": "incomplete", "reason_code": None}
        )


def test_load_specialty_report_discriminates_v2_and_v1() -> None:
    v2 = load_specialty_report(
        {
            "schema_version": "2",
            "change_id": "CH-1",
            "capability_contract_policy": build_capability_replay_v2(
                definition_binding=_definition_binding(),
                rows=_four_complete_rows(),
            ).model_dump(mode="json"),
            "traceability_evidence": {"command_status": {"trace_exit": 0, "verify_exit": 0}},
        }
    )
    assert isinstance(v2, SpecialtyReportV2)

    v1 = load_specialty_report(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "capability_contract_policy": {"capabilities": {"required": [], "missing": []}},
            "traceability_evidence": {},
        }
    )
    assert isinstance(v1, LegacySpecialtyReportV1)


def test_semantics_v1_rejects_wired_layer_not_wired() -> None:
    rows = _four_complete_rows()
    rows[0] = {"layer": "api", "case_type": "API", "status": "not_wired", "reason_code": None}
    with pytest.raises(ValidationError, match="not_wired"):
        build_capability_replay_v2(
            definition_binding=_definition_binding(),
            rows=rows,
            semantics="counterfactual_plan_check_actions/v1",
        )


def test_semantics_v1_rejects_forged_fuzz_performance_complete() -> None:
    rows = _four_complete_rows()
    rows[2] = _complete_row("fuzz", case_type="Fuzz")
    with pytest.raises(ValidationError, match="cannot have status complete"):
        build_capability_replay_v2(
            definition_binding=_definition_binding(),
            rows=rows,
            semantics="counterfactual_plan_check_actions/v1",
        )


def test_semantics_v2_allows_topology_driven_fuzz_complete_row() -> None:
    rows = _four_complete_rows()
    rows[2] = _complete_row("fuzz", case_type="Fuzz")
    replay = build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows)
    assert replay.semantics == "counterfactual_plan_check_actions/v2"
    assert replay.rows[2].status == "complete"
    assert replay.rows[2].layer == "fuzz"


def test_semantics_v2_allows_api_not_wired_without_static_sets() -> None:
    rows = _four_complete_rows()
    rows[0] = {"layer": "api", "case_type": "API", "status": "not_wired", "reason_code": None}
    replay = build_capability_replay_v2(definition_binding=_definition_binding(), rows=rows)
    assert replay.semantics == "counterfactual_plan_check_actions/v2"
    assert replay.rows[0].status == "not_wired"


def test_build_capability_replay_v2_rejects_false_incomplete_claim() -> None:
    with pytest.raises(ValidationError, match="integrity"):
        build_capability_replay_v2(
            definition_binding=_definition_binding(),
            rows=_four_complete_rows(),
            integrity="incomplete",
        )


# ---------------------------------------------------------------------------
# Specialty report v3 closed wire contract
# ---------------------------------------------------------------------------

_RECENCY_HOURS = 72
_CHANGE_ID = "CH-V3-1"
_BATCH_ID = "20260729120000"


def _trace_execution(
    *,
    target: Literal["api", "e2e", "fuzz", "performance"] = "api",
) -> TraceExecution:
    return TraceExecution(
        batch_id=_BATCH_ID,
        target=target,
        status="passed",
        ts=AWARE_NOW.replace(hour=11),
        ts_source="executed_at",
    )


def _trace_row(
    *,
    case_id: str,
    case_type: Literal["API", "E2E", "Fuzz", "Performance"],
) -> TraceRow:
    target = cast(
        Literal["api", "e2e", "fuzz", "performance"],
        {"API": "api", "E2E": "e2e", "Fuzz": "fuzz", "Performance": "performance"}[case_type],
    )
    kinds: tuple[str, ...]
    if case_type == "Fuzz":
        kinds = ("covered", "fuzz_run")
    elif case_type == "Performance":
        kinds = ("covered", "perf_run")
    else:
        kinds = ("covered",)
    latest = _trace_execution(target=target)
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type=case_type,
        automation_required=True,
        coverage_state="covered",
        latest_execution=latest,
        freshest_pass=latest,
        presence_in_current_batch="executed",
        atemporal_kinds_present=kinds,
    )


def _four_layer_projection(*, phase: Literal["execution", "reconciled"]) -> TraceProjection:
    return TraceProjection(
        change_id=_CHANGE_ID,
        phase=phase,
        authoritative_batch_id=_BATCH_ID,
        rows=(
            _trace_row(case_id="TC_API_001", case_type="API"),
            _trace_row(case_id="TC_E2E_001", case_type="E2E"),
            _trace_row(case_id="TC_FUZZ_001", case_type="Fuzz"),
            _trace_row(case_id="TC_PERF_001", case_type="Performance"),
        ),
        integrity="complete",
    )


def _policy() -> Policy:
    return Policy(
        version=1,
        human_review_risk_levels=["high"],
        force_continue_allowed=True,
        plan_checks={
            "l1_path": "warn",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "warn",
        },
        coverage_floor=CoverageFloor(risk_high=0.9, risk_medium=0.7),
        fuzz=FuzzPolicy(required_when_endpoint_has_auth=True),
        healing=HealingPolicy(auth_module="require_human"),
        evidence_sufficiency=EvidenceSufficiency(
            recency_hours=_RECENCY_HOURS,
            required_kinds={
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            on_insufficient="require_human",
        ),
    )


def _canonical_capability(*, baseline_policy_digest: str):
    binding = _definition_binding()
    binding["baseline_policy_digest"] = baseline_policy_digest
    return build_capability_replay_v2(definition_binding=binding, rows=_four_complete_rows())


def canonical_execution_phase() -> dict[str, object]:
    return TracePhaseEvidence.from_projection(_four_layer_projection(phase="execution")).model_dump(
        mode="json"
    )


def _canonical_complete_v3() -> SpecialtyReportV3:
    execution = _four_layer_projection(phase="execution")
    reconciled = _four_layer_projection(phase="reconciled")
    policy = _policy()
    baseline = policy_digest(policy)
    report = evaluate_sufficiency(
        execution,
        policy,
        as_of=AWARE_NOW,
        require_current_batch=True,
    )
    execution_evidence = TracePhaseEvidence.from_projection(execution)
    reconciled_evidence = TracePhaseEvidence.from_projection(reconciled)
    sufficiency = join_layer_sufficiency(
        execution,
        execution_evidence.facts,
        report,
        expected_policy_digest=baseline,
    )
    return SpecialtyReportV3(
        schema_version="3",
        change_id=_CHANGE_ID,
        capability_contract_policy=_canonical_capability(baseline_policy_digest=baseline),
        traceability_evidence=CompleteTraceabilityEvidenceV3(
            status="complete",
            command_status=TraceCommandStatus(trace_exit=0, verify_exit=0),
            execution=execution_evidence,
            reconciled=reconciled_evidence,
            sufficiency=sufficiency,
            coverage=CoverageSummary(status="PASS", line=0.9, branch=0.8, final_status="PASS"),
            verify=VerifyDiagnostics(
                phase="reconciled",
                verdict="pass",
                policy_digest=baseline,
                projection_digest=reconciled_evidence.overview.projection_digest,
                blocking_gap_count=0,
                open_problem_count=0,
                reported_insufficient_count=0,
            ),
        ),
    )


def incomplete_v3(reason: TraceCollectionFailureReason) -> dict[str, object]:
    capability = _canonical_capability(baseline_policy_digest="policy-digest")
    return {
        "schema_version": "3",
        "change_id": _CHANGE_ID,
        "capability_contract_policy": capability.model_dump(mode="json"),
        "traceability_evidence": {
            "status": "incomplete",
            "reason_code": reason,
            "detail": "",
            "command_status": {"trace_exit": 1, "verify_exit": 0},
        },
    }


def test_canonical_complete_v3_round_trip_is_frozen_and_extra_forbid() -> None:
    report = _canonical_complete_v3()
    assert report.schema_version == "3"
    assert report.model_config.get("frozen") is True
    nested_models = (
        SpecialtyReportV3,
        CompleteTraceabilityEvidenceV3,
        IncompleteTraceabilityEvidenceV3,
        TraceCommandStatus,
        ProjectionOverview,
        TracePhaseEvidence,
        CoverageSummary,
        VerifyDiagnostics,
    )
    for model in nested_models:
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"

    evidence = report.traceability_evidence
    assert isinstance(evidence, CompleteTraceabilityEvidenceV3)
    assert [layer.layer for layer in evidence.execution.facts.layers] == list(LAYER_NAMES)
    assert [layer.layer for layer in evidence.sufficiency.layers] == list(LAYER_NAMES)

    payload = report.model_dump(mode="json")
    reloaded = load_specialty_report_document(payload)
    assert isinstance(reloaded, SpecialtyReportV3)
    assert reloaded.schema_version == "3"
    assert reloaded.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "sufficiency", "layers", 0, "reason_counts"),
                {"not_a_reason": 1},
            ),
            id="unknown_sufficiency_reason",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "sufficiency", "layers", 0, "execution_state_counts"),
                {"never_run": 0, "stale": 0, "fresh": 0, "hot": 1},
            ),
            id="unknown_execution_state",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "sufficiency", "as_of"),
                datetime(2026, 7, 30, 12, 0, 0).isoformat(),
            ),
            id="naive_as_of",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "sufficiency", "recency_hours"), 0),
            id="zero_recency_hours",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "execution", "overview", "row_count"), True),
            id="boolean_count",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "execution", "overview", "gap_count"), 1.5),
            id="float_count",
        ),
        pytest.param(
            lambda p: _mutate_fact_layer_order(p),
            id="wrong_layer_order",
        ),
        pytest.param(
            lambda p: _set_path(p, ("change_id",), "CH-OTHER"),
            id="outer_change_id_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(
                p, ("traceability_evidence", "reconciled", "overview", "batch_id"), "other-batch"
            ),
            id="phase_batch_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "execution", "overview", "phase"), "reconciled"),
            id="overview_phase_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "execution", "overview", "projection_digest"),
                "deadbeef",
            ),
            id="overview_digest_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(
                p, ("traceability_evidence", "execution", "overview", "integrity"), "incomplete"
            ),
            id="overview_integrity_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "execution", "overview", "row_count"), 99),
            id="overview_row_count_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "execution", "overview", "gap_count"), 99),
            id="overview_gap_count_mismatch",
        ),
        pytest.param(
            lambda p: _break_fact_layer_total(p),
            id="fact_layer_total_mismatch",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "execution", "facts", "global_gaps"),
                {"total": 1, "by_code": {}},
            ),
            id="gap_total_breakdown_mismatch",
        ),
        pytest.param(
            lambda p: _break_sufficiency_arithmetic(p),
            id="sufficiency_arithmetic_mismatch",
        ),
        pytest.param(
            lambda p: _break_sufficiency_vs_facts_total(p),
            id="sufficiency_total_vs_facts",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "sufficiency", "source_projection_digest"),
                "not-execution",
            ),
            id="sufficiency_digest_unbound",
        ),
        pytest.param(
            lambda p: _set_path(
                p, ("traceability_evidence", "verify", "projection_digest"), "not-reconciled"
            ),
            id="verify_digest_unbound",
        ),
        pytest.param(
            lambda p: _set_path(
                p, ("traceability_evidence", "sufficiency", "source_policy_digest"), "other-policy"
            ),
            id="sufficiency_policy_unbound",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "verify", "policy_digest"), "other-policy"),
            id="verify_policy_unbound",
        ),
        pytest.param(
            lambda p: _drop_definition_binding(p),
            id="missing_definition_binding",
        ),
        pytest.param(
            lambda p: _set_path(
                p,
                ("traceability_evidence", "sufficiency", "semantics"),
                "evidence_sufficiency/v1",
            ),
            id="wrong_semantics",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "sufficiency", "require_current_batch"), False),
            id="wrong_require_current_batch",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "verify", "phase"), "verify"),
            id="wrong_verify_phase",
        ),
        pytest.param(
            lambda p: _set_path(p, ("traceability_evidence", "extra_complete_field"), "x"),
            id="extra_complete_field",
        ),
    ],
)
def test_complete_v3_rejects_invariant_mutations(mutator) -> None:
    payload = mutator(_canonical_complete_v3().model_dump(mode="json"))
    with pytest.raises(ValidationError):
        load_specialty_report_document(payload)


def _set_path(payload: dict[str, object], path: tuple[object, ...], value: object) -> dict[str, object]:
    data = copy.deepcopy(payload)
    cursor: object = data
    for key in path[:-1]:
        assert isinstance(cursor, (dict, list))
        cursor = cursor[key]  # type: ignore[index]
    assert isinstance(cursor, (dict, list))
    cursor[path[-1]] = value  # type: ignore[index]
    return data


def _mutate_fact_layer_order(payload: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(payload)
    evidence = data["traceability_evidence"]
    assert isinstance(evidence, dict)
    execution = evidence["execution"]
    assert isinstance(execution, dict)
    facts = execution["facts"]
    assert isinstance(facts, dict)
    layers = facts["layers"]
    assert isinstance(layers, list)
    layers[0], layers[1] = layers[1], layers[0]
    return data


def _break_fact_layer_total(payload: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(payload)
    evidence = data["traceability_evidence"]
    assert isinstance(evidence, dict)
    execution = evidence["execution"]
    assert isinstance(execution, dict)
    facts = execution["facts"]
    assert isinstance(facts, dict)
    layers = facts["layers"]
    assert isinstance(layers, list)
    layer0 = layers[0]
    assert isinstance(layer0, dict)
    layer0["total"] = int(layer0["total"]) + 1
    return data


def _break_sufficiency_arithmetic(payload: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(payload)
    evidence = data["traceability_evidence"]
    assert isinstance(evidence, dict)
    sufficiency = evidence["sufficiency"]
    assert isinstance(sufficiency, dict)
    layers = sufficiency["layers"]
    assert isinstance(layers, list)
    layer0 = layers[0]
    assert isinstance(layer0, dict)
    layer0["sufficient"] = int(layer0["sufficient"]) + 1
    return data


def _break_sufficiency_vs_facts_total(payload: dict[str, object]) -> dict[str, object]:
    """Keep layer self-consistent while breaking sufficient+insufficient vs facts.total."""
    data = copy.deepcopy(payload)
    evidence = data["traceability_evidence"]
    assert isinstance(evidence, dict)
    sufficiency = evidence["sufficiency"]
    assert isinstance(sufficiency, dict)
    layers = sufficiency["layers"]
    assert isinstance(layers, list)
    layer0 = layers[0]
    assert isinstance(layer0, dict)
    layer0["sufficient"] = 0
    layer0["insufficient"] = 0
    layer0["reason_counts"] = {}
    layer0["execution_state_counts"] = {"never_run": 0, "stale": 0, "fresh": 0}
    return data


def _drop_definition_binding(payload: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(payload)
    capability = data["capability_contract_policy"]
    assert isinstance(capability, dict)
    capability["definition_binding"] = None
    capability["integrity"] = "incomplete"
    return data


@pytest.mark.parametrize("reason", get_args(TraceCollectionFailureReason))
def test_incomplete_v3_reason_round_trips(reason: TraceCollectionFailureReason) -> None:
    payload = incomplete_v3(reason)
    loaded = load_specialty_report_document(payload)
    assert isinstance(loaded, SpecialtyReportV3)
    evidence = loaded.traceability_evidence
    assert isinstance(evidence, IncompleteTraceabilityEvidenceV3)
    assert evidence.status == "incomplete"
    assert evidence.reason_code == reason
    assert evidence.detail == ""
    assert evidence.command_status is not None
    assert evidence.command_status.trace_exit == 1


def test_incomplete_v3_rejects_unknown_reason() -> None:
    payload = incomplete_v3("execution_projection_missing")
    evidence = payload["traceability_evidence"]
    assert isinstance(evidence, dict)
    evidence["reason_code"] = "not_a_closed_reason"
    with pytest.raises(ValidationError):
        load_specialty_report_document(payload)


def test_incomplete_v3_rejects_non_string_detail() -> None:
    payload = incomplete_v3("quality_gate_missing")
    evidence = payload["traceability_evidence"]
    assert isinstance(evidence, dict)
    evidence["detail"] = 123
    with pytest.raises(ValidationError):
        load_specialty_report_document(payload)


def test_incomplete_v3_rejects_partial_complete_matrix() -> None:
    payload = incomplete_v3("reconciled_projection_stale")
    evidence = payload["traceability_evidence"]
    assert isinstance(evidence, dict)
    evidence["execution"] = canonical_execution_phase()
    with pytest.raises(ValidationError, match="extra"):
        load_specialty_report_document(payload)


def test_incomplete_v3_empty_detail_default_is_valid() -> None:
    capability = _canonical_capability(baseline_policy_digest="policy-digest")
    loaded = load_specialty_report_document(
        {
            "schema_version": "3",
            "change_id": _CHANGE_ID,
            "capability_contract_policy": capability.model_dump(mode="json"),
            "traceability_evidence": {
                "status": "incomplete",
                "reason_code": "layer_summary_invalid",
            },
        }
    )
    assert isinstance(loaded, SpecialtyReportV3)
    evidence = loaded.traceability_evidence
    assert isinstance(evidence, IncompleteTraceabilityEvidenceV3)
    assert evidence.detail == ""
    assert evidence.command_status is None


def test_document_loader_preserves_v1_and_v2_concrete_types() -> None:
    v2_payload = {
        "schema_version": "2",
        "change_id": "CH-1",
        "capability_contract_policy": build_capability_replay_v2(
            definition_binding=_definition_binding(),
            rows=_four_complete_rows(),
        ).model_dump(mode="json"),
        "traceability_evidence": {"command_status": {"trace_exit": 0, "verify_exit": 0}},
    }
    v2 = load_specialty_report_document(v2_payload)
    assert isinstance(v2, SpecialtyReportV2)
    assert v2.traceability_evidence == {"command_status": {"trace_exit": 0, "verify_exit": 0}}

    v1_payload = {
        "schema_version": "1",
        "change_id": "CH-1",
        "capability_contract_policy": {"capabilities": {"required": [], "missing": []}},
        "traceability_evidence": {"legacy": True},
    }
    v1 = load_specialty_report_document(v1_payload)
    assert isinstance(v1, LegacySpecialtyReportV1)
    assert v1.traceability_evidence == {"legacy": True}


@pytest.mark.parametrize("version", [None, "4", ""])
def test_document_loader_rejects_missing_null_and_unknown_versions(version: object) -> None:
    payload: dict[str, object] = {
        "change_id": "CH-1",
        "capability_contract_policy": {"capabilities": {"required": [], "missing": []}},
        "traceability_evidence": {},
    }
    if version is not None:
        payload["schema_version"] = version
    with pytest.raises(ValidationError):
        load_specialty_report_document(payload)


def test_public_load_specialty_report_accepts_v1_v2_v3() -> None:
    v3 = load_specialty_report(_canonical_complete_v3().model_dump(mode="json"))
    assert isinstance(v3, SpecialtyReportV3)

    v2 = load_specialty_report(
        {
            "schema_version": "2",
            "change_id": "CH-1",
            "capability_contract_policy": build_capability_replay_v2(
                definition_binding=_definition_binding(),
                rows=_four_complete_rows(),
            ).model_dump(mode="json"),
            "traceability_evidence": {"command_status": {"trace_exit": 0, "verify_exit": 0}},
        }
    )
    assert isinstance(v2, SpecialtyReportV2)

    v1 = load_specialty_report(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "capability_contract_policy": {"capabilities": {"required": [], "missing": []}},
            "traceability_evidence": {},
        }
    )
    assert isinstance(v1, LegacySpecialtyReportV1)


def test_traceability_evidence_v3_is_status_discriminated_union() -> None:
    assert TraceabilityEvidenceV3 is not None
    complete = _canonical_complete_v3().traceability_evidence
    assert complete.status == "complete"
    incomplete = IncompleteTraceabilityEvidenceV3(
        status="incomplete",
        reason_code="verify_result_missing",
    )
    assert incomplete.status == "incomplete"


def test_render_mixed_report_states_four_layer_matrices() -> None:
    complete_a = _canonical_complete_v3()
    payload_b = _canonical_complete_v3().model_dump(mode="json")
    payload_b["change_id"] = "CH-V3-2"
    payload_b["traceability_evidence"]["execution"]["facts"]["change_id"] = "CH-V3-2"
    payload_b["traceability_evidence"]["reconciled"]["facts"]["change_id"] = "CH-V3-2"
    payload_b["traceability_evidence"]["reconciled"]["overview"]["integrity"] = "incomplete"
    payload_b["traceability_evidence"]["reconciled"]["facts"]["projection_integrity"] = "incomplete"
    complete_b = SpecialtyReportV3.model_validate(payload_b)
    incomplete = SpecialtyReportV3.model_validate(incomplete_v3("quality_gate_missing"))

    v2_payload = {
        "schema_version": "2",
        "change_id": "CH-LEGACY-V2",
        "capability_contract_policy": build_capability_replay_v2(
            definition_binding=_definition_binding(),
            rows=_four_complete_rows(),
        ).model_dump(mode="json"),
        "traceability_evidence": {
            "command_status": {"trace_exit": 0, "verify_exit": 0},
            "execution_projection": {
                "phase": "execution",
                "batch_id": "b",
                "integrity": "complete",
                "row_count": 0,
                "source_count": 0,
                "gap_count": 0,
                "unmapped_test_count": 0,
            },
            "reconciled_projection": {
                "phase": "reconciled",
                "batch_id": "b",
                "integrity": "complete",
                "row_count": 0,
                "source_count": 0,
                "gap_count": 0,
                "unmapped_test_count": 0,
                "failure_row_count": 0,
                "failure_link_count": 0,
                "open_problem_row_count": 0,
                "open_problem_link_count": 0,
                "unique_open_problem_count": 0,
            },
            "sufficiency": {"sufficient_count": 0, "insufficient_count": 0, "reason_counts": {}},
            "coverage": {"status": "PASS", "line": 1.0, "branch": 1.0, "final_status": "PASS"},
            "verify": {
                "phase": "reconciled",
                "verdict": "pass",
                "policy_digest": "p",
                "projection_digest": "d",
                "blocking_gap_count": 0,
                "open_problem_count": 0,
                "reported_insufficient_count": 0,
                "observed_insufficient_count": 0,
            },
        },
    }
    v2 = SpecialtyReportV2.model_validate(v2_payload)
    v1 = LegacySpecialtyReportV1(
        schema_version="1",
        change_id="CH-LEGACY-V1",
        capability_contract_policy={
            "mechanical_checks": {"status": "pass", "finding_count": 0, "by_check": {}},
            "capabilities": {"required": [], "missing": []},
            "contracts": {
                "agent_execution_contract_digests": [],
                "rendered_output_contract_count": 0,
                "prompt_observability": "ok",
            },
            "policy": {
                "digest": "d",
                "recorded_digest": "d",
                "source": "pinned",
                "plan_checks": {},
            },
            "policy_replay": [
                {"action": "warn", "verdict": "pass"},
                {"action": "block", "verdict": "pass"},
                {"action": "require_human", "verdict": "pass"},
            ],
        },
        traceability_evidence=v2.traceability_evidence,
    )

    rendered = render_specialty_sections([complete_b, incomplete, complete_a, v2, v1])
    facts = rendered.split("### Trace Layer Facts", 1)[1].split("###", 1)[0]
    fact_data = [line for line in facts.splitlines() if line.startswith("| `")]
    assert len(fact_data) == 16
    sufficiency = rendered.split("### Trace Layer Sufficiency", 1)[1]
    sufficiency_data = [line for line in sufficiency.splitlines() if line.startswith("| `CH-V3-")]
    assert len(sufficiency_data) == 8
    assert "quality_gate_missing" in rendered
    assert rendered.count("`legacy_unlayered`") >= 2
    assert "CH-LEGACY-V1" in rendered and "CH-LEGACY-V2" in rendered
    assert all("CH-V3-1" in line or "CH-V3-2" in line for line in fact_data)
    assert "Global Gaps" in rendered
    assert "Incomplete Collection" in rendered
