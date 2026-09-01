from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from typing import Any

from langgraph.errors import GraphInterrupt
from pydantic import BaseModel

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.events import SystemInterruptIssued
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    AttemptResolution,
    CommittedEffectFailure,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
)
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.checkpoint_store import MemoryCheckpointStore
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)
from graph_engine.testing.recording_build_context import (
    RecordingCapabilityBuildContext,
    _TraceRecorder,
)

_REVISION = "a" * 64
_LOCK = "b" * 64
_INPUT = "c" * 64


@dataclass(frozen=True, slots=True)
class SemanticAttemptCall:
    semantic_node_id: str
    contract_id: str
    input_digest: str


@dataclass(frozen=True, slots=True)
class GraphHarnessResult:
    semantic_calls: tuple[SemanticAttemptCall, ...]
    select_values: tuple[object, ...] = ()
    validator_calls: tuple[str, ...] = ()
    promotion_decision: str | None = None
    published_update: Mapping[str, object] | None = None
    interrupt_envelope: object | None = None
    terminal: object | None = None

    def model_dump_json(self) -> str:
        payload = {
            "semantic_calls": [asdict(call) for call in self.semantic_calls],
            "select_values": list(self.select_values),
            "validator_calls": list(self.validator_calls),
            "promotion_decision": self.promotion_decision,
            "published_update": self.published_update,
            "interrupt_envelope": self.interrupt_envelope,
            "terminal": self.terminal,
        }
        return json.dumps(payload, default=str, sort_keys=True)


def committed(output: object, receipt: ReceiptRef) -> CommittedTaskResult[Any]:
    return CommittedTaskResult(output=output, receipt=receipt)


class ScriptedAttempt:
    def __init__(self) -> None:
        self._queues: dict[str, list[AttemptResolution]] = {}
        self.journal: MemoryAttemptJournal | None = None
        self.semantic_calls: list[SemanticAttemptCall] = []
        self.validator_calls: list[str] = []
        self.promotion_decisions: list[str] = []
        self.issued_events: list[SystemInterruptIssued] = []

    def load_script(self, script: Mapping[str, Sequence[AttemptResolution]]) -> None:
        self._queues = {str(node_id): list(resolutions) for node_id, resolutions in script.items()}
        self.semantic_calls.clear()
        self.validator_calls.clear()
        self.promotion_decisions.clear()
        self.issued_events.clear()

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: object,
        validated_input: object,
        context: object,
    ) -> AttemptResolution:
        del attempt_key
        semantic_node_id = str(getattr(context, "semantic_node_id"))
        task = _task_contract(contract)
        if isinstance(validated_input, BaseModel):
            digest_payload = validated_input.model_dump(mode="json")
        else:
            raise TypeError("scripted attempt input must be a Pydantic model")
        self.semantic_calls.append(
            SemanticAttemptCall(
                semantic_node_id=semantic_node_id,
                contract_id=task.contract_id,
                input_digest=canonical_digest(digest_payload),
            )
        )
        self.validator_calls.extend(task.validators)
        queue = self._queues.get(semantic_node_id)
        if not queue:
            raise AssertionError(f"no scripted resolution for {semantic_node_id!r}")
        resolution = queue.pop(0)
        self.promotion_decisions.append(_promotion_decision(resolution))
        return resolution

    async def record_system_interrupt_issued(
        self, attempt_key: AttemptKey, event: SystemInterruptIssued, context: AttemptExecutionContext
    ) -> object:
        self.issued_events.append(event)
        journal = self.journal
        if journal is None:
            raise TypeError("scripted attempt has no journal")
        snapshot = await journal.load(attempt_key)
        return await journal.append(
            attempt_key,
            (event,),
            expected_revision=0 if snapshot is None else snapshot.revision,
            fencing_token=context.fencing_token,
        )


class MemoryCheckpointAnchorJournal:
    def __init__(self) -> None:
        self._started: dict[str, InvocationStarted] = {}
        self._anchors: dict[tuple[str, str], CheckpointAnchor] = {}

    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None:
        await self.assert_current_fence(record.invocation_id, fencing_token)
        existing = self._started.get(record.invocation_id)
        if existing is None:
            self._started[record.invocation_id] = record
            return
        if existing != record:
            raise CheckpointIntegrityError("invocation identity drifted")

    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None:
        await self.assert_current_fence(anchor.invocation_id, fencing_token)
        started_record = self._started.get(anchor.invocation_id)
        if started_record is None:
            raise CheckpointIntegrityError("invocation has not started")
        if anchor.anchor_state() != started_record.anchor_state():
            raise CheckpointIntegrityError("checkpoint identity drifted from invocation")
        key = (anchor.thread_id, anchor.checkpoint_id)
        existing = self._anchors.get(key)
        if existing is None:
            self._anchors[key] = anchor
            return
        if existing != anchor:
            raise CheckpointIntegrityError("checkpoint identity drifted")

    async def read_checkpoint_anchor(self, thread_id: str, checkpoint_id: str) -> CheckpointAnchor | None:
        return self._anchors.get((thread_id, checkpoint_id))

    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None:
        started_record = self._started.get(invocation_id)
        if started_record is None:
            if fencing_token < 1:
                raise CheckpointIntegrityError("fencing token is stale")
            return
        if started_record.fencing_token != fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")


