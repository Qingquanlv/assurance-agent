from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from graph_engine.attempts.activity import (
    attempt_activity_in_flight,
    attempt_activity_is_terminal,
)
from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TerminalReceiptRef,
)
from graph_engine.attempts.events import ActivityPrepared, AttemptSnapshot
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from graph_engine.plugin_api import EffectIntent


async def execute_or_recover_activity(
    *,
    journal: AttemptJournalPort,
    assert_fence: Callable[[str], Awaitable[None]],
    attempt_key: AttemptKey,
    contract: ResolvedAttemptContract[Any, Any],
    validated_input: BaseModel,
    scope: AuthorizedAttemptScope,
    snapshot: AttemptSnapshot,
) -> tuple[object, AttemptSnapshot]:
    activity_id = snapshot.activity_id or attempt_key.digest
    if attempt_activity_is_terminal(snapshot.activity_state) and snapshot.activity_outcome is not None:
        return _executed_from_snapshot(contract, snapshot), snapshot

    if attempt_activity_in_flight(snapshot.activity_state):
        await assert_fence("external_dispatch")
        reconcile = getattr(contract.executor, "reconcile", None)
        if reconcile is None:
            return (
                PermanentTaskFailure(kind="internal", message="in-flight activity cannot be adopted"),
                snapshot,
            )
        output = await reconcile(validated_input, scope, snapshot)
        return output, await _reload(journal, attempt_key, snapshot)

    snapshot = await journal.append(
        attempt_key,
        (ActivityPrepared(activity_id=activity_id),),
        expected_revision=snapshot.revision,
        fencing_token=scope.execution.fencing_token,
    )
    await assert_fence("external_dispatch")
    output = await contract.executor.execute(validated_input, scope)
    return output, await _reload(journal, attempt_key, snapshot)


async def _reload(
    journal: AttemptJournalPort,
    attempt_key: AttemptKey,
    snapshot: AttemptSnapshot,
) -> AttemptSnapshot:
    latest = await journal.load(attempt_key)
    return latest if latest is not None else snapshot


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
        effects=tuple(EffectIntent(kind=item.kind, payload=item.payload) for item in snapshot.effects),
        source_terminal_receipt=receipt,
    )


__all__ = ["execute_or_recover_activity"]
