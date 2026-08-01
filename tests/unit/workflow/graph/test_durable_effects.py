"""Inline durable effects: intent/ack, contract cardinality, crash cuts, dark-ship."""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.workflow.core.events import HealingAttemptAllocatedEvent, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    SuperstepCommittedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ExecutionContract,
    catalog_from_pinned_contracts,
    load_execution_contracts,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.durable_effects import (
    DurableEffectAcknowledgementV1,
    DurableEffectContext,
    DurableEffectIntegrityError,
    DurableEffectIntentV1,
    DurableEffectRuntime,
    DurableEffectValidationError,
    EffectRegistration,
    EffectRegistry,
    build_intent,
    derive_effect_id,
    payload_sha256,
    production_effect_registry,
    reconcile_effect,
    reconciler_semantics_digest,
    scan_unacknowledged_intents,
    validate_acknowledgement,
    validate_result_intents,
)
from assurance_agent.workflow.graph.effect_retry import EffectRetryStore, RootEffectFenceStore
from assurance_agent.workflow.graph.models import TaskResult
from assurance_agent.workflow.graph.planner import _task_ready_as_predecessor
from assurance_agent.workflow.graph.models import TaskProjection

TEST_KIND = "test_marker/v1"


class MarkerPayloadV1(StrictWireModel):
    schema_version: Literal["1"]
    marker_key: str
    value: str


def _test_registry() -> EffectRegistry:
    registry = EffectRegistry()
    digest = reconciler_semantics_digest(kind=TEST_KIND, rules=("append-or-reuse-marker",))

    def _key(payload: Mapping[str, object]) -> str:
        return str(payload["marker_key"])

    def _reconcile(
        intent: DurableEffectIntentV1,
        context: DurableEffectContext,
        runtime: DurableEffectRuntime,
    ) -> DurableEffectAcknowledgementV1:
        domain = HealingAttemptAllocatedEvent(
            episode_id=str(intent.payload["marker_key"]),
            attempt_id=context.attempt_id,
            attempt_number=1,
            operation_id=f"{context.target}:{intent.payload['value']}",
            source_batch_id=context.invocation_id,
        )
        domain_digest = payload_sha256(domain.model_dump(mode="json"))
        with transaction(runtime.change_dir) as txn:
            for raw in read_events_strict(runtime.change_dir):
                if raw.get("type") != "healing_attempt_allocated":
                    continue
                if raw.get("episode_id") != domain.episode_id:
                    continue
                existing_op = raw.get("operation_id")
                if existing_op != domain.operation_id:
                    raise DurableEffectIntegrityError("same-key domain payload drift")
                seq = raw.get("seq")
                assert isinstance(seq, int)
                return DurableEffectAcknowledgementV1(
                    schema_version="1",
                    root_invocation_id=context.root_invocation_id,
                    invocation_id=context.invocation_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    effect_id=intent.effect_id,
                    kind=intent.kind,
                    reconciler_semantics_digest=intent.reconciler_semantics_digest,
                    payload_sha256=intent.payload_sha256,
                    domain_source_sequence=seq,
                    domain_event_digest=domain_digest,
                )
            txn.append_strict(domain)
        for raw in read_events_strict(runtime.change_dir):
            if raw.get("type") == "healing_attempt_allocated" and raw.get("episode_id") == domain.episode_id:
                seq = raw.get("seq")
                assert isinstance(seq, int)
                return DurableEffectAcknowledgementV1(
                    schema_version="1",
                    root_invocation_id=context.root_invocation_id,
                    invocation_id=context.invocation_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    effect_id=intent.effect_id,
                    kind=intent.kind,
                    reconciler_semantics_digest=intent.reconciler_semantics_digest,
                    payload_sha256=intent.payload_sha256,
                    domain_source_sequence=seq,
                    domain_event_digest=domain_digest,
                )
        raise DurableEffectIntegrityError("domain event missing after append")

    registry.register(
        EffectRegistration(
            kind=TEST_KIND,
            payload_model=MarkerPayloadV1,
            reconciler_semantics_digest=digest,
            domain_idempotency_key=_key,
            reconcile=_reconcile,
        )
    )
    return registry


