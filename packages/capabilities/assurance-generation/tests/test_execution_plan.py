from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from assurance_generation.contracts.execution_plan import (
    BASE_RUNTIME_OBLIGATIONS,
    TRACE_OBLIGATIONS,
    CaseExecutionPlanV1,
    CasePlanContextV1,
    USER_SQLITE_BINDING_ID,
    required_obligations,
    validate_case_plan_sources,
)
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.operations.execution_plan import PlanNotReady, compile_case_plan
from assurance_generation.operations.planning import PlanFinalizeHandler
from assurance_generation.operations.review import PlanReviewFinalizeHandler
from assurance_intake.contracts.verification import AssertionSourcesV1
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    VALID_LEAFS,
    fake_agent_result,
    review_result,
    valid_plan_result,
)
from tests.product.test_change_local_output_routing import dual_roots, execute_task
from tests.verification_support import read_fixture


def _fixture() -> tuple[dict[str, object], AssertionSourcesV1, dict[str, object], CasePlanContextV1]:
    plan = read_fixture("user-plan.json")
    return (
        read_fixture("user-case.json"),
        AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
        cast(dict[str, object], copy.deepcopy(plan["bindings"])),
        CasePlanContextV1.model_validate(plan["context"]),
    )


def _materialize_context(project: Path) -> CasePlanContextV1:
    case = read_fixture("user-case.json")
    case_path = project / "qa/changes/CH-USER-001/cases/system/user/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(cast(JSONValue, case))
    case_path.write_bytes(data)
    raw = cast(dict[str, Any], copy.deepcopy(read_fixture("user-plan.json")["context"]))
    raw["reviewed_case"]["case_refs"][0]["digest"] = hashlib.sha256(data).hexdigest()
    return CasePlanContextV1.model_validate(raw)


