from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptKey,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    ExecutorStepResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
    SystemReference,
    TerminalReceiptRef,
    resolve_contract,
)
from graph_engine.plugin_api import (
    DirectoryIdentity,
    ResourceClaims,
    TaskWorkspaceBinding,
    TaskWorkspaceIdentity,
)

from agent_runtime_contracts import (
    AgentExecutionContract,
    AgentPhaseWriteClaims,
    AgentRunResult,
    FinallyContext,
    after,
    before,
    canonical_digest,
    finally_,
)
from agent_runtime_contracts.attempt_executor import (
    FinalizePhase,
    PreparePhase,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
    RuntimePhase,
)
from agent_runtime_contracts.schema import thaw_json


class CaseDesignInput(BaseModel):
    change_id: str
    path: Literal["primary", "repair"] = "primary"


class CaseDesignPrepared(BaseModel):
    change_id: str
    path: Literal["primary", "repair"]
    prompt: str


class CaseDesignAgentResult(BaseModel):
    output_files: tuple[str, ...]


class CaseDesignOutput(BaseModel):
    status: str
    output_files: tuple[str, ...]
    path: Literal["primary", "repair"]


_EVIDENCE = "b" * 64


def _run_result(payload: object) -> AgentRunResult:
    thawed = thaw_json(payload)
    return AgentRunResult.model_validate(
        {
            "result_payload": thawed,
            "result_digest": canonical_digest(thawed),
            "evidence_digest": _EVIDENCE,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )


class RecordingPrepare:
    def __init__(self, prepared: CaseDesignPrepared, order: list[str]) -> None:
        self.prepared = prepared
        self.order = order
        self.seen_input: CaseDesignInput | None = None

    async def execute(
        self,
        validated_input: CaseDesignInput,
        scope: AuthorizedAttemptScope,
    ) -> CaseDesignPrepared:
        del scope
        self.order.append("prepare")
        self.seen_input = validated_input
        return self.prepared


class RecordingRuntime:
    handler_id = "assurance.intake.runtime.opencode.execute"

    def __init__(
        self,
        result: object,
        workspace: ReadOnlyRawWorkspace,
        order: list[str],
    ) -> None:
        self.result = result
        self.workspace = workspace
        self.order = order
        self.seen_prepared: CaseDesignPrepared | None = None
        self.seen_schema: object = None

    async def execute(
        self,
        prepared: CaseDesignPrepared,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome:
        del scope
        self.order.append("runtime")
        self.seen_prepared = prepared
        run_result = self.result if isinstance(self.result, AgentRunResult) else _run_result(self.result)
        return RawAgentRuntimeOutcome(run_result=run_result, raw_workspace=self.workspace)


class RecordingFinalize:
    def __init__(self, output: object, order: list[str]) -> None:
        self.output = output
        self.order = order
        self.seen: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult] | None = None

    async def execute(
        self,
        bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
        scope: AuthorizedAttemptScope,
    ) -> CaseDesignOutput:
        del scope
        self.order.append("finalize")
        self.seen = bundle
        return (
            self.output
            if isinstance(self.output, CaseDesignOutput)
            else CaseDesignOutput.model_validate(self.output)
        )


def _context(*, authorization_id: str | None = None) -> AttemptExecutionContext:
    return AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="intake",
        semantic_node_id="case-design",
        attempt_key=AttemptKey(digest="a" * 64),
        fencing_token=1,
        authorization_id=authorization_id,
    )


class _RecordingHost:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def persist_phase_delta(self, **kwargs: object) -> TerminalReceiptRef:
        self.calls.append(kwargs)
        staged = kwargs["staged_paths"]
        paths = list(staged) if isinstance(staged, (list, tuple)) else []
        digest = canonical_digest({"phase": kwargs["phase"], "paths": paths})
        return TerminalReceiptRef(identity_digest=digest, receipt_digest=digest)


def _scope(tmp_path: Path, *, authorization_id: str | None = None) -> AuthorizedAttemptScope:
    project = tmp_path / "project"
    write = tmp_path / "write"
    project.mkdir(exist_ok=True)
    write.mkdir(exist_ok=True)
    digest = "a" * 64
    identity = TaskWorkspaceIdentity.model_construct(
        task_id="task",
        attempt=1,
        attempt_id="attempt-1",
        output_paths=(),
        baseline_files=(),
        project_digest=digest,
        write_root_digest=digest,
        identity_digest=digest,
        layout_schema_version="1",
    )
    directory = DirectoryIdentity.model_construct(
        path_digest=digest,
        device=1,
        inode=1,
        identity_digest=digest,
    )
    return AuthorizedAttemptScope(
        execution=_context(authorization_id=authorization_id),
        workspace=TaskWorkspaceBinding(
            identity=identity,
            project_root=project,
            write_root=write,
            project_root_identity=directory,
            write_root_identity=directory,
        ),
    )


