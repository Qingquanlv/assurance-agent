from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.boot.boot import ContractOwnershipError
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import ResourceClaims
from graph_engine.testing import (
    GraphHarness,
    RecordingCapabilityBuildContext,
    SemanticAttemptCall,
    committed,
)


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


class RunState(TypedDict):
    change_id: str
    status: str


OUTPUT = RunOutput(status="ok")
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)


def _execution_contract() -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.agent.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _intake_contract() -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.intake.agent.prepare.v1",
        owner_id="assurance.intake",
        handler_id="assurance.intake.prepare",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


def select_run(state: RunState) -> RunInput:
    return RunInput(change_id=str(state["change_id"]))


def publish_run(state: RunState, output: RunOutput, receipt: ReceiptRef) -> dict[str, object]:
    del receipt
    return {"change_id": state["change_id"], "status": output.status}


def select_probe(state: object) -> RunInput:
    del state
    return RunInput(change_id="probe")


def publish_probe(state: object, output: RunOutput, receipt: ReceiptRef) -> dict[str, object]:
    del state, output, receipt
    return {}


def _execution_graph(context: RecordingCapabilityBuildContext) -> object:
    builder: StateGraph[RunState] = StateGraph(RunState)
    builder.add_node(
        "execution.run",
        context.attempt(
            "assurance.execution.agent.run.v1",
            semantic_node_id="execution.run",
            activation=BusinessActivation.one_shot(),
            select=select_run,
            publish=publish_run,
        ),
    )
    builder.add_edge(START, "execution.run")
    builder.add_edge("execution.run", END)
    return context.compile_subgraph(builder)


async def test_harness_records_semantic_attempt_not_internal_task_identity() -> None:
    harness = GraphHarness()
    contract = _execution_contract()
    context = harness.recording_context(
        owner_id="assurance.execution",
        contracts={contract.contract_id: contract},
    )
    graph = _execution_graph(context)
    output = OUTPUT
    receipt = RECEIPT
    result = await harness.run(
        graph,
        input={"change_id": "chg-1"},
        script={"execution.run": [committed(output, receipt)]},
    )
    input_digest = canonical_digest(RunInput(change_id="chg-1").model_dump(mode="json"))
    assert result.semantic_calls == (
        SemanticAttemptCall("execution.run", "assurance.execution.agent.run.v1", input_digest),
    )
    assert "task_id" not in result.model_dump_json()


def test_recording_context_rejects_foreign_contract_and_root_saver() -> None:
    own = _intake_contract()
    intake_context = RecordingCapabilityBuildContext(
        owner_id="assurance.intake",
        contracts={own.contract_id: own},
    )
    with pytest.raises(ContractOwnershipError):
        intake_context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign-probe",
            activation=BusinessActivation.one_shot(),
            select=select_probe,
            publish=publish_probe,
        )
    assert not hasattr(intake_context, "checkpointer")


def test_with_test_contract_stays_owner_local_and_does_not_touch_catalog() -> None:
    catalog: dict[str, object] = {}
    own = _intake_contract()
    intake_context = RecordingCapabilityBuildContext(
        owner_id="assurance.intake",
        contracts={own.contract_id: own},
    )
    clone = TaskAttemptContract(
        contract_id="test.assurance.intake.prepare.v1",
        owner_id="assurance.intake",
        handler_id=own.handler_id,
        input_model=own.input_model,
        output_model=own.output_model,
        resources=own.resources,
        retry=own.retry,
        timeout=own.timeout,
        validators=own.validators,
    )
    installed = intake_context.with_test_contract(clone)
    installed.attempt(
        clone.contract_id,
        semantic_node_id="intake.test-clone",
        activation=BusinessActivation.one_shot(),
        select=select_probe,
        publish=publish_probe,
    )
    assert clone.contract_id not in catalog
    assert catalog == {}
    assert "mark_routes_successful" not in dir(intake_context)
    assert not hasattr(intake_context, "succeed_all_routes")
