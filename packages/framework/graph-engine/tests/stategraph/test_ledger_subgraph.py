from __future__ import annotations

from collections.abc import Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef
from graph_engine.attempts.models.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.attempts.models.keys import BusinessActivation
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.ledger import AttemptLedgerState, NamedWrite, ledger_refs
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.recording_build_context import RecordingCapabilityBuildContext


class _ProbeInput(BaseModel):
    marker: str


class _ProbeOutput(BaseModel):
    status: str


class _NamedWrites:
    def __init__(self, contract_id: str) -> None:
        self.contract_id = contract_id

    def ledger_namespace(self) -> str:
        return "lane"

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return (
            NamedWrite(name="note", root="qa/note.md"),
            NamedWrite(name="extra", root="qa/extra.md"),
        )


def _contract(contract_id: str) -> TaskAttemptContract[_ProbeInput, _ProbeOutput]:
    return TaskAttemptContract(
        contract_id=contract_id,
        owner_id="test.lane",
        handler_id="test.lane.handle",
        input_model=_ProbeInput,
        output_model=_ProbeOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


def _select_write(_state: Mapping[str, object]) -> _ProbeInput:
    return _ProbeInput(marker="write")


def _publish(_state: object, _output: object, _receipt: object) -> dict[str, object]:
    return {}


def _compile_writer(
    context: RecordingCapabilityBuildContext,
    contract: _NamedWrites,
) -> CompiledStateGraph:
    builder: AttemptGraph[AttemptLedgerState] = AttemptGraph(
        AttemptLedgerState,
        context,
        namespace="lane",
        output_schema=AttemptLedgerState,
    )
    builder.add_attempt(
        "write",
        contract,
        select=_select_write,
        publish=_publish,
        activation=BusinessActivation.one_shot(),
        semantic_node_id="lane.write",
    )
    builder.add_edge(START, "write")
    builder.add_edge("write", END)
    return builder.compile_subgraph()


def _compile_reader(
    context: RecordingCapabilityBuildContext,
    contract: TaskAttemptContract[_ProbeInput, _ProbeOutput],
    seen: list[object],
) -> CompiledStateGraph:
    def select(state: Mapping[str, object]) -> _ProbeInput:
        seen.append(ledger_refs(state.get("artifact_ledger"), "lane.note"))
        return _ProbeInput(marker="read")

    builder: AttemptGraph[AttemptLedgerState] = AttemptGraph(
        AttemptLedgerState,
        context,
        namespace="lane",
        output_schema=AttemptLedgerState,
    )
    builder.add_attempt(
        "read",
        contract,
        select=select,
        publish=_publish,
        activation=BusinessActivation.one_shot(),
        semantic_node_id="lane.read",
    )
    builder.add_edge(START, "read")
    builder.add_edge("read", END)
    return builder.compile_subgraph()


async def test_artifact_ledger_crosses_subgraph_boundaries_and_last_write_wins() -> None:
    harness = GraphHarness()
    write_contract = _contract("test.lane.agent.write.v1")
    read_contract = _contract("test.lane.agent.read.v1")
    context = harness.recording_context(
        owner_id="test.lane",
        contracts={
            write_contract.contract_id: write_contract,
            read_contract.contract_id: read_contract,
        },
    )
    seen: list[object] = []
    writer = _compile_writer(context, _NamedWrites(write_contract.contract_id))
    reader = _compile_reader(context, read_contract, seen)
    parent: StateGraph[AttemptLedgerState] = StateGraph(AttemptLedgerState)
    parent.add_node("write", writer)
    parent.add_node("read", reader)
    parent.add_node("rewrite", writer)
    parent.add_edge(START, "write")
    parent.add_edge("write", "read")
    parent.add_edge("read", "rewrite")
    parent.add_edge("rewrite", END)
    first = ArtifactRef(path="qa/note.md", digest="a" * 64)
    extra = ArtifactRef(path="qa/extra.md", digest="e" * 64)
    second = ArtifactRef(path="qa/note.md", digest="b" * 64)
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest="c" * 64)
    output = _ProbeOutput(status="ok")
    result = await harness.run(
        parent,
        input={},
        script={
            "lane.write": [
                committed(output, receipt, artifacts=(first, extra)),
                committed(output, receipt, artifacts=(second,)),
            ],
            "lane.read": [committed(output, receipt, artifacts=())],
        },
    )
    assert seen == [[{"path": "qa/note.md", "digest": "a" * 64}]]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert ledger_refs(terminal["artifact_ledger"], "lane.note") == [
        {"path": "qa/note.md", "digest": "b" * 64}
    ]
    assert ledger_refs(terminal["artifact_ledger"], "lane.extra") == [
        {"path": "qa/extra.md", "digest": "e" * 64}
    ]
