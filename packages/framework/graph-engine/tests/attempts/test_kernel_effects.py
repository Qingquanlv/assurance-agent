from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import (
    CommittedEffectFailure,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ContributionAuthority,
    EffectRegistry,
    ExecutableAuthority,
    SchemaRegistry,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.effects.contracts import (
    EXPECTED_EFFECT_KINDS,
    DualSettlementError,
    effect_idempotency_key,
)
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginContribution,
    PluginDescriptor,
    ResourceClaims,
    SchemaContribution,
    TaskFailure,
    TaskWorkspaceBinding,
)
from graph_engine.runtime.effects import EffectExecutor
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.task_workspace import TaskWorkspaceProvider, TaskWorkspaceStore


GRAPH_NAMES = (
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)
_INTENT_SCHEMA = (
    b'{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],"additionalProperties":false}'
)
_RECEIPT_SCHEMA = (
    b'{"type":"object","properties":{"remote_id":{"type":"string"}},"required":["remote_id"],'
    b'"additionalProperties":false}'
)
_KIND_OWNER = {
    "assurance.healing.effect.allocation.v2": "assurance.healing",
    "assurance.healing.effect.heal-apply.v2": "assurance.healing",
    "assurance.healing.effect.proposal-approved.v1": "assurance.healing",
    "assurance.improvement.effect.archive.v1": "assurance.improvement",
    "assurance.improvement.effect.delivery.v1": "assurance.improvement",
    "assurance.improvement.effect.promotion.v1": "assurance.improvement",
}
_PERMANENT = EffectApplyResult(
    status="permanent",
    failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
)
_TRANSIENT = EffectApplyResult(
    status="transient",
    failure=TaskFailure(kind="transient", message="busy"),
)


class RunInput(BaseModel):
    change_id: str


class RunOutput(BaseModel):
    status: str


class RecordingEffectHandler:
    def __init__(
        self,
        *,
        apply_result: EffectApplyResult | Sequence[EffectApplyResult] | None = None,
        reconcile_result: EffectReconcileResult | Sequence[EffectReconcileResult] | None = None,
        apply_error: BaseException | None = None,
        reconcile_error: BaseException | None = None,
        apply_untyped: object | None = None,
    ) -> None:
        self._apply_keys: list[str] = []
        self._reconcile_keys: list[str] = []
        self._apply_error = apply_error
        self._reconcile_error = reconcile_error
        self._apply_untyped = apply_untyped
        self._apply_results = _queued(apply_result)
        self._reconcile_results = _queued(reconcile_result)

    @property
    def apply_keys(self) -> tuple[str, ...]:
        return tuple(self._apply_keys)

    @property
    def reconcile_keys(self) -> tuple[str, ...]:
        return tuple(self._reconcile_keys)

    async def apply(self, _intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
        self._apply_keys.append(idempotency_key)
        if self._apply_error is not None:
            raise self._apply_error
        if self._apply_untyped is not None:
            return self._apply_untyped  # type: ignore[return-value]
        return _take(self._apply_results, "apply")

    async def reconcile(self, _intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
        self._reconcile_keys.append(idempotency_key)
        if self._reconcile_error is not None:
            raise self._reconcile_error
        return _take(self._reconcile_results, "reconcile")


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None
        self.promotions = 0

    async def open_or_create(self, attempt_key: AttemptKey, claims: ResourceClaims) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims)
        return self.binding

    async def seal(self, binding: TaskWorkspaceBinding):
        return await self.inner.seal(binding)

    async def prepare(self, binding: TaskWorkspaceBinding, sealed):
        return await self.inner.prepare(binding, sealed)

    async def promote(self, prepared):
        self.promotions += 1
        return await self.inner.promote(prepared)

    async def recover_promotion(self, prepared):
        self.promotions += 1
        return await self.inner.recover_promotion(prepared)


class _WritingExecutor:
    def __init__(
        self,
        workspace: _RecordingWorkspace,
        *,
        declared_effects: tuple[EffectIntent, ...] = (),
    ) -> None:
        self.workspace = workspace
        self.declared_effects = declared_effects
        self.calls = 0

    async def execute(self, validated_input: RunInput, context: AttemptExecutionContext) -> RunOutput:
        del validated_input, context
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        (binding.write_root / "out.txt").write_bytes(b"committed")
        return RunOutput(status="ok")


def _queued(
    value: EffectApplyResult | EffectReconcileResult | Sequence[object] | None,
) -> list[object] | object | None:
    if value is None or isinstance(value, EffectApplyResult | EffectReconcileResult):
        return value
    return list(value)


def _take(stored: list[object] | object | None, label: str) -> object:
    if stored is None:
        raise AssertionError(f"{label} result is not configured")
    if isinstance(stored, list):
        if not stored:
            raise AssertionError(f"no remaining {label} result")
        return stored.pop(0)
    return stored


def _source(plugin_id: str) -> SourceSnapshot:
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=Path(f"/sources/{plugin_id}"),
            distribution=plugin_id.replace(".", "-"),
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=f"{plugin_id.replace('.', '_')}:provider",
            declaration_path=f"{plugin_id.replace('.', '_')}/plugin-declaration.json",
            import_roots=("",),
            plugin_id=plugin_id,
            plugin_version="1.0.0",
        ),
        (),
    )