def _contract() -> AgentExecutionContract[CaseDesignInput, CaseDesignAgentResult, CaseDesignOutput]:
    return AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=(), finalize=()),
    )


@dataclass
class RecordingTask:
    contract = _contract()
    prepare_phase: PreparePhase[CaseDesignInput, CaseDesignPrepared]
    opencode: RuntimePhase[CaseDesignPrepared]
    finalize_phase: FinalizePhase[
        CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput
    ]

    @before
    async def prepare(
        self, validated_input: CaseDesignInput, scope: AuthorizedAttemptScope
    ) -> CaseDesignPrepared | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self,
        bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
        scope: AuthorizedAttemptScope,
    ) -> CaseDesignOutput | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)


def _contract_with_prepare_write_claim(
    claim: str,
) -> AgentExecutionContract[CaseDesignInput, CaseDesignAgentResult, CaseDesignOutput]:
    return AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        resources=ResourceClaims(writes=(claim,)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(
            prepare=(claim,),
            runtime=(),
            finalize=(),
        ),
    )


def _workspace(tmp_path: Path) -> ReadOnlyRawWorkspace:
    root = tmp_path / "raw-workspace"
    root.mkdir()
    (root / "qa").mkdir()
    (root / "qa" / "proposal.md").write_text("design", encoding="utf-8")
    return ReadOnlyRawWorkspace(root)


def _executor(
    *,
    prepare: RecordingPrepare,
    runtime: RecordingRuntime,
    finalize: RecordingFinalize,
    task_type: type[RecordingTask] | None = None,
) -> ResolvedRawAgentExecutor[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput]:
    return ResolvedRawAgentExecutor(
        _contract(),
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
        task_type=task_type,
    )


@pytest.mark.parametrize("task_type", (None, RecordingTask))
def test_raw_executor_runs_prepare_runtime_result_finalize_output_in_order(
    tmp_path: Path, task_type: type[RecordingTask] | None
) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/proposal.md",))
    expected_output = CaseDesignOutput(
        status="committed",
        output_files=expected_agent_result.output_files,
        path="primary",
    )
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize, task_type=task_type).execute(
            validated_input, _scope(tmp_path)
        )
    )

    assert order == ["prepare", "runtime", "finalize"]
    assert prepare.seen_input == validated_input
    assert runtime.seen_prepared == prepared_value
    assert runtime.seen_schema is None
    assert finalize.seen is not None
    assert finalize.seen.validated_input == validated_input
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert finalize.seen.run_evidence.result_digest == canonical_digest(expected_agent_result.model_dump())
    assert finalize.seen.raw_workspace.read_text("qa/proposal.md") == "design"
    assert isinstance(output, ExecutedAttemptResult)
    assert output.output == expected_output


def test_agent_task_hooks_run_on_a_fresh_instance_each_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, RecordingTask]] = []
    for name in ("prepare", "run", "finalize"):
        original = getattr(RecordingTask, name)

        @wraps(original)
        async def recorded(self: RecordingTask, *args: object, _name=name, _original=original):
            calls.append((_name, self))
            return await _original(self, *args)

        monkeypatch.setattr(RecordingTask, name, recorded)

    order: list[str] = []
    executor = _executor(
        prepare=RecordingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
        task_type=RecordingTask,
    )
    scope = _scope(tmp_path)
    for _ in range(2):
        result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), scope))
        assert isinstance(result, ExecutedAttemptResult)

    assert [name for name, _ in calls] == ["prepare", "run", "finalize"] * 2
    assert calls[0][1] is calls[1][1] is calls[2][1]
    assert calls[3][1] is calls[4][1] is calls[5][1]
    assert calls[0][1] is not calls[3][1]


def test_agent_task_contract_must_match_executor(tmp_path: Path) -> None:
    class MismatchedTask(RecordingTask):
        contract = _contract_with_prepare_write_claim("qa/wrong")

    with pytest.raises(ValueError, match="contract does not match"):
        _executor(
            prepare=RecordingPrepare(
                CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), []
            ),
            runtime=RecordingRuntime(
                CaseDesignAgentResult(output_files=()).model_dump(), _workspace(tmp_path), []
            ),
            finalize=RecordingFinalize(_success_output(), []),
            task_type=MismatchedTask,
        )