def _intent(
    registry: EffectRegistry,
    *,
    invocation_id: str = "inv-1",
    task_id: str = "task-a",
    attempt_id: str = "att-1",
    marker_key: str = "mk-1",
    value: str = "v1",
) -> DurableEffectIntentV1:
    return build_intent(
        kind=TEST_KIND,
        payload=MarkerPayloadV1(schema_version="1", marker_key=marker_key, value=value),
        invocation_id=invocation_id,
        task_id=task_id,
        attempt_id=attempt_id,
        registry=registry,
    )


def test_production_registry_has_exact_healing_kinds() -> None:
    from assurance_agent.workflow.graph.durable_effects import (
        FIXER_PROPOSAL_APPROVED_V1,
        HEAL_RECORD_APPLY_V2,
        HEALING_ALLOCATION_V2,
    )

    registry = production_effect_registry()
    assert registry.kinds() == frozenset(
        {HEALING_ALLOCATION_V2, FIXER_PROPOSAL_APPROVED_V1, HEAL_RECORD_APPLY_V2}
    )
    assert len(registry) == 3


def test_packaged_contracts_select_exact_healing_durable_effects() -> None:
    catalog = load_execution_contracts(Path.cwd())
    selected = {
        target: contract.durable_effects
        for target, contract in catalog.contracts.items()
        if contract.durable_effects
    }
    assert selected == {
        "operation:allocate-healing-attempt": ("healing_allocation/v2",),
        "operation:record-fixer-approval": ("fixer_proposal_approved/v1",),
        "operation:record-codegen-fix-apply": ("heal_record_apply/v2",),
    }


def test_strict_intent_and_ack_round_trip(tmp_path: Path) -> None:
    registry = _test_registry()
    intent = _intent(registry)
    context = DurableEffectContext(
        root_invocation_id="inv-1",
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        target="operation:test-marker",
        output_digests={},
        write_set_id=None,
    )
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    runtime = DurableEffectRuntime(
        change_dir=change,
        project_root=tmp_path,
        fence_store=RootEffectFenceStore(tmp_path),
        retry_store=EffectRetryStore(tmp_path),
    )
    ack = reconcile_effect(intent, context, runtime, registry=registry)
    validate_acknowledgement(ack, intent=intent, context=context)
    # Exact duplicate reconcile is idempotent.
    again = reconcile_effect(intent, context, runtime, registry=registry)
    assert again.model_dump(mode="json") == ack.model_dump(mode="json")


def test_reject_opaque_and_malformed_payload() -> None:
    registry = _test_registry()
    with pytest.raises(DurableEffectValidationError, match="malformed"):
        build_intent(
            kind=TEST_KIND,
            payload={"schema_version": "1", "marker_key": "x"},  # missing value
            invocation_id="inv",
            task_id="t",
            attempt_id="a",
            registry=registry,
        )
    with pytest.raises(DurableEffectValidationError, match="unregistered"):
        build_intent(
            kind="unknown/v1",
            payload={"schema_version": "1", "marker_key": "x", "value": "y"},
            invocation_id="inv",
            task_id="t",
            attempt_id="a",
            registry=registry,
        )


def test_ack_requires_matching_inline_intent() -> None:
    registry = _test_registry()
    intent = _intent(registry)
    other = _intent(registry, marker_key="other")
    context = DurableEffectContext(
        root_invocation_id="inv-1",
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        target="operation:test-marker",
        output_digests={},
    )
    ack = DurableEffectAcknowledgementV1(
        schema_version="1",
        root_invocation_id="inv-1",
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        effect_id=other.effect_id,
        kind=intent.kind,
        reconciler_semantics_digest=intent.reconciler_semantics_digest,
        payload_sha256=intent.payload_sha256,
        domain_source_sequence=1,
        domain_event_digest="sha256:" + ("a" * 64),
    )
    with pytest.raises(DurableEffectIntegrityError, match="effect_id"):
        validate_acknowledgement(ack, intent=intent, context=context)


