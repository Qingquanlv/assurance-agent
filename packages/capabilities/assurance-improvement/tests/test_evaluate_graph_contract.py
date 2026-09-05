from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from pydantic import ValidationError

from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    close_improvement_task,
    select_evaluate_memory,
)
from assurance_improvement.contracts.delivery import MemoryEvalReceipt, artifact_digest
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
from graph_engine.effects.state import EffectCallContext, MemoryEffectState
from assurance_improvement.operations.delivery import EvaluateMemoryImprovementHandler, EvaluateMemoryInput
from assurance_improvement.operations.keys import delivery_effect_key
from assurance_improvement.plugin import ImprovementPlugin
from assurance_improvement.resource_loader import resource_bytes
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract, resolve_contract
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    ResourceClaims,
    TaskFailure,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from tests.product.test_change_local_output_routing import execute_task

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    IMPROVEMENT_ID,
    as_object,
    improvement_projection,
    json_value,
)

_LIFECYCLE_ONLY: dict[str, object] = {
    "change_id": "CH-EVAL-001",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": ["qa/changes"],
    "evidence_refs": [{"path": "qa/changes/CH-EVAL-001/report/report.md", "digest": "a" * 64}],
    "lifecycle_state": "approved",
}
_EVALUATE_ID = "assurance.improvement.evaluate-memory-improvement"
_DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"


def complete_evaluate_payload() -> dict[str, object]:
    return {
        "projection": improvement_projection(),
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "target_digest": HEX_A,
    }


def test_lifecycle_only_public_payload_is_rejected_by_evaluate_selector() -> None:
    with pytest.raises((ValidationError, ValueError)):
        select_evaluate_memory(_LIFECYCLE_ONLY)


@pytest.mark.asyncio
async def test_lifecycle_only_public_payload_fails_production_evaluate() -> None:
    outcome = await execute_task(EvaluateMemoryImprovementHandler(), json_value(_LIFECYCLE_ONLY))
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_complete_typed_selector_returns_receipt_and_delivery_intent() -> None:
    selected = select_evaluate_memory(complete_evaluate_payload())
    assert isinstance(selected, EvaluateMemoryInput)
    assert selected.eval_run_id == "eval-1"
    assert selected.outcome == "passed"
    assert selected.target_digest == HEX_A
    outcome = await execute_task(
        EvaluateMemoryImprovementHandler(),
        json_value(selected.model_dump(mode="json")),
    )
    assert outcome.status == "succeeded"
    receipt = MemoryEvalReceipt.model_validate(outcome.output)
    projection = ImprovementProjection.model_validate(improvement_projection())
    assert receipt.approved_state_digest == artifact_digest(projection)
    assert receipt.approved_version == projection.version
    assert len(outcome.effects) == 1
    intent = outcome.effects[0]
    assert intent.kind == _DELIVERY_KIND
    payload = as_object(intent.payload)
    assert payload["kind"] == "memory_eval"
    assert payload["memory_eval"]["eval_run_id"] == "eval-1"


def test_memory_eval_is_payload_discriminator_not_a_seventh_effect_kind() -> None:
    assert _DELIVERY_KIND in EXPECTED_EFFECT_KINDS
    assert "memory_eval" not in EXPECTED_EFFECT_KINDS
    assert "assurance.improvement.effect.memory_eval.v1" not in EXPECTED_EFFECT_KINDS
    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert {item.kind for item in contribution.effects} == {
        "assurance.improvement.effect.archive.v1",
        "assurance.improvement.effect.delivery.v1",
        "assurance.improvement.effect.promotion.v1",
    }
    assert len(EXPECTED_EFFECT_KINDS) == 6
    contract = TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID]
    assert contract.validators == ()
    assert contract.output_model is MemoryEvalReceipt


def test_offline_benchmark_eval_comparator_is_not_this_handler() -> None:
    contract = TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID]
    assert contract.handler_id == _EVALUATE_ID
    assert contract.input_model is EvaluateMemoryInput
    assert isinstance(EvaluateMemoryImprovementHandler(), EvaluateMemoryImprovementHandler)
    assert "assurance.improvement.graph.benchmark-eval" not in TASK_ATTEMPT_CONTRACTS
    assert "assurance.improvement.evaluate-benchmark" not in TASK_ATTEMPT_CONTRACTS