def test_finally_runs_when_runtime_raises(tmp_path: Path) -> None:
    exits: list[FinallyContext] = []

    class Task(RecordingTask):
        @finally_
        async def on_exit(self, context: FinallyContext) -> None:
            exits.append(context)

    class FailingRuntime(RecordingRuntime):
        async def execute(
            self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
        ) -> RawAgentRuntimeOutcome:
            del prepared, scope
            raise RuntimeError("runtime stopped")

    order: list[str] = []
    executor = _executor(
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), order),
        runtime=FailingRuntime({}, _workspace(tmp_path), order),
        finalize=RecordingFinalize(_success_output(), order),
        task_type=Task,
    )

    with pytest.raises(RuntimeError, match="runtime stopped"):
        asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert len(exits) == 1
    assert exits[0].mode == "execute"
    assert exits[0].last_phase == "runtime"
    assert exits[0].outcome == "exception"
    assert exits[0].error_type == "RuntimeError"
    assert "finalize" not in order


def _task_with_exit(exits: list[FinallyContext]) -> type[RecordingTask]:
    class Task(RecordingTask):
        @finally_
        async def on_exit(self, context: FinallyContext) -> None:
            exits.append(context)

    return Task


@pytest.mark.parametrize("failed_phase", ("prepare", "runtime", "finalize"))
def test_task_typed_phase_failure_still_runs_finally_once(tmp_path: Path, failed_phase: str) -> None:
    exits: list[FinallyContext] = []
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=(
            _FailingPrepare()
            if failed_phase == "prepare"
            else RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), order)
        ),
        runtime=(
            _FailingRuntime()
            if failed_phase == "runtime"
            else RecordingRuntime(
                CaseDesignAgentResult(output_files=()).model_dump(), _workspace(tmp_path), order
            )
        ),
        finalize=(
            _FailingFinalize() if failed_phase == "finalize" else RecordingFinalize(_success_output(), order)
        ),
        task_type=_task_with_exit(exits),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert len(exits) == 1
    assert exits[0].outcome == "resolution"
    assert exits[0].last_phase == failed_phase
    assert (
        executor.phase_log
        == ["prepare", "runtime", "finalize"][: ("prepare", "runtime", "finalize").index(failed_phase) + 1]
    )


@pytest.mark.parametrize("phase", ("prepare", "runtime", "finalize"))
@pytest.mark.parametrize("raised", (RuntimeError, asyncio.CancelledError))
def test_task_phase_exception_and_cancel_preserve_original(
    tmp_path: Path, phase: str, raised: type[BaseException]
) -> None:
    exits: list[FinallyContext] = []

    class RaisingPrepare(RecordingPrepare):
        async def execute(
            self, validated_input: CaseDesignInput, scope: AuthorizedAttemptScope
        ) -> CaseDesignPrepared:
            del validated_input, scope
            raise raised("stopped")

    class RaisingRuntime(RecordingRuntime):
        async def execute(
            self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
        ) -> RawAgentRuntimeOutcome:
            del prepared, scope
            raise raised("stopped")

    class RaisingFinalize(RecordingFinalize):
        async def execute(
            self,
            bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
            scope: AuthorizedAttemptScope,
        ) -> CaseDesignOutput:
            del bundle, scope
            raise raised("stopped")

    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=(RaisingPrepare if phase == "prepare" else RecordingPrepare)(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []
        ),
        runtime=(RaisingRuntime if phase == "runtime" else RecordingRuntime)(
            {"output_files": []}, _workspace(tmp_path), []
        ),
        finalize=(RaisingFinalize if phase == "finalize" else RecordingFinalize)(_success_output(), []),
        task_type=_task_with_exit(exits),
    )

    with pytest.raises(raised, match="stopped"):
        asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert len(exits) == 1
    assert exits[0].outcome == "exception"
    assert exits[0].error_type == raised.__name__
    assert (
        executor.phase_log
        == ["prepare", "runtime", "finalize"][: ("prepare", "runtime", "finalize").index(phase) + 1]
    )


@pytest.mark.parametrize("invalid", ("raw", "output"))
def test_result_validation_exception_or_resolution_runs_finally(tmp_path: Path, invalid: str) -> None:
    exits: list[FinallyContext] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []),
        runtime=RecordingRuntime(
            {"wrong": "raw"} if invalid == "raw" else {"output_files": []},
            _workspace(tmp_path),
            [],
        ),
        finalize=RecordingFinalize({"status": "missing"}, []),
        task_type=_task_with_exit(exits),
    )

    if invalid == "raw":
        result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))
        assert isinstance(result, PermanentTaskFailure)
        assert exits[0].outcome == "resolution"
    else:
        with pytest.raises(ValidationError):
            asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))
        assert exits[0].outcome == "exception"
    assert len(exits) == 1


