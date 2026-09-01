from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt
from pydantic import BaseModel

from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.events import (
    AttemptOpened,
    SystemInterruptCompletionCheckpointed,
    SystemInterruptIssuanceAnchored,
    SystemInterruptIssued,
)
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    PendingTaskResult,
    ReceiptRef,
    SystemReference,
)
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorNotice
from graph_engine.persistence.checkpoint_store import MemoryCheckpointStore
from graph_engine.persistence.journal import CheckpointAnchor
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
    omit_checkpoint_bridge_fields,
)


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


REVISION = "a" * 64
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
OUTPUT = RunOutput(status="ok")
LOCK = "b" * 64
INPUT = "c" * 64

_HELPERS_PATH = Path(__file__).resolve().parents[1] / "persistence" / "test_checkpoint_store_contract.py"
_HELPERS_SPEC = importlib.util.spec_from_file_location("attempt10_checkpoint_helpers", _HELPERS_PATH)
assert _HELPERS_SPEC is not None and _HELPERS_SPEC.loader is not None
_helpers = importlib.util.module_from_spec(_HELPERS_SPEC)
_HELPERS_SPEC.loader.exec_module(_helpers)
MemoryCheckpointAnchorJournal = _helpers.MemoryCheckpointAnchorJournal
identity = _helpers.identity
started = _helpers.started


class ScriptedKernel:
    def __init__(self) -> None:
        self.resolutions: list[object] = []
        self.seen_key: AttemptKey | None = None
        self.calls = 0

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


class ScriptedJournal(MemoryAttemptJournal):
    def __init__(self, kernel: ScriptedKernel) -> None:
        super().__init__()
        self.kernel = kernel

    def mark_kernel_now_committed(self, attempt_key: AttemptKey) -> None:
        del attempt_key
        self.kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))


def _resolved():
    class _Executor:
        async def execute(self, validated_input: RunInput, context: object) -> RunOutput:
            del validated_input, context
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


def select_activation(_state: dict[str, object]) -> BusinessActivation:
    return BusinessActivation.one_shot()


def select_input(state: dict[str, object]) -> RunInput:
    assert CHECKPOINT_MARKERS_STATE_KEY not in state
    return RunInput(change_id=str(state["change_id"]))


def publish_output(_state: dict[str, object], output: RunOutput, receipt: ReceiptRef) -> dict[str, object]:
    return {"execution": output, "receipts": (receipt,)}


def _open_event() -> AttemptOpened:
    return AttemptOpened(
        contract_digest=canonical_digest({"c": 1}),
        input_digest=canonical_digest({"i": 1}),
        graph_revision=REVISION,
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
    )


def _issued(*, generation: int, ordinal: int, envelope_digest: str) -> SystemInterruptIssued:
    return SystemInterruptIssued(generation=generation, ordinal=ordinal, envelope_digest=envelope_digest)


def _marker(
    *,
    kind: str,
    attempt_key: str,
    generation: int,
    ordinal: int,
    envelope_digest: str,
) -> CheckpointBridgeMarker:
    return CheckpointBridgeMarker(
        kind=kind,  # type: ignore[arg-type]
        attempt_key=attempt_key,
        generation=generation,
        ordinal=ordinal,
        envelope_digest=envelope_digest,
    )


def _anchor(*, checkpoint_id: str = "cp-interrupt") -> CheckpointAnchor:
    return CheckpointAnchor.build(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=None,
        checkpoint_bytes=b"checkpoint",
        pending_write_bytes=(),
        task_identity="",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=4,
    )


async def _seed_issued(
    journal: MemoryAttemptJournal,
    *,
    key: AttemptKey,
    generation: int = 1,
    ordinal: int = 0,
    envelope_digest: str | None = None,
) -> str:
    digest = envelope_digest or canonical_digest({"generation": generation, "ordinal": ordinal})
    events: list[object] = [
        _open_event(),
        _issued(generation=generation, ordinal=ordinal, envelope_digest=digest),
    ]
    await journal.append(key, tuple(events), expected_revision=0, fencing_token=4)  # type: ignore[arg-type]
    return digest


class _ReplayThenRaise:
    def __init__(self, replay_count: int) -> None:
        self.replay_count = replay_count
        self.calls = 0

    def __call__(self, value: object) -> object:
        self.calls += 1
        if self.calls <= self.replay_count:
            return {"resumed": True}
        raise GraphInterrupt((Interrupt(value=value),))