def _owner_contribution(
    owner_id: str,
    kinds: tuple[str, ...],
    handler: RecordingEffectHandler,
    policy: EffectPolicy,
    receipt_schema: bytes,
) -> tuple[SourceSnapshot, AuthenticatedContribution]:
    source = _source(owner_id)
    contribution = PluginContribution(
        schemas=(
            SchemaContribution(f"{owner_id}.intent", "application/schema+json", _INTENT_SCHEMA),
            SchemaContribution(f"{owner_id}.receipt", "application/schema+json", receipt_schema),
        ),
        effects=tuple(
            EffectRegistration(
                kind=kind,
                intent_schema_id=f"{owner_id}.intent",
                receipt_schema_id=f"{owner_id}.receipt",
                handler=handler,
                policy=policy,
            )
            for kind in kinds
        ),
    )
    source_key = SourceKey(SourceRole.PLUGIN, owner_id)
    proofs = [
        ExecutableProvenance.create(
            kind=kind,
            registry_id=registration.kind,
            owner_id=owner_id,
            source_key=source_key,
            source_digest=source.digest,
            module=ExecutableModuleProvenance(
                module_name="test_kernel_effects.implementation",
                standard_loader=StandardLoader.SOURCE,
                standard_is_package=False,
                relative_origin="implementation.py",
                authenticated_locations=(),
                physical_sha256="0" * 64,
                source_digest=source.digest,
            ),
            callable_path="assurance.effects.implementation:Handler.apply",
            binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
        )
        for registration in contribution.effects
        for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
    ]
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=owner_id,
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=(),
        commit_validators=(),
        schemas=tuple(item.schema_id for item in contribution.schemas),
        effects=tuple(item.kind for item in contribution.effects),
    )
    executable_objects = {
        (kind, registration.kind): registration.handler
        for registration in contribution.effects
        for kind in (ExecutableKind.EFFECT_APPLY, ExecutableKind.EFFECT_RECONCILE)
    }
    ordered_proofs = tuple(sorted(proofs, key=lambda item: (item.registry_id, item.kind.value)))
    authority_set = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id=owner_id,
        source_key=source_key,
        source_digest=source.digest,
        contribution=contribution,
        authorities=tuple(
            ExecutableAuthority(
                executable=executable_objects[(proof.kind, proof.registry_id)],
                function=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[proof.kind.slot],
                bound_self=executable_objects[(proof.kind, proof.registry_id)],
                descriptor=type(executable_objects[(proof.kind, proof.registry_id)]).__dict__[
                    proof.kind.slot
                ],
                provenance=proof,
            )
            for proof in ordered_proofs
        ),
    )
    authenticated = AuthenticatedContribution(
        owner_id=owner_id,
        source_key=source_key,
        source_digest=source.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=ordered_proofs,
        authority=authority_set,
    )
    return source, authenticated


def build_effect_registries(
    handler: RecordingEffectHandler,
    *,
    kinds: tuple[str, ...] | None = None,
    policy: EffectPolicy | None = None,
    receipt_schema: bytes = _RECEIPT_SCHEMA,
) -> tuple[EffectRegistry, SchemaRegistry]:
    selected = kinds if kinds is not None else tuple(sorted(EXPECTED_EFFECT_KINDS))
    selected_policy = policy or EffectPolicy(max_attempts=3, timeout_seconds=30, backoff_seconds=0)
    grouped: dict[str, list[str]] = {}
    for kind in selected:
        grouped.setdefault(_KIND_OWNER[kind], []).append(kind)
    sources: list[SourceSnapshot] = []
    contributions: list[AuthenticatedContribution] = []
    for owner_id, owner_kinds in grouped.items():
        source, authenticated = _owner_contribution(
            owner_id,
            tuple(owner_kinds),
            handler,
            selected_policy,
            receipt_schema,
        )
        sources.append(source)
        contributions.append(authenticated)
    registries = _build_registries(
        tuple(sources),
        tuple(contributions),
        tuple(grouped),
    )
    return registries.effects, registries.schemas


def graph_revision() -> str:
    return canonical_digest({"revision": "kernel-effects"})


def contract() -> TaskAttemptContract[RunInput, RunOutput]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=RunInput,
        output_model=RunOutput,
        resources=ResourceClaims(writes=("out.txt",)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def make_effect_kernel(
    tmp_path: Path,
    *,
    handler: RecordingEffectHandler,
    kind: str,
    policy: EffectPolicy | None = None,
    transaction_cut: Any = None,
):
    effects, schemas = build_effect_registries(handler, policy=policy)
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = _WritingExecutor(
        workspace,
        declared_effects=(EffectIntent(kind=kind, payload={"n": 1}),),
    )
    resolved = resolve_contract(contract(), executor=writer)
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=graph_revision(),
        effects=effects,
        schemas=schemas,
        transaction_cut=transaction_cut,
    )
    validated = RunInput(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=graph_revision(),
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=4,
    )
    return kernel, key, resolved, validated, context, writer, workspace, project, store, effects, schemas