def _write_api_candidate(write_root: Path) -> tuple[tuple[str, ...], dict[str, Any]]:
    root = "qa/changes/CH-USER-001/plans"
    names = (
        "api-plan.md",
        "api-test-data-plan.md",
        "api-codegen-plan.md",
        "api-codegen-mapping.json",
        "api-execution-bindings.json",
        "m3-review-summary.md",
    )
    paths = tuple(f"{root}/{name}" for name in names)
    for relative in paths:
        path = write_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("plan\n", encoding="utf-8")
    (write_root / f"{root}/api-codegen-mapping.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "layer": "api",
                "entries": [
                    {
                        "case_id": "TC_USER_CREATE_001",
                        "symbol": "test_tc_user_create_001__create",
                        "target_file": "tests/api/test_user_create.py",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fixture = read_fixture("user-plan.json")
    (write_root / f"{root}/api-execution-bindings.json").write_text(
        json.dumps(
            {
                "schema_version": fixture["schema_version"],
                "case_id": fixture["case_id"],
                "bindings": fixture["bindings"],
            }
        ),
        encoding="utf-8",
    )
    plan_result: dict[str, Any] = {
        "schema_version": "1",
        "family": "api",
        "change_id": "CH-USER-001",
        "case_ids": ["TC_USER_CREATE_001"],
        "required_capabilities": ["entities.item.create"],
        "coverage": [
            {
                "case_id": "TC_USER_CREATE_001",
                "operation": "create",
                "risk": "high",
                "required_capabilities": ["entities.item.create"],
            }
        ],
        "output_files": list(paths),
    }
    return paths, plan_result


def _finalize_input(
    result: dict[str, Any],
    *,
    context: CasePlanContextV1,
    artifact_paths: tuple[str, ...] = (),
) -> dict[str, JSONValue]:
    payload = fake_agent_result(
        result,
        artifact_paths=list(artifact_paths),
        capability_leafs=VALID_LEAFS,
    )
    payload.update(
        {
            "plan_digest": context.plan_digest,
            "plan_ref": context.plan_ref.model_dump(mode="json"),
            "reviewed_case": context.reviewed_case.model_dump(mode="json"),
            "case_plan_context": context.model_dump(mode="json"),
            "assertion_sources": read_fixture("user-sources.json"),
            "validation_profile": "api_db.v1",
        }
    )
    return payload


def test_missing_oracle_is_not_ready() -> None:
    case, sources, bindings, context = _fixture()
    bindings.pop("user.row_count")

    with pytest.raises(PlanNotReady, match="user.row_count"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_missing_business_assertion_cannot_shrink_required_obligations() -> None:
    case, sources, bindings, context = _fixture()
    assertions = cast(list[dict[str, object]], case["assertions"])
    case["assertions"] = [
        assertion for assertion in assertions if assertion["assertion_id"] != "user.dept_id"
    ]
    bindings.pop("user.dept_id")

    with pytest.raises(PlanNotReady, match="business assertion obligations"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_unsupported_assertion_comparator_is_not_ready() -> None:
    case, sources, bindings, context = _fixture()
    assertions = cast(list[dict[str, object]], case["assertions"])
    row_count = next(assertion for assertion in assertions if assertion["assertion_id"] == "user.row_count")
    row_count["comparator"] = "eq"

    with pytest.raises(PlanNotReady, match="comparator"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_missing_user_input_is_not_ready() -> None:
    case, sources, bindings, context = _fixture()
    inputs = cast(dict[str, object], case["inputs"])
    inputs.pop("dept_id")

    with pytest.raises(PlanNotReady, match="User inputs"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_compiler_derives_expected_required_and_completion_from_the_frozen_case() -> None:
    case, sources, bindings, context = _fixture()

    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)

    case_assertions = cast(list[dict[str, str]], case["assertions"])
    assertion_ids = frozenset(item["assertion_id"] for item in case_assertions)
    assert plan.required == tuple(sorted(assertion_ids | BASE_RUNTIME_OBLIGATIONS))
    assert plan.completion.obligations == plan.required
    assert plan.trace.status == "not_required"
    assert plan.action.method == "POST"
    assert plan.action.path == "/api/v1/user/create"
    assert plan.oracle.binding_id == USER_SQLITE_BINDING_ID
    assert plan.oracle.lookup_inputs == ("username", "email")
    assertions = {item.assertion_id: item for item in plan.assertions}
    for binding in plan.bindings:
        if binding.obligation_id in assertions:
            assert binding.expected_id == binding.obligation_id
            assert binding.comparator == assertions[binding.obligation_id].comparator
        else:
            assert binding.expected_id is None
            assert binding.comparator is None


def test_trace_profile_adds_all_trace_obligations() -> None:
    case, sources, bindings, context = _fixture()

    plan = compile_case_plan(case, sources, bindings, "api_db_trace.v1", context=context)

    case_assertions = cast(list[dict[str, str]], case["assertions"])
    assertion_ids = frozenset(item["assertion_id"] for item in case_assertions)
    assert frozenset(plan.required) == required_obligations(assertion_ids, "api_db_trace.v1")
    assert TRACE_OBLIGATIONS <= frozenset(plan.required)
    assert plan.trace.status == "required"


@pytest.mark.parametrize("override", ["expected", "required", "ready"])
def test_candidate_binding_cannot_override_deterministic_plan_fields(override: str) -> None:
    case, sources, bindings, context = _fixture()
    binding = bindings["user.row_count"]
    assert isinstance(binding, dict)
    binding[override] = 1

    with pytest.raises(PlanNotReady, match="user.row_count"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_unknown_obligation_binding_is_not_ready() -> None:
    case, sources, bindings, context = _fixture()
    bindings["user.password_hash"] = {"kind": "sqlite_user_field", "field": "username"}

    with pytest.raises(PlanNotReady, match="unknown obligation.*user.password_hash"):
        compile_case_plan(case, sources, bindings, "api_db.v1", context=context)


def test_source_spec_digest_drift_is_not_ready() -> None:
    case, sources, bindings, context = _fixture()
    drifted = sources.model_copy(update={"spec_digest": "9" * 64})

    with pytest.raises(PlanNotReady, match="sources.*frozen specification digest"):
        compile_case_plan(case, drifted, bindings, "api_db.v1", context=context)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("required", "required"),
        ("bindings", "bindings"),
    ],
)
def test_empty_required_or_bindings_is_rejected(field: str, message: str) -> None:
    case, sources, bindings, context = _fixture()
    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)
    payload = plan.model_dump(mode="json")
    payload[field] = []

    with pytest.raises(ValidationError, match=message):
        CaseExecutionPlanV1.model_validate(payload)


def test_duplicate_obligation_binding_is_rejected() -> None:
    case, sources, bindings, context = _fixture()
    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)
    payload = plan.model_dump(mode="json")
    payload["bindings"].append(copy.deepcopy(payload["bindings"][0]))

    with pytest.raises(ValidationError, match="sorted and unique"):
        CaseExecutionPlanV1.model_validate(payload)


def test_dangling_expected_reference_is_rejected() -> None:
    case, sources, bindings, context = _fixture()
    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)
    payload = plan.model_dump(mode="json")
    target = next(item for item in payload["bindings"] if item["obligation_id"] == "user.email")
    target["expected_id"] = "user.unknown"

    with pytest.raises(ValidationError, match="dangling or mismatched expected"):
        CaseExecutionPlanV1.model_validate(payload)


def test_trace_requirements_cannot_conflict_with_profile() -> None:
    case, sources, bindings, context = _fixture()
    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)
    payload = plan.model_dump(mode="json")
    payload["trace"] = {
        "status": "required",
        "http_obligation": "trace.http",
        "user_write_obligation": "trace.user_write",
        "checkpoint_obligation": "trace.user_completed",
        "checkpoint_id": "user.create.completed",
        "checkpoint_version": "1",
        "drain_obligation": "trace.drained",
        "require_same_action_and_sut": True,
    }

    with pytest.raises(ValidationError, match="profile conflicts with Trace"):
        CaseExecutionPlanV1.model_validate(payload)


def test_source_digest_is_rechecked_against_source_document() -> None:
    case, sources, bindings, context = _fixture()
    plan = compile_case_plan(case, sources, bindings, "api_db.v1", context=context)
    drifted = plan.model_copy(update={"assertion_sources_digest": "9" * 64})

    with pytest.raises(ValueError, match="source digest"):
        validate_case_plan_sources(
            drifted,
            case_id=cast(str, case["case_id"]),
            revision=cast(str, case["revision"]),
            spec_digest=cast(str, case["spec_digest"]),
            assertions=plan.assertions,
            sources=sources,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"coverage_epoch": 3}, "epoch"),
        ({"plan_digest": "9" * 64}, "plan binding"),
        ({"change_id": "CH-OTHER-001"}, "change_id"),
    ],
)
def test_context_rejects_reviewed_case_identity_drift(mutation: dict[str, object], message: str) -> None:
    raw = cast(dict[str, object], copy.deepcopy(read_fixture("user-plan.json")["context"]))
    raw.update(mutation)

    with pytest.raises(ValidationError, match=message):
        CasePlanContextV1.model_validate(raw)


