from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptExecutor,
    AttemptKey,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.plugin_api import ResourceClaims


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


class _NamedExecutor:
    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(
        self, validated_input: RunInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[RunOutput]:
        del validated_input, scope
        return ExecutedAttemptResult(output=RunOutput(status=self.name))


def _contract(*, validators: tuple[str, ...] = ()) -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=validators,
    )


def test_task_contract_requires_explicit_validator_tuple() -> None:
    with pytest.raises(TypeError):
        TaskAttemptContract(
            contract_id="assurance.execution.run.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.run",
            input_model=RunInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
        )


def test_explicit_empty_validators_are_valid() -> None:
    contract = _contract(validators=())
    assert contract.validators == ()


def test_resolved_contract_digest_is_data_only() -> None:
    contract = _contract(validators=())
    executor_a: AttemptExecutor[RunInput, RunOutput] = _NamedExecutor("a")
    executor_b: AttemptExecutor[RunInput, RunOutput] = _NamedExecutor("b")
    first = resolve_contract(contract, executor=executor_a)
    second = resolve_contract(contract, executor=executor_b)
    assert first.contract_digest == second.contract_digest
    assert "executor" not in first.canonical_projection()
    assert first.executor is executor_a
    assert second.executor is executor_b


def test_resolved_contract_projection_covers_data_fields() -> None:
    contract = _contract(validators=("assurance.execution.validator.evidence.v1",))
    resolved = resolve_contract(contract, executor=_NamedExecutor("a"))
    projection = resolved.canonical_projection()
    assert set(projection) == {
        "contract_id",
        "owner_id",
        "handler_id",
        "input_model",
        "output_model",
        "input_schema_digest",
        "output_schema_digest",
        "resources",
        "retry",
        "timeout",
        "validators",
    }
    assert projection["contract_id"] == "assurance.execution.run.v1"
    assert projection["owner_id"] == "assurance.execution"
    assert projection["handler_id"] == "assurance.execution.run"
    assert projection["input_model"] == f"{RunInput.__module__}.{RunInput.__qualname__}"
    assert projection["output_model"] == f"{RunOutput.__module__}.{RunOutput.__qualname__}"
    assert projection["validators"] == ["assurance.execution.validator.evidence.v1"]
    assert isinstance(projection["input_schema_digest"], str)
    assert len(projection["input_schema_digest"]) == 64
    assert isinstance(resolved, ResolvedAttemptContract)


def test_ordered_validator_ids_change_the_digest() -> None:
    first = resolve_contract(
        _contract(validators=("assurance.execution.validator.evidence.v1",)),
        executor=_NamedExecutor("a"),
    )
    second = resolve_contract(
        _contract(validators=("assurance.execution.validator.evidence.v2",)),
        executor=_NamedExecutor("a"),
    )
    assert first.contract_digest != second.contract_digest


def test_execution_context_is_frozen() -> None:
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=AttemptKey(digest="a" * 64),
        fencing_token=1,
    )
    with pytest.raises(ValidationError):
        context.invocation_id = "other"


def test_closed_result_contract_is_exported() -> None:
    assert AuthorizedAttemptScope.__name__ == "AuthorizedAttemptScope"
    assert ExecutedAttemptResult.__name__ == "ExecutedAttemptResult"
