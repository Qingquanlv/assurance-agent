from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Generic, TypeVar, cast

from pydantic import BaseModel

from graph_engine.attempts import (
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    ExecutorResolution,
    ExecutorStepResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
    ResolvedAttemptContract,
    SystemReference,
    TaskAttemptContract,
    TerminalReceiptRef,
    resolve_contract,
)
from graph_engine.plugin_api import ResourceClaimTemplate, TaskOutcome

from agent_runtime_contracts.executor.phases import (
    AgentResultT,
    FinalizePhase,
    InputT,
    OutputT,
    PreparePhase,
    PreparedT,
    RawFinalizeBundle,
    phase_task_id,
)
from agent_runtime_contracts.executor.snapshot import (
    _covered_by_claims,
    _resolve_claim_paths,
    _snapshot_files,
    _unprovable_raw_admission,
)
from agent_runtime_contracts.ops.contract import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.runtime.protocol import RawAgentRuntimeOutcome, RuntimePhase
from agent_runtime_contracts.wire.schema import thaw_json, validate_local_agent_result

PhaseName = str
logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_PHASE_RESOLUTIONS = (
    PermanentTaskFailure,
    RejectedTaskResult,
    PendingTaskResult,
    IndeterminateTaskResult,
)


def _typed_failure(result: object) -> ExecutorResolution | None:
    if isinstance(result, _PHASE_RESOLUTIONS):
        return result
    return None


