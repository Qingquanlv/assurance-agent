from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.explore import TestStrategyV1 as Strategy
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.common import TestFamily
from assurance_intake.contracts.plan import (
    PlanBudgetsV1,
    PreparedQualityGoalV1,
    ResolvePlanInputV1,
    TestFamilyPolicyV1 as FamilyPolicy,
    plan_artifact_ref,
    plan_bytes,
)
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.contracts.quality_goals import (
    CoverageFloorsV1,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_intake.domain.resolve_plan import (
    InputError,
    derive_family_proposal,
    resolve_families,
    resolve_plan,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _strategy(*recommended: str) -> Strategy:
    selected = set(recommended)
    return Strategy.model_validate(
        {
            "scope": {"in_scope": ["checkout"], "out_of_scope": []},
            "data_focus": [],
            "depth": "core",
            "layer_recommendation": [
                {
                    "layer": layer,
                    "recommended": layer in selected,
                    "rationale": f"{layer} rationale",
                    "evidence_ids": [],
                }
                for layer in ("API", "E2E", "Fuzz", "Performance")
            ],
        }
    )


def _goal(*required: TestFamily) -> PreparedQualityGoalV1:
    return PreparedQualityGoalV1.model_validate(
        {
            "obligations_ref": {
                "path": "qa/results/explore/exploration.json",
                "digest": _SHA_A,
            },
            "source_resource_digests": (
                ("assurance.product.configuration.capability-catalog", _SHA_A),
                ("assurance.product.configuration.data-knowledge", _SHA_B),
            ),
            "required_test_families": required,
            "metric_catalog": (
                "constraint_coverage",
                "auth_matrix_coverage",
                "journey_coverage",
            ),
            "coverage_policy": CoverageGoalPolicyV1(
                coverage_floor_by_tier=CoverageFloorsV1(
                    low=0.7,
                    medium=0.8,
                    high=0.9,
                    critical=1.0,
                )
            ),
            "sufficiency_policy": SufficiencyPolicyV1(
                recency_hours=24,
                require_current_batch=True,
            ),
        }
    )


def _request(
    *,
    candidate: tuple[TestFamily, ...] = ("api", "e2e"),
    allowed: tuple[TestFamily, ...] = ("api", "e2e"),
    required: tuple[TestFamily, ...] = (),
) -> ResolvePlanInputV1:
    return ResolvePlanInputV1.model_validate(
        {
            "change_id": "CH-1",
            "requirement_digest": _SHA_A,
            "candidate_test_families": candidate,
            "budgets": PlanBudgetsV1(
                review_rounds=1,
                coverage_rounds=2,
                healing_rounds=1,
                execution_retries=0,
            ),
            "policy_resource_id": "assurance.product.configuration.product-policy",
            "policy_digest": _SHA_B,
            "family_policy": FamilyPolicy(required=required, allowed=allowed),
            "exploration_ref": {
                "path": "qa/results/explore/exploration.json",
                "digest": _SHA_A,
            },
            "impact_inventory_ref": {
                "path": "qa/results/explore/impact-inventory.json",
                "digest": _SHA_B,
            },
            "source_resource_digests": (
                ("assurance.product.configuration.capability-catalog", _SHA_A),
                ("assurance.product.configuration.data-knowledge", _SHA_B),
            ),
            "capability_leafs": ("cart.read",),
        }
    )


def _inventory(*rows: dict[str, Any]) -> ChangeImpactInventoryV1:
    return ChangeImpactInventoryV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": list(rows),
            "exclusions": [],
        }
    )


def _journey_row(row_id: str, *, disposition: str = "add", gap_reason: str | None = None) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "journey", "key": "dept_management_crud"},
        "obligation": "the department tree page must still render after the change",
        "expected_basis_ids": [],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": disposition,
        "gap_reason": gap_reason,
        "confidence": "medium",
    }


def test_proposal_is_derived_from_the_four_typed_rows() -> None:
    assert derive_family_proposal(_strategy("E2E", "Performance")) == ("e2e", "performance")
    assert derive_family_proposal(_strategy()) == ()


def test_policy_filter_cannot_produce_empty_selected() -> None:
    selected, cause = resolve_families(
        candidate=("api", "e2e"),
        proposed=("e2e",),
        policy=FamilyPolicy(required=(), allowed=("api",)),
        goal_required=(),
    )
    assert (selected, cause) == (("api",), "empty_after_policy")


def test_goal_required_family_is_retained() -> None:
    selected, cause = resolve_families(
        candidate=("api", "e2e"),
        proposed=("api",),
        policy=FamilyPolicy(required=(), allowed=("api", "e2e")),
        goal_required=("e2e",),
    )
    assert selected == ("api", "e2e")
    assert cause is None


@pytest.mark.parametrize(
    ("candidate", "allowed", "goal_required"),
    [
        (("api",), ("e2e",), ()),
        (("api",), ("api",), ("e2e",)),
    ],
)
def test_incompatible_scope_fails_before_case(
    candidate: tuple[TestFamily, ...],
    allowed: tuple[TestFamily, ...],
    goal_required: tuple[TestFamily, ...],
) -> None:
    with pytest.raises(InputError, match="incompatible"):
        resolve_families(
            candidate=candidate,
            proposed=("api",),
            policy=FamilyPolicy(required=(), allowed=allowed),
            goal_required=goal_required,
        )