async def test_issuance_observer_proves_exposure_and_leaves_generation_active() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    marker = _marker(
        kind="system_interrupt_issued",
        attempt_key=key.digest,
        generation=1,
        ordinal=0,
        envelope_digest=digest,
    )
    notice = CheckpointAnchorNotice(anchor=_anchor(), markers=(marker,), source="pending_write")
    await observer.on_anchored(notice)
    snapshot = await journal.load(key)
    assert snapshot is not None
    assert snapshot.active_interrupt is not None
    assert snapshot.active_interrupt.issuance_anchored is True
    assert snapshot.active_interrupt.retired is False
    assert snapshot.active_interrupt.generation == 1
    kinds = [type(event).__name__ for record in journal._logs[key.digest] for event in record.events]
    assert "SystemInterruptIssuanceAnchored" in kinds
    assert "SystemInterruptCompletionCheckpointed" not in kinds


async def test_issuance_observer_is_idempotent_on_duplicate_delivery() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    notice = CheckpointAnchorNotice(
        anchor=_anchor(),
        markers=(
            _marker(
                kind="system_interrupt_issued",
                attempt_key=key.digest,
                generation=1,
                ordinal=0,
                envelope_digest=digest,
            ),
        ),
        source="pending_write",
    )
    await observer.on_anchored(notice)
    first = await journal.load(key)
    await observer.on_anchored(notice)
    second = await journal.load(key)
    assert first is not None and second is not None
    assert first.active_interrupt == second.active_interrupt
    anchored = [
        event
        for record in journal._logs[key.digest]
        for event in record.events
        if isinstance(event, SystemInterruptIssuanceAnchored)
    ]
    assert len(anchored) == 1


async def test_completion_observer_retires_only_matching_generations() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest_one = canonical_digest({"generation": 1})
    digest_two = canonical_digest({"generation": 2})
    unrelated = canonical_digest({"generation": 9})
    await journal.append(
        key,
        (
            _open_event(),
            _issued(generation=1, ordinal=0, envelope_digest=digest_one),
            _issued(generation=2, ordinal=1, envelope_digest=digest_two),
            _issued(generation=9, ordinal=2, envelope_digest=unrelated),
        ),
        expected_revision=0,
        fencing_token=4,
    )
    observer = AttemptCheckpointObserver(journal)
    await observer.on_anchored(
        CheckpointAnchorNotice(
            anchor=_anchor(checkpoint_id="cp-resume"),
            markers=(
                _marker(
                    kind="system_interrupt_completed",
                    attempt_key=key.digest,
                    generation=1,
                    ordinal=0,
                    envelope_digest=digest_one,
                ),
                _marker(
                    kind="system_interrupt_completed",
                    attempt_key=key.digest,
                    generation=2,
                    ordinal=1,
                    envelope_digest=digest_two,
                ),
            ),
            source="checkpoint",
        )
    )
    snapshot = await journal.load(key)
    assert snapshot is not None
    assert 1 in snapshot.retired_generations
    assert 2 in snapshot.retired_generations
    assert 9 not in snapshot.retired_generations
    assert snapshot.active_interrupt is not None
    assert snapshot.active_interrupt.generation == 9


async def test_pending_write_completion_markers_never_retire() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    await observer.on_anchored(
        CheckpointAnchorNotice(
            anchor=_anchor(),
            markers=(
                _marker(
                    kind="system_interrupt_completed",
                    attempt_key=key.digest,
                    generation=1,
                    ordinal=0,
                    envelope_digest=digest,
                ),
            ),
            source="pending_write",
        )
    )
    snapshot = await journal.load(key)
    assert snapshot is not None
    assert snapshot.active_interrupt is not None
    assert snapshot.active_interrupt.retired is False
    assert snapshot.retired_generations == ()


async def test_mismatched_digest_or_generation_is_rejected() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    with pytest.raises(ValueError):
        await observer.on_anchored(
            CheckpointAnchorNotice(
                anchor=_anchor(),
                markers=(
                    _marker(
                        kind="system_interrupt_issued",
                        attempt_key=key.digest,
                        generation=1,
                        ordinal=0,
                        envelope_digest="d" * 64,
                    ),
                ),
                source="pending_write",
            )
        )
    with pytest.raises(ValueError):
        await observer.on_anchored(
            CheckpointAnchorNotice(
                anchor=_anchor(),
                markers=(
                    _marker(
                        kind="system_interrupt_issued",
                        attempt_key=key.digest,
                        generation=3,
                        ordinal=0,
                        envelope_digest=digest,
                    ),
                ),
                source="pending_write",
            )
        )


