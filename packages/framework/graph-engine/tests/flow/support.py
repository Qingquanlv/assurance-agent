"""Shared fixtures for flow compiler tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from graph_engine.attempts.models.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import AttemptResolution, ReceiptRef
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.ledger import InputBinding, NamedWrite
from graph_engine.testing import GraphHarness
from graph_engine.testing.recording_build_context import RecordingCapabilityBuildContext

OWNER = "test.lane"
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="c" * 64)
CONFIG: dict[str, Any] = {
    "configurable": {
        "thread_id": "inv-1",
        "assurance_revision_id": "a" * 64,
        "assurance_product_lock_digest": "b" * 64,
        "assurance_root_input_digest": "d" * 64,
        "assurance_fencing_token": 1,
        "assurance_entrypoint": "execute",
    }
}


class ChangeInput(BaseModel):
    change_id: str = "c1"


class MarkerOutput(BaseModel):
    marker: str = "ok"


def contract(
    name: str,
    input_model: type[BaseModel] = ChangeInput,
    output_model: type[BaseModel] = MarkerOutput,
) -> TaskAttemptContract[Any, Any]:
    return TaskAttemptContract(
        contract_id=f"test.lane.task.{name}",
        owner_id=OWNER,
        handler_id=f"test.lane.{name}",
        input_model=input_model,
        output_model=output_model,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


class Toy:
    """An op-protocol stand-in around one registered contract."""

    def __init__(
        self,
        task: TaskAttemptContract[Any, Any],
        *,
        namespace: str = "lane",
        writes: tuple[NamedWrite, ...] = (),
        bindings: tuple[InputBinding, ...] = (),
    ) -> None:
        self.contract_id = task.contract_id
        self.input_model = task.input_model
        self.output_model = task.output_model
        self._namespace = namespace
        self._writes = writes
        self._bindings = bindings

    def ledger_namespace(self) -> str:
        return self._namespace

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return self._writes

    def input_bindings(self) -> tuple[InputBinding, ...]:
        return self._bindings


def open_harness(
    *tasks: TaskAttemptContract[Any, Any],
) -> tuple[GraphHarness, RecordingCapabilityBuildContext]:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id=OWNER,
        contracts={task.contract_id: task for task in tasks},
    )
    return harness, context


def spy_keys(harness: GraphHarness) -> list[AttemptKey]:
    seen: list[AttemptKey] = []
    original = harness._kernel.execute_or_recover

    async def spy(
        attempt_key: AttemptKey,
        contract: object,
        validated_input: object,
        context: object,
    ) -> AttemptResolution:
        seen.append(attempt_key)
        return await original(attempt_key, contract, validated_input, context)

    harness._kernel.execute_or_recover = spy
    return seen


def state_of(result: object) -> Mapping[str, Any]:
    terminal = getattr(result, "terminal", None)
    if not isinstance(terminal, Mapping):
        raise AssertionError(f"flow did not finish, got {terminal!r}")
    return terminal


def calls_of(result: object) -> list[str]:
    semantic_calls: Sequence[Any] = getattr(result, "semantic_calls", ())
    return [call.semantic_node_id for call in semantic_calls]


__all__ = [
    "CONFIG",
    "ChangeInput",
    "MarkerOutput",
    "OWNER",
    "RECEIPT",
    "Toy",
    "calls_of",
    "contract",
    "open_harness",
    "spy_keys",
    "state_of",
]
