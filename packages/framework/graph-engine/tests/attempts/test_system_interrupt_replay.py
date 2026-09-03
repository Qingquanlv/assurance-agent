from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel

from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.events import AttemptOpened, SystemInterruptIssued
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.canonical import canonical_digest
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    PendingTaskResult,
    ReceiptRef,
    SystemReference,
)
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.checkpoint_bridge import MAX_ACTIVE_GENERATIONS
from langgraph.checkpoint.memory import InMemorySaver


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


REVISION = "a" * 64
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
OUTPUT = RunOutput(status="ok")
committed_output = OUTPUT


class ScriptedKernel:
    def __init__(self) -> None:
        self.resolutions: list[object] = []
        self.seen_key: AttemptKey | None = None
        self.calls = 0
        self.journal: MemoryAttemptJournal | None = None
        self.issued_events: list[SystemInterruptIssued] = []

    def push(self, resolution: object) -> None:
        self.resolutions.append(resolution)

    async def execute_or_recover(
        self, attempt_key: AttemptKey, contract: object, validated_input: object, context: object
    ) -> object:
        del contract, validated_input, context
        self.seen_key = attempt_key
        self.calls += 1
        if not self.resolutions:
            raise AssertionError("scripted kernel has no queued resolution")
        return self.resolutions.pop(0)

    async def record_system_interrupt_issued(
        self, attempt_key: AttemptKey, event: SystemInterruptIssued, context: object
    ) -> object:
        self.issued_events.append(event)
        journal = self.journal
        if journal is None:
            raise TypeError("scripted kernel has no journal")
        snapshot = await journal.load(attempt_key)
        return await journal.append(
            attempt_key,
            (event,),
            expected_revision=0 if snapshot is None else snapshot.revision,
            fencing_token=context.fencing_token,
        )


class ScriptedJournal(MemoryAttemptJournal):
    def __init__(self, kernel: ScriptedKernel) -> None:
        super().__init__()
        self.kernel = kernel

    def mark_kernel_now_committed(self, attempt_key: AttemptKey) -> None:
        del attempt_key
        self.kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))


def _resolved():
    class _Executor:
        async def execute(self, validated_input: RunInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    return resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.execution.run.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.run",
            input_model=RunInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=_Executor(),
    )


def _runtime(kernel: ScriptedKernel) -> SimpleNamespace:
    return SimpleNamespace(
        attempt_kernel=kernel,
        revision_id=REVISION,
        fencing_token=4,
        invocation_id="inv-1",
        public_entrypoint="execute",
    )


def _state() -> dict[str, object]:
    return {"change_id": "chg-1"}


def select_activation(_state: dict[str, object]) -> BusinessActivation:
    return BusinessActivation.one_shot()


def select_input(state: dict[str, object]) -> RunInput:
    return RunInput(change_id=str(state["change_id"]))


def publish_output(_state: dict[str, object], output: RunOutput, receipt: ReceiptRef) -> dict[str, object]:
    del receipt
    return {"result": output}


def _attempt_key() -> AttemptKey:
    return derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=REVISION,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id="assurance.execution.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )


async def invoke_until_interrupt(node: Any, state: dict[str, object], runtime: object) -> Any:
    with pytest.raises(GraphInterrupt) as exc_info:
        await node(state, runtime=runtime)
    return exc_info.value.args[0][0]


class _ReplayThenRaise:
    def __init__(self, replay_count: int) -> None:
        self.replay_count = replay_count
        self.calls = 0

    def __call__(self, value: object) -> object:
        self.calls += 1
        if self.calls <= self.replay_count:
            return {"resumed": True}
        from langgraph.types import Interrupt

        raise GraphInterrupt((Interrupt(value=value),))


async def resume_after_crash_before_checkpoint(
    node: Any,
    first: Any,
    *,
    state: dict[str, object],
    runtime: object,
    monkeypatch: pytest.MonkeyPatch,
    replay_count: int = 1,
) -> dict[str, object]:
    del first
    monkeypatch.setattr(
        "graph_engine.attempts.node_factory.interrupt",
        _ReplayThenRaise(replay_count),
    )
    result = await node(state, runtime=runtime)
    assert isinstance(result, dict)
    return result


