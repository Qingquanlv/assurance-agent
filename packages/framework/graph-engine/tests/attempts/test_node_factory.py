from __future__ import annotations

from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.events import AttemptOpened, SystemInterruptIssued
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
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
    SystemReference,
)
from graph_engine.boot.boot import (
    BootValidationError,
    ContractOwnershipError,
    EngineCapabilityBuildContext,
    bind_attempt_factory,
)
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.plugin_api import ResourceClaims
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
    omit_checkpoint_bridge_fields,
    replace_checkpoint_marker_batch,
)
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt


def test_add_attempt_node_preserves_identity_and_hooks() -> None:
    from unittest.mock import Mock

    from langgraph.graph import END, START, StateGraph

    from graph_engine.boot.boot import CapabilityBuildContext
    from graph_engine.stategraph import add_attempt_node

    class StringState(TypedDict):
        value: str

    def execute(state: StringState) -> StringState:
        return {"value": state["value"] + "!"}

    context = Mock(spec=CapabilityBuildContext)
    context.attempt.return_value = execute
    activation, select, publish = object(), object(), object()
    builder = StateGraph(StringState)
    add_attempt_node(
        builder,
        context,
        "feature.repair",
        contract_id="feature.task.v1",
        activation=activation,
        select=select,
        publish=publish,
    )
    context.attempt.assert_called_once_with(
        "feature.task.v1",
        semantic_node_id="feature.repair",
        activation=activation,
        select=select,
        publish=publish,
    )
    builder.add_edge(START, "feature.repair")
    builder.add_edge("feature.repair", END)
    assert builder.compile().invoke({"value": "ok"}) == {"value": "ok!"}


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


REVISION = "a" * 64
RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
OUTPUT = RunOutput(status="ok")


class ScriptedKernel:
    def __init__(self, resolution: AttemptResolution | None = None) -> None:
        self.resolutions: list[AttemptResolution] = [] if resolution is None else [resolution]
        self.seen_key: AttemptKey | None = None
        self.seen_keys: list[AttemptKey] = []
        self.calls = 0
        self.seen_inputs: list[RunInput] = []

    def push(self, resolution: AttemptResolution) -> None:
        self.resolutions.append(resolution)

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        del contract, context
        self.seen_key = attempt_key
        self.seen_keys.append(attempt_key)
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


def _contract(
    *, max_attempts: int = 1, interval_seconds: float = 0
) -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=max_attempts, interval_seconds=interval_seconds),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _resolved(*, max_attempts: int = 1, interval_seconds: float = 0):
    class _Executor:
        async def execute(self, validated_input: RunInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    return resolve_contract(
        _contract(max_attempts=max_attempts, interval_seconds=interval_seconds), executor=_Executor()
    )


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


def _runtime(kernel: object) -> SimpleNamespace:
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


class _ReplayThenRaise:
    def __init__(self, replay_count: int) -> None:
        self.replay_count = replay_count
        self.calls = 0

    def __call__(self, value: object) -> object:
        self.calls += 1
        if self.calls <= self.replay_count:
            return {"resumed": True}
        raise GraphInterrupt((Interrupt(value=value),))


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


class RepairInput(BaseModel):
    change_id: str
    validation_attempt: int = 0
    validation_error: str | None = None

    @model_validator(mode="after")
    def require_business_repair_for_feedback(self) -> RepairInput:
        if self.validation_attempt == 0 and self.validation_error is not None:
            raise ValueError("validation feedback requires the business repair attempt")
        return self


@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        ("invalid_output", "proposed_key is not a matrix field"),
        ("transient", "unresolved MRC rows missing or rebound"),
    ],
)
async def test_technical_retry_only_replaces_feedback_for_invalid_output(
    failure_kind, expected_error: str
) -> None:
    failure = PermanentTaskFailure(
        kind=failure_kind,
        message="proposed_key is not a matrix field",
        retryable=True,
    )
    kernel = ScriptedKernel(failure)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    seen: list[BaseModel] = []

    async def execute_or_recover(
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        seen.append(validated_input)
        return await ScriptedKernel.execute_or_recover(
            kernel, attempt_key, contract, validated_input, context
        )

    kernel.execute_or_recover = execute_or_recover  # type: ignore[method-assign]

    class _Executor:
        async def execute(self, validated_input: RepairInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    contract = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.intake.case-design.v1",
            owner_id="assurance.intake",
            handler_id="assurance.intake.case-design",
            input_model=RepairInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=2, interval_seconds=0),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=_Executor(),
    )

    def select_repair(state: dict[str, object]) -> RepairInput:
        del state
        return RepairInput(
            change_id="chg-1",
            validation_attempt=1,
            validation_error="unresolved MRC rows missing or rebound",
        )

    factory = _factory(kernel)
    node = factory.attempt(
        contract,
        semantic_node_id="case-design",
        activation=select_activation,
        select=select_repair,
        publish=publish_output,
    )
    update = await node(_state(), runtime=_runtime(kernel))

    assert update == {"execution": OUTPUT, "receipts": (RECEIPT,)}
    assert isinstance(seen[0], RepairInput)
    assert seen[0].validation_error == "unresolved MRC rows missing or rebound"
    assert isinstance(seen[1], RepairInput)
    assert seen[1].validation_error == expected_error
    assert seen[1].validation_attempt == 1