async def test_human_interrupt_without_attempt_marker_is_ignored() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    await observer.on_anchored(CheckpointAnchorNotice(anchor=_anchor(), markers=(), source="checkpoint"))
    snapshot = await journal.load(key)
    assert snapshot is not None
    assert snapshot.active_interrupt is not None
    assert snapshot.active_interrupt.issuance_anchored is False


async def test_crash_before_and_after_issuance_observer_delivery() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    notice = CheckpointAnchorNotice(
        anchor=_anchor(),
        markers=(
            _marker(
                kind="system_interrupt_issued",
                attempt_key=key.digest,
                generation=1,
                ordinal=0,
                envelope_digest=digest,
            ),
        ),
        source="pending_write",
    )
    before = await journal.load(key)
    assert before is not None
    assert before.active_interrupt is not None
    assert before.active_interrupt.issuance_anchored is False
    await observer.on_anchored(notice)
    after = await journal.load(key)
    assert after is not None
    assert after.active_interrupt is not None
    assert after.active_interrupt.issuance_anchored is True
    assert after.active_interrupt.retired is False


async def test_crash_before_and_after_completion_observer_delivery() -> None:
    journal = MemoryAttemptJournal()
    key = _attempt_key()
    digest = await _seed_issued(journal, key=key)
    observer = AttemptCheckpointObserver(journal)
    issuance = CheckpointAnchorNotice(
        anchor=_anchor(),
        markers=(
            _marker(
                kind="system_interrupt_issued",
                attempt_key=key.digest,
                generation=1,
                ordinal=0,
                envelope_digest=digest,
            ),
        ),
        source="pending_write",
    )
    await observer.on_anchored(issuance)
    completion = CheckpointAnchorNotice(
        anchor=_anchor(checkpoint_id="cp-resume"),
        markers=(
            _marker(
                kind="system_interrupt_completed",
                attempt_key=key.digest,
                generation=1,
                ordinal=0,
                envelope_digest=digest,
            ),
        ),
        source="checkpoint",
    )
    before = await journal.load(key)
    assert before is not None
    assert before.retired_generations == ()
    await observer.on_anchored(completion)
    after = await journal.load(key)
    assert after is not None
    assert after.retired_generations == (1,)
    await observer.on_anchored(completion)
    duplicate = await journal.load(key)
    assert duplicate is not None
    assert duplicate.retired_generations == (1,)
    completed = [
        event
        for record in journal._logs[key.digest]
        for event in record.events
        if isinstance(event, SystemInterruptCompletionCheckpointed)
    ]
    assert len(completed) == 1


async def test_resumed_node_writes_completion_markers_in_ordinal_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=[])
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    runtime = _runtime(kernel)
    state = {"change_id": "chg-1"}
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    with pytest.raises(GraphInterrupt):
        await node(state, runtime=runtime)
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-2")))
    monkeypatch.setattr("graph_engine.attempts.node_factory.interrupt", _ReplayThenRaise(1))
    with pytest.raises(GraphInterrupt):
        await node(state, runtime=runtime)
    journal.mark_kernel_now_committed(_attempt_key())
    monkeypatch.setattr("graph_engine.attempts.node_factory.interrupt", _ReplayThenRaise(2))
    update = await node(state, runtime=runtime)
    markers = update[CHECKPOINT_MARKERS_STATE_KEY]
    assert [marker["kind"] for marker in markers] == ["system_interrupt_completed"] * 2
    assert [marker["generation"] for marker in markers] == [1, 2]
    assert [marker["ordinal"] for marker in markers] == [0, 1]
    public = omit_checkpoint_bridge_fields(update)
    assert CHECKPOINT_MARKERS_STATE_KEY not in public
    assert public["execution"] == OUTPUT