def test_expected_effect_kinds_match_registry() -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    effect_registry, _schemas = build_effect_registries(handler)
    assert set(effect_registry.entries) == EXPECTED_EFFECT_KINDS
    assert EXPECTED_EFFECT_KINDS == {
        "assurance.healing.effect.allocation.v2",
        "assurance.healing.effect.heal-apply.v2",
        "assurance.healing.effect.proposal-approved.v1",
        "assurance.improvement.effect.archive.v1",
        "assurance.improvement.effect.delivery.v1",
        "assurance.improvement.effect.promotion.v1",
    }


def test_improvement_graph_names_are_not_effect_kinds() -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    effect_registry, _schemas = build_effect_registries(handler)
    for name in GRAPH_NAMES:
        assert name not in EXPECTED_EFFECT_KINDS
        assert name not in effect_registry.entries


@pytest.mark.parametrize("kind", sorted(EXPECTED_EFFECT_KINDS))
@pytest.mark.parametrize(
    "case",
    [
        "applied",
        "transient",
        "pending",
        "not_applied",
        "permanent",
        "invalid_receipt",
        "unknown",
    ],
)
async def test_apply_reconcile_outcomes_for_every_kind(tmp_path: Path, kind: str, case: str) -> None:
    if case == "applied":
        handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    elif case == "transient":
        handler = RecordingEffectHandler(apply_result=_TRANSIENT)
    elif case == "pending":
        handler = RecordingEffectHandler(
            apply_result=_TRANSIENT,
            reconcile_result=EffectReconcileResult(status="pending"),
        )
    elif case == "not_applied":
        handler = RecordingEffectHandler(
            apply_result=_TRANSIENT,
            reconcile_result=EffectReconcileResult(status="not_applied"),
        )
    elif case == "permanent":
        handler = RecordingEffectHandler(apply_result=_PERMANENT)
    elif case == "invalid_receipt":
        handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"n": 1}))
    else:
        handler = RecordingEffectHandler(apply_untyped={"status": "mystery"})

    policy = EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0)
    kernel, key, resolved, validated, context, writer, workspace, project, store, _effects, _schemas = (
        make_effect_kernel(tmp_path, handler=handler, kind=kind, policy=policy)
    )
    try:
        if case == "pending":
            first = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(first, IndeterminateTaskResult)
            result = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(result, PendingTaskResult)
            assert result.wakeup.reference_id
            assert handler.apply_keys == (effect_idempotency_key(key, 1),)
            assert handler.reconcile_keys == (effect_idempotency_key(key, 1),)
            assert workspace.promotions == 1
        elif case == "not_applied":
            first = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(first, IndeterminateTaskResult)
            result = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(result, CommittedEffectFailure)
            assert result.writes_promoted is True
            assert result.promotion_receipt.receipt_id
            assert handler.reconcile_keys == (effect_idempotency_key(key, 1),)
            assert workspace.promotions == 1
        else:
            result = await kernel.execute_or_recover(key, resolved, validated, context)
            if case == "applied":
                assert isinstance(result, CommittedTaskResult)
                assert result.output == RunOutput(status="ok")
                assert handler.apply_keys == (effect_idempotency_key(key, 1),)
                assert handler.reconcile_keys == ()
                snapshot = await kernel.journal.load(key)
                assert snapshot is not None
                assert snapshot.effects
                assert snapshot.effects[0].receipt_digest
            elif case == "transient":
                assert isinstance(result, IndeterminateTaskResult)
                assert result.reconciliation.reference_id
                assert handler.apply_keys == (effect_idempotency_key(key, 1),)
                assert handler.reconcile_keys == ()
            elif case == "permanent":
                assert isinstance(result, CommittedEffectFailure)
                assert result.writes_promoted is True
                assert result.promotion_receipt.receipt_id
                assert (project / "out.txt").read_bytes() == b"committed"
            elif case == "invalid_receipt":
                assert isinstance(result, CommittedEffectFailure)
                assert result.writes_promoted is True
            else:
                assert isinstance(result, IndeterminateTaskResult)
            assert workspace.promotions == 1
            assert writer.calls == 1
    finally:
        store.close()


async def test_one_attempt_cannot_be_settled_by_both_protocols(tmp_path: Path) -> None:
    kind = "assurance.improvement.effect.delivery.v1"
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    kernel, key, resolved, validated, context, _writer, _workspace, _project, store, effects, schemas = (
        make_effect_kernel(tmp_path, handler=handler, kind=kind)
    )
    try:
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        assert snapshot.effects
        legacy = EffectExecutor(effects, schemas, Ledger(tmp_path / "legacy-ledger"))
        with pytest.raises(DualSettlementError, match="in-Attempt"):
            legacy.settle_attempt(snapshot)
        assert "EffectExecutor" not in AssuranceAttemptKernel.__dict__
    finally:
        store.close()


def test_kernel_does_not_import_legacy_effect_executor() -> None:
    import inspect

    from graph_engine.attempts import kernel as kernel_mod

    source = inspect.getsource(kernel_mod)
    assert "runtime.effects" not in source
    assert "EffectExecutor" not in source
