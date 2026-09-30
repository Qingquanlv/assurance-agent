from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agent_runtime_contracts import (
    AgentRunResult,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
)
from agent_runtime_contracts.execution_contract import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_opencode.observe.state import parse_closed_terminal_result
from agent_runtime_opencode.security import reject_canaries_in_payload, scan_for_canaries
from agent_runtime_opencode.session.binding import reject_isolated_root_discovery
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import AttemptRetryPolicy, AttemptTimeoutPolicy
from graph_engine.attempts.events import ActivityBound
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    IndeterminateTaskResult,
    PermanentTaskFailure,
    SystemReference,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS
from graph_engine.effects.state import MemoryEffectState
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, ResourceClaims, TaskWorkspaceBinding
from graph_engine.attempts.activity import BoundedCanonicalJson, bounded_canonical_json
from graph_engine.attempts.workspace import (
    TaskWorkspaceProvider,
    TaskWorkspaceStore,
    TaskWorkspaceViolation,
)


def _fencing_token(context: object) -> int:
    token = getattr(context, "fencing_token", None)
    if isinstance(token, int):
        return token
    execution = getattr(context, "execution", None)
    nested = getattr(execution, "fencing_token", None)
    if isinstance(nested, int):
        return nested
    raise AttributeError("fencing_token")


_HELPER_SPEC = importlib.util.spec_from_file_location(
    "test_kernel_effects",
    Path(__file__).with_name("test_kernel_effects.py"),
)
assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(_HELPERS)
RecordingEffectHandler = _HELPERS.RecordingEffectHandler
build_effect_registries = _HELPERS.build_effect_registries


class RawInput(BaseModel):
    change_id: str


class RawPrepared(BaseModel):
    change_id: str
    prompt_digest: str


class RawAgentResult(BaseModel):
    status: str


class RawOutput(BaseModel):
    status: str


class TransactionCrash(RuntimeError):
    pass


_SECRET = "canary-secret-value"
_ADAPTER = "opencode"
_PROVIDER = "opencode"
_MODEL = "fixture-model"
_SESSION = "ses_raw_1"
_MESSAGE = "msg_raw_1"
_EVIDENCE = "c" * 64
_UNPROVABLE_CUT = "after_session_create_before_prompt_ack"
_COMMITTED_CRASH_CUTS = (
    "before_session_create",
    "after_prompt_ack",
    "after_terminal_before_finalize",
    "after_finalize_before_seal",
    "before_durable_prepare",
    "after_prepare_before_promotion",
)
_MALFORMED_TERMINAL = (
    {
        "info": {
            "id": "asst-1",
            "role": "assistant",
            "time": {"created": 1, "completed": 2},
            "finish": "stop",
        },
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": '{"status":"ok"}'},
            {"type": "tool", "state": {"status": "completed"}},
            {"type": "step-finish", "reason": "stop"},
        ],
    },
)


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


class LivePrepare:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls = 0

    async def execute(self, validated_input: RawInput, context: AttemptExecutionContext) -> RawPrepared:
        del context
        self.calls += 1
        self.order.append("prepare")
        return RawPrepared(
            change_id=validated_input.change_id,
            prompt_digest=canonical_digest({"prompt": validated_input.change_id}),
        )


class LiveFinalize:
    def __init__(self, order: list[str], *, disagree: bool = False) -> None:
        self.order = order
        self.calls = 0
        self.disagree = disagree

    async def execute(
        self,
        bundle: RawFinalizeBundle[RawInput, RawPrepared, RawAgentResult],
        context: AttemptExecutionContext,
    ) -> RawOutput:
        del context
        self.calls += 1
        self.order.append("finalize")
        if self.disagree:
            raise ValueError("result/file disagreement: out.txt is missing")
        bundle.raw_workspace.read_bytes("out.txt")
        return RawOutput(status=bundle.agent_result.status)