def test_resolved_plan_records_fixed_reasons_and_two_distinct_digests() -> None:
    plan = resolve_plan(
        request=_request(allowed=("api",)),
        proposed=("e2e",),
        quality_goal=_goal(),
        inventory=_inventory(),
    )
    assert plan.selected_test_families == ("api",)
    assert [reason.reason_code for reason in plan.resolution_reasons] == [
        "fallback_all_candidates",
        "candidate_outside_allowed",
    ]
    assert plan.resolution_reasons[0].detail == "empty_after_policy"
    assert plan.resolution_reasons[1].family == "e2e"

    ref = plan_artifact_ref(plan)
    assert ref.digest != plan.plan_digest
    assert ref.path == (f"qa/results/plan/{plan.plan_digest}/resolved-assurance-plan.json")
    assert decode_plan(plan_bytes(plan), ref) == plan


def test_resolution_reasons_stay_canonical_when_required_and_excluded_overlap() -> None:
    plan = resolve_plan(
        request=_request(allowed=("api",), required=("api",)),
        proposed=("e2e",),
        quality_goal=_goal(),
        inventory=_inventory(),
    )
    assert [reason.reason_code for reason in plan.resolution_reasons] == [
        "accepted_proposal",
        "required_retained",
        "candidate_outside_allowed",
    ]
    assert plan.resolution_reasons[1].family == "api"


def test_plan_decoder_rejects_noncanonical_or_self_inconsistent_bytes() -> None:
    plan = resolve_plan(request=_request(), proposed=("api",), quality_goal=_goal(), inventory=_inventory())
    ref = plan_artifact_ref(plan)
    with pytest.raises(ValueError, match="canonical"):
        decode_plan(plan_bytes(plan) + b"\n", ref)

    payload = json.loads(plan_bytes(plan))
    payload["selected_test_families"] = ["e2e"]
    changed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    changed_ref = ref.model_copy(update={"digest": __import__("hashlib").sha256(changed).hexdigest()})
    with pytest.raises(ValueError, match="plan_digest"):
        decode_plan(changed, changed_ref)


def test_goal_change_changes_the_plan_identity() -> None:
    request = _request()
    first = resolve_plan(request=request, proposed=("api",), quality_goal=_goal(), inventory=_inventory())
    second = resolve_plan(
        request=request, proposed=("api",), quality_goal=_goal("e2e"), inventory=_inventory()
    )
    assert first.plan_digest != second.plan_digest
    assert plan_artifact_ref(first) != plan_artifact_ref(second)


def test_closed_journey_row_retains_e2e_within_admissible_scope() -> None:
    plan = resolve_plan(
        request=_request(candidate=("api", "e2e"), allowed=("api", "e2e")),
        proposed=("api",),
        quality_goal=_goal(),
        inventory=_inventory(_journey_row("IR-001")),
    )
    assert plan.selected_test_families == ("api", "e2e")
    assert plan.impact_inventory_ref.path == "qa/results/explore/impact-inventory.json"
    retained = [reason for reason in plan.resolution_reasons if reason.reason_code == "impact_retained"]
    assert [reason.family for reason in retained] == ["e2e"]


def test_journey_row_outside_candidates_is_reported_not_forced() -> None:
    plan = resolve_plan(
        request=_request(candidate=("api",), allowed=("api", "e2e")),
        proposed=("api",),
        quality_goal=_goal(),
        inventory=_inventory(_journey_row("IR-001")),
    )
    assert plan.selected_test_families == ("api",)
    unavailable = [
        reason for reason in plan.resolution_reasons if reason.reason_code == "impact_family_unavailable"
    ]
    assert [reason.family for reason in unavailable] == ["e2e"]


def test_pending_rows_surface_as_one_global_reason_and_retain_nothing() -> None:
    plan = resolve_plan(
        request=_request(candidate=("api", "e2e"), allowed=("api", "e2e")),
        proposed=("api",),
        quality_goal=_goal(),
        inventory=_inventory(
            _journey_row(
                "IR-001", disposition="pending_confirmation", gap_reason="owner must confirm the journey"
            ),
            _journey_row(
                "IR-002", disposition="pending_confirmation", gap_reason="owner must confirm the journey"
            ),
        ),
    )
    assert plan.selected_test_families == ("api",)
    pending = [
        reason for reason in plan.resolution_reasons if reason.reason_code == "impact_pending_confirmation"
    ]
    assert len(pending) == 1
    assert pending[0].family is None
    assert "IR-001" in pending[0].summary and "IR-002" in pending[0].summary


def test_plan_input_rejects_a_foreign_inventory_path() -> None:
    payload = _request().model_dump(mode="json")
    payload["impact_inventory_ref"] = {"path": "qa/results/other/impact-inventory.json", "digest": _SHA_B}
    with pytest.raises(ValidationError, match="impact_inventory_ref must bind the current change"):
        ResolvePlanInputV1.model_validate(payload)