class GraphHarness:
    def __init__(self) -> None:
        self._kernel = ScriptedAttempt()
        self._journal = MemoryAttemptJournal()
        self._kernel.journal = self._journal
        self._factory = AttemptNodeFactory(journal=self._journal, kernel=self._kernel)
        self._recorder = _TraceRecorder()
        self._context: RecordingCapabilityBuildContext | None = None

    def recording_context(
        self,
        *,
        owner_id: str,
        contracts: Mapping[str, TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any]],
    ) -> RecordingCapabilityBuildContext:
        context = RecordingCapabilityBuildContext(
            owner_id=owner_id,
            contracts=contracts,
            attempt_factory=self._factory,
            recorder=self._recorder,
        )
        self._context = context
        return context

    def anchored_memory_checkpointer(
        self,
        *,
        invocation_id: str = "inv-1",
        fencing_token: int = 1,
    ) -> AnchoredCheckpointer:
        store = MemoryCheckpointStore()
        journal = MemoryCheckpointAnchorJournal()
        identity = CheckpointAnchorState(
            invocation_id=invocation_id,
            thread_id=invocation_id,
            graph_revision=_REVISION,
            product_lock_digest=_LOCK,
            root_input_digest=_INPUT,
            fencing_token=fencing_token,
        )
        return _AnchoredMemoryBackend(store=store, journal=journal, identity=identity)

    async def run(
        self,
        graph: object,
        *,
        input: Mapping[str, object],
        script: Mapping[str, Sequence[AttemptResolution]],
    ) -> GraphHarnessResult:
        self._kernel.load_script(script)
        self._recorder.select_values.clear()
        self._recorder.published_updates.clear()
        invoke = getattr(graph, "ainvoke")
        interrupt_envelope: object | None = None
        terminal: object | None = None
        try:
            terminal = await invoke(
                dict(input),
                config={
                    "configurable": {
                        "thread_id": "inv-1",
                        "assurance_revision_id": _REVISION,
                        "assurance_fencing_token": 1,
                        "assurance_entrypoint": "execute",
                    }
                },
            )
        except GraphInterrupt as error:
            interrupt_envelope = _interrupt_envelope(error)
        published = self._recorder.published_updates[-1] if self._recorder.published_updates else None
        promotion = self._kernel.promotion_decisions[-1] if self._kernel.promotion_decisions else None
        return GraphHarnessResult(
            semantic_calls=tuple(self._kernel.semantic_calls),
            select_values=tuple(self._recorder.select_values),
            validator_calls=tuple(self._kernel.validator_calls),
            promotion_decision=promotion,
            published_update=published,
            interrupt_envelope=interrupt_envelope,
            terminal=terminal,
        )


class _AnchoredMemoryBackend(AnchoredCheckpointer):
    def __init__(
        self,
        *,
        store: MemoryCheckpointStore,
        journal: MemoryCheckpointAnchorJournal,
        identity: CheckpointAnchorState,
    ) -> None:
        super().__init__(store=store, journal=journal, identity=identity)
        self.backend_id = "anchored-memory"
        self._memory_journal = journal
        self._identity = identity

    async def prepare(self) -> None:
        await self._memory_journal.start_invocation(
            InvocationStarted(
                invocation_id=self._identity.invocation_id,
                thread_id=self._identity.thread_id,
                graph_revision=self._identity.graph_revision,
                product_lock_digest=self._identity.product_lock_digest,
                root_input_digest=self._identity.root_input_digest,
                fencing_token=self._identity.fencing_token,
            ),
            fencing_token=self._identity.fencing_token,
        )


def _task_contract(contract: object) -> TaskAttemptContract[Any, Any]:
    if isinstance(contract, ResolvedAttemptContract):
        return contract.contract
    if isinstance(contract, TaskAttemptContract):
        return contract
    raise TypeError("scripted attempt requires a TaskAttemptContract")


def _promotion_decision(resolution: AttemptResolution) -> str:
    if isinstance(resolution, CommittedTaskResult):
        return "committed"
    if isinstance(resolution, RejectedTaskResult):
        return "rejected"
    if isinstance(resolution, PermanentTaskFailure):
        return "permanent"
    if isinstance(resolution, CommittedEffectFailure):
        return "committed_effect_failure"
    if isinstance(resolution, PendingTaskResult):
        return "pending"
    if isinstance(resolution, IndeterminateTaskResult):
        return "indeterminate"
    raise TypeError(f"unsupported attempt resolution: {type(resolution)!r}")


def _interrupt_envelope(error: GraphInterrupt) -> object:
    interrupts = getattr(error, "args", ())
    if not interrupts:
        return None
    first = interrupts[0]
    if isinstance(first, tuple) and first:
        value = getattr(first[0], "value", first[0])
        return value
    return getattr(first, "value", first)


__all__ = [
    "GraphHarness",
    "GraphHarnessResult",
    "MemoryCheckpointAnchorJournal",
    "ScriptedAttempt",
    "SemanticAttemptCall",
    "committed",
]
