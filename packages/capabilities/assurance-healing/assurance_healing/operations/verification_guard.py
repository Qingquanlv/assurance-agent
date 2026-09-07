"""Deterministic admission guard for repairs to verified generated tests."""

from __future__ import annotations

from typing import Any, cast

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1


def _actual_semantics(actual: object) -> dict[str, object]:
    raw = cast(Any, actual).model_dump(mode="json")
    kind = raw["kind"]
    technical = {"binding_id", "binding_version", "credential_ref"}
    return {name: value for name, value in raw.items() if name not in technical} | {"kind": kind}


def _projection(plan: CaseExecutionPlanV1) -> JSONValue:
    action = plan.action.model_dump(mode="json", exclude={"binding_id", "binding_version", "credential_ref"})
    oracle = plan.oracle.model_dump(mode="json", exclude={"binding_id", "binding_version"})
    bindings = [
        {
            "obligation_id": binding.obligation_id,
            "expected_id": binding.expected_id,
            "comparator": binding.comparator,
            "actual": _actual_semantics(binding.actual),
        }
        for binding in plan.bindings
    ]
    return cast(
        JSONValue,
        {
            "schema_version": plan.schema_version,
            "change_id": plan.change_id,
            "case_id": plan.case_id,
            "revision": plan.revision,
            "coverage_epoch": plan.coverage_epoch,
            "plan_digest": plan.plan_digest,
            "plan_ref": plan.plan_ref.model_dump(mode="json"),
            "reviewed_case": plan.reviewed_case.model_dump(mode="json"),
            "case_ref": plan.case_ref.model_dump(mode="json"),
            "spec_digest": plan.spec_digest,
            "assertion_sources_digest": plan.assertion_sources_digest,
            "verification_policy_digest": plan.verification_policy_digest,
            "validation_profile": plan.validation_profile,
            "inputs": plan.inputs,
            "assertions": [item.model_dump(mode="json") for item in plan.assertions],
            "action": action,
            "initial_state": plan.initial_state.model_dump(mode="json"),
            "oracle": oracle,
            "trace": plan.trace.model_dump(mode="json"),
            "completion": plan.completion.model_dump(mode="json"),
            "required": list(plan.required),
            "bindings": bindings,
        },
    )


def assert_same_obligations(before: CaseExecutionPlanV1, after: CaseExecutionPlanV1) -> None:
    """Reject any repair that changes the frozen business verification contract."""

    if canonical_json_bytes(_projection(before)) != canonical_json_bytes(_projection(after)):
        raise ValueError("verification obligations changed")


__all__ = ["assert_same_obligations"]