class LiveRawRuntime:
    def __init__(
        self,
        workspace: _RecordingWorkspace,
        journal: MemoryAttemptJournal,
        *,
        order: list[str],
        cut: str | None = None,
        fault: str | None = None,
        secret: str = _SECRET,
    ) -> None:
        self.workspace = workspace
        self.journal = journal
        self.order = order
        self.cut = cut
        self.fault = fault
        self.secret = secret
        self.session_creates = 0
        self.prompt_admissions = 0
        self.cancels = 0
        self.reconciles = 0
        self.key: AttemptKey | None = None
        self.fencing_token = 4

    def _reference(self, prepared: RawPrepared, *, session_id: str | None) -> BoundedCanonicalJson:
        result_digest = canonical_digest({"status": "ok"})
        payload = {
            "attempt_key": self.key.digest if self.key is not None else "",
            "adapter": _ADAPTER,
            "provider": _PROVIDER,
            "model": _MODEL,
            "session_id": session_id,
            "message_id": _MESSAGE,
            "prompt_digest": prepared.prompt_digest,
            "result_digest": result_digest,
            "terminal_status": "running" if session_id and self.prompt_admissions == 0 else "admitted",
        }
        encoded = bounded_canonical_json(payload, limit=16 * 1024)
        return encoded

    async def _persist_bind(
        self, prepared: RawPrepared, session_id: str, context: AttemptExecutionContext
    ) -> None:
        assert self.key is not None
        encoded = self._reference(prepared, session_id=session_id)
        snapshot = await self.journal.load(self.key)
        assert snapshot is not None
        if getattr(snapshot, "activity_reference", None) == encoded.value:
            return
        await self.journal.append(
            self.key,
            (
                ActivityBound(
                    activity_id=snapshot.activity_id or self.key.digest,
                    reference=encoded.value,
                    reference_digest=encoded.digest,
                ),
            ),
            expected_revision=snapshot.revision,
            fencing_token=_fencing_token(context),
        )

    def _write_authorized(self) -> None:
        binding = self.workspace.binding
        assert binding is not None
        (binding.write_root / "out.txt").write_bytes(b"committed")

    def _outcome(self) -> RawAgentRuntimeOutcome:
        binding = self.workspace.binding
        assert binding is not None
        payload = {"status": "ok"}
        thawed = thaw_json(payload)
        return RawAgentRuntimeOutcome(
            run_result=AgentRunResult.model_validate(
                {
                    "result_payload": thawed,
                    "result_digest": canonical_digest(thawed),
                    "evidence_digest": _EVIDENCE,
                    "adapter_id": "runtime.opencode",
                    "adapter_version": "0.1.0",
                }
            ),
            raw_workspace=ReadOnlyRawWorkspace(binding.write_root),
        )

    async def execute(
        self, prepared: RawPrepared, context: AttemptExecutionContext
    ) -> RawAgentRuntimeOutcome:
        self.order.append("runtime")
        if self.cut == "before_session_create":
            raise TransactionCrash(self.cut)
        self.session_creates += 1
        await self._persist_bind(prepared, _SESSION, context)
        if self.cut == "after_session_create_before_prompt_ack":
            raise TransactionCrash(self.cut)
        self.prompt_admissions += 1
        await self._persist_bind(prepared, _SESSION, context)
        if self.cut == "after_prompt_ack":
            raise TransactionCrash(self.cut)
        self._apply_fault()
        if self.fault != "result_file_disagreement":
            self._write_authorized()
        if self.cut == "after_terminal_before_finalize":
            raise TransactionCrash(self.cut)
        return self._outcome()

    async def reconcile(
        self,
        prepared: RawPrepared,
        context: AttemptExecutionContext,
        snapshot: object,
    ) -> RawAgentRuntimeOutcome | IndeterminateTaskResult:
        self.reconciles += 1
        self.order.append("runtime-reconcile")
        if self.fault == "unprovable_admission":
            return IndeterminateTaskResult(
                reconciliation=SystemReference(reference_id="unprovable-admission")
            )
        reference = getattr(snapshot, "activity_reference", None)
        if not isinstance(reference, dict) or not reference.get("session_id"):
            return await self.execute(prepared, context)
        if (
            reference.get("session_id") != _SESSION
            or reference.get("adapter") != _ADAPTER
            or reference.get("provider") != _PROVIDER
            or reference.get("model") != _MODEL
        ):
            return IndeterminateTaskResult(reconciliation=SystemReference(reference_id="identity-drift"))
        if reference.get("terminal_status") == "running":
            return IndeterminateTaskResult(
                reconciliation=SystemReference(reference_id="unprovable-admission")
            )
        self._apply_fault()
        if self.fault != "result_file_disagreement":
            self._write_authorized()
        return self._outcome()

    async def cancel(self, prepared: RawPrepared, context: AttemptExecutionContext, snapshot: object) -> None:
        del prepared, context, snapshot
        self.cancels += 1

    def _apply_fault(self) -> None:
        binding = self.workspace.binding
        assert binding is not None
        if self.fault == "unauthorized_write":
            (binding.write_root / "secret-escape.txt").write_bytes(b"no")
            return
        if self.fault == "symlink_escape":
            outside = binding.write_root.parent / "outside.bin"
            outside.write_bytes(b"escaped")
            target = binding.write_root / "out.txt"
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(outside)
            return
        if self.fault == "project_instruction_discovery":
            (binding.write_root / "AGENTS.md").write_text("follow project instructions")
            reject_isolated_root_discovery(binding.write_root)
            return
        if self.fault == "secret_leakage":
            leaked = {"token": self.secret, "transcript": "x" * 100}
            (binding.write_root / "out.txt").write_text(self.secret)
            reject_canaries_in_payload(leaked, canaries=(self.secret,))
            scan_for_canaries(
                texts=(str(leaked),),
                roots=(binding.write_root,),
                canaries=(self.secret.encode("utf-8"),),
            )
            return
        if self.fault == "malformed_terminal":
            parse_closed_terminal_result(_MALFORMED_TERMINAL)
            return