async def test_resume_replays_issued_interrupt_before_kernel_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    trace: list[str] = []
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    state = _state()
    runtime = _runtime(kernel)
    attempt_key = _attempt_key()
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    first = await invoke_until_interrupt(node, state, runtime)
    assert first.value["pending_generation"] == 1
    journal.mark_kernel_now_committed(attempt_key)
    resumed = await resume_after_crash_before_checkpoint(
        node,
        first,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    assert trace[:2] == ["interrupt:generation=1:ordinal=0", "kernel:recover"]
    assert resumed["result"] == committed_output
    assert kernel.calls == 2


async def test_crash_after_resume_before_node_checkpoint_replays_same_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    trace: list[str] = []
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    state = _state()
    runtime = _runtime(kernel)
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    first = await invoke_until_interrupt(node, state, runtime)
    journal.mark_kernel_now_committed(_attempt_key())
    first_resume = await resume_after_crash_before_checkpoint(
        node,
        first,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    assert first_resume["result"] == committed_output
    trace.clear()
    journal.mark_kernel_now_committed(_attempt_key())
    second = await resume_after_crash_before_checkpoint(
        node,
        first,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    assert trace[:2] == ["interrupt:generation=1:ordinal=0", "kernel:recover"]
    assert second["result"] == committed_output


async def test_multiple_pending_generations_replay_every_ordinal_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    trace: list[str] = []
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    state = _state()
    runtime = _runtime(kernel)
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    first = await invoke_until_interrupt(node, state, runtime)
    assert first.value["pending_generation"] == 1
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-2")))
    with pytest.raises(GraphInterrupt) as exc_info:
        await resume_after_crash_before_checkpoint(
            node,
            first,
            state=state,
            runtime=runtime,
            monkeypatch=monkeypatch,
            replay_count=1,
        )
    issued = exc_info.value.args[0][0]
    assert issued.value["pending_generation"] == 2
    assert issued.value["ordinal"] == 1
    trace.clear()
    journal.mark_kernel_now_committed(_attempt_key())
    resumed = await resume_after_crash_before_checkpoint(
        node,
        issued,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
        replay_count=2,
    )
    assert trace[:3] == [
        "interrupt:generation=1:ordinal=0",
        "interrupt:generation=2:ordinal=1",
        "kernel:recover",
    ]
    assert resumed["result"] == committed_output


async def test_technical_retry_reuses_key_and_replays_issued_ordinals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    trace: list[str] = []
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    state = _state()
    runtime = _runtime(kernel)
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    first = await invoke_until_interrupt(node, state, runtime)
    first_key = kernel.seen_key
    journal.mark_kernel_now_committed(_attempt_key())
    await resume_after_crash_before_checkpoint(
        node,
        first,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    assert kernel.seen_key == first_key == _attempt_key()


async def test_replay_cannot_skip_issued_interrupt_when_external_state_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    trace: list[str] = []
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    state = _state()
    runtime = _runtime(kernel)
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    first = await invoke_until_interrupt(node, state, runtime)
    state["external"] = "changed"
    journal.mark_kernel_now_committed(_attempt_key())
    await resume_after_crash_before_checkpoint(
        node,
        first,
        state=state,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    assert trace[0] == "interrupt:generation=1:ordinal=0"
    assert "kernel:recover" in trace


async def test_active_generation_bound_is_enforced_before_another_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=[])
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    key = _attempt_key()
    digest = canonical_digest({"bound": "envelope"})
    await journal.append(
        key,
        (
            AttemptOpened(
                contract_digest=canonical_digest({"c": 1}),
                input_digest=canonical_digest({"i": 1}),
                graph_revision=REVISION,
                invocation_id="inv-1",
                public_entrypoint="execute",
                semantic_node_id="execution.run",
            ),
            *[
                SystemInterruptIssued(generation=index, ordinal=index - 1, envelope_digest=digest)
                for index in range(1, MAX_ACTIVE_GENERATIONS + 1)
            ],
        ),
        expected_revision=0,
        fencing_token=4,
    )
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-overflow")))
    monkeypatch.setattr(
        "graph_engine.attempts.node_factory.interrupt",
        _ReplayThenRaise(MAX_ACTIVE_GENERATIONS),
    )
    with pytest.raises(ValueError, match="active-generation"):
        await node(_state(), runtime=_runtime(kernel))


async def test_compiled_resume_replays_before_next_superstep() -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    kernel.journal = journal
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=[])
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )

    builder = StateGraph(dict)
    builder.add_node("run", node)
    builder.add_edge(START, "run")
    builder.add_edge("run", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": REVISION,
            "assurance_fencing_token": 4,
        }
    }
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    interrupted = await graph.ainvoke({"change_id": "chg-1"}, config)
    assert interrupted["__interrupt__"]
    journal.mark_kernel_now_committed(_attempt_key())
    resumed = await graph.ainvoke(Command(resume=True), config)
    assert resumed["result"] == committed_output