def test_eval_output_contract_rejects_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID].output_model.model_validate(
            {
                "eval_run_id": "eval-1",
                "outcome": "passed",
                "report_sha256": "r",
                "staged_sha256": "s",
                "unexpected": "junk",
            },
        )


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.promotions = 0

    async def open_or_create(self, attempt_key: object, claims: ResourceClaims) -> object:
        return await self.inner.open_or_create(attempt_key, claims)  # type: ignore[arg-type]

    async def seal(self, binding: object) -> object:
        return await self.inner.seal(binding)  # type: ignore[arg-type]

    async def prepare(self, binding: object, sealed: object) -> object:
        return await self.inner.prepare(binding, sealed)  # type: ignore[arg-type]

    async def promote(self, prepared: object) -> object:
        self.promotions += 1
        return await self.inner.promote(prepared)  # type: ignore[arg-type]

    async def recover_promotion(self, prepared: object) -> object:
        self.promotions += 1
        return await self.inner.recover_promotion(prepared)  # type: ignore[arg-type]


class _ObservedDeliveryEffect:
    def __init__(self, inner: ImprovementDeliveryEffect) -> None:
        self.inner = inner
        self.apply_calls = 0
        self.reconcile_calls = 0

    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        self.apply_calls += 1
        return await self.inner.apply(intent, context)

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        self.reconcile_calls += 1
        return await self.inner.reconcile(intent, context)


class CountingEffectState(MemoryEffectState):
    def __init__(self) -> None:
        super().__init__()
        self.delivery_count = 0

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ):
        result = await super().commit(
            effect_kind=effect_kind,
            settlement_key=settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            payload=payload,
            receipt=receipt,
            fencing_token=fencing_token,
        )
        self.delivery_count += 1
        return result


class _PendingDeliveryEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        del intent, context
        return EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message="effect is pending", retryable=True),
        )

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        del intent, context
        return EffectReconcileResult(status="pending")


class _PublicationIndeterminateEffect:
    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        del intent, context
        raise RuntimeError("publication pending")

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        del intent, context
        raise RuntimeError("publication pending")


def _revision() -> str:
    return canonical_digest({"revision": "improvement-evaluate-contract"})


def _kernel_effects_module() -> Any:
    import importlib.util

    path = (
        Path(__file__).resolve().parents[3]
        / "framework"
        / "graph-engine"
        / "tests"
        / "attempts"
        / "test_kernel_effects.py"
    )
    spec = importlib.util.spec_from_file_location("improvement_kernel_effects_helper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_INTENT_SCHEMA", resource_bytes("schemas/improvement-effect-intent.v1.schema.json"))
    return module


def _evaluate_contract(executor: Any) -> ResolvedAttemptContract[Any, Any]:
    from dataclasses import replace

    contract = TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID]
    if isinstance(contract.resources, ResourceClaims) and contract.resources.writes == ():
        contract = replace(contract, resources=ResourceClaims(writes=("out.txt",)))
    return resolve_contract(contract, executor=executor)


def _production_delivery() -> _ObservedDeliveryEffect:
    return _ObservedDeliveryEffect(ImprovementDeliveryEffect())


def _make_kernel(
    tmp_path: Path,
    *,
    effect: _ObservedDeliveryEffect,
    executor: Any,
) -> tuple[
    AssuranceAttemptKernel,
    Any,
    EvaluateMemoryInput,
    AttemptExecutionContext,
    ResolvedAttemptContract[Any, Any],
    _RecordingWorkspace,
    Any,
]:
    helper = _kernel_effects_module()
    effects, schemas = helper.build_effect_registries(
        effect,
        kinds=(_DELIVERY_KIND,),
        policy=EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0),
        receipt_schema=resource_bytes("schemas/improvement-effect-receipt.v1.schema.json"),
    )
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    resolved = _evaluate_contract(executor)
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,  # type: ignore[arg-type]
        graph_revision=_revision(),
        effects=effects,
        schemas=schemas,
        effect_state=CountingEffectState(),
    )
    validated = select_evaluate_memory(complete_evaluate_payload())
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=_revision(),
        public_entrypoint="improvement-evaluate",
        semantic_node_id="improvement.evaluate",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="improvement-evaluate",
        semantic_node_id="improvement.evaluate",
        attempt_key=key,
        fencing_token=4,
    )
    return kernel, key, validated, context, resolved, workspace, store