def test_contract_cardinality_exact_one(tmp_path: Path) -> None:
    registry = _test_registry()
    catalog = catalog_from_pinned_contracts(
        (
            ExecutionContract(
                target="operation:test-marker",
                handler="operation",
                durable_effects=(TEST_KIND,),
            ),
        ),
        effect_registry=registry,
    )
    contract = catalog.contracts["operation:test-marker"]
    intent = _intent(registry)
    ok = validate_result_intents(
        declared_kinds=contract.durable_effects,
        intents=(intent,),
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        target=contract.target,
        registry=registry,
    )
    assert len(ok) == 1

    with pytest.raises(DurableEffectValidationError, match="cardinality"):
        validate_result_intents(
            declared_kinds=contract.durable_effects,
            intents=(),
            invocation_id="inv-1",
            task_id="task-a",
            attempt_id="att-1",
            target=contract.target,
            registry=registry,
        )
    with pytest.raises(DurableEffectValidationError, match="cardinality"):
        validate_result_intents(
            declared_kinds=contract.durable_effects,
            intents=(intent, intent),
            invocation_id="inv-1",
            task_id="task-a",
            attempt_id="att-1",
            target=contract.target,
            registry=registry,
        )
    empty = ExecutionContract(target="operation:plain", handler="operation")
    with pytest.raises(DurableEffectValidationError, match="cardinality"):
        validate_result_intents(
            declared_kinds=empty.durable_effects,
            intents=(intent,),
            invocation_id="inv-1",
            task_id="task-a",
            attempt_id="att-1",
            target=empty.target,
            registry=registry,
        )


def test_wrong_producer_or_unstable_effect_id_is_invalid_output() -> None:
    registry = _test_registry()
    intent = _intent(registry)
    forged = intent.model_copy(update={"effect_id": "0" * 64})
    with pytest.raises(DurableEffectValidationError, match="effect_id"):
        validate_result_intents(
            declared_kinds=(TEST_KIND,),
            intents=(forged,),
            invocation_id="inv-1",
            task_id="task-a",
            attempt_id="att-1",
            target="operation:test-marker",
            registry=registry,
        )
    wrong_attempt = _intent(registry, attempt_id="att-2")
    with pytest.raises(DurableEffectValidationError, match="effect_id"):
        validate_result_intents(
            declared_kinds=(TEST_KIND,),
            intents=(wrong_attempt,),
            invocation_id="inv-1",
            task_id="task-a",
            attempt_id="att-1",
            target="operation:test-marker",
            registry=registry,
        )


def test_unknown_kind_rejected_at_catalog_load() -> None:
    with pytest.raises(ContractError, match="unregistered durable effect"):
        parse_execution_contracts(
            textwrap.dedent(
                f"""\
                schema_version: "1"
                contracts:
                  operation:x:
                    handler: operation
                    durable_effects: ["{TEST_KIND}"]
                """
            )
        )


def test_task_result_defaults_empty_effects() -> None:
    result = TaskResult(status="succeeded")
    assert result.durable_effects == ()


