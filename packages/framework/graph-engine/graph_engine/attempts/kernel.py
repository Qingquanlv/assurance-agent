from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AttemptExecutionContext, AuthorizedAttemptScope
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TerminalReceiptRef,
)
from graph_engine.attempts.events import (
    ActivityPrepared,
    ActivityTerminalObserved,
    AttemptEvent,
    AttemptOpened,
    AttemptSnapshot,
    AttemptTerminated,
    CommitPrepared,
    EffectIntentRecorded,
    ResourcesAuthorized,
    ResourcesReleased,
    SystemInterruptIssued,
    WorkspacePromoted,
)
from graph_engine.attempts.keys import AttemptKey
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
from graph_engine.attempts.resource_arbiter import ResourceArbiterPort, ResourceAuthorization
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.composition.models import EffectRegistry, SchemaRegistry
from graph_engine.effects.apply import AttemptEffectSettler, recorded_effect_intent_events
from graph_engine.effects.state import EffectStatePort
from graph_engine.plugin_api import (
    CommitValidator,
    EffectIntent,
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    ValidationContext,
    WorkspaceProvider,
    run_validators,
)
from graph_engine.attempts.activity import (
    attempt_activity_in_flight,
    attempt_activity_is_terminal,
)
from graph_engine.attempts import workspace as task_workspace_runtime


class AttemptIdentityDrift(GraphEngineError):
    """Raised when replay identity does not match the opened Attempt."""


def _noop_cut(_name: str) -> None:
    return None


_MISSING_CUT = object()


