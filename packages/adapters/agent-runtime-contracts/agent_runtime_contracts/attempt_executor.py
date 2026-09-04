from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from graph_engine.attempts import (
    AttemptKey,
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
from graph_engine.plugin_api import EffectIntent, ResourceClaimTemplate, TaskOutcome

from agent_runtime_contracts.schema import canonical_digest, thaw_json, validate_local_agent_result

from agent_runtime_contracts.execution_contract import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.models import AgentRunResult


InputT = TypeVar("InputT", bound=BaseModel)
PreparedT = TypeVar("PreparedT")
AgentResultT = TypeVar("AgentResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)
_PreparedT_contra = TypeVar("_PreparedT_contra", contravariant=True)
_PreparedT_co = TypeVar("_PreparedT_co", covariant=True)
_OutputT_co = TypeVar("_OutputT_co", bound=BaseModel, covariant=True)
PhaseName = str


def _unprovable_raw_admission(snapshot: object) -> bool:
    reference = getattr(snapshot, "activity_reference", None)
    if not isinstance(reference, dict) or not reference.get("session_id"):
        return False
    return reference.get("terminal_status") == "running"


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def phase_task_id(attempt_key: AttemptKey, phase: str, handler_id: str) -> str:
    return canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "handler_id": handler_id,
            "phase": phase,
        }
    )


def _list_relative_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files.add(path.relative_to(root).as_posix())
    return files


def _resolve_claim_paths(
    templates: tuple[str, ...],
    resolved_writes: tuple[str, ...],
) -> set[str]:
    resolved: set[str] = set()
    for template in templates:
        if "{" not in template:
            resolved.add(template)
            continue
        prefix, _, suffix = template.partition("{change_id}")
        for path in resolved_writes:
            if path.startswith(prefix) and path.endswith(suffix):
                middle = path[len(prefix) : len(path) - len(suffix) if suffix else len(path)]
                if middle and "/" not in middle:
                    resolved.add(path)
    return resolved


@dataclass(frozen=True, slots=True)
class ReadOnlyRawWorkspace:
    _root: Path
    identity_digest: str = ""

    def _resolved(self, relative: str) -> Path:
        if not _canonical_relative(relative):
            raise ValueError(f"raw workspace path must be canonical and relative: {relative}")
        path = self._root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"raw workspace path is a symlink: {relative}")
        try:
            path.resolve().relative_to(self._root.resolve())
        except ValueError as error:
            raise ValueError(
                f"raw workspace path must stay inside the authorized root: {relative}"
            ) from error
        return path

    def read_bytes(self, relative: str) -> bytes:
        path = self._resolved(relative)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative)
        return path.read_bytes()

    def read_text(self, relative: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative).decode(encoding)


@dataclass(frozen=True, slots=True)
class RawAgentRuntimeOutcome:
    run_result: AgentRunResult
    raw_workspace: ReadOnlyRawWorkspace


@dataclass(frozen=True, slots=True)
class RawFinalizeBundle(Generic[InputT, PreparedT, AgentResultT]):
    validated_input: InputT
    prepared: PreparedT
    agent_result: AgentResultT
    run_evidence: AgentRunResult
    raw_workspace: ReadOnlyRawWorkspace


_T = TypeVar("_T")


class PreparePhase(Protocol[_InputT_contra, _PreparedT_co]):
    async def execute(
        self,
        validated_input: _InputT_contra,
        scope: AuthorizedAttemptScope,
    ) -> _PreparedT_co | PermanentTaskFailure: ...


class RuntimePhase(Protocol[_PreparedT_contra]):
    async def execute(
        self,
        prepared: _PreparedT_contra,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure: ...


class FinalizePhase(Protocol[InputT, PreparedT, AgentResultT, _OutputT_co]):
    async def execute(
        self,
        bundle: RawFinalizeBundle[InputT, PreparedT, AgentResultT],
        scope: AuthorizedAttemptScope,
    ) -> _OutputT_co | PermanentTaskFailure: ...


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
        self.effects: tuple[EffectIntent, ...] = ()
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
        return ResolvedRawAgentExecutor(
            self._contract,
            prepare=self._prepare,
            runtime=self._runtime,
            finalize=self._finalize,
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
            effects=self.effects,
            source_terminal_receipt=self._phase_receipts.get("runtime"),
        )

    async def reconcile(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
        snapshot: object,
    ) -> ExecutorStepResult[OutputT]:
        context = scope.execution
        prepared = await self._prepare.execute(validated_input, scope)
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
        outcome = await runtime_reconcile(prepared_value, context, snapshot)
        if isinstance(outcome, (IndeterminateTaskResult, PermanentTaskFailure)):
            return outcome
        if _unprovable_raw_admission(snapshot):
            return IndeterminateTaskResult(
                reconciliation=SystemReference(reference_id="unprovable-admission")
            )
        output = await self._finalize_outcome(validated_input, prepared_value, outcome, scope)
        output_failure = _typed_failure(output)
        if output_failure is not None:
            return output_failure
        return ExecutedAttemptResult(output=cast(OutputT, output), effects=self.effects)

    async def _run_phase(
        self,
        phase: PhaseName,
        action: Callable[[], Awaitable[_T]],
        scope: AuthorizedAttemptScope,
    ) -> _T | ExecutorResolution:
        before = _list_relative_files(scope.workspace.write_root)
        self.phase_log.append(phase)
        result = await action()
        after = _list_relative_files(scope.workspace.write_root)
        delta = after - before
        allowed = self._allowed_paths(phase, scope)
        if not delta <= allowed:
            return PermanentTaskFailure(
                kind="invalid_output",
                message=f"{phase} wrote undeclared staging paths: {sorted(delta - allowed)}",
            )
        failure = _typed_failure(result)
        if failure is not None:
            return failure
        self.phase_deltas[phase] = delta
        self._persist_phase_delta(phase, delta, scope, result)
        return result

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
        _exact, _digest, agent_result = validate_local_agent_result(
            thaw_json(outcome.run_result.result_payload),
            result_model=self._contract.agent_result_model,
            context=self._result_context,
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
        return self._contract.output_model.model_validate(output)


__all__ = [
    "FinalizePhase",
    "PreparePhase",
    "RawAgentRuntimeOutcome",
    "RawFinalizeBundle",
    "ReadOnlyRawWorkspace",
    "ResolvedRawAgentExecutor",
    "RuntimePhase",
    "phase_task_id",
]
