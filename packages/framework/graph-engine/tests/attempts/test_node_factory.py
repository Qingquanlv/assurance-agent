from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    CommittedEffectFailure,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
    SystemReference,
)
from graph_engine.boot.boot import ContractOwnershipError, EngineCapabilityBuildContext
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    omit_checkpoint_bridge_fields,
)
from langgraph.errors import GraphInterrupt


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


REVISION = "a" * 64
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
OUTPUT = RunOutput(status="ok")


class ScriptedKernel:
    def __init__(self, resolution: object | None = None) -> None:
        self.resolutions: list[object] = [] if resolution is None else [resolution]
        self.seen_key: AttemptKey | None = None
        self.calls = 0
        self.seen_inputs: list[RunInput] = []

    def push(self, resolution: object) -> None:
        self.resolutions.append(resolution)

    async def execute_or_recover(
        self, attempt_key: AttemptKey, contract: object, validated_input: object, context: object
    ) -> object:
        del contract, context
        self.seen_key = attempt_key
        self.calls += 1
        if isinstance(validated_input, RunInput):
            self.seen_inputs.append(validated_input)
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


def _contract() -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _resolved():
    class _Executor:
        async def execute(self, validated_input: RunInput, context: object) -> RunOutput:
            del validated_input, context
            return OUTPUT

    return resolve_contract(_contract(), executor=_Executor())


def _state(
    *, change_id: str = "chg-1", round: int | None = None, arrival_id: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {"change_id": change_id, CHECKPOINT_MARKERS_STATE_KEY: [{"stale": True}]}
    if round is not None:
        payload["round"] = round
    if arrival_id is not None:
        payload["arrival_id"] = arrival_id
    return payload


def select_activation(state: dict[str, object]) -> BusinessActivation:
    if "arrival_id" in state:
        return BusinessActivation.for_trigger(str(state["arrival_id"]))
    if "round" in state:
        return BusinessActivation.for_round(int(state["round"]))
    return BusinessActivation.one_shot()


def select_input(state: dict[str, object]) -> RunInput:
    assert CHECKPOINT_MARKERS_STATE_KEY not in state
    return RunInput(change_id=str(state["change_id"]))


def publish_output(state: dict[str, object], output: RunOutput, receipt: ReceiptRef) -> dict[str, object]:
    del state
    return {"execution": output, "receipts": (receipt,)}


def _runtime(kernel: ScriptedKernel) -> SimpleNamespace:
    return SimpleNamespace(
        attempt_kernel=kernel,
        revision_id=REVISION,
        fencing_token=4,
        invocation_id="inv-1",
        public_entrypoint="execute",
    )


def _expected_key(*, change_id: str = "chg-1", activation: BusinessActivation | None = None) -> AttemptKey:
    return derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=REVISION,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot() if activation is None else activation,
        contract_id="assurance.execution.run.v1",
        validated_input=RunInput(change_id=change_id),
    )


def _factory(kernel: ScriptedKernel, *, trace: list[str] | None = None) -> AttemptNodeFactory:
    journal = ScriptedJournal(kernel=kernel)
    return AttemptNodeFactory(journal=journal, kernel=kernel, trace=trace)


