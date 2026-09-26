from __future__ import annotations

from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from graph_engine.application.application import _system_wake_command
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey, BusinessActivation
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState


class _Step(BaseModel):
    name: str


class _State(CheckpointBridgeState, total=False):
    first: str
    second: str


class _Executor:
    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.paused = False
        self.dispatched: list[tuple[str, AttemptKey]] = []
        self.reconciled: list[AttemptKey] = []

    async def execute(
        self, validated_input: _Step, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_Step] | PendingTaskResult:
        self.dispatched.append((validated_input.name, scope.execution.attempt_key))
        output = scope.workspace.write_root / "out" / f"{validated_input.name}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(validated_input.name)
        self.paused = True
        if self.pending:
            return PendingTaskResult(wakeup=SystemReference(reference_id="provider_running"))
        return ExecutedAttemptResult(output=validated_input)

    async def reconcile(
        self, validated_input: _Step, scope: AuthorizedAttemptScope, activity: object
    ) -> ExecutedAttemptResult[_Step]:
        del activity
        self.reconciled.append(scope.execution.attempt_key)
        return ExecutedAttemptResult(output=validated_input)


def _graph(tmp_path: Path, executor: _Executor, names: tuple[str, ...]):
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision="a" * 64,
        pause_requested=lambda: executor.paused,
    )
    resolved = resolve_contract(
        TaskAttemptContract(
            contract_id="test.operator.step",
            owner_id="test.operator",
            handler_id="test.operator.step",
            input_model=_Step,
            output_model=_Step,
            resources=ResourceClaims(writes=("out",)),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=executor,
    )
    factory = AttemptNodeFactory(journal=kernel.journal, kernel=kernel)
    builder = StateGraph(_State)
    previous = START
    for name in names:
        builder.add_node(
            name,
            factory.attempt(
                resolved,
                semantic_node_id=name,
                activation=lambda _state: BusinessActivation.one_shot(),
                select=lambda _state, name=name: _Step(name=name),
                publish=lambda state, output, receipt: {**state, output.name: "committed"},
            ),
        )
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile(checkpointer=InMemorySaver()), kernel, project, store


_CONFIG: RunnableConfig = {
    "configurable": {
        "thread_id": "inv-operator-pause",
        "assurance_revision_id": "a" * 64,
        "assurance_fencing_token": 1,
        "assurance_entrypoint": "execute",
    }
}


async def test_operator_stop_checkpoints_before_next_node_and_resumes_same_attempt(tmp_path: Path) -> None:
    executor = _Executor()
    graph, kernel, project, store = _graph(tmp_path, executor, ("first", "second"))
    try:
        interrupted = await graph.ainvoke({}, _CONFIG)
        assert interrupted["first"] == "committed"
        assert [name for name, _key in executor.dispatched] == ["first"]
        assert (project / "out/first.txt").read_text() == "first"
        assert not (project / "out/second.txt").exists()
        checkpoint = await graph.aget_state(_CONFIG)
        assert checkpoint.next == ("second",)
        assert len(checkpoint.interrupts) == 1
        payload = checkpoint.interrupts[0].value
        assert payload["kind"] == "system_wake"
        assert payload["reason"] == "operator_stop"
        paused_key = AttemptKey(digest=payload["attempt_key"])
        paused = await kernel.journal.load(paused_key)
        assert paused is not None
        assert paused.invocation_id == "inv-operator-pause"
        assert paused.activity_state is None

        executor.paused = False
        command = _system_wake_command(checkpoint.interrupts)
        assert command is not None
        resumed = await graph.ainvoke(command, _CONFIG)
        assert resumed["first"] == resumed["second"] == "committed"
        assert [name for name, _key in executor.dispatched] == ["first", "second"]
        assert executor.dispatched[1][1] == paused_key
        assert (project / "out/second.txt").read_text() == "second"
        completed = await graph.aget_state(_CONFIG)
        assert completed.next == completed.interrupts == ()
    finally:
        store.close()


async def test_operator_stop_allows_pending_activity_to_reconcile_and_commit(tmp_path: Path) -> None:
    executor = _Executor(pending=True)
    graph, kernel, project, store = _graph(tmp_path, executor, ("first",))
    try:
        await graph.ainvoke({}, _CONFIG)
        checkpoint = await graph.aget_state(_CONFIG)
        assert checkpoint.interrupts[0].value["reason"] == "provider_running"
        assert executor.paused is True
        assert not (project / "out/first.txt").exists()
        key = executor.dispatched[0][1]
        pending = await kernel.journal.load(key)
        assert pending is not None and pending.activity_state == "prepared"

        command = _system_wake_command(checkpoint.interrupts)
        assert command is not None
        resumed = await graph.ainvoke(command, _CONFIG)
        assert resumed["first"] == "committed"
        assert executor.dispatched == [("first", key)]
        assert executor.reconciled == [key]
        assert (project / "out/first.txt").read_text() == "first"
        completed = await graph.aget_state(_CONFIG)
        assert completed.next == completed.interrupts == ()
    finally:
        store.close()
