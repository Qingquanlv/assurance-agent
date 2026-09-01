from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import PermanentTaskFailure, RejectedTaskResult
from graph_engine.plugin_api import (
    ResourceClaims,
    SealedFile,
    SealedWriteSet,
    ValidationContext,
    ValidationResult,
    run_validators,
)


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


def _sealed_file(path: str, content: bytes) -> SealedFile:
    digest = sha256(content).hexdigest()
    return SealedFile(
        path=path,
        before_sha256=None,
        before_mode=None,
        after_sha256=digest,
        after_mode=0o644,
        content=content,
    )


def _sealed_write_set(*items: SealedFile) -> SealedWriteSet:
    files = tuple(sorted(items, key=lambda item: item.path))
    return SealedWriteSet(
        files=files,
        sealed_digest=sha256(b"test-sealed").hexdigest(),
    )


def test_validator_context_contains_authenticated_semantic_values() -> None:
    validated_input = RunInput(change_id="chg-1")
    validated_output = RunOutput(status="ok")
    expected_evidence = ("qa/evidence/a.json", "qa/evidence/b.json")
    sealed = _sealed_write_set(_sealed_file("out.txt", b"after"))
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(writes=("out.txt",)),
        task_input=validated_input.model_dump(mode="json"),
        task_output=validated_output.model_dump(mode="json"),
        evidence_refs=expected_evidence,
        write_set=sealed,
    )

    assert context.task_input == validated_input.model_dump(mode="json")
    assert context.task_output == validated_output.model_dump(mode="json")
    assert context.evidence_refs == expected_evidence
    assert context.write_set is not None
    assert context.write_set.sealed_digest == sealed.sealed_digest
    with pytest.raises(ValidationError):
        context.task_input = {"change_id": "mutated"}


def test_validator_ids_run_in_contract_tuple_order() -> None:
    order: list[str] = []

    class _Recorder:
        def __init__(self, validator_id: str) -> None:
            self.validator_id = validator_id

        def validate(self, staged: object, context: ValidationContext) -> ValidationResult:
            del staged, context
            order.append(self.validator_id)
            return ValidationResult(accepted=True)

    first = "assurance.execution.validator.one.v1"
    second = "assurance.execution.validator.two.v1"
    sealed = _sealed_write_set(_sealed_file("out.txt", b"after"))
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(),
        write_set=sealed,
    )

    result = run_validators(
        (first, second),
        {first: _Recorder(first), second: _Recorder(second)},
        sealed,
        context,
    )

    assert result is None
    assert order == [first, second]


def test_first_rejection_stops_promotion_and_returns_rejected_result() -> None:
    order: list[str] = []

    class _Ordered:
        def __init__(self, validator_id: str, *, accepted: bool) -> None:
            self.validator_id = validator_id
            self.accepted = accepted

        def validate(self, staged: object, context: ValidationContext) -> ValidationResult:
            del staged, context
            order.append(self.validator_id)
            if self.accepted:
                return ValidationResult(accepted=True)
            return ValidationResult(accepted=False, reason="first rejection")

    first = "assurance.execution.validator.one.v1"
    second = "assurance.execution.validator.two.v1"
    sealed = _sealed_write_set(_sealed_file("out.txt", b"after"))
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(),
        write_set=sealed,
    )

    result = run_validators(
        (first, second),
        {first: _Ordered(first, accepted=False), second: _Ordered(second, accepted=True)},
        sealed,
        context,
    )

    assert isinstance(result, RejectedTaskResult)
    assert result.reason == "first rejection"
    assert result.writes_promoted is False
    assert order == [first]


def test_validator_exception_is_typed_permanent_failure_never_implicit_accept() -> None:
    class _Boom:
        def validate(self, staged: object, context: ValidationContext) -> ValidationResult:
            del staged, context
            raise RuntimeError("validator crashed")

    sealed = _sealed_write_set(_sealed_file("out.txt", b"after"))
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(),
        write_set=sealed,
    )

    result = run_validators(
        ("assurance.execution.validator.boom.v1",),
        {"assurance.execution.validator.boom.v1": _Boom()},
        sealed,
        context,
    )

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "internal"
    assert result.writes_promoted is False
    assert "validator crashed" in result.message


def test_empty_validator_tuple_performs_zero_calls_and_remains_explicit() -> None:
    class _Forbidden:
        def validate(self, staged: object, context: ValidationContext) -> ValidationResult:
            raise AssertionError("empty validator tuple must not invoke a validator")

    sealed = _sealed_write_set(_sealed_file("out.txt", b"after"))
    context = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(),
        write_set=sealed,
    )

    result = run_validators((), {"assurance.execution.validator.unused.v1": _Forbidden()}, sealed, context)

    assert result is None