class ResolvedRawAgentExecutor(Generic[InputT, PreparedT, AgentResultT, OutputT]):
    def __init__(
        self,
        contract: AgentExecutionContract[InputT, AgentResultT, OutputT],
        *,
        prepare: PreparePhase[InputT, PreparedT],
        runtime: RuntimePhase[PreparedT],
        finalize: FinalizePhase[InputT, PreparedT, AgentResultT, OutputT],
        result_context: Mapping[str, object] | None = None,
        host: object | None = None,
        graph_revision: str = "",
        product_lock_digest: str = "",
    ) -> None:
        self._contract = contract
        self._prepare = prepare
        self._runtime = runtime
        self._finalize = finalize
        self._result_context = None if result_context is None else dict(result_context)
        self._host = host
        self._graph_revision = graph_revision
        self._product_lock_digest = product_lock_digest
        self.phase_log: list[str] = []
        self.phase_deltas: dict[str, set[str]] = {
            "prepare": set(),
            "runtime": set(),
            "finalize": set(),
        }
        self.phase_task_ids: dict[str, str] = {}
        self._phase_receipts: dict[str, TerminalReceiptRef] = {}

    def with_host(
        self,
        host: object,
        *,
        graph_revision: str,
        product_lock_digest: str,
    ) -> ResolvedRawAgentExecutor[InputT, PreparedT, AgentResultT, OutputT]:
        def bind_phase(phase: object) -> object:
            bind = getattr(phase, "with_host", None)
            if not callable(bind):
                return phase
            return bind(
                host,
                graph_revision=graph_revision,
                product_lock_digest=product_lock_digest,
            )

        return ResolvedRawAgentExecutor(
            self._contract,
            prepare=cast(PreparePhase[InputT, PreparedT], bind_phase(self._prepare)),
            runtime=cast(RuntimePhase[PreparedT], bind_phase(self._runtime)),
            finalize=cast(
                FinalizePhase[InputT, PreparedT, AgentResultT, OutputT],
                bind_phase(self._finalize),
            ),
            result_context=self._result_context,
            host=host,
            graph_revision=graph_revision,
            product_lock_digest=product_lock_digest,
        )

    @property
    def claims(self) -> AgentPhaseWriteClaims:
        return self._contract.phase_write_claims

    def to_task_contract(self) -> TaskAttemptContract[InputT, OutputT]:
        return self._contract.to_task_contract()

    def resolve(self) -> ResolvedAttemptContract[InputT, OutputT]:
        return resolve_contract(self._contract.to_task_contract(), executor=self)

    async def execute(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
    ) -> ExecutorStepResult[OutputT]:
        self._reset_phase_state(scope)
        return await self._execute_body(validated_input, scope)

    def _reset_phase_state(self, scope: AuthorizedAttemptScope) -> None:
        self.phase_log = []
        self.phase_deltas = {"prepare": set(), "runtime": set(), "finalize": set()}
        self._phase_receipts = {}
        self.phase_task_ids = {
            "prepare": phase_task_id(
                scope.execution.attempt_key, "prepare", self._contract.prepare_handler_id
            ),
            "runtime": phase_task_id(
                scope.execution.attempt_key, "runtime", getattr(self._runtime, "handler_id", "runtime")
            ),
            "finalize": phase_task_id(
                scope.execution.attempt_key, "finalize", self._contract.finalize_handler_id
            ),
        }

    async def _execute_body(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
    ) -> ExecutorStepResult[OutputT]:
        prepared = await self._run_phase(
            "prepare",
            lambda: self._prepare.execute(validated_input, scope),
            scope,
        )
        prepared_failure = _typed_failure(prepared)
        if prepared_failure is not None:
            return prepared_failure
        prepared_value = cast(PreparedT, prepared)
        outcome = await self._run_phase(
            "runtime",
            lambda: self._runtime.execute(prepared_value, scope),
            scope,
        )
        outcome_failure = _typed_failure(outcome)
        if outcome_failure is not None:
            return outcome_failure
        runtime_outcome = cast(RawAgentRuntimeOutcome, outcome)
        output = await self._run_phase(
            "finalize",
            lambda: self._finalize_outcome(validated_input, prepared_value, runtime_outcome, scope),
            scope,
        )
        output_failure = _typed_failure(output)
        if output_failure is not None:
            return output_failure
        return ExecutedAttemptResult(
            output=cast(OutputT, output),
            source_terminal_receipt=self._phase_receipts.get("runtime"),
        )

    async def reconcile(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
        snapshot: object,
    ) -> ExecutorStepResult[OutputT]:
        self._reset_phase_state(scope)
        return await self._reconcile_body(validated_input, scope, snapshot)

    async def _reconcile_body(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
        snapshot: object,
    ) -> ExecutorStepResult[OutputT]:
        context = scope.execution
        prepared = await self._run_phase(
            "prepare",
            lambda: self._prepare.execute(validated_input, scope),
            scope,
            persist_receipt=False,
        )
        prepared_failure = _typed_failure(prepared)
        if prepared_failure is not None:
            return prepared_failure
        prepared_value = cast(PreparedT, prepared)
        runtime_reconcile = getattr(self._runtime, "reconcile", None)
        if runtime_reconcile is None:
            return PermanentTaskFailure(
                kind="internal",
                message="in-flight activity cannot be adopted",
            )
        outcome = await self._run_phase(
            "runtime",
            lambda: runtime_reconcile(prepared_value, context, snapshot),
            scope,
            persist_receipt=False,
        )
        outcome_failure = _typed_failure(outcome)
        if outcome_failure is not None:
            return outcome_failure
        if _unprovable_raw_admission(snapshot):
            return IndeterminateTaskResult(
                reconciliation=SystemReference(reference_id="unprovable-admission")
            )
        output = await self._run_phase(
            "finalize",
            lambda: self._finalize_outcome(
                validated_input,
                prepared_value,
                cast(RawAgentRuntimeOutcome, outcome),
                scope,
            ),
            scope,
            persist_receipt=False,
        )
        output_failure = _typed_failure(output)
        if output_failure is not None:
            return output_failure
        return ExecutedAttemptResult(output=cast(OutputT, output))

    async def _run_phase(
        self,
        phase: PhaseName,
        action: Callable[[], Awaitable[_T]],
        scope: AuthorizedAttemptScope,
        *,
        persist_receipt: bool = True,
    ) -> _T | ExecutorResolution:
        try:
            before = _snapshot_files(scope.workspace.write_root)
        except OSError as error:
            return PermanentTaskFailure(
                kind="invalid_output", message=f"cannot snapshot staging before {phase}: {error}"
            )
        self.phase_log.append(phase)
        try:
            result = await action()
        except BaseException:
            audit_failure, _delta = self._audit_phase(phase, before, scope)
            if audit_failure is not None:
                logger.error("agent phase %s staging audit failed after exception", phase)
            raise
        audit_failure, delta = self._audit_phase(phase, before, scope)
        if audit_failure is not None:
            return audit_failure
        failure = _typed_failure(result)
        if failure is not None:
            return failure
        self.phase_deltas[phase] = delta
        if persist_receipt:
            self._persist_phase_delta(phase, delta, scope, result)
        return result

    def _audit_phase(
        self,
        phase: PhaseName,
        before: dict[str, tuple[str, int, int, int, int, int, int]],
        scope: AuthorizedAttemptScope,
    ) -> tuple[PermanentTaskFailure | None, set[str]]:
        try:
            after = _snapshot_files(scope.workspace.write_root)
        except OSError as error:
            return PermanentTaskFailure(
                kind="invalid_output", message=f"cannot snapshot staging after {phase}: {error}"
            ), set()
        delta = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        allowed = self._allowed_paths(phase, scope)
        unexpected = {path for path in delta if not _covered_by_claims(path, allowed)}
        if unexpected:
            return PermanentTaskFailure(
                kind="invalid_output",
                message=f"{phase} wrote undeclared staging paths: {sorted(unexpected)}",
            ), delta
        return None, delta

    def _phase_handler_id(self, phase: PhaseName) -> str:
        if phase == "prepare":
            return self._contract.prepare_handler_id
        if phase == "finalize":
            return self._contract.finalize_handler_id
        handler_id = getattr(self._runtime, "handler_id", None)
        if isinstance(handler_id, str) and handler_id:
            return handler_id
        raise ValueError("runtime phase requires a qualified handler id")

    def _persist_phase_delta(
        self,
        phase: PhaseName,
        delta: set[str],
        scope: AuthorizedAttemptScope,
        result: object,
    ) -> None:
        persist = getattr(self._host, "persist_phase_delta", None)
        if self._host is None or not callable(persist):
            return
        if isinstance(result, RawAgentRuntimeOutcome):
            payload = result.run_result.model_dump(mode="json")
        elif isinstance(result, BaseModel):
            payload = result.model_dump(mode="json")
        else:
            payload = None
        receipt = persist(
            scope=scope,
            phase=phase,
            task_id=self.phase_task_ids[phase],
            handler_id=self._phase_handler_id(phase),
            staged_paths=tuple(sorted(delta)),
            outcome=TaskOutcome.succeeded(payload),
            graph_revision=self._graph_revision,
            product_lock_digest=self._product_lock_digest,
        )
        if isinstance(receipt, TerminalReceiptRef):
            self._phase_receipts[phase] = receipt

    def _allowed_paths(self, phase: PhaseName, scope: AuthorizedAttemptScope) -> set[str]:
        claims = getattr(self._contract.phase_write_claims, phase)
        writes = scope.workspace.identity.output_paths
        if not writes:
            writes = tuple(self._contract.resources.writes)
            if isinstance(self._contract.resources, ResourceClaimTemplate):
                return _resolve_claim_paths(claims, writes) | set(claims)
        return _resolve_claim_paths(claims, writes) | set(claims)

    async def _finalize_outcome(
        self,
        validated_input: InputT,
        prepared: PreparedT,
        outcome: RawAgentRuntimeOutcome,
        scope: AuthorizedAttemptScope,
    ) -> OutputT | ExecutorResolution:
        try:
            _exact, _digest, agent_result = validate_local_agent_result(
                thaw_json(outcome.run_result.result_payload),
                result_model=self._contract.agent_result_model,
                context=self._result_context,
            )
        except (TypeError, ValueError) as error:
            return PermanentTaskFailure(
                kind="invalid_output",
                message=f"invalid agent result: {error}",
            )
        bundle = RawFinalizeBundle(
            validated_input=validated_input,
            prepared=prepared,
            agent_result=agent_result,
            run_evidence=outcome.run_result,
            raw_workspace=outcome.raw_workspace,
        )
        output = await self._finalize.execute(bundle, scope)
        failure = _typed_failure(output)
        if failure is not None:
            return failure
        return self._contract.output_model.model_validate(output, context=self._result_context)


__all__ = [
    "ResolvedRawAgentExecutor",
]