@pytest.mark.asyncio
async def test_kernel_settles_delivery_inside_same_attempt_before_receipt(tmp_path: Path) -> None:
    closed = close_improvement_task(_EVALUATE_ID)
    effect = _production_delivery()
    kernel, key, validated, context, resolved, workspace, store = _make_kernel(
        tmp_path, effect=effect, executor=closed
    )
    try:
        trace: list[str] = []
        result = await kernel.execute_or_recover(key, resolved, validated, context, trace=trace)
        assert isinstance(result, CommittedTaskResult)
        assert isinstance(result.output, MemoryEvalReceipt)
        assert closed.dispatch_count == 1
        assert effect.apply_calls == 1
        assert "settle_effects" in trace
        assert "publish_receipt" not in trace
        assert trace.index("settle_effects") < trace.index("record_terminal")
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is not None
        assert snapshot.terminal.resolution_kind == "committed"
        assert snapshot.effects
        assert snapshot.effects[0].kind == _DELIVERY_KIND
        assert snapshot.effects[0].receipt_digest
        intents = snapshot.effects
        assert len(intents) == 1
        assert intents[0].kind == _DELIVERY_KIND
        intent = ImprovementEffectIntentV1.model_validate(intents[0].payload)
        assert intent.kind == "memory_eval"
        assert intent.memory_eval is not None
        assert intent.memory_eval.eval_run_id == "eval-1"
        assert delivery_effect_key(intent) == f"{IMPROVEMENT_ID}:1:memory_eval:{HEX_A}"
        effect_state = kernel.effect_state
        assert isinstance(effect_state, CountingEffectState)
        assert effect_state.delivery_count == 1
        stored = next(iter(effect_state._by_settlement.values()))
        assert stored.receipt is not None
        receipt = ImprovementEffectReceiptV1.model_validate(stored.receipt)
        assert receipt.kind == "memory_eval"
        assert receipt.memory_eval is not None
        assert receipt.memory_eval.eval_run_id == "eval-1"
        assert workspace.promotions == 1
    finally:
        store.close()


def _runtime(kernel: AssuranceAttemptKernel) -> SimpleNamespace:
    return SimpleNamespace(
        attempt_kernel=kernel,
        revision_id=_revision(),
        fencing_token=4,
        invocation_id="inv-1",
        public_entrypoint="improvement-evaluate",
    )


@pytest.mark.asyncio
async def test_pending_effect_system_interrupts_without_success_or_double_dispatch(
    tmp_path: Path,
) -> None:
    closed = close_improvement_task(_EVALUATE_ID)
    effect = _ObservedDeliveryEffect(_PendingDeliveryEffect())  # type: ignore[arg-type]
    kernel, key, validated, context, resolved, workspace, store = _make_kernel(
        tmp_path, effect=effect, executor=closed
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, IndeterminateTaskResult)
        assert closed.dispatch_count == 1
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is None
        factory = AttemptNodeFactory(journal=kernel.journal, kernel=kernel)

        def select_input(state: dict[str, object]) -> EvaluateMemoryInput:
            return select_evaluate_memory(state)

        def publish(
            _state: dict[str, object], output: MemoryEvalReceipt, receipt: object
        ) -> dict[str, object]:
            del _state, receipt
            return {"receipt": output.model_dump(mode="json")}

        node = factory.attempt(
            resolved,
            semantic_node_id="improvement.evaluate",
            activation=lambda _state: BusinessActivation.one_shot(),
            select=select_input,
            publish=publish,
        )
        with pytest.raises(GraphInterrupt) as exc_info:
            await node(complete_evaluate_payload(), runtime=_runtime(kernel))
        payload = exc_info.value.args[0][0].value
        assert payload["kind"] in {"system_wake", "system_block"}
        assert "terminal" not in payload
        assert closed.dispatch_count == 1
        second = await kernel.execute_or_recover(
            key,
            resolved,
            validated,
            context,
        )
        assert isinstance(second, PendingTaskResult)
        assert closed.dispatch_count == 1
        assert effect.apply_calls == 1
        replay = await kernel.journal.load(key)
        assert replay is not None
        assert replay.terminal is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_publication_indeterminate_system_interrupts_without_success_or_double_dispatch(
    tmp_path: Path,
) -> None:
    closed = close_improvement_task(_EVALUATE_ID)
    effect = _ObservedDeliveryEffect(_PublicationIndeterminateEffect())  # type: ignore[arg-type]
    kernel, key, validated, context, resolved, workspace, store = _make_kernel(
        tmp_path, effect=effect, executor=closed
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, IndeterminateTaskResult)
        assert closed.dispatch_count == 1
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        assert snapshot.terminal is None
        factory = AttemptNodeFactory(journal=kernel.journal, kernel=kernel)

        def select_input(state: dict[str, object]) -> EvaluateMemoryInput:
            return select_evaluate_memory(state)

        def publish(
            _state: dict[str, object], output: MemoryEvalReceipt, receipt: object
        ) -> dict[str, object]:
            del _state, receipt
            return {"receipt": output.model_dump(mode="json")}

        node = factory.attempt(
            resolved,
            semantic_node_id="improvement.evaluate",
            activation=lambda _state: BusinessActivation.one_shot(),
            select=select_input,
            publish=publish,
        )
        with pytest.raises(GraphInterrupt) as exc_info:
            await node(complete_evaluate_payload(), runtime=_runtime(kernel))
        payload = exc_info.value.args[0][0].value
        assert payload["kind"] == "system_block"
        assert "terminal" not in payload
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, IndeterminateTaskResult)
        assert closed.dispatch_count == 1
        assert effect.apply_calls == 1
        final = await kernel.journal.load(key)
        assert final is not None
        assert final.terminal is None
    finally:
        store.close()
