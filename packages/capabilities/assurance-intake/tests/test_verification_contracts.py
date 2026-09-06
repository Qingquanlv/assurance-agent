from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_intake.contracts import (
    AssertionSourcesV1 as PublicAssertionSourcesV1,
    BusinessAssertionV1 as PublicBusinessAssertionV1,
)
from assurance_intake.contracts.agent import CaseDesignInputV1, CaseReviewInputV1
from assurance_intake.contracts.verification import (
    AssertionSourcesV1,
    BusinessAssertionV1,
    LiteralExpectedV1,
    validate_assertion_provenance,
)
from assurance_intake.plugin import IntakePlugin
from assurance_intake.operations.agent_skills import case_design_outputs, case_review_inputs
from assurance_intake.resource_loader import resource_bytes
from tests.verification_support import read_fixture


@pytest.mark.parametrize("payload", [None, "", {}, {"assertion_id": "x", "statement": ""}])
def test_empty_assertion_is_rejected(payload: object) -> None:
    with pytest.raises(ValidationError):
        BusinessAssertionV1.model_validate(payload)


def test_literal_expected_nested_json_cannot_be_mutated_after_admission() -> None:
    assertion = BusinessAssertionV1.model_validate(
        {
            "assertion_id": "api.payload",
            "statement": "The response payload is fixed.",
            "source_id": "requirement.user.create",
            "subject": "create.response.payload",
            "comparator": "eq",
            "expected": {"kind": "literal", "value": {"active": True}},
        }
    )
    assert isinstance(assertion.expected, LiteralExpectedV1)
    value = cast(dict[str, object], assertion.expected.value)
    assert isinstance(value, Mapping)

    with pytest.raises(TypeError):
        value["active"] = False


@pytest.mark.parametrize(
    "source",
    [
        {
            "source_id": "implementation.user.create",
            "origin": "source_code",
            "reference": "app/models/user.py",
            "summary": "The current implementation defaults is_active to false.",
            "decision": "accepted",
            "content_ref": {
                "path": "app/models/user.py",
                "digest": "a" * 64,
            },
        },
        {
            "source_id": "requirement.user.create",
            "origin": "requirement",
            "reference": "User creation acceptance criteria",
            "summary": "An agent marked this accepted without authenticating the input.",
            "decision": "accepted",
        },
    ],
)
def test_accepted_decision_without_an_authoritative_business_source_is_rejected(
    source: dict[str, object],
) -> None:
    payload = read_fixture("user-sources.json")
    payload["sources"] = [source]

    with pytest.raises(ValidationError):
        AssertionSourcesV1.model_validate(payload)


def _user_contract() -> tuple[dict[str, object], tuple[BusinessAssertionV1, ...], AssertionSourcesV1]:
    case = read_fixture("user-case.json")
    raw_assertions = case["assertions"]
    assert isinstance(raw_assertions, list)
    assertions = tuple(BusinessAssertionV1.model_validate(item) for item in raw_assertions)
    sources = AssertionSourcesV1.model_validate(read_fixture("user-sources.json"))
    return case, assertions, sources


def test_user_fixture_freezes_the_eight_reviewed_business_assertions() -> None:
    case, assertions, sources = _user_contract()

    validated = validate_assertion_provenance(
        case_id="TC_USER_CREATE_001",
        revision="1",
        spec_digest="1" * 64,
        assertions=assertions,
        sources=sources,
    )

    assert [assertion.assertion_id for assertion in validated] == [
        "api.http_status",
        "api.code",
        "user.row_count",
        "user.username",
        "user.email",
        "user.is_active",
        "user.is_superuser",
        "user.dept_id",
    ]
    assert [assertion.subject for assertion in validated] == [
        "create.response.http_status",
        "create.response.business_code",
        "created_user.count",
        "created_user.username",
        "created_user.email",
        "created_user.is_active",
        "created_user.is_superuser",
        "created_user.dept_id",
    ]
    assert [assertion.expected.model_dump(mode="json") for assertion in validated] == [
        {"kind": "literal", "value": 200},
        {"kind": "literal", "value": 200},
        {"kind": "literal", "value": 1},
        {"kind": "input", "key": "username"},
        {"kind": "input", "key": "email"},
        {"kind": "input", "key": "is_active"},
        {"kind": "input", "key": "is_superuser"},
        {"kind": "input", "key": "dept_id"},
    ]
    assert "execution_id" not in case
    assert "execution_id" not in type(sources).model_fields