class AssuranceAttemptKernel:
    def __init__(
        self,
        *,
        journal: AttemptJournalPort,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        graph_revision: str,
        validators: Mapping[str, CommitValidator] | None = None,
        effects: EffectRegistry | None = None,
        schemas: SchemaRegistry | None = None,
        effect_state: EffectStatePort | None = None,
        transaction_cut: Callable[[str], None] | None = None,
    ) -> None:
        self.journal = journal
        self.arbiter = arbiter
        self.workspace = workspace
        self.graph_revision = graph_revision
        self.validators = dict(validators or {})
        self.effects = effects
        self.schemas = schemas
        self.effect_state = effect_state
        self._transaction_cut = transaction_cut or _noop_cut

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        *,
        trace: list[str] | None = None,
        transaction_cut: Callable[[str], None] | None | object = _MISSING_CUT,
    ) -> AttemptResolution:
        if transaction_cut is _MISSING_CUT:
            cut = self._transaction_cut
        elif transaction_cut is None:
            cut = _noop_cut
        else:
            cut = transaction_cut
        return await self._run(
            attempt_key,
            contract,
            validated_input,
            context,
            trace=[] if trace is None else trace,
            cut=cut,  # type: ignore[arg-type]
        )

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

    async def record_system_interrupt_issued(
        self,
        attempt_key: AttemptKey,
        event: SystemInterruptIssued,
        context: AttemptExecutionContext,
    ) -> AttemptSnapshot:
        snapshot = await self.journal.load(attempt_key)
        return await self.journal.append(
            attempt_key,
            (event,),
            expected_revision=0 if snapshot is None else snapshot.revision,
            fencing_token=context.fencing_token,
        )

    async def observe_activity_completion(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
    ) -> AttemptSnapshot | None:
        del context
        return await self.journal.load(attempt_key)

    async def _run(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        *,
        trace: list[str],
        cut: Callable[[str], None],
    ) -> AttemptResolution:
        snapshot = await self.adopt_or_create(attempt_key, contract, validated_input, context)
        trace.append("adopt_or_create")
        if snapshot.terminal is not None:
            await self._release_if_held(attempt_key, context)
            return _resolution_from_terminal(snapshot.terminal, contract)

        claims = _resolved_claims(contract, validated_input)
        authorization, snapshot, context = await self._authorize(
            attempt_key, claims, validated_input, context, snapshot
        )
        trace.append("authorize_resources")
        if isinstance(authorization, PendingTaskResult):
            return authorization

        binding = await self.workspace.open_or_create(attempt_key, claims)
        trace.append("begin_workspace")
        scope = AuthorizedAttemptScope(execution=context, workspace=binding)

        step, snapshot = await self._execute_or_adopt(
            attempt_key, contract, validated_input, scope, snapshot, cut
        )
        if isinstance(step, (RejectedTaskResult, PermanentTaskFailure)):
            return await self._fail_closed(attempt_key, context, snapshot, authorization, step, cut)
        if _is_resolution(step):
            return cast(AttemptResolution, step)
        if not isinstance(step, ExecutedAttemptResult):
            return await self._fail_closed(
                attempt_key,
                context,
                snapshot,
                authorization,
                PermanentTaskFailure(
                    kind="invalid_output",
                    message="executor did not return ExecutedAttemptResult",
                ),
                cut,
            )
        trace.append("execute")

        try:
            validated_output, intent_events, observed = _validated_observed_batch(
                contract,
                step,
                self.effects,
                self.schemas,
                snapshot.activity_id or attempt_key.digest,
            )
        except ValidationError as error:
            return await self._fail_closed(
                attempt_key,
                context,
                snapshot,
                authorization,
                PermanentTaskFailure(kind="invalid_output", message=str(error)),
                cut,
            )
        except (KeyError, ValueError) as error:
            return await self._fail_closed(
                attempt_key,
                context,
                snapshot,
                authorization,
                PermanentTaskFailure(kind="configuration", message=str(error)),
                cut,
            )
        trace.append("validate_output")
        if snapshot.activity_state != "terminal_observed":
            snapshot = await self.journal.append(
                attempt_key,
                (observed, *intent_events),
                expected_revision=snapshot.revision,
                fencing_token=context.fencing_token,
            )
        cut("after_observed_result")
        cut("after_finalize_before_seal")

        if snapshot.prepared_digest is None:
            sealed = await self.workspace.seal(binding)
            trace.append("seal_candidate")
            rejected = run_validators(
                contract.contract.validators,
                self.validators,
                sealed,
                ValidationContext(
                    invocation_id=context.invocation_id,
                    task_id=attempt_key.digest,
                    graph_instance_id=context.invocation_id,
                    node_id=context.semantic_node_id,
                    resources=claims,
                    task_input=validated_input.model_dump(mode="json"),
                    task_output=validated_output.model_dump(mode="json"),
                    write_set=sealed,
                ),
            )
            trace.append("run_validators")
            if rejected is not None:
                return await self._fail_closed(
                    attempt_key,
                    context,
                    snapshot,
                    authorization,
                    rejected,
                    cut,
                    output=validated_output.model_dump(mode="json"),
                )
            cut("before_durable_prepare")
            await self._assert_fence(attempt_key, context, "durable_prepare", cut)
            prepared = await self.workspace.prepare(binding, sealed)
            snapshot = await self.journal.append(
                attempt_key,
                (CommitPrepared(prepared_digest=prepared.prepared_digest),),
                expected_revision=snapshot.revision,
                fencing_token=context.fencing_token,
            )
            await self.journal.ensure_durable(attempt_key)
            trace.append("durable_prepare")
            cut("after_prepare_before_promotion")
            receipt, snapshot = await self._promote(attempt_key, context, snapshot, prepared, cut)
        else:
            trace.extend(["seal_candidate", "run_validators", "durable_prepare"])
            prepared = await self._recover_prepared(binding, snapshot)
            receipt, snapshot = await self._promote(attempt_key, context, snapshot, prepared, cut)
        trace.append("promote")
        cut("after_promotion_before_receipt")

        await self._assert_fence(attempt_key, context, "effect_application", cut)
        settled, snapshot = await self._settle_effects(
            attempt_key,
            context,
            snapshot,
            receipt,
            cut,
        )
        trace.append("settle_effects")
        if isinstance(settled, (PendingTaskResult, IndeterminateTaskResult)):
            return settled
        if isinstance(settled, CommittedEffectFailure):
            snapshot = await self._terminate(
                attempt_key,
                context,
                snapshot,
                authorization,
                AttemptTerminated(
                    resolution_kind="committed_effect_failure",
                    receipt_id=receipt.identity_digest,
                    receipt_digest=receipt.receipt_digest,
                    reason=settled.reason,
                ),
                cut,
            )
            assert snapshot.terminal is not None
            return _resolution_from_terminal(snapshot.terminal, contract)

        snapshot = await self._terminate(
            attempt_key,
            context,
            snapshot,
            authorization,
            AttemptTerminated(
                resolution_kind="committed",
                output=validated_output.model_dump(mode="json"),
                receipt_id=receipt.identity_digest,
                receipt_digest=receipt.receipt_digest,
            ),
            cut,
        )
        trace.append("publish_receipt")
        assert snapshot.terminal is not None
        return _resolution_from_terminal(snapshot.terminal, contract)

    async def _authorize(
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

    async def _execute_or_adopt(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
        snapshot: AttemptSnapshot,
        cut: Callable[[str], None],
    ) -> tuple[object, AttemptSnapshot]:
        context = scope.execution
        activity_id = snapshot.activity_id or attempt_key.digest
        if attempt_activity_is_terminal(snapshot.activity_state) and snapshot.activity_outcome is not None:
            return _executed_from_snapshot(contract, snapshot), snapshot
        if attempt_activity_in_flight(snapshot.activity_state):
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

    async def _promote(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        prepared: PreparedWorkspaceRef,
        cut: Callable[[str], None],
    ) -> tuple[PromotionReceipt, AttemptSnapshot]:
        await self._assert_fence(attempt_key, context, "promotion", cut)
        if (
            snapshot.promotion_receipt_id is not None
            and snapshot.promotion_receipt_digest is not None
            and snapshot.promotion_staged_digest is not None
        ):
            return (
                PromotionReceipt(
                    identity_digest=snapshot.promotion_receipt_id,
                    staged_digest=snapshot.promotion_staged_digest,
                    receipt_digest=snapshot.promotion_receipt_digest,
                ),
                snapshot,
            )
        previous_cut = task_workspace_runtime._promotion_transaction_cut
        task_workspace_runtime._promotion_transaction_cut = cut
        try:
            receipt = await self.workspace.promote(prepared)
        finally:
            task_workspace_runtime._promotion_transaction_cut = previous_cut
        if snapshot.promotion_receipt_digest is None:
            snapshot = await self.journal.append(
                attempt_key,
                (
                    WorkspacePromoted(
                        receipt_id=receipt.identity_digest,
                        receipt_digest=receipt.receipt_digest,
                        staged_digest=receipt.staged_digest,
                    ),
                ),
                expected_revision=snapshot.revision,
                fencing_token=context.fencing_token,
            )
        return receipt, snapshot

    async def _recover_prepared(
        self,
        binding: Any,
        snapshot: AttemptSnapshot,
    ) -> PreparedWorkspaceRef:
        sealed = await self.workspace.seal(binding)
        prepared = await self.workspace.prepare(binding, sealed)
        if snapshot.prepared_digest is not None and prepared.prepared_digest != snapshot.prepared_digest:
            raise AttemptIdentityDrift("prepared digest drifted")
        return prepared

    async def _settle_effects(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        receipt: PromotionReceipt,
        cut: Callable[[str], None],
    ) -> tuple[AttemptResolution | None, AttemptSnapshot]:
        if not snapshot.effects:
            return None, snapshot
        if self.effects is None or self.schemas is None or self.effect_state is None:
            raise AttemptIdentityDrift("declared effects require an effect registry and state port")
        settler = AttemptEffectSettler(self.effects, self.schemas, self.effect_state)
        return await settler.settle(
            attempt_key=attempt_key,
            snapshot=snapshot,
            journal=self.journal,
            context=context,
            promotion=receipt,
            cut=cut,
        )

    async def _terminate(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        authorization: ResourceAuthorization,
        terminal: AttemptTerminated,
        cut: Callable[[str], None],
    ) -> AttemptSnapshot:
        await self._assert_fence(attempt_key, context, "terminal_receipt", cut)
        events: list[AttemptEvent] = [terminal]
        if not snapshot.released:
            events.append(ResourcesReleased(authorization_id=authorization.authorization_id))
        snapshot = await self.journal.append(
            attempt_key,
            tuple(events),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        await self.arbiter.release(attempt_key, fencing_token=context.fencing_token)
        await self.journal.ensure_durable(attempt_key)
        return snapshot

    async def _fail_closed(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        snapshot: AttemptSnapshot,
        authorization: ResourceAuthorization | PendingTaskResult,
        resolution: RejectedTaskResult | PermanentTaskFailure,
        cut: Callable[[str], None],
        output: JSONValue = None,
    ) -> AttemptResolution:
        if isinstance(authorization, PendingTaskResult):
            return resolution
        await self._terminate(
            attempt_key,
            context,
            snapshot,
            authorization,
            _terminal_for_resolution(resolution, output),
            cut,
        )
        return resolution

    async def _assert_fence(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
        name: str,
        cut: Callable[[str], None],
    ) -> None:
        cut(f"fence:{name}")
        await self.arbiter.assert_usable(attempt_key, fencing_token=context.fencing_token)

    async def _reload(self, attempt_key: AttemptKey, snapshot: AttemptSnapshot) -> AttemptSnapshot:
        latest = await self.journal.load(attempt_key)
        return latest if latest is not None else snapshot

    async def _release_if_held(
        self,
        attempt_key: AttemptKey,
        context: AttemptExecutionContext,
    ) -> None:
        try:
            await self.arbiter.release(attempt_key, fencing_token=context.fencing_token)
        except StaleFencingToken:
            return


_RESOLUTION_TYPES = (
    RejectedTaskResult,
    PermanentTaskFailure,
    PendingTaskResult,
    IndeterminateTaskResult,
    CommittedEffectFailure,
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
        output = contract.contract.output_model.model_validate(snapshot.activity_outcome)
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
        effects=tuple(EffectIntent(kind=item.kind, payload=item.payload) for item in snapshot.effects),
        source_terminal_receipt=receipt,
    )


def _validated_observed_batch(
    contract: ResolvedAttemptContract[Any, Any],
    step: ExecutedAttemptResult[Any],
    effects: EffectRegistry | None,
    schemas: SchemaRegistry | None,
    activity_id: str,
) -> tuple[BaseModel, tuple[EffectIntentRecorded, ...], ActivityTerminalObserved]:
    validated_output = contract.contract.output_model.model_validate(
        step.output.model_dump(mode="json") if isinstance(step.output, BaseModel) else step.output
    )
    if step.effects:
        if effects is None or schemas is None:
            raise ValueError("declared effects require an effect registry")
        intent_events = recorded_effect_intent_events(effects, schemas, step.effects)
    else:
        intent_events = ()
    payload: JSONValue = validated_output.model_dump(mode="json")
    receipt = step.source_terminal_receipt
    observed = ActivityTerminalObserved(
        activity_id=activity_id,
        outcome=payload,
        outcome_digest=canonical_digest(payload),
        source_identity_digest=receipt.identity_digest if receipt is not None else "",
        source_receipt_digest=receipt.receipt_digest if receipt is not None else "",
    )
    return validated_output, intent_events, observed


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
        resolution_kind="permanent",
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
    if terminal.resolution_kind == "committed_effect_failure":
        return CommittedEffectFailure(
            writes_promoted=True,
            promotion_receipt=ReceiptRef(
                receipt_id=terminal.receipt_id,
                receipt_digest=terminal.receipt_digest,
            ),
            reason=terminal.reason or "effect permanently failed",
        )
    if terminal.resolution_kind == "rejected":
        return RejectedTaskResult(reason=terminal.reason or "rejected")
    if terminal.resolution_kind == "permanent":
        kind = terminal.failure_kind if terminal.failure_kind in _FAILURE_KINDS else "internal"
        return PermanentTaskFailure(kind=kind, message=terminal.message or "permanent")  # type: ignore[arg-type]
    raise AttemptIdentityDrift(f"unknown resolution kind {terminal.resolution_kind}")


def _committed_from_terminal(
    terminal: AttemptTerminated,
    contract: ResolvedAttemptContract[Any, Any],
) -> CommittedTaskResult[Any]:
    output = contract.contract.output_model.model_validate(terminal.output)
    return CommittedTaskResult(
        output=output,
        receipt=ReceiptRef(receipt_id=terminal.receipt_id, receipt_digest=terminal.receipt_digest),
    )


__all__ = [
    "AssuranceAttemptKernel",
    "AttemptIdentityDrift",
]