@pytest.mark.parametrize("mutate", (False, True))
def test_finally_exception_does_not_mask_result_but_staging_change_is_rejected(
    tmp_path: Path, mutate: bool
) -> None:
    class Task(RecordingTask):
        @finally_
        async def on_exit(self, context: FinallyContext) -> None:
            if mutate:
                target = write_root / "qa/already-staged.txt"
                target.write_text("changed", encoding="utf-8")
            raise RuntimeError("observer failed")

    scope = _scope(tmp_path)
    write_root = scope.workspace.write_root
    target = write_root / "qa/already-staged.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original", encoding="utf-8")
    executor = _executor(
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []),
        runtime=RecordingRuntime({"output_files": []}, _workspace(tmp_path), []),
        finalize=RecordingFinalize(_success_output(), []),
        task_type=Task,
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), scope))

    if mutate:
        assert isinstance(result, PermanentTaskFailure)
        assert result.kind == "invalid_output"
    else:
        assert isinstance(result, ExecutedAttemptResult)


def test_finally_exception_does_not_mask_typed_failure(tmp_path: Path) -> None:
    class Task(RecordingTask):
        @finally_
        async def on_exit(self, context: FinallyContext) -> None:
            raise RuntimeError("observer failed")

    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=_FailingPrepare(),
        runtime=RecordingRuntime({}, _workspace(tmp_path), []),
        finalize=RecordingFinalize(_success_output(), []),
        task_type=Task,
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.message == "prepare rejected"


def test_raising_phase_still_audits_staging_without_success_receipt(tmp_path: Path) -> None:
    class WritingRaisingRuntime(RecordingRuntime):
        async def execute(
            self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
        ) -> RawAgentRuntimeOutcome:
            target = scope.workspace.write_root / "qa/undeclared.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"unclaimed")
            raise RuntimeError("runtime stopped")

    host = _RecordingHost()
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []),
        runtime=WritingRaisingRuntime({}, _workspace(tmp_path), []),
        finalize=RecordingFinalize(_success_output(), []),
        task_type=RecordingTask,
    ).with_host(host, graph_revision="c" * 64, product_lock_digest="d" * 64)

    with pytest.raises(RuntimeError, match="runtime stopped"):
        asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert [call["phase"] for call in host.calls] == ["prepare"]
    assert executor.phase_deltas["runtime"] == set()


def test_reconcile_uses_runtime_reconcile_without_resubmitting(tmp_path: Path) -> None:
    exits: list[FinallyContext] = []
    calls: list[str] = []

    class Task(_task_with_exit(exits)):
        async def run(
            self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
        ) -> RawAgentRuntimeOutcome:
            raise AssertionError("must not resubmit")

    class RecoveringRuntime(RecordingRuntime):
        async def execute(
            self, prepared: CaseDesignPrepared, scope: AuthorizedAttemptScope
        ) -> RawAgentRuntimeOutcome:
            raise AssertionError("must not resubmit")

        async def reconcile(
            self, prepared: CaseDesignPrepared, context: AttemptExecutionContext, snapshot: object
        ) -> RawAgentRuntimeOutcome:
            del prepared, context, snapshot
            calls.append("reconcile")
            return RawAgentRuntimeOutcome(
                run_result=_run_result({"output_files": []}),
                raw_workspace=self.workspace,
            )

    executor = _executor(
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), calls),
        runtime=RecoveringRuntime({}, _workspace(tmp_path), calls),
        finalize=RecordingFinalize(_success_output(), calls),
        task_type=Task,
    )
    host = _RecordingHost()
    bound = executor.with_host(host, graph_revision="c" * 64, product_lock_digest="d" * 64)

    result = asyncio.run(bound.reconcile(CaseDesignInput(change_id="CH-1"), _scope(tmp_path), object()))

    assert isinstance(result, ExecutedAttemptResult)
    assert calls == ["prepare", "reconcile", "finalize"]
    assert host.calls == []
    assert bound.phase_log == ["prepare", "runtime", "finalize"]
    assert len(exits) == 1
    assert exits[0].mode == "reconcile"
    assert exits[0].last_phase == "finalize"


@pytest.mark.parametrize(
    "resolution",
    (
        PermanentTaskFailure(kind="internal", message="failed"),
        RejectedTaskResult(reason="rejected"),
        PendingTaskResult(wakeup=SystemReference(reference_id="wait")),
        IndeterminateTaskResult(reconciliation=SystemReference(reference_id="unknown")),
    ),
)
def test_reconcile_propagates_all_typed_resolutions(tmp_path: Path, resolution: object) -> None:
    exits: list[FinallyContext] = []

    class ReconcilingRuntime(RecordingRuntime):
        async def reconcile(
            self, prepared: CaseDesignPrepared, context: AttemptExecutionContext, snapshot: object
        ) -> object:
            del prepared, context, snapshot
            return resolution

    executor = _executor(
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []),
        runtime=ReconcilingRuntime({}, _workspace(tmp_path), []),
        finalize=RecordingFinalize(_success_output(), []),
        task_type=_task_with_exit(exits),
    )

    result = asyncio.run(executor.reconcile(CaseDesignInput(change_id="CH-1"), _scope(tmp_path), object()))

    assert result == resolution
    assert executor.phase_log == ["prepare", "runtime"]
    assert len(exits) == 1
    assert exits[0].mode == "reconcile"
    assert exits[0].outcome == "resolution"