def _contract() -> AgentExecutionContract[RawInput, RawAgentResult, RawOutput]:
    return AgentExecutionContract(
        contract_id="assurance.execution.agent.run.v1",
        owner_id="assurance.execution",
        prepare_handler_id="assurance.execution.run.prepare",
        finalize_handler_id="assurance.execution.run.finalize",
        skill_id="aa-run",
        agent_profile="assurance-v1-runner",
        input_model=RawInput,
        agent_result_model=RawAgentResult,
        output_model=RawOutput,
        resources=ResourceClaims(writes=("out.txt",)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=("out.txt",), finalize=()),
    )


def _revision() -> str:
    return canonical_digest({"revision": "raw-agent-recovery"})


def _build(
    tmp_path: Path,
    *,
    cut: str | None = None,
    fault: str | None = None,
    journal: MemoryAttemptJournal | None = None,
    authorization_store: MemoryResourceAuthorizationStore | None = None,
    effects: Any = None,
    schemas: Any = None,
    declared_effects: tuple[EffectIntent, ...] = (),
    fencing_token: int = 4,
):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    journal = journal if journal is not None else MemoryAttemptJournal()
    authorization_store = (
        authorization_store if authorization_store is not None else MemoryResourceAuthorizationStore()
    )
    order: list[str] = []
    prepare = LivePrepare(order)
    runtime = LiveRawRuntime(workspace, journal, order=order, cut=cut, fault=fault)
    finalize = LiveFinalize(order, disagree=fault == "result_file_disagreement")
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )
    if declared_effects:
        executor.effects = declared_effects
    resolved = executor.resolve()
    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(authorization_store),
        workspace=workspace,
        graph_revision=_revision(),
        effects=effects,
        schemas=schemas,
        effect_state=MemoryEffectState() if effects is not None else None,
    )
    validated = RawInput(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=_revision(),
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    runtime.key = key
    runtime.fencing_token = fencing_token
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=fencing_token,
    )
    return kernel, key, resolved, validated, context, executor, runtime, workspace, project, store, finalize


