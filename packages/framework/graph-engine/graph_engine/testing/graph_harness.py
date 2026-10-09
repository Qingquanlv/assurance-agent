from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import importlib.util
import json
from pathlib import Path
from typing import Any, Protocol, cast

from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph
from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef, coerce_artifact_ref
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.checkpoint import AttemptCheckpoint, AttemptPhase
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    AttemptResolution,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
)
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.checkpoint_store import MemoryCheckpointStore
from graph_engine.persistence.journal import CheckpointAnchorState, InvocationStarted
from graph_engine.testing.recording_build_context import (
    RecordingCapabilityBuildContext,
    _TraceRecorder,
)


class _FoundationCheckpointHelpers(Protocol):
    MemoryCheckpointAnchorJournal: type[Any]


_FOUNDATION_HELPERS: _FoundationCheckpointHelpers | None = None


def _foundation_checkpoint_helpers() -> _FoundationCheckpointHelpers:
    global _FOUNDATION_HELPERS
    if _FOUNDATION_HELPERS is None:
        path = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "persistence"
            / "test_checkpoint_store_contract.py"
        )
        spec = importlib.util.spec_from_file_location("graph_engine_checkpoint_store_helpers", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Foundation checkpoint test helpers are missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FOUNDATION_HELPERS = cast(_FoundationCheckpointHelpers, module)
    return _FOUNDATION_HELPERS


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


def committed(
    output: object,
    receipt: ReceiptRef,
    *,
    artifacts: Sequence[ArtifactRef | Mapping[str, object]] | None = None,
) -> CommittedTaskResult[Any]:
    """Script a commit. Artifact refs default to ``output["artifacts"]`` when present.

    Production commits take those refs from the kernel seal. This helper only
    stands in for that seal inside graph tests.
    """
    return CommittedTaskResult(
        output=output,
        receipt=receipt,
        committed_artifacts=(
            _scripted_artifacts(output)
            if artifacts is None
            else tuple(coerce_artifact_ref(item) for item in artifacts)
        ),
    )


def _scripted_artifacts(output: object) -> tuple[ArtifactRef, ...]:
    payload: object = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("artifacts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    refs: list[ArtifactRef] = []
    for item in raw:
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("digest"), str)
        ):
            refs.append(ArtifactRef(path=item["path"], digest=item["digest"]))
    return tuple(refs)


class ScriptedAttempt:
    def __init__(self, checkpoints: MemoryAttemptCheckpointStore | None = None) -> None:
        self.checkpoints = checkpoints or MemoryAttemptCheckpointStore()
        self._queues: dict[str, list[AttemptResolution]] = {}
        self.semantic_calls: list[SemanticAttemptCall] = []
        self.validator_calls: list[str] = []
        self.promotion_decisions: list[str] = []

    def load_script(self, script: Mapping[str, Sequence[AttemptResolution]]) -> None:
        self._queues = {str(node_id): list(resolutions) for node_id, resolutions in script.items()}
        self.semantic_calls.clear()
        self.validator_calls.clear()
        self.promotion_decisions.clear()

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: object,
        validated_input: object,
        context: object,
    ) -> AttemptResolution:
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
        if (
            isinstance(resolution, (PendingTaskResult, IndeterminateTaskResult))
            and await self.checkpoints.load(attempt_key) is None
        ):
            await self.checkpoints.commit(
                AttemptCheckpoint(
                    attempt_key=attempt_key,
                    revision=0,
                    fencing_token=getattr(context, "fencing_token"),
                    phase=AttemptPhase.AUTHORIZE,
                    contract_digest=canonical_digest(task.canonical_projection()),
                    input_digest=canonical_digest(digest_payload),
                    graph_revision="0" * 64,
                    invocation_id=getattr(context, "invocation_id"),
                    public_entrypoint=getattr(context, "public_entrypoint"),
                    semantic_node_id=semantic_node_id,
                ),
                expected_revision=0,
                fencing_token=getattr(context, "fencing_token"),
            )
        self.promotion_decisions.append(_promotion_decision(resolution))
        return resolution


class GraphHarness:
    def __init__(self) -> None:
        self._checkpoints = MemoryAttemptCheckpointStore()
        self._kernel = ScriptedAttempt(self._checkpoints)
        self._factory = AttemptNodeFactory(checkpoints=self._checkpoints, kernel=self._kernel)
        self._recorder = _TraceRecorder()
        self._context: RecordingCapabilityBuildContext | None = None
        self._prepared_checkpointer: AnchoredCheckpointer | None = None

    @property
    def prepared_checkpointer(self) -> AnchoredCheckpointer | None:
        return self._prepared_checkpointer

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
        helpers = _foundation_checkpoint_helpers()
        identity = CheckpointAnchorState(
            invocation_id=invocation_id,
            thread_id=invocation_id,
            graph_revision=_REVISION,
            product_lock_digest=_LOCK,
            root_input_digest=_INPUT,
            fencing_token=fencing_token,
        )
        return AnchoredCheckpointer(
            store=MemoryCheckpointStore(),
            journal=helpers.MemoryCheckpointAnchorJournal(),
            identity=identity,
        )

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
        backend = self.anchored_memory_checkpointer()
        await _prepare_anchored_backend(backend)
        self._prepared_checkpointer = backend
        compiled = graph.compile(checkpointer=backend) if isinstance(graph, StateGraph) else graph
        invoke = getattr(compiled, "ainvoke")
        interrupt_envelope: object | None = None
        terminal: object | None = None
        config = {
            "configurable": {
                "thread_id": "inv-1",
                "assurance_revision_id": _REVISION,
                "assurance_product_lock_digest": _LOCK,
                "assurance_root_input_digest": _INPUT,
                "assurance_fencing_token": 1,
                "assurance_entrypoint": "execute",
            }
        }
        try:
            terminal = await invoke(dict(input), config=config)
        except GraphInterrupt as error:
            interrupt_envelope = _interrupt_envelope(error)
            terminal = None
        else:
            interrupt_envelope = _interrupt_from_result(terminal)
            if interrupt_envelope is not None:
                terminal = None
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
    if isinstance(resolution, PendingTaskResult):
        return "pending"
    if isinstance(resolution, IndeterminateTaskResult):
        return "indeterminate"
    raise TypeError(f"unsupported attempt resolution: {type(resolution)!r}")


async def _prepare_anchored_backend(backend: AnchoredCheckpointer) -> None:
    identity = backend._identity
    await backend._journal.start_invocation(
        InvocationStarted(
            invocation_id=identity.invocation_id,
            thread_id=identity.thread_id,
            graph_revision=identity.graph_revision,
            product_lock_digest=identity.product_lock_digest,
            root_input_digest=identity.root_input_digest,
            fencing_token=identity.fencing_token,
        ),
        fencing_token=identity.fencing_token,
    )


def _interrupt_envelope(error: GraphInterrupt) -> object:
    interrupts = getattr(error, "args", ())
    if not interrupts:
        return None
    first = interrupts[0]
    if isinstance(first, tuple) and first:
        value = getattr(first[0], "value", first[0])
        return value
    return getattr(first, "value", first)


def _interrupt_from_result(result: object) -> object | None:
    if not isinstance(result, Mapping):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


__all__ = [
    "GraphHarness",
    "GraphHarnessResult",
    "ScriptedAttempt",
    "SemanticAttemptCall",
    "committed",
]
