"""The runtime-evidence port is injected only for contracts that declare it."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from graph_engine.attempts import RUNTIME_EVIDENCE
from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.orchestration.kernel import AssuranceAttemptKernel
from graph_engine.attempts.models.keys import BusinessActivation, derive_attempt_key
from graph_engine.attempts.models.resolutions import CommittedTaskResult, PermanentTaskFailure
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiter
from graph_engine.attempts.resources.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import ResourceClaims


class _Input(BaseModel):
    change_id: str


class _Output(BaseModel):
    status: str


class _Source:
    def __init__(self, document: JSONValue) -> None:
        self.document = document
        self.calls: list[tuple[str, str]] = []

    async def project(
        self,
        *,
        invocation_id: str,
        exclude_attempt_key_digest: str,
    ) -> JSONValue:
        self.calls.append((invocation_id, exclude_attempt_key_digest))
        return self.document


class _Reader:
    def __init__(self) -> None:
        self.document: JSONValue = None
        self.denied = False
        self.calls = 0

    async def execute(
        self, validated_input: _Input, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_Output]:
        del validated_input
        self.calls += 1
        try:
            self.document = await scope.read_runtime_evidence()
        except GraphEngineError:
            self.denied = True
        (scope.workspace.write_root / "out.txt").write_bytes(b"ok")
        return ExecutedAttemptResult(output=_Output(status="ok"))


def _contract(*, capabilities: tuple[str, ...] = ()) -> TaskAttemptContract[_Input, _Output]:
    return TaskAttemptContract(
        contract_id="assurance.execution.run.v1",
        owner_id="assurance.execution",
        handler_id="assurance.execution.run",
        input_model=_Input,
        output_model=_Output,
        resources=ResourceClaims(writes=("out.txt",)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        capabilities=capabilities,
    )


async def _run(
    tmp_path: Path,
    *,
    capabilities: tuple[str, ...] = (),
    source: _Source | None = None,
) -> tuple[object, _Reader, str]:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    reader = _Reader()
    resolved = resolve_contract(_contract(capabilities=capabilities), executor=reader)
    kernel = AssuranceAttemptKernel(
        checkpoints=MemoryAttemptCheckpointStore(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=canonical_digest({"revision": "runtime-evidence"}),
        runtime_evidence=source,
    )
    validated = _Input(change_id="chg-1")
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision=canonical_digest({"revision": "runtime-evidence"}),
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=key,
        fencing_token=4,
    )
    try:
        result = await kernel.execute_or_recover(key, resolved, validated, context)
    finally:
        store.close()
    return result, reader, key.digest


def test_an_empty_capability_list_stays_out_of_the_contract_digest() -> None:
    plain = _contract().canonical_projection()
    declared = _contract(capabilities=(RUNTIME_EVIDENCE,)).canonical_projection()
    assert "capabilities" not in plain
    assert declared["capabilities"] == [RUNTIME_EVIDENCE]


async def test_an_undeclared_contract_cannot_read_runtime_evidence(tmp_path: Path) -> None:
    source = _Source({"entries": []})
    result, reader, _key = await _run(tmp_path, source=source)
    assert isinstance(result, CommittedTaskResult)
    assert reader.denied is True
    assert reader.document is None
    assert source.calls == []


async def test_a_declared_contract_reads_only_the_host_document(tmp_path: Path) -> None:
    source = _Source({"schema_version": "1", "entries": []})
    result, reader, attempt_key = await _run(
        tmp_path,
        capabilities=(RUNTIME_EVIDENCE,),
        source=source,
    )
    assert isinstance(result, CommittedTaskResult)
    assert reader.denied is False
    assert reader.document == {"schema_version": "1", "entries": []}
    assert source.calls == [("inv-1", attempt_key)]


async def test_a_declared_contract_fails_when_the_host_has_no_port(tmp_path: Path) -> None:
    result, reader, _key = await _run(tmp_path, capabilities=(RUNTIME_EVIDENCE,))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "configuration"
    assert result.message == "runtime evidence port is not configured"
    assert reader.calls == 0
