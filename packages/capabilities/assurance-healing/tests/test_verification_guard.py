from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from graph_engine.attempts import AttemptKey

from assurance_generation.contracts.execution_plan import (
    CaseExecutionPlanV1,
    CasePlanContextV1,
    TraceNotRequiredV1,
)
from assurance_generation.operations.execution_plan import compile_case_plan
from assurance_intake.contracts.verification import AssertionSourcesV1
from tests.verification_support import read_fixture
from tests.verified_generation_fixture import accepted_verified_execution_input


def _plan(profile: str = "api_db_trace.v1") -> CaseExecutionPlanV1:
    fixture = cast(dict[str, Any], copy.deepcopy(read_fixture("user-plan.json")))
    return compile_case_plan(
        cast(dict[str, object], copy.deepcopy(read_fixture("user-case.json"))),
        AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
        cast(dict[str, object], fixture["bindings"]),
        profile,
        context=CasePlanContextV1.model_validate(fixture["context"]),
    )


def _mutate(plan: CaseExecutionPlanV1, mutation: str) -> CaseExecutionPlanV1:
    if mutation == "delete_db_oracle":
        return plan.model_copy(
            update={
                "oracle": plan.oracle.model_copy(
                    update={
                        "assertion_obligations": tuple(
                            item for item in plan.oracle.assertion_obligations if item != "user.email"
                        )
                    }
                )
            }
        )
    elif mutation == "required_to_optional":
        return plan.model_copy(
            update={"required": tuple(item for item in plan.required if item != "user.email")}
        )
    elif mutation == "expected_from_observed_actual":
        assertion = next(item for item in plan.assertions if item.assertion_id == "api.code")
        assertions = list(plan.assertions)
        assertions[assertions.index(assertion)] = assertion.model_copy(
            update={"expected": assertion.expected.model_copy(update={"value": 500})}
        )
        return plan.model_copy(update={"assertions": tuple(assertions)})
    elif mutation == "comparator":
        binding = next(item for item in plan.bindings if item.obligation_id == "user.row_count")
        bindings = list(plan.bindings)
        bindings[bindings.index(binding)] = binding.model_copy(update={"comparator": "eq"})
        return plan.model_copy(update={"bindings": tuple(bindings)})
    elif mutation == "completion_budget":
        return plan.model_copy(
            update={"completion": plan.completion.model_copy(update={"http_timeout_seconds": 30})}
        )
    elif mutation == "trace_downgrade":
        return plan.model_copy(update={"trace": TraceNotRequiredV1(status="not_required")})
    elif mutation == "spec_identity":
        return plan.model_copy(update={"spec_digest": "9" * 64})
    elif mutation == "source_identity":
        return plan.model_copy(update={"assertion_sources_digest": "8" * 64})
    elif mutation == "profile_identity":
        return plan.model_copy(update={"validation_profile": "api_db.v1"})
    else:  # pragma: no cover - closed test table
        raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        "delete_db_oracle",
        "required_to_optional",
        "expected_from_observed_actual",
        "comparator",
        "completion_budget",
        "trace_downgrade",
        "spec_identity",
        "source_identity",
        "profile_identity",
    ],
)
def test_verified_repair_rejects_weakened_frozen_obligation(mutation: str) -> None:
    from assurance_healing.operations.verification_guard import assert_same_obligations

    before = _plan()
    after = _mutate(before, mutation)

    with pytest.raises(ValueError, match="verification obligations changed"):
        assert_same_obligations(before, after)


def test_verified_repair_allows_only_technical_identity_and_credential_rotation() -> None:
    from assurance_healing.operations.verification_guard import assert_same_obligations

    before = _plan("api_db.v1")
    action = before.action.model_copy(update={"credential_ref": "secret://rotated-user-admin"})
    bindings = tuple(
        binding.model_copy(
            update={"actual": binding.actual.model_copy(update={"credential_ref": action.credential_ref})}
        )
        if binding.obligation_id == "action.finished"
        else binding
        for binding in before.bindings
    )
    after = before.model_copy(
        update={
            "technical_config_digest": "7" * 64,
            "sut_digest": "6" * 64,
            "action": action,
            "bindings": bindings,
        }
    )

    assert_same_obligations(before, after)


@pytest.mark.parametrize("mutation", ["missing", "invalid"])
def test_verified_bridge_defect_is_derived_only_after_repaired_admission_succeeds(
    tmp_path, mutation: str
) -> None:
    from assurance_generation.contracts.admission import diagnose_verified_bridge_defect

    prepared = accepted_verified_execution_input(tmp_path)
    generation = prepared.generation_result
    assert generation is not None
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    path = tmp_path / bridge.path
    if mutation == "missing":
        path.unlink()
    else:
        path.write_text("def forged():\n    return True\n", encoding="utf-8")

    defect = diagnose_verified_bridge_defect(
        tmp_path,
        generation=generation,
        validation_profile="api_db.v1",
        selected_test_families=("api",),
        capability_leafs=("entities.item.create",),
        attempt_key=AttemptKey(digest="4" * 64),
    )

    assert defect.defect_kind == f"{mutation}_bridge"
    assert defect.bridge_ref == bridge
    assert defect.generation == generation
    assert defect.attempt_key.digest == "4" * 64


def test_verified_bridge_defect_rejects_non_bridge_generation_corruption(tmp_path) -> None:
    from assurance_generation.contracts.admission import diagnose_verified_bridge_defect

    prepared = accepted_verified_execution_input(tmp_path)
    generation = prepared.generation_result
    assert generation is not None
    (tmp_path / generation.mapping_ref.path).write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        diagnose_verified_bridge_defect(
            tmp_path,
            generation=generation,
            validation_profile="api_db.v1",
            selected_test_families=("api",),
            capability_leafs=("entities.item.create",),
            attempt_key=AttemptKey(digest="4" * 64),
        )