@pytest.mark.parametrize(
    "cut",
    [
        "before_session_create",
        _UNPROVABLE_CUT,
        "after_prompt_ack",
        "after_terminal_before_finalize",
        "after_finalize_before_seal",
        "before_durable_prepare",
        "after_prepare_before_promotion",
    ],
)
async def test_raw_crash_windows_adopt_recorded_session_without_second_prompt(
    tmp_path: Path, cut: str
) -> None:
    hits = {"count": 0}

    def kernel_cut(name: str) -> None:
        if name == cut:
            hits["count"] += 1
            raise TransactionCrash(cut)

    kernel, key, resolved, validated, context, _executor, runtime, workspace, project, store, finalize = (
        _build(
            tmp_path,
            cut=None
            if cut
            in {"after_finalize_before_seal", "before_durable_prepare", "after_prepare_before_promotion"}
            else cut,
        )
    )
    try:
        with pytest.raises(TransactionCrash, match=cut):
            await kernel.execute_or_recover(
                key,
                resolved,
                validated,
                context,
                transaction_cut=kernel_cut
                if cut
                in {"after_finalize_before_seal", "before_durable_prepare", "after_prepare_before_promotion"}
                else None,
            )
        runtime.cut = None
        recovered = await kernel.execute_or_recover(key, resolved, validated, context, transaction_cut=None)
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        reference = snapshot.activity_reference
        assert isinstance(reference, dict)
        assert reference["session_id"] == _SESSION
        assert reference["adapter"] == _ADAPTER
        assert reference["provider"] == _PROVIDER
        assert reference["model"] == _MODEL
        if cut == _UNPROVABLE_CUT:
            assert isinstance(recovered, IndeterminateTaskResult)
            assert runtime.session_creates == 1
            assert runtime.prompt_admissions == 0
            assert runtime.reconciles == 1
            assert workspace.promotions == 0
            assert snapshot.terminal is None
            assert reference["terminal_status"] == "running"
            assert not (project / "out.txt").exists()
            assert finalize.calls == 0
            replay = await kernel.execute_or_recover(key, resolved, validated, context)
            assert isinstance(replay, IndeterminateTaskResult)
            assert runtime.session_creates == 1
            assert runtime.prompt_admissions == 0
            return
        assert cut in _COMMITTED_CRASH_CUTS
        assert isinstance(recovered, CommittedTaskResult)
        assert recovered.output.status == "ok"
        assert runtime.session_creates <= 1
        if cut != "before_session_create":
            assert runtime.session_creates == 1
        if cut in {
            "after_prompt_ack",
            "after_terminal_before_finalize",
            "after_finalize_before_seal",
            "before_durable_prepare",
            "after_prepare_before_promotion",
        }:
            assert runtime.prompt_admissions == 1
        assert snapshot.terminal is not None
        assert (project / "out.txt").read_bytes() == b"committed"
        assert workspace.promotions == 1
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, CommittedTaskResult)
        assert replay.receipt == recovered.receipt
        assert runtime.session_creates <= 1
        assert runtime.prompt_admissions <= 1
        assert finalize.calls == 1
    finally:
        store.close()


async def test_fresh_executor_adopts_journaled_session_and_never_redispatches(tmp_path: Path) -> None:
    journal = MemoryAttemptJournal()
    authorization = MemoryResourceAuthorizationStore()
    first = _build(tmp_path, cut="after_prompt_ack", journal=journal, authorization_store=authorization)
    kernel, key, resolved, validated, context, _executor, runtime, _workspace, _project, store, _finalize = (
        first
    )
    try:
        with pytest.raises(TransactionCrash, match="after_prompt_ack"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        snapshot = await journal.load(key)
        assert snapshot is not None
        reference = snapshot.activity_reference
        assert isinstance(reference, dict)
        assert _SECRET not in str(reference)
        assert "transcript" not in reference
        second = _build(tmp_path, journal=journal, authorization_store=authorization)
        (
            recovered_kernel,
            _key,
            recovered_resolved,
            _validated,
            recovered_context,
            _recovered_executor,
            recovered_runtime,
            recovered_workspace,
            project,
            recovered_store,
            _recovered_finalize,
        ) = second
        recovered_runtime.key = key
        result = await recovered_kernel.execute_or_recover(
            key, recovered_resolved, validated, recovered_context
        )
        assert isinstance(result, CommittedTaskResult)
        assert recovered_runtime.session_creates == 0
        assert recovered_runtime.prompt_admissions == 0
        assert recovered_runtime.reconciles == 1
        assert runtime.prompt_admissions == 1
        assert (project / "out.txt").read_bytes() == b"committed"
        recovered_store.close()
    finally:
        store.close()


async def test_unprovable_admission_is_indeterminate_never_automatic_redispatch(
    tmp_path: Path,
) -> None:
    kernel, key, resolved, validated, context, _executor, runtime, _workspace, _project, store, _finalize = (
        _build(tmp_path, cut="after_session_create_before_prompt_ack")
    )
    try:
        with pytest.raises(TransactionCrash, match="after_session_create_before_prompt_ack"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        runtime.cut = None
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, IndeterminateTaskResult)
        assert runtime.session_creates == 1
        assert runtime.prompt_admissions == 0
        assert runtime.reconciles == 1
        again = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(again, IndeterminateTaskResult)
        assert runtime.session_creates == 1
        assert runtime.prompt_admissions == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "fault",
    [
        "unauthorized_write",
        "symlink_escape",
        "project_instruction_discovery",
        "secret_leakage",
        "malformed_terminal",
        "result_file_disagreement",
    ],
)
async def test_security_failures_fail_before_promote(tmp_path: Path, fault: str) -> None:
    kernel, key, resolved, validated, context, _executor, runtime, workspace, project, store, _finalize = (
        _build(tmp_path, fault=fault)
    )
    try:
        try:
            result = await kernel.execute_or_recover(key, resolved, validated, context)
        except (TaskWorkspaceViolation, ValueError, TransactionCrash):
            result = None
        assert workspace.promotions == 0
        assert not (project / "out.txt").exists() or fault == "symlink_escape"
        if result is not None:
            assert isinstance(result, (PermanentTaskFailure, IndeterminateTaskResult))
            assert getattr(result, "writes_promoted", False) is False
        if fault != "result_file_disagreement":
            assert runtime.prompt_admissions <= 1
        if fault == "secret_leakage":
            snapshot = await kernel.journal.load(key)
            assert snapshot is not None
            encoded = str(snapshot.activity_reference) + str(snapshot.terminal)
            assert _SECRET not in encoded
            assert "transcript" not in str(snapshot.activity_reference)
    finally:
        store.close()


