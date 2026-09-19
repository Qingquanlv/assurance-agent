from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.explore import (
    ExploreAdvisoryV1,
    ObligationDraftV1,
    SourceCatalogEntryV1,
    SourceQuoteV1,
)
from assurance_intake.contracts.impact import impact_row_identity
from assurance_intake.contracts.obligations import ExpectedBasisV1, PreparedObligationV1
from assurance_intake.contracts.plan import bind_impact_row_ids
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _digest() -> str:
    return "a" * 64


def _artifact(path: str = "qa/requirement.md") -> dict[str, str]:
    return {"path": path, "digest": _digest()}


def _source(*, kind: str = "requirement", locator: str = "login") -> dict[str, object]:
    return {"kind": kind, "artifact": _artifact(), "locator": locator}


def _observation(**overrides: Any) -> dict[str, object]:
    row: dict[str, object] = {
        "observation_key": "locked_valid_password",
        "condition": "连续5次失败后，用正确密码登录",
        "predicate": "status_code_eq",
        "expected": 423,
        "basis_refs": [_source()],
    }
    row.update(overrides)
    return row


def _requirement(**overrides: Any) -> dict[str, object]:
    row: dict[str, object] = {
        "requirement_id": "R-LOCK",
        "profile_id": "api.state-sequence.v1",
        "prerequisites": ["isolated_account"],
        "observations": [_observation()],
        "semantic_review_required": True,
        "subject_binding_required": True,
    }
    row.update(overrides)
    return row


def _obligation(**overrides: Any) -> dict[str, object]:
    row: dict[str, object] = {
        "mrc_id": "MRC-LOCK",
        "key": "auth.lockout",
        "proposed_key": None,
        "category": "api",
        "layer": "api",
        "statement": "锁定后拒绝正确密码",
        "applicability_conditions": [],
        "expected_basis_refs": [
            {"source": _source(), "source_status": "authenticated"},
        ],
        "impact_row_ids": ["IR-1"],
        "required": True,
        "scope_disposition": "included",
        "exclusion_basis": None,
        "open_questions": [],
        "verification_requirements": [_requirement()],
    }
    row.update(overrides)
    return row


def test_current_code_is_not_authenticated_expected_basis() -> None:
    with pytest.raises(ValidationError, match="normative"):
        ExpectedBasisV1.model_validate(
            {
                "source": {
                    "kind": "code",
                    "artifact": {"path": "src/login.py", "digest": _digest()},
                    "locator": "login",
                },
                "source_status": "authenticated",
            }
        )


def test_pending_code_basis_is_allowed() -> None:
    row = ExpectedBasisV1.model_validate(
        {
            "source": {
                "kind": "code",
                "artifact": {"path": "src/login.py", "digest": _digest()},
                "locator": "login",
            },
            "source_status": "pending",
        }
    )
    assert row.source_status == "pending"


def test_authenticated_requirement_basis_is_allowed() -> None:
    row = ExpectedBasisV1.model_validate(
        {"source": _source(kind="requirement"), "source_status": "authenticated"}
    )
    assert row.source.kind == "requirement"


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"key": None, "proposed_key": "auth.lockout"}, None),
        ({"verification_requirements": []}, None),
        (
            {
                "verification_requirements": [
                    _requirement(profile_id="concurrency.v1", observations=[])
                ]
            },
            None,
        ),
    ],
)
def test_pending_unknown_and_empty_requirements_are_kept(
    payload: dict[str, object], match: str | None
) -> None:
    row = PreparedObligationV1.model_validate(_obligation(**payload))
    assert row.mrc_id == "MRC-LOCK"
    if "proposed_key" in payload:
        assert row.key is None
        assert row.proposed_key == "auth.lockout"
    if payload.get("verification_requirements") == []:
        assert row.verification_requirements == ()


def test_excluded_without_basis_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exclusion"):
        PreparedObligationV1.model_validate(
            _obligation(
                required=False,
                scope_disposition="excluded",
                exclusion_basis=None,
            )
        )


def test_included_must_stay_required() -> None:
    with pytest.raises(ValidationError, match="included"):
        PreparedObligationV1.model_validate(_obligation(required=False))