def test_reconcile_without_runtime_support_fails_closed(tmp_path: Path) -> None:
    exits: list[FinallyContext] = []
    executor = _executor(
        prepare=RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="cases"), []),
        runtime=RecordingRuntime({}, _workspace(tmp_path), []),
        finalize=RecordingFinalize(_success_output(), []),
        task_type=_task_with_exit(exits),
    )

    result = asyncio.run(executor.reconcile(CaseDesignInput(change_id="CH-1"), _scope(tmp_path), object()))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "internal"
    assert executor.phase_log == ["prepare"]
    assert len(exits) == 1
    assert exits[0].mode == "reconcile"
    assert exits[0].last_phase == "prepare"


@pytest.mark.parametrize("task_type", (None, RecordingTask))
def test_invalid_raw_result_fails_before_finalize(
    tmp_path: Path, task_type: type[RecordingTask] | None
) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    order: list[str] = []
    prepare = RecordingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
    )
    runtime = RecordingRuntime({"not": "an-agent-result"}, _workspace(tmp_path), order)
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/proposal.md",), path="primary"),
        order,
    )

    result = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize, task_type=task_type).execute(
            validated_input, _scope(tmp_path)
        )
    )

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert "agent result" in result.message
    assert order == ["prepare", "runtime"]
    assert finalize.seen is None


@pytest.mark.parametrize("task_type", (None, RecordingTask))
def test_invalid_output_fails_after_finalize(tmp_path: Path, task_type: type[RecordingTask] | None) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/proposal.md",))
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize({"status": "missing-required-fields"}, order)

    with pytest.raises(ValidationError):
        asyncio.run(
            _executor(prepare=prepare, runtime=runtime, finalize=finalize, task_type=task_type).execute(
                validated_input, _scope(tmp_path)
            )
        )

    assert order == ["prepare", "runtime", "finalize"]
    assert finalize.seen is not None


def test_raw_workspace_is_read_only(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert workspace.read_bytes("qa/proposal.md") == b"design"
    with pytest.raises(AttributeError):
        workspace.write_text("qa/proposal.md", "mutated")  # type: ignore[attr-defined]
    assert not hasattr(workspace, "write_bytes")
    assert workspace.read_text("qa/proposal.md") == "design"


def test_raw_executor_resolves_single_graph_facing_task_contract(tmp_path: Path) -> None:
    order: list[str] = []
    prepare = RecordingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
    )
    runtime = RecordingRuntime(
        CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
        _workspace(tmp_path),
        order,
    )
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/proposal.md",), path="primary"),
        order,
    )
    executor = _executor(prepare=prepare, runtime=runtime, finalize=finalize)

    resolved = executor.resolve()
    again = resolve_contract(executor.to_task_contract(), executor=executor)

    assert resolved.contract_digest == again.contract_digest
    assert "executor" not in resolved.canonical_projection()
    assert resolved.executor is executor
    assert resolved.contract.input_model is CaseDesignInput
    assert resolved.contract.output_model is CaseDesignOutput
    assert resolved.contract.handler_id == "assurance.intake.case-design.prepare"
    assert not hasattr(executor, "negotiate_provider_schema")


def test_case_design_raw_attempt_preserves_both_legacy_prepare_consumers(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="primary case-design")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/proposal.md", "qa/.qa.yaml"))
    expected_output = CaseDesignOutput(
        status="committed",
        output_files=expected_agent_result.output_files,
        path="primary",
    )
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
            validated_input, _scope(tmp_path)
        )
    )

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert isinstance(output, ExecutedAttemptResult)
    assert output.output == expected_output


def test_case_design_repair_raw_attempt_preserves_both_legacy_prepare_consumers(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="repair")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="repair", prompt="repair case-design")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/cases/api/case.yaml",))
    expected_output = CaseDesignOutput(
        status="repaired",
        output_files=expected_agent_result.output_files,
        path="repair",
    )
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
            validated_input, _scope(tmp_path)
        )
    )

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert finalize.seen.validated_input.path == "repair"
    assert isinstance(output, ExecutedAttemptResult)
    assert output.output == expected_output