def test_predecessor_readiness_requires_commit_and_ack() -> None:
    intent = {
        "schema_version": "1",
        "effect_id": "abc",
        "kind": TEST_KIND,
        "reconciler_semantics_digest": "sha256:" + ("b" * 64),
        "payload_sha256": "sha256:" + ("c" * 64),
        "payload": {"schema_version": "1", "marker_key": "k", "value": "v"},
    }
    bare = TaskProjection(task_id="t1", node_id="n1", status="succeeded")
    assert not _task_ready_as_predecessor(bare)
    committed = bare.model_copy(update={"outputs_committed": True})
    assert _task_ready_as_predecessor(committed)
    with_effect = committed.model_copy(update={"durable_effects": (intent,)})
    assert not _task_ready_as_predecessor(with_effect)
    acked = with_effect.model_copy(update={"acknowledged_effect_ids": ("abc",)})
    assert _task_ready_as_predecessor(acked)


def test_crash_cut_after_success_before_ack_reconciles_once(tmp_path: Path) -> None:
    """After success+commit, recovery reconciles domain+ack without handler reinvoke."""
    registry = _test_registry()
    intent = _intent(registry)
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    with transaction(change) as txn:
        txn.append_strict(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id="inv-1",
                checkpoint_ns="inv-1",
                superstep_id="ss-1",
                task_id="task-a",
                attempt_id="att-1",
                node_id="n1",
                input_sha256="in",
                graph_digest="g" * 64,
                contract_digest="c" * 64,
                attempt_number=1,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                started_at="2099-01-01T00:00:00+00:00",
                target="operation:test-marker",
            )
        )
        txn.append_strict(
            TaskAttemptSucceededEvent(
                type="task_attempt_succeeded",
                invocation_id="inv-1",
                checkpoint_ns="inv-1",
                superstep_id="ss-1",
                task_id="task-a",
                attempt_id="att-1",
                durable_effects=[intent.model_dump(mode="json")],
            )
        )
        txn.append_strict(
            SuperstepCommittedEvent(
                type="superstep_committed",
                invocation_id="inv-1",
                checkpoint_ns="inv-1",
                superstep_id="ss-1",
                checkpoint_id="cp-1",
                write_set_ids=[],
                target_tree_id="t" * 64,
                state_values={},
                committed_task_ids=["task-a"],
            )
        )
    pending = scan_unacknowledged_intents(change, "inv-1")
    assert len(pending) == 1
    context = DurableEffectContext(
        root_invocation_id="inv-1",
        invocation_id="inv-1",
        task_id="task-a",
        attempt_id="att-1",
        target="operation:test-marker",
        output_digests={},
    )
    runtime = DurableEffectRuntime(
        change_dir=change,
        project_root=tmp_path,
        fence_store=RootEffectFenceStore(tmp_path),
        retry_store=EffectRetryStore(tmp_path),
    )
    reconcile_effect(intent, context, runtime, registry=registry)
    reconcile_effect(intent, context, runtime, registry=registry)
    events = read_events_strict(change)
    domain = [e for e in events if e.get("type") == "healing_attempt_allocated"]
    acks = [e for e in events if e.get("type") == "durable_effect_acknowledged"]
    assert len(domain) == 1
    assert len(acks) == 1
    # Fold accepts ack after commit.
    # Need a started invocation for full fold — use minimal started first.
    # Here we only assert scan is empty after ack.
    assert scan_unacknowledged_intents(change, "inv-1") == ()


def test_derive_effect_id_stable() -> None:
    left = derive_effect_id(
        kind=TEST_KIND,
        invocation_id="inv",
        task_id="t",
        attempt_id="a",
        domain_idempotency_key="k",
    )
    right = derive_effect_id(
        kind=TEST_KIND,
        invocation_id="inv",
        task_id="t",
        attempt_id="a",
        domain_idempotency_key="k",
    )
    assert left == right
    assert left != derive_effect_id(
        kind=TEST_KIND,
        invocation_id="inv",
        task_id="t",
        attempt_id="a",
        domain_idempotency_key="k2",
    )


def test_intent_rejects_payload_digest_drift() -> None:
    with pytest.raises(ValidationError):
        DurableEffectIntentV1(
            schema_version="1",
            effect_id="x" * 64,
            kind=TEST_KIND,
            reconciler_semantics_digest="sha256:" + ("a" * 64),
            payload_sha256="sha256:" + ("b" * 64),
            payload={"schema_version": "1", "marker_key": "k", "value": "v"},
        )