class AttemptFreeInput(BaseModel):
    change_id: str
    validation_error: str | None = None


async def test_technical_retry_carries_validation_error_without_an_attempt_counter() -> None:
    failure = PermanentTaskFailure(
        kind="invalid_output",
        message="schema rejected the authored document",
        retryable=True,
    )
    kernel = ScriptedKernel(failure)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    seen: list[BaseModel] = []

    async def execute_or_recover(
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        seen.append(validated_input)
        return await ScriptedKernel.execute_or_recover(
            kernel, attempt_key, contract, validated_input, context
        )

    kernel.execute_or_recover = execute_or_recover  # type: ignore[method-assign]

    class _Executor:
        async def execute(self, validated_input: AttemptFreeInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    contract = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.quality.agent.inspect.v1",
            owner_id="assurance.quality",
            handler_id="assurance.quality.inspect.prepare",
            input_model=AttemptFreeInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=2, interval_seconds=0),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=_Executor(),
    )

    def select_attempt_free(state: dict[str, object]) -> AttemptFreeInput:
        del state
        return AttemptFreeInput(change_id="chg-1")

    factory = _factory(kernel)
    node = factory.attempt(
        contract,
        semantic_node_id="quality.inspect",
        activation=select_activation,
        select=select_attempt_free,
        publish=publish_output,
    )
    update = await node(_state(), runtime=_runtime(kernel))

    assert update == {"execution": OUTPUT, "receipts": (RECEIPT,)}
    assert isinstance(seen[1], AttemptFreeInput)
    assert seen[1].validation_error == "schema rejected the authored document"


@pytest.mark.parametrize(
    ("carry", "failures", "expected_seeds"),
    [
        (True, ("invalid_output", "transient"), (None, 0, 0)),
        (True, ("invalid_output", "invalid_output"), (None, 0, 1)),
        (True, ("transient", "invalid_output"), (None, None, 1)),
        (False, ("invalid_output", "invalid_output"), (None, None, None)),
    ],
)
async def test_retry_seeds_only_from_the_latest_invalid_output_when_opted_in(
    carry: bool, failures: tuple[str, ...], expected_seeds: tuple[int | None, ...]
) -> None:
    kernel = ScriptedKernel()
    for kind in failures:
        kernel.push(PermanentTaskFailure(kind=kind, message=f"{kind} rejected", retryable=True))  # type: ignore[arg-type]
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    seeds: list[AttemptKey | None] = []

    async def execute_or_recover(
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        seeds.append(context.seed_attempt_key)
        return await ScriptedKernel.execute_or_recover(
            kernel, attempt_key, contract, validated_input, context
        )

    kernel.execute_or_recover = execute_or_recover  # type: ignore[method-assign]

    class _Executor:
        async def execute(self, validated_input: AttemptFreeInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    contract = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.intake.agent.case-design.v1",
            owner_id="assurance.intake",
            handler_id="assurance.intake.case-design.prepare",
            input_model=AttemptFreeInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=3, interval_seconds=0, carry_invalid_output=carry),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=_Executor(),
    )

    def select_attempt_free(state: dict[str, object]) -> AttemptFreeInput:
        del state
        return AttemptFreeInput(change_id="chg-1")

    node = _factory(kernel).attempt(
        contract,
        semantic_node_id="intake.case-design",
        activation=select_activation,
        select=select_attempt_free,
        publish=publish_output,
    )
    await node(_state(), runtime=_runtime(kernel))

    assert len(set(kernel.seen_keys)) == 3
    assert seeds == [None if index is None else kernel.seen_keys[index] for index in expected_seeds]