async def test_committed_resolution_publishes_typed_output_and_receipt() -> None:
    kernel = ScriptedKernel(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    factory = _factory(kernel)
    contract = _resolved()
    state = _state()
    runtime = _runtime(kernel)
    expected_semantic_key = _expected_key()
    output = OUTPUT
    receipt_ref = RECEIPT
    node = factory.attempt(
        contract,
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    update = await node(state, runtime=runtime)
    assert kernel.seen_key == expected_semantic_key
    assert update == {"execution": output, "receipts": (receipt_ref,)}


def test_owner_context_rejects_foreign_contract_and_node_site_authority() -> None:
    kernel = ScriptedKernel()
    factory = _factory(kernel)
    own = TaskAttemptContract(
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
    intake_context = EngineCapabilityBuildContext(
        owner_id="assurance.intake",
        contracts={own.contract_id: own},
        approved_source_roots=(),
        attempt_factory=factory,
    )
    activation = select_activation
    select = select_input
    publish = publish_output
    own_id = own.contract_id
    with pytest.raises(ContractOwnershipError):
        intake_context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign",
            activation=activation,
            select=select,
            publish=publish,
        )
    with pytest.raises(TypeError):
        intake_context.attempt(
            own_id,
            semantic_node_id="intake.own",
            activation=activation,
            handler_id="injected",
            select=select,
            publish=publish,
        )


def test_factory_has_no_human_interrupt_surface() -> None:
    factory = _factory(ScriptedKernel())
    assert not hasattr(factory, "human")
    assert not hasattr(factory, "human_interrupt")
    assert "human" not in AttemptNodeFactory.__dict__


async def test_rejected_permanent_and_effect_failure_are_typed_and_never_terminal() -> None:
    cases = (
        RejectedTaskResult(reason="validator rejected"),
        PermanentTaskFailure(kind="internal", message="handler crashed"),
        CommittedEffectFailure(
            writes_promoted=True, promotion_receipt=RECEIPT, reason="effect permanently failed"
        ),
    )
    for resolution in cases:
        kernel = ScriptedKernel(resolution)
        factory = _factory(kernel)
        node = factory.attempt(
            _resolved(),
            semantic_node_id="execution.run",
            activation=select_activation,
            select=select_input,
            publish=publish_output,
        )
        update = await node(_state(), runtime=_runtime(kernel))
        assert "terminal" not in update
        assert "status" not in update
        failure = update["attempt_failure"]
        assert failure["writes_promoted"] is getattr(resolution, "writes_promoted", False)
        if isinstance(resolution, RejectedTaskResult):
            assert failure["resolution_kind"] == "rejected"
            assert failure["reason"] == "validator rejected"
        elif isinstance(resolution, PermanentTaskFailure):
            assert failure["resolution_kind"] == "permanent"
            assert failure["kind"] == "internal"
            assert failure["message"] == "handler crashed"
        else:
            assert failure["resolution_kind"] == "committed_effect_failure"
            assert failure["writes_promoted"] is True
            assert failure["promotion_receipt"] == RECEIPT


async def test_pending_and_indeterminate_emit_system_interrupts_without_terminal() -> None:
    pending = PendingTaskResult(wakeup=SystemReference(reference_id="wake-1"))
    indeterminate = IndeterminateTaskResult(reconciliation=SystemReference(reference_id="recon-1"))
    for resolution, kind in ((pending, "system_wake"), (indeterminate, "system_block")):
        kernel = ScriptedKernel(resolution)
        factory = _factory(kernel)
        node = factory.attempt(
            _resolved(),
            semantic_node_id="execution.run",
            activation=select_activation,
            select=select_input,
            publish=publish_output,
        )
        with pytest.raises(GraphInterrupt) as exc_info:
            await node(_state(), runtime=_runtime(kernel))
        interrupt = exc_info.value.args[0][0]
        payload = interrupt.value
        assert payload["kind"] == kind
        assert payload["pending_generation"] == 1
        assert payload["ordinal"] == 0
        assert "terminal" not in payload
        assert "status" not in payload
        markers = payload[CHECKPOINT_MARKERS_STATE_KEY]
        assert len(markers) == 1
        assert markers[0]["kind"] == "system_interrupt_issued"
        assert markers[0]["generation"] == 1
        assert markers[0]["ordinal"] == 0


async def test_one_shot_replay_reuses_key_and_new_round_changes_it() -> None:
    kernel = ScriptedKernel()
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    runtime = _runtime(kernel)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(), runtime=runtime)
    first = kernel.seen_key
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(), runtime=runtime)
    assert kernel.seen_key == first == _expected_key()

    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(round=2), runtime=runtime)
    round_two = kernel.seen_key
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(round=3), runtime=runtime)
    assert round_two != kernel.seen_key
    assert round_two == _expected_key(activation=BusinessActivation.for_round(2))


async def test_distinct_trigger_arrivals_change_key_and_replay_reuses_it() -> None:
    kernel = ScriptedKernel()
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    runtime = _runtime(kernel)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(arrival_id="join.arrival-1"), runtime=runtime)
    first = kernel.seen_key
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(arrival_id="join.arrival-2"), runtime=runtime)
    assert kernel.seen_key != first
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    await node(_state(arrival_id="join.arrival-1"), runtime=runtime)
    assert kernel.seen_key == first


async def test_empty_or_noncanonical_activation_is_rejected_before_kernel() -> None:
    kernel = ScriptedKernel(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    factory = _factory(kernel)

    def empty(_state: dict[str, object]) -> object:
        return ""

    def noncanonical(_state: dict[str, object]) -> object:
        return BusinessActivation.model_construct(kind="trigger", value="Not Canonical")

    for selector in (empty, noncanonical):
        node = factory.attempt(
            _resolved(),
            semantic_node_id="execution.run",
            activation=selector,
            select=select_input,
            publish=publish_output,
        )
        with pytest.raises((ValueError, ValidationError, TypeError)):
            await node(_state(), runtime=_runtime(kernel))
        assert kernel.calls == 0


async def test_fresh_success_omits_reserved_channel_from_public_output() -> None:
    kernel = ScriptedKernel(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    update = await node(_state(), runtime=_runtime(kernel))
    assert CHECKPOINT_MARKERS_STATE_KEY not in update
    assert omit_checkpoint_bridge_fields(update) == update


def test_observer_is_the_checkpoint_anchor_port() -> None:
    observer = AttemptCheckpointObserver(MemoryAttemptJournal())
    assert hasattr(observer, "on_anchored")