def test_plan_result_rejects_machine_plan_identity_drift_without_changing_root_plan() -> None:
    payload = valid_plan_result("api")
    payload.update(
        {
            "case_execution_plan_ref": {
                "path": "qa/changes/CH-DEMO-001/plans/api-case-execution-plan.json",
                "digest": "1" * 64,
            },
            "case_execution_plan_digest": "2" * 64,
        }
    )

    with pytest.raises(ValidationError, match="ref and digest do not match"):
        PlanResultV1.model_validate(payload, context={"capability_leafs": frozenset(VALID_LEAFS)})


@pytest.mark.asyncio
async def test_plan_finalizer_alone_writes_the_formal_machine_plan(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    context = _materialize_context(project)
    paths, result = _write_api_candidate(write_root)

    outcome = await execute_task(
        PlanFinalizeHandler("api"),
        _finalize_input(result, context=context, artifact_paths=paths),
        project,
        write_root=write_root,
    )

    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, object], outcome.output)
    machine_ref = cast(dict[str, str], output["case_execution_plan_ref"])
    assert machine_ref["path"] == "qa/changes/CH-USER-001/plans/api-case-execution-plan.json"
    assert output["case_execution_plan_digest"] == machine_ref["digest"]
    assert output["case_execution_plan_digest"] != context.plan_digest
    assert (write_root / machine_ref["path"]).is_file()


@pytest.mark.asyncio
async def test_plan_review_finalizer_rejects_invalid_closure_even_when_agent_passes(
    tmp_path: Path,
) -> None:
    project = tmp_path
    context = _materialize_context(project)
    paths, result = _write_api_candidate(project)
    finalized = await execute_task(
        PlanFinalizeHandler("api"),
        _finalize_input(result, context=context, artifact_paths=paths),
        project,
        write_root=project,
    )
    assert finalized.status == "succeeded", finalized.failure
    formal = project / "qa/changes/CH-USER-001/plans/api-case-execution-plan.json"
    document = json.loads(formal.read_text(encoding="utf-8"))
    document["cases"][0]["assertion_sources_digest"] = "9" * 64
    formal.write_bytes(canonical_json_bytes(cast(JSONValue, document)) + b"\n")

    reviewed = await execute_task(
        PlanReviewFinalizeHandler("api"),
        _finalize_input(review_result("api"), context=context),
        project,
        write_root=project,
    )

    assert reviewed.status == "failed"
    assert reviewed.failure is not None
    assert reviewed.failure.kind == "invalid_output"
    assert "source digest" in reviewed.failure.message
