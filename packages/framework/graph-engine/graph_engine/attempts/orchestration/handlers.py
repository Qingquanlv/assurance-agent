from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from graph_engine.artifacts import refs_from_write_set
from graph_engine.attempts.orchestration.commit import AttemptTransactions, CommitHandler, _CommitRejected
from graph_engine.attempts.models.context import AttemptExecutionContext, AuthorizedAttemptScope
from graph_engine.attempts.models.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TerminalReceiptRef,
)
from graph_engine.attempts.models.errors import AttemptIdentityDrift, AttemptIntegrityError
from graph_engine.attempts.orchestration.checkpoint import AttemptCheckpoint, AttemptPhase, AttemptResult
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import (
    AttemptResolution,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
    SystemReference,
)
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiterPort, ResourceAuthorization
from graph_engine.attempts.orchestration.runtime import (
    DurableProgress,
    HandlerResult,
    PhaseHandler,
    ReturnResolution,
)
from graph_engine.attempts.models.runtime_evidence import RUNTIME_EVIDENCE, RuntimeEvidenceSource
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore
from graph_engine.persistence.resource_authorization import ResourceAuthorizationError
from graph_engine.plugin_api import (
    CommitValidator,
    FailureKind,
    ResourceClaims,
    WorkspaceProvider,
)


def _bound_runtime_evidence(
    source: RuntimeEvidenceSource,
    *,
    invocation_id: str,
    exclude_attempt_key_digest: str,
) -> Callable[[], Awaitable[JSONValue]]:
    async def read() -> JSONValue:
        return await source.project(
            invocation_id=invocation_id,
            exclude_attempt_key_digest=exclude_attempt_key_digest,
        )

    return read


