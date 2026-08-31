from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import BusinessActivation, derive_attempt_key
from graph_engine.canonical import canonical_digest


class RunInput(BaseModel):
    change_id: str


def test_attempt_key_is_stable_across_technical_retry_and_replay() -> None:
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.for_round(2),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )
    replayed = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.for_round(2),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )
    assert key == replayed


def test_new_business_round_changes_attempt_key() -> None:
    def key_for(*, round: int) -> object:
        return derive_attempt_key(
            invocation_id="inv-1",
            graph_revision="a" * 64,
            public_entrypoint="execute",
            semantic_node_id="execution.run",
            business_activation=BusinessActivation.for_round(round),
            contract_id="assurance.execution.agent.run.v1",
            validated_input=RunInput(change_id="chg-1"),
        )

    assert key_for(round=2) != key_for(round=3)


def test_canonical_input_digest_is_part_of_the_attempt_key() -> None:
    first = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )
    second = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-2"),
    )
    assert first != second
    assert first.digest == canonical_digest(
        {
            "invocation_id": "inv-1",
            "graph_revision": "a" * 64,
            "public_entrypoint": "execute",
            "semantic_node_id": "execution.run",
            "business_activation": {"kind": "root", "value": "1"},
            "contract_id": "assurance.execution.agent.run.v1",
            "task_input_digest": canonical_digest(RunInput(change_id="chg-1").model_dump(mode="json")),
        }
    )


def test_business_activation_constructors_and_rejections() -> None:
    root = BusinessActivation.one_shot()
    assert root.kind == "root"
    assert root.value == "1"

    round_activation = BusinessActivation.for_round(2)
    assert round_activation.kind == "round"
    assert round_activation.value == "2"

    trigger = BusinessActivation.for_trigger("join.arrival-1")
    assert trigger.kind == "trigger"
    assert trigger.value == "join.arrival-1"

    with pytest.raises(ValueError):
        BusinessActivation.for_round(-1)
    with pytest.raises((ValueError, ValidationError)):
        BusinessActivation.for_trigger("")
    with pytest.raises((ValueError, ValidationError)):
        BusinessActivation.for_trigger("   ")
    with pytest.raises((ValueError, ValidationError)):
        BusinessActivation.for_trigger("x" * 65)
    with pytest.raises((ValueError, ValidationError)):
        BusinessActivation.for_trigger("Not Canonical")
    with pytest.raises((ValueError, ValidationError)):
        BusinessActivation.for_trigger("langgraph/task-id")