async def test_persisted_recovery_facts_exclude_secrets_and_transcripts(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, _executor, runtime, _workspace, _project, store, _finalize = (
        _build(tmp_path)
    )
    try:
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        reference = snapshot.activity_reference
        assert isinstance(reference, dict)
        for required in (
            "attempt_key",
            "adapter",
            "provider",
            "model",
            "session_id",
            "message_id",
            "prompt_digest",
            "result_digest",
        ):
            assert reference[required]
        assert snapshot.terminal is not None
        assert snapshot.prepared_digest is not None
        assert snapshot.promotion_receipt_digest is not None
        encoded = str(reference) + str(snapshot.terminal)
        assert _SECRET not in encoded
        assert "transcript" not in reference
        assert runtime.session_creates == 1
        assert runtime.prompt_admissions == 1
    finally:
        store.close()


async def test_cancel_and_reconcile_are_idempotent_and_fenced(tmp_path: Path) -> None:
    kernel, key, resolved, validated, context, _executor, runtime, _workspace, _project, store, _finalize = (
        _build(tmp_path, cut="after_prompt_ack")
    )
    try:
        with pytest.raises(TransactionCrash, match="after_prompt_ack"):
            await kernel.execute_or_recover(key, resolved, validated, context)
        snapshot = await kernel.journal.load(key)
        assert snapshot is not None
        runtime.cut = None
        prepared = RawPrepared(change_id="chg-1", prompt_digest="a" * 64)
        await runtime.cancel(prepared, context, snapshot)
        await runtime.cancel(prepared, context, snapshot)
        assert runtime.cancels == 2
        stale = context.model_copy(update={"fencing_token": 3})
        with pytest.raises(StaleFencingToken):
            await kernel.execute_or_recover(key, resolved, validated, stale)
        result = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(result, CommittedTaskResult)
        again = await kernel.execute_or_recover(key, resolved, validated, context)
        assert again == result
        assert runtime.session_creates == 1
        assert runtime.prompt_admissions == 1
    finally:
        store.close()


@pytest.mark.parametrize("kind", sorted(EXPECTED_EFFECT_KINDS))
async def test_raw_agent_exercises_existing_effect_kinds_without_repeat(tmp_path: Path, kind: str) -> None:
    handler = RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    effects, schemas = build_effect_registries(handler)
    kernel, key, resolved, validated, context, _executor, runtime, workspace, project, store, _finalize = (
        _build(
            tmp_path,
            effects=effects,
            schemas=schemas,
            declared_effects=(EffectIntent(kind=kind, payload={"n": 1}),),
        )
    )
    try:
        first = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(first, CommittedTaskResult)
        replay = await kernel.execute_or_recover(key, resolved, validated, context)
        assert isinstance(replay, CommittedTaskResult)
        assert replay.receipt == first.receipt
        assert runtime.session_creates == 1
        assert runtime.prompt_admissions == 1
        assert workspace.promotions == 1
        assert handler.apply_keys
        assert handler.reconcile_keys == ()
        assert (project / "out.txt").read_bytes() == b"committed"
    finally:
        store.close()