def test_crash_cut_before_success_leaves_no_inline_intent(tmp_path: Path) -> None:
    """Cut before success line: no inline intent, recovery has nothing to reconcile."""
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    with transaction(change) as txn:
        txn.append_strict(
            TaskAttemptStartedEvent(
                type="task_attempt_started",
                invocation_id="inv-1",
                checkpoint_ns="inv-1",
                superstep_id="ss-1",
                task_id="task-a",
                attempt_id="att-1",
                node_id="n1",
                input_sha256="in",
                graph_digest="g" * 64,
                contract_digest="c" * 64,
                attempt_number=1,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                started_at="2099-01-01T00:00:00+00:00",
                target="operation:test-marker",
            )
        )
    assert scan_unacknowledged_intents(change, "inv-1") == ()


def test_reconcile_uses_operation_target_and_suppresses_fence_on_retry_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1/P2: context.target is contract target; fence on schedule_next is no-progress."""
    from datetime import datetime, timezone
    from typing import Any, cast

    from assurance_agent.workflow.graph import runtime as runtime_mod
    from assurance_agent.workflow.graph.checkpoint import CheckpointStore
    from assurance_agent.workflow.graph.durable_effects import DurableEffectRetryableError
    from assurance_agent.workflow.graph.models import GraphProjection, RuntimeContext
    from assurance_agent.workflow.graph.runtime import GraphRuntime

    registry = _test_registry()
    intent = _intent(registry)
    change = tmp_path / "change"
    change.mkdir()
    (change / "events.jsonl").write_text("", encoding="utf-8")
    success = TaskAttemptSucceededEvent(
        type="task_attempt_succeeded",
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        superstep_id="ss-1",
        task_id="task-a",
        attempt_id="att-1",
        durable_effects=[intent.model_dump(mode="json")],
    )
    monkeypatch.setattr(
        runtime_mod,
        "scan_unacknowledged_intents",
        lambda *_a, **_k: ((success, intent),),
    )
    seen_targets: list[str] = []

    def _retryable(
        _intent: DurableEffectIntentV1,
        effect_context: DurableEffectContext,
        *_a: object,
        **_k: object,
    ) -> DurableEffectAcknowledgementV1:
        seen_targets.append(effect_context.target)
        raise DurableEffectRetryableError("lock held", error_code="progression_lock_timeout")

    monkeypatch.setattr(runtime_mod, "reconcile_effect", _retryable)
    fence = RootEffectFenceStore(tmp_path)
    fence.prepare_terminal("inv-1", now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    class _Clock:
        def now(self) -> datetime:
            return datetime(2026, 8, 1, tzinfo=timezone.utc)

    runtime = GraphRuntime(
        checkpoint_store=CheckpointStore(change),
        object_store=cast(Any, None),
        workspace_backend=cast(Any, None),
        definition_resolver=cast(Any, None),
        clock=_Clock(),  # type: ignore[arg-type]
    )

    class _Sched:
        _effect_registry = registry

    monkeypatch.setattr(runtime, "_scheduler_for", lambda _proj: _Sched())
    projection = GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest="g" * 64,
        contract_digests={},
        params={},
        root_tree_id="t" * 64,
        current_tree_id="t" * 64,
        tasks={
            "task-a": TaskProjection(
                task_id="task-a",
                node_id="n1",
                status="succeeded",
                outputs_committed=True,
                target="operation:test-marker",
                durable_effects=(intent.model_dump(mode="json"),),
                latest_attempt_id="att-1",
            )
        },
    )
    context = RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change,
        change_id="change-1",
    )
    assert runtime._reconcile_durable_effects(projection, context) is False  # noqa: SLF001
    assert seen_targets == ["operation:test-marker"]
