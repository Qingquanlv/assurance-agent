from __future__ import annotations

import asyncio
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
    PermanentTaskFailure,
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
    canonical_digest,
)
from agent_runtime_contracts.attempt_executor import (
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
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
) -> ResolvedRawAgentExecutor[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput]:
    return ResolvedRawAgentExecutor(
        _contract(),
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )


def test_raw_executor_runs_prepare_runtime_result_finalize_output_in_order(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",))
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


def test_invalid_raw_result_fails_before_finalize(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    order: list[str] = []
    prepare = RecordingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
    )
    runtime = RecordingRuntime({"not": "an-agent-result"}, _workspace(tmp_path), order)
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary"),
        order,
    )

    with pytest.raises((ValidationError, ValueError)):
        asyncio.run(
            _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
                validated_input, _scope(tmp_path)
            )
        )

    assert order == ["prepare", "runtime"]
    assert finalize.seen is None


def test_invalid_output_fails_after_finalize(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",))
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize({"status": "missing-required-fields"}, order)

    with pytest.raises(ValidationError):
        asyncio.run(
            _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
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
        CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
        _workspace(tmp_path),
        order,
    )
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary"),
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
    expected_agent_result = CaseDesignAgentResult(
        output_files=("qa/changes/CH-1/proposal.md", "qa/changes/CH-1/.qa.yaml")
    )
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
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/cases/api/case.yaml",))
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
def raw_executor_fixture(tmp_path: Path) -> _RawExecutorFixture:
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
        CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
        _workspace(tmp_path),
        order,
        "qa/runtime.txt",
        b"runtime\n",
        scope.workspace.write_root,
    )
    finalize = _WritingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary"),
        order,
        "qa/finalize.txt",
        b"finalize\n",
    )
    executor = ResolvedRawAgentExecutor(contract, prepare=prepare, runtime=runtime, finalize=finalize)
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
    return CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary")


def test_prepare_phase_failure_is_returned_not_raised(tmp_path: Path) -> None:
    order: list[str] = []
    executor = ResolvedRawAgentExecutor(
        _contract(),
        prepare=_FailingPrepare(),
        runtime=RecordingRuntime(
            CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
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
            CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
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


def test_undeclared_phase_write_returns_typed_failure(tmp_path: Path) -> None:
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
            CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
            _workspace(tmp_path),
            order,
        ),
        finalize=RecordingFinalize(_success_output(), order),
    )

    result = asyncio.run(executor.execute(CaseDesignInput(change_id="CH-1"), _scope(tmp_path)))

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert "undeclared" in result.message
    assert order == ["prepare"]