async def test_later_reentry_after_completion_does_not_replay_stale_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    journal = ScriptedJournal(kernel)
    factory = AttemptNodeFactory(journal=journal, kernel=kernel, trace=[])
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    runtime = _runtime(kernel)
    state = {"change_id": "chg-1"}
    kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    with pytest.raises(GraphInterrupt):
        await node(state, runtime=runtime)
    snapshot = await journal.load(_attempt_key())
    assert snapshot is not None
    issued = snapshot.active_interrupt
    assert issued is not None
    observer = AttemptCheckpointObserver(journal)
    await observer.on_anchored(
        CheckpointAnchorNotice(
            anchor=_anchor(),
            markers=(
                _marker(
                    kind="system_interrupt_issued",
                    attempt_key=_attempt_key().digest,
                    generation=issued.generation,
                    ordinal=issued.ordinal,
                    envelope_digest=issued.envelope_digest,
                ),
            ),
            source="pending_write",
        )
    )
    journal.mark_kernel_now_committed(_attempt_key())
    monkeypatch.setattr("graph_engine.attempts.node_factory.interrupt", _ReplayThenRaise(1))
    update = await node(state, runtime=runtime)
    await observer.on_anchored(
        CheckpointAnchorNotice(
            anchor=_anchor(checkpoint_id="cp-resume"),
            markers=tuple(
                CheckpointBridgeMarker.model_validate(item) for item in update[CHECKPOINT_MARKERS_STATE_KEY]
            ),
            source="checkpoint",
        )
    )
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    factory.trace.clear()
    later = await node(state, runtime=runtime)
    assert factory.trace == [] or factory.trace[0] != "interrupt:generation=1:ordinal=0"
    assert CHECKPOINT_MARKERS_STATE_KEY not in later
    assert later == {"execution": OUTPUT, "receipts": (RECEIPT,)}


async def test_invocation_wide_pending_barrier_keeps_sibling_write() -> None:
    left_kernel = ScriptedKernel()
    right_kernel = ScriptedKernel()
    left_journal = ScriptedJournal(left_kernel)
    right_journal = ScriptedJournal(right_kernel)
    left = AttemptNodeFactory(journal=left_journal, kernel=left_kernel, trace=[]).attempt(
        _resolved(),
        semantic_node_id="execution.left",
        activation=select_activation,
        select=select_input,
        publish=lambda _state, output, receipt: {"left": output.status},
    )
    right = AttemptNodeFactory(journal=right_journal, kernel=right_kernel, trace=[]).attempt(
        _resolved(),
        semantic_node_id="execution.right",
        activation=select_activation,
        select=select_input,
        publish=lambda _state, output, receipt: {"right": output.status},
    )
    left_kernel.push(PendingTaskResult(wakeup=SystemReference(reference_id="wake-left")))
    right_kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))

    class BranchState(TypedDict):
        change_id: str
        left: str
        right: str

    store = MemoryCheckpointStore()
    checkpoint_journal = MemoryCheckpointAnchorJournal()
    await checkpoint_journal.start_invocation(started(), fencing_token=4)
    saver = AnchoredCheckpointer(
        store=store,
        journal=checkpoint_journal,
        observers=(),
        identity=identity(),
    )
    builder = StateGraph(BranchState)
    builder.add_node("left", left)
    builder.add_node("right", right)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    graph = builder.compile(checkpointer=saver)
    config = {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": 4,
        }
    }
    interrupted = await graph.ainvoke({"change_id": "chg-1", "left": "", "right": ""}, config)
    assert interrupted["__interrupt__"]
    assert left_kernel.calls == 1
    assert right_kernel.calls == 1
    pending_anchors = checkpoint_journal.pending_write_anchors()
    assert any(anchor.pending_write_bytes for anchor in pending_anchors)
    snapshot = await saver.aget_tuple(config)
    assert snapshot is not None
    assert snapshot.checkpoint["channel_values"].get("right") in {"", None}
    assert any(channel == "right" and value == "ok" for _, channel, value in (snapshot.pending_writes or []))
    left_journal.mark_kernel_now_committed(
        derive_attempt_key(
            invocation_id="inv-1",
            graph_revision=REVISION,
            public_entrypoint="execute",
            semantic_node_id="execution.left",
            business_activation=BusinessActivation.one_shot(),
            contract_id="assurance.execution.run.v1",
            validated_input=RunInput(change_id="chg-1"),
        )
    )
    resumed = await graph.ainvoke(Command(resume=True), config)
    assert resumed["right"] == "ok"
    assert right_kernel.calls == 1
    assert left_kernel.calls == 2