class _WritingPrepare(RecordingPrepare):
    def __init__(
        self,
        prepared: CaseDesignPrepared,
        order: list[str],
        write_relative: str,
        payload: bytes,
    ) -> None:
        super().__init__(prepared, order)
        self.write_relative = write_relative
        self.payload = payload

    async def execute(
        self,
        validated_input: CaseDesignInput,
        scope: AuthorizedAttemptScope,
    ) -> CaseDesignPrepared:
        target = scope.workspace.write_root.joinpath(*self.write_relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return await super().execute(validated_input, scope)


class _WritingRuntime(RecordingRuntime):
    def __init__(
        self,
        result: object,
        workspace: ReadOnlyRawWorkspace,
        order: list[str],
        write_relative: str,
        payload: bytes,
        write_root: Path,
    ) -> None:
        super().__init__(result, workspace, order)
        self.write_relative = write_relative
        self.payload = payload
        self.write_root = write_root

    async def execute(
        self,
        prepared: CaseDesignPrepared,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome:
        target = scope.workspace.write_root.joinpath(*self.write_relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return await super().execute(prepared, scope)


class _WritingFinalize(RecordingFinalize):
    def __init__(
        self,
        output: object,
        order: list[str],
        write_relative: str,
        payload: bytes,
    ) -> None:
        super().__init__(output, order)
        self.write_relative = write_relative
        self.payload = payload

    async def execute(
        self,
        bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
        scope: AuthorizedAttemptScope,
    ) -> CaseDesignOutput:
        target = scope.workspace.write_root.joinpath(*self.write_relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return await super().execute(bundle, scope)


class _RawExecutorFixture:
    def __init__(
        self,
        executor: ResolvedRawAgentExecutor[
            CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput
        ],
        claims: object,
        validated_input: CaseDesignInput,
        scope: AuthorizedAttemptScope,
    ) -> None:
        self.executor = executor
        self.claims = claims
        self.validated_input = validated_input
        self.scope = scope
        self.phase_log: list[str] = []
        self.prepare_delta: set[str] = set()
        self.runtime_delta: set[str] = set()
        self.finalize_delta: set[str] = set()

    async def execute(self) -> ExecutorStepResult[CaseDesignOutput]:
        result = await self.executor.execute(self.validated_input, self.scope)
        self.phase_log = list(self.executor.phase_log)
        deltas = self.executor.phase_deltas
        self.prepare_delta = set(deltas["prepare"])
        self.runtime_delta = set(deltas["runtime"])
        self.finalize_delta = set(deltas["finalize"])
        return result


@pytest.fixture
def raw_executor_fixture(tmp_path: Path, request: pytest.FixtureRequest) -> _RawExecutorFixture:
    from agent_runtime_contracts import AgentPhaseWriteClaims

    claims = AgentPhaseWriteClaims(
        prepare=("qa/prepare.txt",),
        runtime=("qa/runtime.txt",),
        finalize=("qa/finalize.txt",),
    )
    from graph_engine.plugin_api import ResourceClaims

    contract = AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        resources=ResourceClaims(writes=(*claims.prepare, *claims.runtime, *claims.finalize)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=claims,
    )
    scope = _scope(tmp_path)
    order: list[str] = []
    prepare = _WritingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
        order,
        "qa/prepare.txt",
        b"prepare\n",
    )
    runtime = _WritingRuntime(
        CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
        _workspace(tmp_path),
        order,
        "qa/runtime.txt",
        b"runtime\n",
        scope.workspace.write_root,
    )
    finalize = _WritingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/proposal.md",), path="primary"),
        order,
        "qa/finalize.txt",
        b"finalize\n",
    )
    claimed_contract = contract

    class ClaimedTask(RecordingTask):
        contract = claimed_contract

    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
        task_type=ClaimedTask if getattr(request, "param", False) else None,
    )
    return _RawExecutorFixture(
        executor,
        claims,
        CaseDesignInput(change_id="CH-1", path="primary"),
        scope,
    )


def test_raw_executor_uses_three_disjoint_staging_phases(raw_executor_fixture) -> None:
    result = asyncio.run(raw_executor_fixture.execute())
    assert raw_executor_fixture.phase_log == ["prepare", "runtime", "finalize"]
    assert isinstance(result, ExecutedAttemptResult)
    assert raw_executor_fixture.prepare_delta <= set(raw_executor_fixture.claims.prepare)
    assert raw_executor_fixture.runtime_delta <= set(raw_executor_fixture.claims.runtime)
    assert raw_executor_fixture.finalize_delta <= set(raw_executor_fixture.claims.finalize)
    assert not (
        raw_executor_fixture.prepare_delta & raw_executor_fixture.runtime_delta
        | raw_executor_fixture.prepare_delta & raw_executor_fixture.finalize_delta
        | raw_executor_fixture.runtime_delta & raw_executor_fixture.finalize_delta
    )


def test_phase_write_claims_cover_descendant_files() -> None:
    from agent_runtime_contracts.attempt_executor import _covered_by_claims

    claims = {"qa/tests"}
    assert _covered_by_claims("qa/tests/test_a.py", claims)
    assert not _covered_by_claims("qa/cases/api/case.yaml", claims)


@pytest.mark.parametrize("when", ("before", "runtime"))
@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_phase_file_returns_typed_failure(tmp_path: Path, when: str) -> None:
    scope = _scope(tmp_path)
    target = scope.workspace.write_root / "qa/unreadable.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"unreadable\n")
    if when == "before":
        target.chmod(0)

    class UnreadableRuntime:
        async def execute(self, prepared: object, scope: AuthorizedAttemptScope) -> PermanentTaskFailure:
            target.chmod(0)
            return PermanentTaskFailure(kind="invalid_output", message="runtime rejected")

    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=RecordingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), []
        ),
        runtime=UnreadableRuntime(),
        finalize=RecordingFinalize(_success_output(), []),
    )
    try:
        result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), scope))
        assert isinstance(result, PermanentTaskFailure)
        assert result.kind == "invalid_output"
        assert "snapshot" in result.message
    finally:
        target.chmod(0o600)