def test_excluded_must_not_remain_required() -> None:
    with pytest.raises(ValidationError, match="excluded"):
        PreparedObligationV1.model_validate(
            _obligation(
                scope_disposition="excluded",
                exclusion_basis=_source(kind="decision", locator="/candidate_test_families"),
            )
        )


def test_duplicate_observation_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="observation_key"):
        PreparedObligationV1.model_validate(
            _obligation(
                verification_requirements=[
                    _requirement(
                        observations=[
                            _observation(),
                            _observation(condition="another step"),
                        ]
                    )
                ]
            )
        )


def test_old_obligation_shape_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PreparedObligationV1.model_validate(
            {
                "mrc_id": "MRC-API-001",
                "key": "create_item",
                "category": "api",
                "required": True,
                "layer": "api",
            }
        )


def test_expected_status_must_be_http_or_none() -> None:
    PreparedObligationV1.model_validate(
        _obligation(
            verification_requirements=[
                _requirement(observations=[_observation(expected=None)])
            ]
        )
    )
    with pytest.raises(ValidationError, match="status"):
        PreparedObligationV1.model_validate(
            _obligation(
                verification_requirements=[
                    _requirement(observations=[_observation(expected=99)])
                ]
            )
        )


def test_unknown_source_id_is_rejected_against_catalog() -> None:
    catalog = (
        SourceCatalogEntryV1.model_validate(
            {
                "source_id": "requirement",
                "kind": "requirement",
                "artifact": _artifact(),
                "quotable": True,
            }
        ),
    )
    draft = {
        "draft_id": "D-1",
        "proposed_key": "auth.lockout",
        "category": "api",
        "layer": "api",
        "statement": "锁定后拒绝正确密码",
        "applicability_conditions": [],
        "impact_row_ids": [],
        "proposed_profile_id": "api.state-sequence.v1",
        "prerequisites": [],
        "observation_goals": [],
        "basis_quotes": [{"source_id": "invented", "quote": "锁定", "context_quote": None}],
        "open_questions": [],
    }
    with pytest.raises(ValidationError, match="source_id"):
        ObligationDraftV1.model_validate(draft, context={"source_catalog": catalog})


def test_explore_advisory_requires_obligation_drafts() -> None:
    with pytest.raises(ValidationError):
        ExploreAdvisoryV1.model_validate(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "context_ref": "explore/context.json",
                "generated_at": "2026-09-05T00:00:00Z",
                "executive_summary": "Checkout changes",
                "watchlist": [],
                "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
                "source_code_evidence": [],
                "case_design_guidance": {
                    "priority_hints": [],
                    "suggested_scenarios": [],
                    "regression_focus": [],
                },
                "minimum_required_coverage": {"api": ["create_item"]},
                "open_questions_for_case_design": [],
                "test_strategy": {
                    "scope": {"in_scope": ["checkout"], "out_of_scope": []},
                    "data_focus": [],
                    "depth": "core",
                    "layer_recommendation": [
                        {
                            "layer": layer,
                            "recommended": True,
                            "rationale": "Required by the change",
                            "evidence_ids": [],
                        }
                        for layer in ("API", "E2E", "Fuzz", "Performance")
                    ],
                },
            }
        )


def test_impact_row_identity_binds_inventory_digest() -> None:
    first = impact_row_identity(plan_digest=_digest(), inventory_digest="b" * 64, row_id="IR-1")
    second = impact_row_identity(plan_digest=_digest(), inventory_digest="c" * 64, row_id="IR-1")
    assert first != second
    bound = bind_impact_row_ids(
        plan_digest=_digest(),
        inventory_ref=EvidenceArtifactRefV1(
            path="qa/results/explore/impact-inventory.json",
            digest="b" * 64,
        ),
        row_ids=("IR-1",),
    )
    assert bound == (first,)


def test_source_quote_requires_non_empty_quote() -> None:
    with pytest.raises(ValidationError):
        SourceQuoteV1.model_validate({"source_id": "requirement", "quote": "", "context_quote": None})