def test_reviewed_requirement_true_is_not_replaced_by_current_source_false() -> None:
    case, assertions, sources = _user_contract()
    implementation_value = False

    validated = validate_assertion_provenance(
        case_id=str(case["case_id"]),
        revision=str(case["revision"]),
        spec_digest=str(case["spec_digest"]),
        assertions=assertions,
        sources=sources,
    )
    expected = next(item.expected for item in validated if item.assertion_id == "user.is_active")

    assert expected.kind == "input"
    assert expected.key == "is_active"
    inputs = case["inputs"]
    assert isinstance(inputs, dict)
    assert inputs[expected.key] is True
    assert inputs[expected.key] is not implementation_value


@pytest.mark.parametrize(
    ("revision", "spec_digest"),
    [("2", "1" * 64), ("1", "3" * 64)],
)
def test_provenance_version_or_digest_mismatch_is_not_ready(
    revision: str,
    spec_digest: str,
) -> None:
    _, assertions, sources = _user_contract()

    with pytest.raises(ValueError, match="does not match"):
        validate_assertion_provenance(
            case_id="TC_USER_CREATE_001",
            revision=revision,
            spec_digest=spec_digest,
            assertions=assertions,
            sources=sources,
        )


def test_duplicate_assertion_id_is_not_ready() -> None:
    _, assertions, sources = _user_contract()

    with pytest.raises(ValueError, match="assertion IDs must be unique"):
        validate_assertion_provenance(
            case_id="TC_USER_CREATE_001",
            revision="1",
            spec_digest="1" * 64,
            assertions=(*assertions, assertions[0]),
            sources=sources,
        )


def test_assertion_without_a_reviewed_source_is_not_ready() -> None:
    _, assertions, sources = _user_contract()
    missing = assertions[0].model_copy(update={"source_id": "requirement.missing"})

    with pytest.raises(ValueError, match="missing authoritative source"):
        validate_assertion_provenance(
            case_id="TC_USER_CREATE_001",
            revision="1",
            spec_digest="1" * 64,
            assertions=(missing, *assertions[1:]),
            sources=sources,
        )


def test_verification_contracts_are_public_and_the_source_schema_is_registered() -> None:
    assert PublicBusinessAssertionV1 is BusinessAssertionV1
    assert PublicAssertionSourcesV1 is AssertionSourcesV1
    schema_id = "assurance.intake.schema.assertion-sources.v1"
    assert resource_bytes("schemas/assertion-sources.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, AssertionSourcesV1.model_json_schema())
    )
    assert schema_id in IntakePlugin.descriptor().schemas


def test_explicit_assertion_sidecar_is_locked_for_design_and_review() -> None:
    case_path = "qa/changes/CH-1/cases/system/user/case.yaml"
    source_path = "qa/changes/CH-1/cases/system/user/assertion-sources.json"
    common = {
        "change_id": "CH-1",
        "capability_leafs": ("entities.user.create",),
        "artifact_paths": ("qa/changes",),
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": f"qa/changes/CH-1/plan/{'a' * 64}/resolved-assurance-plan.json",
            "digest": "b" * 64,
        },
        "case_delta_paths": (case_path,),
        "assertion_source_paths": (source_path,),
    }

    design = CaseDesignInputV1.model_validate(common)
    review_paths = case_review_inputs("CH-1", design.case_delta_paths, design.assertion_source_paths)
    review = CaseReviewInputV1.model_validate(
        {
            **common,
            "review_input_paths": review_paths,
        }
    )

    assert source_path in case_design_outputs("CH-1", design.case_delta_paths, design.assertion_source_paths)
    assert source_path in review.review_input_paths
    assert source_path not in case_design_outputs("CH-1", (case_path,))


def test_assertion_sidecar_must_match_an_exact_locked_case_directory() -> None:
    with pytest.raises(ValidationError, match="assertion_source_paths"):
        CaseDesignInputV1.model_validate(
            {
                "change_id": "CH-1",
                "capability_leafs": ("entities.user.create",),
                "artifact_paths": ("qa/changes",),
                "plan_digest": "a" * 64,
                "plan_ref": {
                    "path": f"qa/changes/CH-1/plan/{'a' * 64}/resolved-assurance-plan.json",
                    "digest": "b" * 64,
                },
                "case_delta_paths": ("qa/changes/CH-1/cases/system/user/case.yaml",),
                "assertion_source_paths": ("qa/changes/CH-1/cases/system/other/assertion-sources.json",),
            }
        )