@pytest.mark.parametrize("raw_executor_fixture", (False, True), indirect=True)
def test_raw_executor_persists_phase_deltas_into_host_receipt(
    raw_executor_fixture: _RawExecutorFixture,
) -> None:
    host = _RecordingHost()
    raw_executor_fixture.scope = _scope(
        raw_executor_fixture.scope.workspace.project_root.parent,
        authorization_id="b" * 64,
    )
    raw_executor_fixture.scope.workspace.write_root.mkdir(parents=True, exist_ok=True)
    executor = raw_executor_fixture.executor.with_host(
        host,
        graph_revision="c" * 64,
        product_lock_digest="d" * 64,
    )
    raw_executor_fixture.executor = executor
    result = asyncio.run(raw_executor_fixture.execute())
    assert isinstance(result, ExecutedAttemptResult)
    assert result.source_terminal_receipt is not None
    assert [call["phase"] for call in host.calls] == ["prepare", "runtime", "finalize"]
    assert host.calls[0]["staged_paths"] == tuple(sorted(raw_executor_fixture.prepare_delta))
    assert host.calls[1]["staged_paths"] == tuple(sorted(raw_executor_fixture.runtime_delta))
    assert host.calls[2]["staged_paths"] == tuple(sorted(raw_executor_fixture.finalize_delta))
    runtime_paths = host.calls[1]["staged_paths"]
    assert isinstance(runtime_paths, tuple)
    assert result.source_terminal_receipt.identity_digest == canonical_digest(
        {"phase": "runtime", "paths": list(runtime_paths)}
    )


def test_raw_workspace_hides_public_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert not hasattr(workspace, "root")
    assert workspace.read_text("qa/proposal.md") == "design"


class _FailingPrepare:
    async def execute(
        self,
        validated_input: CaseDesignInput,
        scope: AuthorizedAttemptScope,
    ) -> PermanentTaskFailure:
        del validated_input, scope
        return PermanentTaskFailure(kind="invalid_output", message="prepare rejected")


class _FailingRuntime:
    async def execute(
        self,
        prepared: CaseDesignPrepared,
        scope: AuthorizedAttemptScope,
    ) -> PermanentTaskFailure:
        del prepared, scope
        return PermanentTaskFailure(kind="invalid_output", message="runtime rejected")


class _FailingFinalize:
    async def execute(
        self,
        bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
        scope: AuthorizedAttemptScope,
    ) -> PermanentTaskFailure:
        del bundle, scope
        return PermanentTaskFailure(kind="invalid_output", message="finalize rejected")


def _success_output() -> CaseDesignOutput:
    return CaseDesignOutput(status="committed", output_files=("qa/proposal.md",), path="primary")


def test_prepare_phase_failure_is_returned_not_raised(tmp_path: Path) -> None:
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=_FailingPrepare(),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "prepare rejected"
    assert order == []


def test_runtime_phase_failure_is_returned_not_raised(tmp_path: Path) -> None:
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=RecordingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
            order,
        ),
        runtime=_FailingRuntime(),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "runtime rejected"
    assert order == ["prepare"]


def test_finalize_phase_failure_is_returned_not_raised(tmp_path: Path) -> None:
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=RecordingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
            order,
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=_FailingFinalize(),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "finalize rejected"
    assert order == ["prepare", "runtime"]


