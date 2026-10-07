from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, assert_never, cast

from pydantic import BaseModel, ValidationError

from graph_engine.artifacts import refs_from_write_set
from graph_engine.attempts.commit import AttemptTransactions, CommitHandler, _CommitRejected, _PromotedCommit
from graph_engine.attempts.context import AttemptExecutionContext, AuthorizedAttemptScope
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TerminalReceiptRef,
)
from graph_engine.attempts.errors import AttemptIdentityDrift, AttemptIntegrityError
from graph_engine.attempts.events import (
    ActivityPrepared,
    AttemptOpened,
    AttemptSnapshot,
    AttemptTerminated,
    ResourcesAuthorized,
    ResourcesReleased,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import (
    AttemptResolution,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
    SystemReference,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiterPort, ResourceAuthorization
from graph_engine.attempts.runtime import AttemptAction, ContinueAttempt, HandlerResult, ReturnResolution
from graph_engine.attempts.runtime_evidence import RUNTIME_EVIDENCE, RuntimeEvidenceSource
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_journal import AttemptJournalPort
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
    """Own identity adoption, authorization, workspace scope and activity dispatch."""

    def __init__(
        self,
        journal: AttemptJournalPort,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        graph_revision: str,
        runtime_evidence: RuntimeEvidenceSource | None,
    ) -> None:
        super().__init__(journal, arbiter)
        self.workspace = workspace
        self.graph_revision = graph_revision
        self.runtime_evidence = runtime_evidence

    async def adopt_or_create(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
    ) -> AttemptSnapshot:
        snapshot = await self.journal.load(attempt_key)
        identity = _identity(contract, validated_input, self.graph_revision, context)
        if snapshot is None:
            return await self.journal.append(
                attempt_key,
                (
                    AttemptOpened(
                        contract_digest=identity["contract_digest"],
                        input_digest=identity["input_digest"],
                        graph_revision=identity["graph_revision"],
                        invocation_id=context.invocation_id,
                        public_entrypoint=context.public_entrypoint,
                        semantic_node_id=context.semantic_node_id,
                    ),
                ),
                expected_revision=0,
                fencing_token=context.fencing_token,
            )
        _assert_identity(snapshot, identity)
        if snapshot.terminal is not None or snapshot.released:
            if snapshot.authorization_id is not None:
                try:
                    await self.arbiter.adopt(attempt_key, fencing_token=context.fencing_token)
                except ResourceAuthorizationError:
                    pass
            return snapshot
        if snapshot.authorization_id is not None:
            await self.arbiter.adopt(attempt_key, fencing_token=context.fencing_token)
            if context.fencing_token > snapshot.fencing_token:
                snapshot = await self.journal.append(
                    attempt_key,
                    (ResourcesAuthorized(authorization_id=snapshot.authorization_id),),
                    expected_revision=snapshot.revision,
                    fencing_token=context.fencing_token,
                )
        return snapshot

    async def authorize(
        self,
        attempt_key: AttemptKey,
        claims: ResourceClaims,
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
    ) -> tuple[ResourceAuthorization | PendingTaskResult, AttemptSnapshot, AttemptExecutionContext]:
        if snapshot.authorization_id is not None:
            granted = await self.arbiter.adopt(attempt_key, fencing_token=context.fencing_token)
        else:
            granted = await self.arbiter.acquire(
                attempt_key,
                claims,
                fencing_token=context.fencing_token,
                validated_input=validated_input,
            )
        if isinstance(granted, PendingTaskResult):
            return granted, snapshot, context
        if snapshot.authorization_id is None:
            snapshot = await self.journal.append(
                attempt_key,
                (ResourcesAuthorized(authorization_id=granted.authorization_id),),
                expected_revision=snapshot.revision,
                fencing_token=context.fencing_token,
            )
        context = context.model_copy(update={"authorization_id": granted.authorization_id})
        return granted, snapshot, context

    async def activity(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
        snapshot: AttemptSnapshot,
        cut: Callable[[str], None],
        action: AttemptAction,
    ) -> tuple[object, AttemptSnapshot]:
        context = scope.execution
        activity_id = snapshot.activity_id or attempt_key.digest
        if action is AttemptAction.ADOPT_RESULT:
            if snapshot.activity_state != "terminal_observed" or snapshot.activity_outcome is None:
                raise AttemptIntegrityError("result adoption requires an observed non-null outcome")
            return _executed_from_snapshot(contract, snapshot), snapshot
        if action is AttemptAction.RECONCILE:
            if snapshot.activity_state not in {"prepared", "dispatch_started", "bound"}:
                raise AttemptIntegrityError("activity reconciliation requires an in-flight activity")
            await self._assert_fence(attempt_key, context, "external_dispatch", cut)
            reconcile = getattr(contract.executor, "reconcile", None)
            if reconcile is None:
                return (
                    PermanentTaskFailure(kind="internal", message="in-flight activity cannot be adopted"),
                    snapshot,
                )
            output = await reconcile(validated_input, scope, snapshot)
            snapshot = await self._reload(attempt_key, snapshot)
            return output, snapshot
        if action is not AttemptAction.DISPATCH:
            raise AttemptIntegrityError(f"unsupported activity action: {action.value}")
        snapshot = await self.journal.append(
            attempt_key,
            (ActivityPrepared(activity_id=activity_id),),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        await self._assert_fence(attempt_key, context, "external_dispatch", cut)
        output = await contract.executor.execute(validated_input, scope)
        snapshot = await self._reload(attempt_key, snapshot)
        return output, snapshot

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
        journal: AttemptJournalPort,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
    ) -> None:
        super().__init__(journal, arbiter)
        self.workspace = workspace

    async def _terminate(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        authorization: ResourceAuthorization,
        terminal: AttemptTerminated,
        cut: Callable[[str], None],
    ) -> AttemptSnapshot:
        if snapshot.terminal is None:
            await self._assert_fence(attempt_key, context, "terminal_receipt", cut)
            snapshot = await self.journal.append(
                attempt_key,
                (terminal,),
                expected_revision=snapshot.revision,
                fencing_token=context.fencing_token,
            )
            await self.journal.ensure_durable(attempt_key)
            cut("terminal_durable")
        return await self._complete_terminal_release(
            attempt_key,
            context,
            snapshot,
            cut,
            authorization_id=authorization.authorization_id,
        )

    async def _complete_terminal_release(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        cut: Callable[[str], None],
        *,
        authorization_id: str | None = None,
    ) -> AttemptSnapshot:
        grant_id = authorization_id or snapshot.authorization_id
        active = await self.arbiter.is_active(attempt_key)
        if snapshot.released and active:
            raise AttemptIntegrityError("release proof contradicts an active grant")
        if snapshot.released:
            return snapshot
        if active:
            await self._assert_fence(attempt_key, context, "resource_release", cut)
            await self.arbiter.release(attempt_key, fencing_token=context.fencing_token)
            cut("authorization_released")
        if await self.arbiter.is_active(attempt_key):
            raise AttemptIntegrityError("release proof")
        if grant_id is None:
            raise AttemptIntegrityError("release proof requires the authorization id")
        snapshot = await self.journal.append(
            attempt_key,
            (ResourcesReleased(authorization_id=grant_id),),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        await self.journal.ensure_durable(attempt_key)
        cut("release_proof")
        return snapshot

    async def replay(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
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

    async def publish(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        context: AttemptExecutionContext,
        authorization: ResourceAuthorization,
        result: _CommitRejected | _PromotedCommit,
        trace: list[str],
        cut: Callable[[str], None],
    ) -> AttemptResolution:
        if isinstance(result, _CommitRejected):
            await self._terminate(
                attempt_key,
                context,
                result.snapshot,
                authorization,
                _terminal_for_resolution(result.resolution, result.terminal_output),
                cut,
            )
            return result.resolution
        snapshot = await self._terminate(
            attempt_key,
            context,
            result.snapshot,
            authorization,
            AttemptTerminated(
                resolution_kind="committed",
                output=result.output,
                receipt_id=result.receipt.identity_digest,
                receipt_digest=result.receipt.receipt_digest,
            ),
            cut,
        )
        trace.extend(["record_terminal", "release_resources", "record_release_proof"])
        assert snapshot.terminal is not None
        resolution = _resolution_from_terminal(snapshot.terminal, contract)
        if isinstance(resolution, CommittedTaskResult):
            return resolution.model_copy(update={"committed_artifacts": result.artifacts})
        return resolution


class AttemptHandlers:
    """Invocation-local domain inputs/results and delegation to transaction owners."""

    def __init__(
        self,
        *,
        journal: AttemptJournalPort,
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
        self.activity = AuthorizationActivityHandler(
            journal, arbiter, workspace, graph_revision, runtime_evidence
        )
        self.commit = CommitHandler(journal, arbiter, workspace, validators)
        self.terminal = TerminalHandler(journal, arbiter, workspace)
        self.pause_requested = pause_requested
        self.attempt_key = attempt_key
        self.contract = contract
        self.validated_input = validated_input
        self.context = context
        self.trace = trace
        self.cut = cut
        self.claims: ResourceClaims | None = None
        self.authorization: ResourceAuthorization | None = None
        self.scope: AuthorizedAttemptScope | None = None
        self.step: object = None
        self.commit_result: _CommitRejected | _PromotedCommit | None = None

    async def handle(self, action: AttemptAction, snapshot: AttemptSnapshot | None) -> HandlerResult:
        if action is AttemptAction.OPEN:
            opened = await self.activity.adopt_or_create(
                self.attempt_key,
                self.contract,
                self.validated_input,
                self.context,
            )
            self.trace.append("adopt_or_create")
            return ContinueAttempt(opened)
        assert snapshot is not None
        match action:
            case AttemptAction.REPLAY_TERMINAL:
                return ReturnResolution(
                    await self.terminal.replay(
                        self.attempt_key,
                        self.contract,
                        self.validated_input,
                        self.context,
                        snapshot,
                        self.cut,
                    )
                )
            case AttemptAction.AUTHORIZE:
                if (
                    snapshot.activity_state is None
                    and self.pause_requested is not None
                    and self.pause_requested()
                ):
                    return ReturnResolution(
                        PendingTaskResult(wakeup=SystemReference(reference_id="operator_stop"))
                    )
                self.claims = _resolved_claims(self.contract, self.validated_input)
                authorization, snapshot, self.context = await self.activity.authorize(
                    self.attempt_key,
                    self.claims,
                    self.validated_input,
                    self.context,
                    snapshot,
                )
                self.trace.append("authorize_resources")
                if isinstance(authorization, PendingTaskResult):
                    return ReturnResolution(authorization)
                self.authorization = authorization
                return ContinueAttempt(snapshot)
            case AttemptAction.BEGIN_WORKSPACE:
                assert self.claims is not None
                scope = await self.activity.begin_workspace(
                    self.attempt_key,
                    self.contract,
                    self.context,
                    self.claims,
                    self.trace,
                )
                if isinstance(scope, PermanentTaskFailure):
                    self.commit_result = _CommitRejected(snapshot, scope)
                    return ContinueAttempt(snapshot, terminate=True)
                self.scope = scope
                return ContinueAttempt(snapshot)
            case AttemptAction.DISPATCH | AttemptAction.RECONCILE | AttemptAction.ADOPT_RESULT:
                assert self.scope is not None
                self.step, snapshot = await self.activity.activity(
                    self.attempt_key,
                    self.contract,
                    self.validated_input,
                    self.scope,
                    snapshot,
                    self.cut,
                    action,
                )
                if isinstance(self.step, (RejectedTaskResult, PermanentTaskFailure)):
                    self.commit_result = _CommitRejected(snapshot, self.step)
                    return ContinueAttempt(snapshot, terminate=True)
                if _is_resolution(self.step):
                    return ReturnResolution(cast(AttemptResolution, self.step))
                return ContinueAttempt(snapshot)
            case AttemptAction.COMMIT:
                assert self.claims is not None and self.scope is not None
                self.commit_result = await self.commit.commit_or_recover(
                    attempt_key=self.attempt_key,
                    contract=self.contract,
                    validated_input=self.validated_input,
                    context=self.context,
                    claims=self.claims,
                    binding=self.scope.workspace,
                    step=self.step,
                    snapshot=snapshot,
                    trace=self.trace,
                    cut=self.cut,
                )
                return ContinueAttempt(
                    self.commit_result.snapshot, terminate=isinstance(self.commit_result, _CommitRejected)
                )
            case AttemptAction.TERMINATE:
                assert self.authorization is not None and self.commit_result is not None
                return ReturnResolution(
                    await self.terminal.publish(
                        self.attempt_key,
                        self.contract,
                        self.context,
                        self.authorization,
                        self.commit_result,
                        self.trace,
                        self.cut,
                    )
                )
            case _:
                assert_never(action)


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


def _assert_identity(snapshot: AttemptSnapshot, identity: Mapping[str, str]) -> None:
    if snapshot.input_digest != identity["input_digest"]:
        raise AttemptIdentityDrift("input digest drifted")
    if snapshot.contract_digest != identity["contract_digest"]:
        raise AttemptIdentityDrift("contract digest drifted")
    if snapshot.graph_revision != identity["graph_revision"]:
        raise AttemptIdentityDrift("revision digest drifted")


def _executed_from_snapshot(
    contract: ResolvedAttemptContract[Any, Any],
    snapshot: AttemptSnapshot,
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
) -> AttemptTerminated:
    if isinstance(resolution, RejectedTaskResult):
        return AttemptTerminated(resolution_kind="rejected", output=output, reason=resolution.reason)
    return AttemptTerminated(
        resolution_kind="retryable" if resolution.retryable else "permanent",
        output=output,
        failure_kind=resolution.kind,
        message=resolution.message,
    )


def _resolution_from_terminal(
    terminal: AttemptTerminated,
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
    terminal: AttemptTerminated,
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