def test_carry_invalid_output_is_omitted_from_the_default_retry_projection() -> None:
    default = AttemptRetryPolicy(max_attempts=2, interval_seconds=0)
    carrying = default.model_copy(update={"carry_invalid_output": True})

    assert default.model_dump(mode="json") == {"max_attempts": 2, "interval_seconds": 0}
    assert carrying.model_dump(mode="json")["carry_invalid_output"] is True
    assert canonical_digest(default.model_dump(mode="json")) != canonical_digest(
        carrying.model_dump(mode="json")
    )


@pytest.mark.parametrize("failure_kind", ["invalid_output", "transient"])
async def test_technical_retry_does_not_advance_business_repair_attempt(failure_kind) -> None:
    failure = PermanentTaskFailure(
        kind=failure_kind,
        message="unresolved MRC rows must remain skipped_by_scope",
        retryable=True,
    )
    kernel = ScriptedKernel(failure)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    seen: list[BaseModel] = []

    async def execute_or_recover(
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptResolution:
        seen.append(validated_input)
        return await ScriptedKernel.execute_or_recover(
            kernel, attempt_key, contract, validated_input, context
        )

    kernel.execute_or_recover = execute_or_recover  # type: ignore[method-assign]

    class _Executor:
        async def execute(self, validated_input: RepairInput, scope: object) -> RunOutput:
            del validated_input, scope
            return OUTPUT

    contract = resolve_contract(
        TaskAttemptContract(
            contract_id="assurance.intake.agent.case-design.v1",
            owner_id="assurance.intake",
            handler_id="assurance.intake.case-design.prepare",
            input_model=RepairInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=2, interval_seconds=0),
            timeout=AttemptTimeoutPolicy(seconds=60),
            validators=(),
        ),
        executor=_Executor(),
    )

    def select_first(state: dict[str, object]) -> RepairInput:
        del state
        return RepairInput(change_id="chg-1", validation_attempt=0)

    factory = _factory(kernel)
    node = factory.attempt(
        contract,
        semantic_node_id="intake.case-design",
        activation=select_activation,
        select=select_first,
        publish=publish_output,
    )
    update = await node(_state(), runtime=_runtime(kernel))

    assert update == {"execution": OUTPUT, "receipts": (RECEIPT,)}
    assert isinstance(seen[0], RepairInput)
    assert seen[0].validation_attempt == 0
    assert seen[0].validation_error is None
    assert isinstance(seen[1], RepairInput)
    assert seen[1].validation_attempt == 0
    assert seen[1].validation_error is None


async def test_retryable_failure_uses_contract_budget_and_isolated_attempt_keys() -> None:
    kernel = ScriptedKernel(
        PermanentTaskFailure(kind="transient", message="provider TLS failed", retryable=True)
    )
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(max_attempts=2),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )

    update = await node(_state(), runtime=_runtime(kernel))

    assert update == {"execution": OUTPUT, "receipts": (RECEIPT,)}
    assert kernel.calls == 2
    assert kernel.seen_keys[0] == _expected_key()
    assert kernel.seen_keys[1] == derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=REVISION,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id="assurance.execution.run.v1",
        validated_input=RunInput(change_id="chg-1"),
        technical_attempt=2,
    )
    assert kernel.seen_keys[1] != kernel.seen_keys[0]


