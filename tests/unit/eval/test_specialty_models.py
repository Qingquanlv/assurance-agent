"""Specialty report v2 model invariants and integrity derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.eval.specialty_models import (
    CompleteLayerRow,
    IncompleteLayerRow,
    LegacySpecialtyReportV1,
    NotSelectedLayerRow,
    NotWiredLayerRow,
    SpecialtyReportV2,
    build_capability_replay_v2,
    load_specialty_report,
)
from assurance_agent.eval.specialty_render import render_specialty_sections


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