class AuthorizationActivityHandler(AttemptTransactions):
    """Restore authorization/workspace without deriving a second scheduler."""

    def __init__(
        self,
        checkpoints: AttemptCheckpointStore,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        graph_revision: str,
        runtime_evidence: RuntimeEvidenceSource | None,
    ) -> None:
        super().__init__(checkpoints, arbiter)
        self.workspace = workspace
        self.graph_revision = graph_revision
        self.runtime_evidence = runtime_evidence

    async def adopt_or_create(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptCheckpoint:
        snapshot = await self.checkpoints.load(attempt_key)
        identity = _identity(contract, validated_input, self.graph_revision, context)
        if snapshot is None:
            return await self.checkpoints.commit(
                AttemptCheckpoint(
                    attempt_key=attempt_key,
                    revision=0,
                    fencing_token=context.fencing_token,
                    phase=AttemptPhase.AUTHORIZE,
                    contract_digest=identity["contract_digest"],
                    input_digest=identity["input_digest"],
                    graph_revision=identity["graph_revision"],
                    invocation_id=context.invocation_id,
                    public_entrypoint=context.public_entrypoint,
                    semantic_node_id=context.semantic_node_id,
                ),
                expected_revision=0,
                fencing_token=context.fencing_token,
            )
        _assert_identity(snapshot, identity)
        if snapshot.authorization_id is not None:
            try:
                await self.arbiter.adopt(attempt_key, fencing_token=context.fencing_token)
            except ResourceAuthorizationError:
                if snapshot.terminal is None:
                    raise
        if context.fencing_token != snapshot.fencing_token:
            snapshot = await self._save(snapshot, context)
        return snapshot

    async def authorize(
        self,
        attempt_key: AttemptKey,
        claims: ResourceClaims,
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        snapshot: AttemptCheckpoint,
    ) -> tuple[ResourceAuthorization | PendingTaskResult, AttemptCheckpoint, AttemptExecutionContext]:
        if snapshot.authorization_id is not None:
            granted = await self.arbiter.adopt(attempt_key, fencing_token=context.fencing_token)
        else:
            granted = await self.arbiter.acquire(
                attempt_key, claims, fencing_token=context.fencing_token, validated_input=validated_input
            )
        if isinstance(granted, PendingTaskResult):
            return granted, snapshot, context
        if snapshot.authorization_id is None:
            snapshot = await self._save(
                snapshot, context, phase=AttemptPhase.EXECUTE, authorization_id=granted.authorization_id
            )
        return granted, snapshot, context.model_copy(update={"authorization_id": granted.authorization_id})

    async def begin_workspace(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        context: AttemptExecutionContext,
        claims: ResourceClaims,
        trace: list[str],
    ) -> AuthorizedAttemptScope | PermanentTaskFailure:
        binding = await self.workspace.open_or_create(attempt_key, claims, seed_from=context.seed_attempt_key)
        trace.append("begin_workspace")
        reader = None
        if RUNTIME_EVIDENCE in contract.contract.capabilities:
            if self.runtime_evidence is None:
                return PermanentTaskFailure(
                    kind="configuration", message="runtime evidence port is not configured"
                )
            reader = _bound_runtime_evidence(
                self.runtime_evidence,
                invocation_id=context.invocation_id,
                exclude_attempt_key_digest=attempt_key.digest,
            )
        return AuthorizedAttemptScope(execution=context, workspace=binding, runtime_evidence=reader)


class TerminalHandler(AttemptTransactions):
    """Own terminal publication, release proof, and terminal replay verification."""

    def __init__(
        self,
        checkpoints: AttemptCheckpointStore,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
    ) -> None:
        super().__init__(checkpoints, arbiter)
        self.workspace = workspace

    async def _terminate(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptCheckpoint,
        authorization: ResourceAuthorization,
        terminal: AttemptResult,
        cut: Callable[[str], None],
    ) -> AttemptCheckpoint:
        if snapshot.terminal is None:
            await self._assert_fence(attempt_key, context, "terminal_receipt", cut)
            snapshot = await self._save(snapshot, context, phase=AttemptPhase.RELEASE, terminal=terminal)
            await self.checkpoints.ensure_durable(attempt_key)
            cut("terminal_durable")
        return snapshot

    async def _complete_terminal_release(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptCheckpoint,
        cut: Callable[[str], None],
        *,
        authorization_id: str | None = None,
    ) -> AttemptCheckpoint:
        grant_id = authorization_id or snapshot.authorization_id
        active = await self.arbiter.is_active(attempt_key)
        if snapshot.released and active:
            raise AttemptIntegrityError("release proof contradicts an active grant")
        if snapshot.released:
            if snapshot.phase is AttemptPhase.RELEASE:
                return await self._save(snapshot, context, phase=AttemptPhase.DONE)
            return snapshot
        if active:
            await self._assert_fence(attempt_key, context, "resource_release", cut)
            await self.arbiter.release(attempt_key, fencing_token=context.fencing_token)
            cut("authorization_released")
        if await self.arbiter.is_active(attempt_key):
            raise AttemptIntegrityError("release proof")
        if grant_id is None:
            raise AttemptIntegrityError("release proof requires the authorization id")
        snapshot = await self._save(snapshot, context, phase=AttemptPhase.DONE, released=True)
        await self.checkpoints.ensure_durable(attempt_key)
        cut("release_proof")
        return snapshot

    async def replay(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        snapshot: AttemptCheckpoint,
        cut: Callable[[str], None],
    ) -> AttemptResolution:
        snapshot = await self._complete_terminal_release(attempt_key, context, snapshot, cut)
        assert snapshot.terminal is not None
        resolution = _resolution_from_terminal(snapshot.terminal, contract)
        if isinstance(resolution, CommittedTaskResult):
            sealed = await self.workspace.seal(
                await self.workspace.open_or_create(
                    attempt_key,
                    _resolved_claims(contract, validated_input),
                    seed_from=context.seed_attempt_key,
                )
            )
            if sealed.sealed_digest != snapshot.promotion_staged_digest:
                raise AttemptIntegrityError("committed staging drifted after promotion")
            return resolution.model_copy(update={"committed_artifacts": refs_from_write_set(sealed)})
        return resolution


class AttemptHandlers:
    """Persisted phase handlers; all process-local prerequisites are restored on entry."""

    def __init__(
        self,
        *,
        checkpoints: AttemptCheckpointStore,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        graph_revision: str,
        validators: Mapping[str, CommitValidator],
        runtime_evidence: RuntimeEvidenceSource | None,
        pause_requested: Callable[[], bool] | None,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        trace: list[str],
        cut: Callable[[str], None],
    ) -> None:
        self.checkpoints = checkpoints
        self.activity = AuthorizationActivityHandler(
            checkpoints, arbiter, workspace, graph_revision, runtime_evidence
        )
        self.commit = CommitHandler(checkpoints, arbiter, workspace, validators)
        self.terminal = TerminalHandler(checkpoints, arbiter, workspace)
        self.pause_requested = pause_requested
        self.attempt_key = attempt_key
        self.contract = contract
        self.validated_input = validated_input
        self.context = context
        self.trace = trace
        self.cut = cut
        self.scope: AuthorizedAttemptScope | None = None
        self.phases: Mapping[AttemptPhase, PhaseHandler] = {
            AttemptPhase.AUTHORIZE: self.authorize,
            AttemptPhase.EXECUTE: self.execute,
            AttemptPhase.RECONCILE: self.reconcile,
            AttemptPhase.COMMIT: self.commit_output,
            AttemptPhase.TERMINATE: self.terminate,
            AttemptPhase.RELEASE: self.release,
            AttemptPhase.DONE: self.done,
        }

    async def open_or_restore(self) -> AttemptCheckpoint:
        checkpoint = await self.activity.adopt_or_create(
            self.attempt_key, self.contract, self.validated_input, self.context
        )
        self.trace.append("adopt_or_create")
        return checkpoint

    async def _scope(
        self, checkpoint: AttemptCheckpoint
    ) -> tuple[AttemptCheckpoint, AuthorizedAttemptScope | PermanentTaskFailure | PendingTaskResult]:
        if self.scope is not None:
            return checkpoint, self.scope
        claims = _resolved_claims(self.contract, self.validated_input)
        if self.context.authorization_id != checkpoint.authorization_id:
            granted, checkpoint, self.context = await self.activity.authorize(
                self.attempt_key, claims, self.validated_input, self.context, checkpoint
            )
            self.trace.append("authorize_resources")
            if isinstance(granted, PendingTaskResult):
                return checkpoint, granted
        scope = await self.activity.begin_workspace(
            self.attempt_key, self.contract, self.context, claims, self.trace
        )
        if isinstance(scope, AuthorizedAttemptScope):
            self.scope = scope
        return checkpoint, scope

    async def authorize(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        if self.pause_requested is not None and self.pause_requested():
            return ReturnResolution(PendingTaskResult(wakeup=SystemReference(reference_id="operator_stop")))
        granted, checkpoint, self.context = await self.activity.authorize(
            self.attempt_key,
            _resolved_claims(self.contract, self.validated_input),
            self.validated_input,
            self.context,
            checkpoint,
        )
        if isinstance(granted, PendingTaskResult):
            return ReturnResolution(granted)
        self.trace.append("authorize_resources")
        return DurableProgress()

    async def execute(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        checkpoint, scope = await self._scope(checkpoint)
        if not isinstance(scope, AuthorizedAttemptScope):
            return await self._observe(checkpoint, scope)
        # Persist recovery entry BEFORE invoking external business work. Continue this invocation directly.
        checkpoint = await self.activity._save(
            checkpoint,
            self.context,
            phase=AttemptPhase.RECONCILE,
            activity_id=self.attempt_key.digest,
            activity_state="prepared",
        )
        await self.activity._assert_fence(self.attempt_key, self.context, "external_dispatch", self.cut)
        step = await self.contract.executor.execute(self.validated_input, scope)
        checkpoint = await self.activity._reload(self.attempt_key, checkpoint)
        return await self._observe(checkpoint, step)

    async def reconcile(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        checkpoint, scope = await self._scope(checkpoint)
        if not isinstance(scope, AuthorizedAttemptScope):
            return await self._observe(checkpoint, scope)
        await self.activity._assert_fence(self.attempt_key, self.context, "external_dispatch", self.cut)
        reconcile = getattr(self.contract.executor, "reconcile", None)
        if reconcile is None:
            step = PermanentTaskFailure(kind="internal", message="in-flight activity cannot be adopted")
        else:
            step = await reconcile(self.validated_input, scope, checkpoint)
        checkpoint = await self.activity._reload(self.attempt_key, checkpoint)
        return await self._observe(checkpoint, step)

    async def _fail(
        self,
        checkpoint: AttemptCheckpoint,
        failure: RejectedTaskResult | PermanentTaskFailure,
        output: JSONValue = None,
    ) -> HandlerResult:
        await self.activity._assert_fence(self.attempt_key, self.context, "terminal_receipt", self.cut)
        await self.activity._save(
            checkpoint,
            self.context,
            phase=AttemptPhase.TERMINATE,
            pending_result=_terminal_for_resolution(failure, output),
        )
        return DurableProgress()

    async def _observe(self, checkpoint: AttemptCheckpoint, step: object) -> HandlerResult:
        if isinstance(step, (RejectedTaskResult, PermanentTaskFailure)):
            return await self._fail(checkpoint, step)
        if _is_resolution(step):
            return ReturnResolution(cast(AttemptResolution, step))
        if not isinstance(step, ExecutedAttemptResult):
            return await self._fail(
                checkpoint,
                PermanentTaskFailure(
                    kind="invalid_output", message="executor did not return ExecutedAttemptResult"
                ),
            )
        self.trace.append("execute")
        try:
            output = self.contract.contract.output_model.model_validate(
                step.output.model_dump(mode="json") if isinstance(step.output, BaseModel) else step.output,
                context=self.contract.validation_context,
            ).model_dump(mode="json")
            digest = canonical_digest(output)
        except ValidationError as error:
            return await self._fail(
                checkpoint, PermanentTaskFailure(kind="invalid_output", message=str(error))
            )
        except (KeyError, ValueError) as error:
            return await self._fail(
                checkpoint, PermanentTaskFailure(kind="configuration", message=str(error))
            )
        receipt = step.source_terminal_receipt
        await self.activity._save(
            checkpoint,
            self.context,
            phase=AttemptPhase.COMMIT,
            activity_state="terminal_observed",
            activity_outcome=output,
            activity_outcome_digest=digest,
            source_identity_digest=receipt.identity_digest if receipt else None,
            source_receipt_digest=receipt.receipt_digest if receipt else None,
        )
        self.trace.append("validate_output")
        self.cut("after_observed_result")
        self.cut("after_finalize_before_seal")
        return DurableProgress()

    async def commit_output(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        checkpoint, scope = await self._scope(checkpoint)
        if not isinstance(scope, AuthorizedAttemptScope):
            return await self._observe(checkpoint, scope)
        result = await self.commit.commit_or_recover(
            attempt_key=self.attempt_key,
            contract=self.contract,
            validated_input=self.validated_input,
            context=self.context,
            claims=_resolved_claims(self.contract, self.validated_input),
            binding=scope.workspace,
            step=_executed_from_snapshot(self.contract, checkpoint),
            snapshot=checkpoint,
            trace=self.trace,
            cut=self.cut,
        )
        if isinstance(result, _CommitRejected):
            return await self._fail(result.snapshot, result.resolution, result.terminal_output)
        await self.activity._save(
            result.snapshot,
            self.context,
            phase=AttemptPhase.TERMINATE,
            pending_result=AttemptResult(
                resolution_kind="committed",
                output=result.output,
                receipt_id=result.receipt.identity_digest,
                receipt_digest=result.receipt.receipt_digest,
            ),
        )
        return DurableProgress()

    async def terminate(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        assert checkpoint.pending_result is not None
        # Recovery may enter here without any process-local authorization handle.
        if checkpoint.authorization_id is None:
            raise AttemptIntegrityError("terminal publication requires authorization")
        granted = await self.activity.arbiter.adopt(
            self.attempt_key, fencing_token=self.context.fencing_token
        )
        await self.terminal._terminate(
            self.attempt_key, self.context, checkpoint, granted, checkpoint.pending_result, self.cut
        )
        self.trace.append("record_terminal")
        return DurableProgress()

    async def release(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        await self.terminal._complete_terminal_release(self.attempt_key, self.context, checkpoint, self.cut)
        self.trace.extend(["release_resources", "record_release_proof"])
        return DurableProgress()

    async def done(self, checkpoint: AttemptCheckpoint) -> HandlerResult:
        return ReturnResolution(
            await self.terminal.replay(
                self.attempt_key, self.contract, self.validated_input, self.context, checkpoint, self.cut
            )
        )


_RESOLUTION_TYPES = (
    RejectedTaskResult,
    PermanentTaskFailure,
    PendingTaskResult,
    IndeterminateTaskResult,
    CommittedTaskResult,
)


def _is_resolution(value: object) -> bool:
    return isinstance(value, _RESOLUTION_TYPES)


def _identity(
    contract: ResolvedAttemptContract[Any, Any],
    validated_input: BaseModel,
    graph_revision: str,
    context: AttemptExecutionContext,
) -> dict[str, str]:
    del context
    payload: JSONValue = validated_input.model_dump(mode="json")
    return {
        "contract_digest": contract.contract_digest,
        "input_digest": canonical_digest(payload),
        "graph_revision": graph_revision,
    }


def _assert_identity(snapshot: AttemptCheckpoint, identity: Mapping[str, str]) -> None:
    if snapshot.input_digest != identity["input_digest"]:
        raise AttemptIdentityDrift("input digest drifted")
    if snapshot.contract_digest != identity["contract_digest"]:
        raise AttemptIdentityDrift("contract digest drifted")
    if snapshot.graph_revision != identity["graph_revision"]:
        raise AttemptIdentityDrift("revision digest drifted")


def _executed_from_snapshot(
    contract: ResolvedAttemptContract[Any, Any],
    snapshot: AttemptCheckpoint,
) -> ExecutedAttemptResult[Any] | PermanentTaskFailure:
    try:
        output = contract.contract.output_model.model_validate(
            snapshot.activity_outcome,
            context=contract.validation_context,
        )
    except ValidationError as error:
        return PermanentTaskFailure(kind="invalid_output", message=str(error))
    receipt = None
    if snapshot.source_identity_digest and snapshot.source_receipt_digest:
        receipt = TerminalReceiptRef(
            identity_digest=snapshot.source_identity_digest,
            receipt_digest=snapshot.source_receipt_digest,
        )
    return ExecutedAttemptResult(
        output=output,
        source_terminal_receipt=receipt,
    )


def _resolved_claims(
    contract: ResolvedAttemptContract[Any, Any], validated_input: BaseModel
) -> ResourceClaims:
    resources = contract.contract.resources
    if isinstance(resources, ResourceClaims):
        return resources
    return resources.resolve(validated_input.model_dump(mode="json"))


_FAILURE_KINDS = {
    "transient",
    "timeout",
    "invalid_input",
    "invalid_output",
    "external_effect",
    "internal",
    "configuration",
}


def _terminal_for_resolution(
    resolution: RejectedTaskResult | PermanentTaskFailure,
    output: JSONValue = None,
) -> AttemptResult:
    if isinstance(resolution, RejectedTaskResult):
        return AttemptResult(resolution_kind="rejected", output=output, reason=resolution.reason)
    return AttemptResult(
        resolution_kind="retryable" if resolution.retryable else "permanent",
        output=output,
        failure_kind=resolution.kind,
        message=resolution.message,
    )


def _resolution_from_terminal(
    terminal: AttemptResult,
    contract: ResolvedAttemptContract[Any, Any],
) -> AttemptResolution:
    if terminal.resolution_kind == "committed":
        return _committed_from_terminal(terminal, contract)
    if terminal.resolution_kind == "rejected":
        return RejectedTaskResult(reason=terminal.reason or "rejected")
    if terminal.resolution_kind in {"permanent", "retryable"}:
        kind = terminal.failure_kind if terminal.failure_kind in _FAILURE_KINDS else "internal"
        return PermanentTaskFailure(
            kind=cast(FailureKind, kind),
            message=terminal.message or "permanent",
            retryable=terminal.resolution_kind == "retryable",
        )
    raise AttemptIdentityDrift(f"unknown resolution kind {terminal.resolution_kind}")


def _committed_from_terminal(
    terminal: AttemptResult,
    contract: ResolvedAttemptContract[Any, Any],
) -> CommittedTaskResult[Any]:
    output = contract.contract.output_model.model_validate(
        terminal.output,
        context=contract.validation_context,
    )
    return CommittedTaskResult(
        output=output,
        receipt=ReceiptRef(receipt_id=terminal.receipt_id, receipt_digest=terminal.receipt_digest),
    )