@pytest.mark.parametrize("succeed_on_last_attempt", [False, True])
async def test_ten_attempts_wait_ten_seconds_only_between_retryable_failures(
    monkeypatch: pytest.MonkeyPatch, succeed_on_last_attempt: bool
) -> None:
    failure = PermanentTaskFailure(
        kind="transient",
        message="provider TLS failed",
        retryable=True,
    )
    kernel = ScriptedKernel(failure)
    for _ in range(8):
        kernel.push(failure)
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT) if succeed_on_last_attempt else failure)
    waits: list[tuple[int, float]] = []

    async def record_sleep(seconds: float) -> None:
        waits.append((kernel.calls, seconds))

    monkeypatch.setattr("asyncio.sleep", record_sleep)
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(max_attempts=10, interval_seconds=10),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )

    update = await node(_state(), runtime=_runtime(kernel))

    assert kernel.calls == 10
    assert len(set(kernel.seen_keys)) == 10
    assert waits == [(attempt, 10) for attempt in range(1, 10)]
    assert update == (
        {"execution": OUTPUT, "receipts": (RECEIPT,)}
        if succeed_on_last_attempt
        else {
            "attempt_failure": {
                "resolution_kind": "permanent",
                "kind": "transient",
                "message": "provider TLS failed",
                "writes_promoted": False,
            }
        }
    )


async def test_nonretryable_failure_ignores_unused_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"nonretryable failure must not wait: {seconds}")

    monkeypatch.setattr("asyncio.sleep", unexpected_sleep)
    kernel = ScriptedKernel(PermanentTaskFailure(kind="invalid_input", message="bad input"))
    factory = _factory(kernel)
    node = factory.attempt(
        _resolved(max_attempts=10, interval_seconds=10),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )

    update = await node(_state(), runtime=_runtime(kernel))

    assert kernel.calls == 1
    assert update["attempt_failure"]["kind"] == "invalid_input"


def test_owner_context_rejects_foreign_contract_and_node_site_authority() -> None:
    from langgraph.graph import StateGraph

    from graph_engine.stategraph import add_attempt_node

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
    with pytest.raises(ContractOwnershipError):
        add_attempt_node(
            StateGraph(dict),
            intake_context,
            "intake.foreign",
            contract_id="assurance.generation.agent.api.plan.v1",
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


async def test_pending_and_indeterminate_emit_system_interrupts_without_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"unresolved attempt must not retry: {seconds}")

    monkeypatch.setattr("asyncio.sleep", unexpected_sleep)
    pending = PendingTaskResult(wakeup=SystemReference(reference_id="wake-1"))
    indeterminate = IndeterminateTaskResult(reconciliation=SystemReference(reference_id="recon-1"))
    for resolution, kind in ((pending, "system_wake"), (indeterminate, "system_block")):
        kernel = ScriptedKernel(resolution)
        factory = _factory(kernel)
        node = factory.attempt(
            _resolved(max_attempts=10, interval_seconds=10),
            semantic_node_id="execution.run",
            activation=select_activation,
            select=select_input,
            publish=publish_output,
        )
        with pytest.raises(GraphInterrupt) as exc_info:
            await node(_state(), runtime=_runtime(kernel))
        assert kernel.calls == 1
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