@pytest.mark.parametrize("existing", (False, True))
def test_undeclared_phase_write_returns_typed_failure(tmp_path: Path, existing: bool) -> None:
    scope = _scope(tmp_path)
    if existing:
        target = scope.workspace.write_root / "qa/undeclared.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original\n")
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=_WritingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
            order,
            "qa/undeclared.txt",
            b"secret\n",
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=("qa/proposal.md",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), scope))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert "undeclared" in result.message
    assert order == ["prepare"]


def test_task_hook_undeclared_write_is_rejected(tmp_path: Path) -> None:
    class WritingTask(RecordingTask):
        @before
        async def prepare(
            self, validated_input: CaseDesignInput, scope: AuthorizedAttemptScope
        ) -> CaseDesignPrepared | PermanentTaskFailure:
            target = scope.workspace.write_root / "qa/undeclared.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"unclaimed")
            return await super().prepare(validated_input, scope)

    order: list[str] = []
    executor = _executor(
        prepare=RecordingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=()).model_dump(), _workspace(tmp_path), order
        ),
        finalize=RecordingFinalize(_success_output(), order),
        task_type=WritingTask,
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert "undeclared" in result.message
    assert order == ["prepare"]


def test_directory_phase_write_claim_allows_nested_file(tmp_path: Path) -> None:
    order: list[str] = []
    claim = "qa/results/files"
    executor = ResolvedRawAgentExecutor(
        _contract_with_prepare_write_claim(claim),
        prepare=_WritingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
            order,
            f"{claim}/api/generated.json",
            b"{}\n",
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=(f"{claim}/api/generated.json",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, ExecutedAttemptResult)
    assert order == ["prepare", "runtime", "finalize"]


def test_directory_phase_write_claim_rejects_sibling_prefix(tmp_path: Path) -> None:
    order: list[str] = []
    claim = "qa/results/files"
    sibling_path = "qa/results/files-evil/escape.json"
    executor = ResolvedRawAgentExecutor(
        _contract_with_prepare_write_claim(claim),
        prepare=_WritingPrepare(
            CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"),
            order,
            sibling_path,
            b"{}\n",
        ),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=(f"{claim}/api/generated.json",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert sibling_path in result.message
    assert order == ["prepare"]


def test_lifecycle_decorator_preserves_callable_and_marks_role() -> None:
    from agent_runtime_contracts import before

    async def prepare(self: object, value: object, scope: object) -> object:
        return value

    decorated = before(prepare)
    assert decorated is prepare
    assert getattr(decorated, "__agent_lifecycle_phase__") == "before"
    assert asyncio.iscoroutinefunction(decorated)


def test_lifecycle_decorator_rejects_sync_function() -> None:
    from agent_runtime_contracts import before

    def prepare(self: object, value: object, scope: object) -> object:
        return value

    with pytest.raises(TypeError, match="async"):
        before(prepare)


def test_lifecycle_validation_accepts_fixed_async_methods_without_exit() -> None:
    from agent_runtime_contracts import after, before
    from agent_runtime_contracts.lifecycle import validate_task_type

    class Task:
        @before
        async def prepare(self, value: object, scope: object) -> object:
            return value

        async def run(self, value: object, scope: object) -> object:
            return value

        @after
        async def finalize(self, value: object, scope: object) -> object:
            return value

    validate_task_type(Task)


@pytest.mark.parametrize("invalid", ["missing-run", "wrong-mark", "missing-scope", "extra-mark"])
def test_lifecycle_validation_rejects_invalid_task(invalid: str) -> None:
    from agent_runtime_contracts import after, before
    from agent_runtime_contracts.lifecycle import validate_task_type

    class Task:
        @before
        async def prepare(self, value: object, scope: object) -> object:
            return value

        async def run(self, value: object, scope: object) -> object:
            return value

        @after
        async def finalize(self, value: object, scope: object) -> object:
            return value

    if invalid == "missing-run":
        del Task.run
    elif invalid == "wrong-mark":
        Task.finalize.__agent_lifecycle_phase__ = "before"  # type: ignore[attr-defined]
    elif invalid == "missing-scope":

        async def run_without_scope(self: object, value: object) -> object:
            return value

        Task.run = run_without_scope  # type: ignore[assignment]
    else:

        @before
        async def extra(self: object, value: object, scope: object) -> object:
            return value

        Task.extra = extra  # type: ignore[attr-defined]

    with pytest.raises(TypeError):
        validate_task_type(Task)


def test_finally_context_is_read_only() -> None:
    from dataclasses import FrozenInstanceError

    from agent_runtime_contracts import FinallyContext

    context = FinallyContext(
        contract_id="assurance.intake.agent.case-design.v1",
        attempt_key="a" * 64,
        mode="execute",
        last_phase="runtime",
        outcome="resolution",
    )
    with pytest.raises(FrozenInstanceError):
        context.outcome = "executed"  # type: ignore[misc]
