from __future__ import annotations

from assurance_intake.contracts.explore import ObligationDraftV1
from assurance_intake.contracts.obligations import PreparedObligationV1, SourceRefV1
from assurance_intake.operations.obligations import (
    InputError,
    journey_keys_from_document,
    normalize_obligation_drafts,
    obligation_gaps,
    validate_discovery_closure,
)
import pytest


def test_journey_keys_require_canonical_unique_document_values() -> None:
    assert journey_keys_from_document({"journeys": ["create", "delete"]}) == ("create", "delete")
    with pytest.raises(ValueError, match="sorted and unique"):
        journey_keys_from_document({"journeys": ["delete", "create"]})


def test_unavailable_family_preserves_required() -> None:
    row = PreparedObligationV1.model_validate(
        {
            "mrc_id": "MRC-LOCK",
            "key": None,
            "proposed_key": "auth.lockout",
            "category": "api",
            "layer": "api",
            "statement": "锁定后拒绝正确密码",
            "applicability_conditions": [],
            "expected_basis_refs": [],
            "impact_row_ids": ["IR-1"],
            "required": True,
            "scope_disposition": "included",
            "exclusion_basis": None,
            "open_questions": ["等待确认响应码"],
            "verification_requirements": [],
        }
    )
    before = row.model_dump_json()
    gaps = obligation_gaps(
        row,
        admissible_families=frozenset({"e2e"}),
        supported_profiles=frozenset(),
    )
    assert "family_unavailable" in gaps
    assert row.required and row.model_dump_json() == before


def _draft(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "draft_id": "D-LOCK",
        "proposed_key": "auth.lockout",
        "category": "api",
        "layer": "api",
        "statement": "锁定后拒绝正确密码",
        "applicability_conditions": [],
        "impact_row_ids": ["IR-1"],
        "proposed_profile_id": "api.state-sequence.v1",
        "prerequisites": ["isolated_account"],
        "observation_goals": [
            {
                "key": "locked_valid_password",
                "condition": "连续5次失败后，用正确密码登录",
                "proposed_expected_status": 423,
                "basis_quotes": [{"source_id": "requirement", "quote": "锁定返回423", "context_quote": None}],
            }
        ],
        "basis_quotes": [{"source_id": "requirement", "quote": "锁定返回423", "context_quote": None}],
        "open_questions": [],
    }
    row.update(overrides)
    return row


def test_normalize_assigns_stable_ids_without_step_ids() -> None:
    draft = ObligationDraftV1.model_validate(_draft())
    first = normalize_obligation_drafts((draft,), resolved_quotes={})
    second = normalize_obligation_drafts((draft,), resolved_quotes={})
    assert first == second
    assert first[0].mrc_id
    assert first[0].key is None
    assert first[0].proposed_key == "auth.lockout"
    assert first[0].verification_requirements[0].observations[0].expected == 423
    assert first[0].verification_requirements[0].observations[0].observation_key == "locked_valid_password"


def test_unresolved_quotes_stay_pending_and_keep_expected() -> None:
    draft = ObligationDraftV1.model_validate(_draft(open_questions=["等待确认时长"]))
    rows = normalize_obligation_drafts((draft,), resolved_quotes={})
    assert rows[0].expected_basis_refs == ()
    assert rows[0].verification_requirements[0].observations[0].expected == 423
    gaps = obligation_gaps(
        rows[0],
        admissible_families=frozenset({"api"}),
        supported_profiles=frozenset({"api.state-sequence.v1"}),
    )
    assert "expectation_unconfirmed" in gaps


def test_resolved_quote_is_pending_until_source_auth() -> None:
    ref = SourceRefV1.model_validate(
        {
            "kind": "requirement",
            "artifact": {"path": "qa/requirement.md", "digest": "a" * 64},
            "locator": "7:22",
        }
    )
    draft = ObligationDraftV1.model_validate(_draft())
    rows = normalize_obligation_drafts(
        (draft,),
        resolved_quotes={("requirement", "锁定返回423"): ref},
    )
    assert rows[0].expected_basis_refs[0].source_status == "pending"
    assert rows[0].expected_basis_refs[0].source == ref


def test_unsupported_profile_keeps_the_obligation() -> None:
    draft = ObligationDraftV1.model_validate(_draft(proposed_profile_id="concurrency.v1"))
    rows = normalize_obligation_drafts((draft,), resolved_quotes={})
    gaps = obligation_gaps(
        rows[0],
        admissible_families=frozenset({"api"}),
        supported_profiles=frozenset({"api.state-sequence.v1"}),
    )
    assert rows[0].required is True
    assert "method_unsupported" in gaps


def test_discovery_closure_rejects_dangling_impact_row() -> None:
    row = PreparedObligationV1.model_validate(
        {
            "mrc_id": "MRC-LOCK",
            "key": None,
            "proposed_key": "auth.lockout",
            "category": "api",
            "layer": "api",
            "statement": "锁定后拒绝正确密码",
            "applicability_conditions": [],
            "expected_basis_refs": [],
            "impact_row_ids": ["IR-MISSING"],
            "required": True,
            "scope_disposition": "included",
            "exclusion_basis": None,
            "open_questions": [],
            "verification_requirements": [],
        }
    )
    with pytest.raises(InputError, match="impact"):
        validate_discovery_closure(
            obligations=(row,),
            impact_rows=("IR-1",),
            audit=(),
        )