async def test_factory_persists_pending_interrupt_with_one_method_kernel() -> None:
    kernel = ScriptedKernel(PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")))
    journal = ScriptedJournal(kernel)
    factory = AttemptNodeFactory(journal=journal, kernel=kernel)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    with pytest.raises(GraphInterrupt) as exc_info:
        await node(_state(), runtime=_runtime(kernel))
    key = _expected_key()
    envelope_digest = canonical_digest(
        {
            "attempt_key": key.digest,
            "generation": 1,
            "interrupt_kind": "system_wake",
            "ordinal": 0,
            "reference_id": "wake-1",
        }
    )
    records = journal._logs[key.digest]
    assert len(records) == 1
    assert records[0].events == (
        SystemInterruptIssued(generation=1, ordinal=0, envelope_digest=envelope_digest),
    )
    assert exc_info.value.args[0][0].value == {
        "kind": "system_wake",
        "pending_generation": 1,
        "ordinal": 0,
        "attempt_key": key.digest,
        "envelope_digest": envelope_digest,
        CHECKPOINT_MARKERS_STATE_KEY: [
            {
                "kind": "system_interrupt_issued",
                "attempt_key": key.digest,
                "generation": 1,
                "ordinal": 0,
                "envelope_digest": envelope_digest,
            }
        ],
        "reason": "wake-1",
        "wakeup": {"reference_id": "wake-1"},
    }


async def test_factory_uses_snapshot_revision_produced_during_kernel_execution() -> None:
    journal = MemoryAttemptJournal()

    class OpeningPendingKernel:
        async def execute_or_recover(
            self,
            attempt_key: AttemptKey,
            contract: ResolvedAttemptContract[Any, Any],
            validated_input: BaseModel,
            context: AttemptExecutionContext,
        ) -> AttemptResolution:
            del contract, validated_input
            await journal.append(
                attempt_key,
                (
                    AttemptOpened(
                        contract_digest=canonical_digest({"contract": "opened"}),
                        input_digest=canonical_digest({"input": "opened"}),
                        graph_revision=REVISION,
                        invocation_id=context.invocation_id,
                        public_entrypoint=context.public_entrypoint,
                        semantic_node_id=context.semantic_node_id,
                    ),
                ),
                expected_revision=0,
                fencing_token=context.fencing_token,
            )
            return PendingTaskResult(wakeup=SystemReference(reference_id="wake-1"))

    kernel = OpeningPendingKernel()
    factory = AttemptNodeFactory(journal=journal, kernel=kernel)
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    with pytest.raises(GraphInterrupt):
        await node(_state(), runtime=_runtime(kernel))
    snapshot = await journal.load(_expected_key())
    assert snapshot is not None
    assert snapshot.revision == 2
    assert len(snapshot.active_interrupts) == 1


async def test_completion_batch_goes_through_replace_checkpoint_marker_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ScriptedKernel()
    factory = _factory(kernel)
    key = _expected_key()
    digest = canonical_digest({"generation": 1})
    await factory._journal.append(
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
            SystemInterruptIssued(generation=1, ordinal=0, envelope_digest=digest),
        ),
        expected_revision=0,
        fencing_token=4,
    )
    kernel.push(CommittedTaskResult(output=OUTPUT, receipt=RECEIPT))
    monkeypatch.setattr("graph_engine.attempts.node_factory.interrupt", _ReplayThenRaise(1))
    node = factory.attempt(
        _resolved(),
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    update = await node(_state(), runtime=_runtime(kernel))
    markers = update[CHECKPOINT_MARKERS_STATE_KEY]
    expected = replace_checkpoint_marker_batch(
        None,
        [
            CheckpointBridgeMarker(
                kind="system_interrupt_completed",
                attempt_key=key.digest,
                generation=1,
                ordinal=0,
                envelope_digest=digest,
            )
        ],
    )
    assert markers == expected
    assert all(isinstance(marker, CheckpointBridgeMarker) for marker in markers)


def test_boot_binds_factory_to_kernel_journal() -> None:
    journal = MemoryAttemptJournal()
    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=object(),
        workspace=object(),
        graph_revision=REVISION,
    )
    factory = bind_attempt_factory(kernel)
    assert factory is not None
    assert factory._journal is journal


def test_port_only_kernel_fails_closed_without_ephemeral_journal() -> None:
    with pytest.raises(BootValidationError, match="journal"):
        bind_attempt_factory(object())
    assert bind_attempt_factory(None) is None
