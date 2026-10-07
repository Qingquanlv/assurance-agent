from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from graph_engine.artifacts import ArtifactRef, refs_from_write_set
from graph_engine.attempts import workspace as task_workspace_runtime
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
)
from graph_engine.attempts.errors import AttemptIdentityDrift
from graph_engine.attempts.events import (
    ActivityTerminalObserved,
    AttemptSnapshot,
    CommitPrepared,
    WorkspacePromoted,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import (
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiterPort
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_journal import AttemptJournalPort
from graph_engine.plugin_api import (
    CommitValidator,
    PromotionReceipt,
    ResourceClaims,
    TaskWorkspaceBinding,
    ValidationContext,
    WorkspaceProvider,
    run_validators,
)


@dataclass(frozen=True, slots=True)
class _CommitRejected:
    snapshot: AttemptSnapshot
    resolution: RejectedTaskResult | PermanentTaskFailure
    terminal_output: JSONValue = None


@dataclass(frozen=True, slots=True)
class _PromotedCommit:
    snapshot: AttemptSnapshot
    output: JSONValue
    receipt: PromotionReceipt
    artifacts: tuple[ArtifactRef, ...]


class AttemptTransactions:
    """Shared fence/reload primitives; transaction owners choose their ordering."""

    def __init__(self, journal: AttemptJournalPort, arbiter: ResourceArbiterPort) -> None:
        self.journal = journal
        self.arbiter = arbiter

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


class CommitHandler(AttemptTransactions):
    """Own output observation, validation, durable prepare and promotion."""

    def __init__(
        self,
        journal: AttemptJournalPort,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        validators: Mapping[str, CommitValidator],
    ) -> None:
        super().__init__(journal, arbiter)
        self.workspace = workspace
        self.validators = validators

    async def commit_or_recover(
        self,
        *,
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
    ) -> _CommitRejected | _PromotedCommit:
        if not isinstance(step, ExecutedAttemptResult):
            return _CommitRejected(
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
            return _CommitRejected(
                snapshot=snapshot,
                resolution=PermanentTaskFailure(kind="invalid_output", message=str(error)),
            )
        except (KeyError, ValueError) as error:
            return _CommitRejected(
                snapshot=snapshot,
                resolution=PermanentTaskFailure(kind="configuration", message=str(error)),
            )

        trace.append("validate_output")
        if snapshot.activity_state != "terminal_observed":
            snapshot = await self.journal.append(
                attempt_key,
                (observed,),
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
                    task_output=output,
                    write_set=sealed,
                ),
            )
            trace.append("run_validators")
            if rejected is not None:
                return _CommitRejected(
                    snapshot=snapshot,
                    resolution=rejected,
                    terminal_output=output,
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
        else:
            trace.extend(["seal_candidate", "run_validators", "durable_prepare"])
            sealed = await self.workspace.seal(binding)
            prepared = await self.workspace.prepare(binding, sealed)
            if prepared.prepared_digest != snapshot.prepared_digest:
                raise AttemptIdentityDrift("prepared digest drifted")

        await self._assert_fence(attempt_key, context, "promotion", cut)
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
        trace.append("promote")
        cut("after_promotion_before_receipt")
        return _PromotedCommit(
            snapshot=snapshot,
            output=output,
            receipt=receipt,
            artifacts=refs_from_write_set(sealed),
        )
