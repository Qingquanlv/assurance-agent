from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from graph_engine.artifacts import ArtifactRef, refs_from_write_set
from graph_engine.attempts import workspace as task_workspace_runtime
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ExecutedAttemptResult, ResolvedAttemptContract
from graph_engine.attempts.errors import AttemptIdentityDrift
from graph_engine.attempts.events import (
    ActivityTerminalObserved,
    AttemptSnapshot,
    CommitPrepared,
    EffectIntentRecorded,
    WorkspacePromoted,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import PermanentTaskFailure, RejectedTaskResult
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.models import EffectRegistry, SchemaRegistry
from graph_engine.effects.state import effect_intent_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.json_schema import validate_json_schema
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from graph_engine.plugin_api import (
    CommitValidator,
    EffectIntent,
    PromotionReceipt,
    ResourceClaims,
    TaskWorkspaceBinding,
    ValidationContext,
    WorkspaceProvider,
    run_validators,
)


@dataclass(frozen=True, slots=True)
class CommitRejected:
    snapshot: AttemptSnapshot
    resolution: RejectedTaskResult | PermanentTaskFailure
    terminal_output: JSONValue = None


@dataclass(frozen=True, slots=True)
class PromotedCommit:
    snapshot: AttemptSnapshot
    output: JSONValue
    receipt: PromotionReceipt
    artifacts: tuple[ArtifactRef, ...]


async def commit_or_recover(
    *,
    journal: AttemptJournalPort,
    workspace: WorkspaceProvider,
    validators: Mapping[str, CommitValidator],
    effects: EffectRegistry | None,
    schemas: SchemaRegistry | None,
    assert_fence: Callable[[str], Awaitable[None]],
    attempt_key: AttemptKey,
    contract: ResolvedAttemptContract[Any, Any],
    validated_input: BaseModel,
    context: AttemptExecutionContext,
    claims: ResourceClaims,
    binding: TaskWorkspaceBinding,
    step: object,
    snapshot: AttemptSnapshot,
    trace: list[str],
    cut: Callable[[str], None],
) -> CommitRejected | PromotedCommit:
    if not isinstance(step, ExecutedAttemptResult):
        return CommitRejected(
            snapshot=snapshot,
            resolution=PermanentTaskFailure(
                kind="invalid_output",
                message="executor did not return ExecutedAttemptResult",
            ),
        )
    trace.append("execute")

    try:
        validated_output = contract.contract.output_model.model_validate(
            step.output.model_dump(mode="json") if isinstance(step.output, BaseModel) else step.output,
            context=contract.validation_context,
        )
        if step.effects:
            if effects is None or schemas is None:
                raise ValueError("declared effects require an effect registry")
            intent_events = _recorded_effect_intent_events(
                effects,
                schemas,
                step.effects,
            )
        else:
            intent_events = ()
        output: JSONValue = validated_output.model_dump(mode="json")
        source_receipt = step.source_terminal_receipt
        observed = ActivityTerminalObserved(
            activity_id=snapshot.activity_id or attempt_key.digest,
            outcome=output,
            outcome_digest=canonical_digest(output),
            source_identity_digest=(source_receipt.identity_digest if source_receipt is not None else ""),
            source_receipt_digest=(source_receipt.receipt_digest if source_receipt is not None else ""),
        )
    except ValidationError as error:
        return CommitRejected(
            snapshot=snapshot,
            resolution=PermanentTaskFailure(kind="invalid_output", message=str(error)),
        )
    except (KeyError, ValueError) as error:
        return CommitRejected(
            snapshot=snapshot,
            resolution=PermanentTaskFailure(kind="configuration", message=str(error)),
        )

    trace.append("validate_output")
    if snapshot.activity_state != "terminal_observed":
        snapshot = await journal.append(
            attempt_key,
            (
                observed,
                *intent_events,
            ),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
    cut("after_observed_result")
    cut("after_finalize_before_seal")

    if snapshot.prepared_digest is None:
        sealed = await workspace.seal(binding)
        trace.append("seal_candidate")
        rejected = run_validators(
            contract.contract.validators,
            validators,
            sealed,
            ValidationContext(
                invocation_id=context.invocation_id,
                task_id=attempt_key.digest,
                graph_instance_id=context.invocation_id,
                node_id=context.semantic_node_id,
                resources=claims,
                task_input=validated_input.model_dump(mode="json"),
                task_output=output,
                write_set=sealed,
            ),
        )
        trace.append("run_validators")
        if rejected is not None:
            return CommitRejected(
                snapshot=snapshot,
                resolution=rejected,
                terminal_output=output,
            )
        cut("before_durable_prepare")
        await assert_fence("durable_prepare")
        prepared = await workspace.prepare(binding, sealed)
        snapshot = await journal.append(
            attempt_key,
            (CommitPrepared(prepared_digest=prepared.prepared_digest),),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        await journal.ensure_durable(attempt_key)
        trace.append("durable_prepare")
        cut("after_prepare_before_promotion")
    else:
        trace.extend(["seal_candidate", "run_validators", "durable_prepare"])
        sealed = await workspace.seal(binding)
        prepared = await workspace.prepare(binding, sealed)
        if prepared.prepared_digest != snapshot.prepared_digest:
            raise AttemptIdentityDrift("prepared digest drifted")

    await assert_fence("promotion")
    if (
        snapshot.promotion_receipt_id is not None
        and snapshot.promotion_receipt_digest is not None
        and snapshot.promotion_staged_digest is not None
    ):
        receipt = PromotionReceipt(
            identity_digest=snapshot.promotion_receipt_id,
            staged_digest=snapshot.promotion_staged_digest,
            receipt_digest=snapshot.promotion_receipt_digest,
        )
    else:
        previous_cut = task_workspace_runtime._promotion_transaction_cut
        task_workspace_runtime._promotion_transaction_cut = cut
        try:
            receipt = await workspace.promote(prepared)
        finally:
            task_workspace_runtime._promotion_transaction_cut = previous_cut
        if snapshot.promotion_receipt_digest is None:
            snapshot = await journal.append(
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
    trace.append("promote")
    cut("after_promotion_before_receipt")
    return PromotedCommit(
        snapshot=snapshot,
        output=output,
        receipt=receipt,
        artifacts=refs_from_write_set(sealed),
    )


def _recorded_effect_intent_events(
    effects: EffectRegistry,
    schemas: SchemaRegistry,
    intents: Sequence[EffectIntent],
) -> tuple[EffectIntentRecorded, ...]:
    events: list[EffectIntentRecorded] = []
    for ordinal, intent in enumerate(intents, start=1):
        if intent.kind not in effects.entries:
            raise KeyError(f"unknown effect kind: {intent.kind}")
        registration = effects.require(intent.kind)
        schema = schemas.entries.get(registration.intent_schema_id)
        if schema is None:
            raise ValueError(f"effect intent schema is not registered: {registration.intent_schema_id}")
        validate_json_schema(thaw_json(intent.payload), schema.content)
        events.append(
            EffectIntentRecorded(
                effect_ordinal=ordinal,
                effect_kind=intent.kind,
                intent_digest=effect_intent_digest(intent.kind, intent.payload),
                payload=thaw_json(intent.payload),
            )
        )
    return tuple(events)


__all__ = ["CommitRejected", "PromotedCommit", "commit_or_recover"]
