from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from pydantic import BaseModel

from agent_runtime_contracts import RawAgentRuntimeOutcome, ResolvedRawAgentExecutor
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_execution.contracts.execution import ExecutionManifest
from assurance_execution.contracts.selection import SelectedTargets
from assurance_execution.graphs.factory import ExecutionGraphs, build_execution_graphs
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract, resolve_contract
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import (
    PendingTaskResult,
    ReceiptRef,
    RejectedTaskResult,
    SystemReference,
)
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import (
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedWriteSet,
    TaskWorkspaceBinding,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.testing import GraphHarness, RecordingCapabilityBuildContext, committed

_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
_RUN_ID = "assurance.execution.agent.run.v1"


def bundle_fields(bundle: ExecutionGraphs) -> tuple[str, ...]:
    return tuple(item.name for item in fields(bundle))


def _mapping() -> dict[str, object]:
    return {
        "selected": ["tests/a.py"],
        "mappings": [
            {
                "test": "tests/a.py",
                "case_id": "TC_A",
                "capability": "entities.item.create",
                "layer": "api",
            }
        ],
    }


def execution_graph_input(
    *,
    change_id: str = "CH-DEMO-001",
    activation: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": change_id,
        "batch_id": "20260822T000000Z",
        "selected_test_families": ["api"],
        "capability_leafs": ["entities.item.create"],
        "case_ids": ["TC_A"],
        "artifact_paths": [],
        "mapping": _mapping(),
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "rounds_budget": 2,
        "rounds_used": 0,
    }
    if activation is not None:
        payload["activation"] = activation
    return payload


def execution_manifest(*, change_id: str = "CH-DEMO-001") -> ExecutionManifest:
    return ExecutionManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id="20260822T000000Z",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        result_files={"tests/a.py": _SHA},
        final_status="PASS",
    )


def execution_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


@pytest.fixture
def recording_context() -> RecordingCapabilityBuildContext:
    return GraphHarness().recording_context(
        owner_id="assurance.execution",
        contracts=execution_contracts(),
    )


def test_execution_factory_exports_execute_and_rerun(recording_context) -> None:
    bundle = build_execution_graphs(recording_context)
    assert bundle_fields(bundle) == ("execute", "rerun")
    assert recording_context.bound_contract_ids == (
        "assurance.execution.agent.execute.v1",
        "assurance.execution.agent.run.v1",
    )
    assert recording_context.compiled_subgraph_checkpointers == (None, None)


async def test_execute_and_rerun_publish_typed_public_output() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.execution",
        contracts=execution_contracts(),
    )
    bundle = build_execution_graphs(context)
    output = execution_manifest()
    execute = await harness.run(
        bundle.execute,
        input=execution_graph_input(),
        script={"execution.execute": [committed(output, _RECEIPT)]},
    )
    assert execute.published_update == {
        "rounds_budget": 2,
        "rounds_used": 0,
        "status": "passed",
    }
    assert execute.terminal is not None
    assert execute.promotion_decision == "committed"
    assert execute.interrupt_envelope is None

    rerun = await harness.run(
        bundle.rerun,
        input=execution_graph_input(activation={"kind": "round", "value": "0"}),
        script={"execution.run": [committed(output, _RECEIPT)]},
    )
    assert rerun.published_update == {
        "rounds_budget": 2,
        "rounds_used": 0,
        "status": "passed",
    }
    assert rerun.promotion_decision == "committed"

    rejected = await harness.run(
        bundle.execute,
        input=execution_graph_input(),
        script={"execution.execute": [RejectedTaskResult(reason="policy rejected")]},
    )
    assert rejected.promotion_decision == "rejected"
    assert rejected.published_update is None
    assert rejected.terminal is not None
    assert cast(dict[str, object], rejected.terminal).get("attempt_failure") == {
        "resolution_kind": "rejected",
        "reason": "policy rejected",
        "writes_promoted": False,
    }

    pending = await harness.run(
        bundle.execute,
        input=execution_graph_input(),
        script={"execution.execute": [PendingTaskResult(wakeup=SystemReference(reference_id="wake-1"))]},
    )
    assert pending.promotion_decision == "pending"
    assert pending.terminal is None
    assert pending.interrupt_envelope is not None


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None
        self.prepare_calls = 0
        self.promote_calls = 0

    async def open_or_create(self, attempt_key: AttemptKey, claims: ResourceClaims) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims)
        return self.binding

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        return await self.inner.seal(binding)

    async def prepare(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        self.prepare_calls += 1
        return await self.inner.prepare(binding, sealed)

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        self.promote_calls += 1
        return await self.inner.promote(prepared)

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        return await self.inner.recover_promotion(prepared)


class _WritingExecutor:
    def __init__(self, workspace: _RecordingWorkspace, output: ExecutionManifest) -> None:
        self.workspace = workspace
        self.output = output
        self.calls = 0

    async def execute(self, validated_input: BaseModel, context: object) -> ExecutionManifest:
        del validated_input, context
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        target = binding.write_root / "tests" / "a.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        return self.output


class _DeferredPhase:
    async def execute(self, prepared: object, context: object) -> RawAgentRuntimeOutcome:
        raise RuntimeError("semantic attempt phase is not driven")


def _boot_resolved_execute() -> ResolvedAttemptContract[Any, Any]:
    agent = AGENT_JOB_CONTRACTS["execute"]
    return ResolvedRawAgentExecutor(
        agent,
        prepare=cast(Any, _DeferredPhase()),
        runtime=_DeferredPhase(),
        finalize=cast(Any, _DeferredPhase()),
    ).resolve()


def _revision() -> str:
    return canonical_digest({"revision": "execution-graph"})


def _invoke_config(*, entrypoint: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": _revision(),
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": entrypoint,
        }
    }


async def test_execution_graph_replays_committed_attempt_without_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    output = execution_manifest()
    writer = _WritingExecutor(workspace, output)
    core = _boot_resolved_execute()
    writable = ResourceClaims(writes=("tests",))
    execute_contract = resolve_contract(
        replace(core.contract, resources=writable),
        executor=writer,
    )
    run_contract = resolve_contract(
        replace(AGENT_JOB_CONTRACTS["run"].to_task_contract(), resources=writable),
        executor=writer,
    )
    journal = MemoryAttemptJournal()
    cuts = ["after_promotion_before_receipt"]

    def transaction_cut(name: str) -> None:
        if cuts and name == cuts[0]:
            cuts.pop(0)
            raise RuntimeError("crash after promotion")

    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=_revision(),
        transaction_cut=transaction_cut,
    )
    factory = AttemptNodeFactory(journal=journal, kernel=kernel)
    context = RecordingCapabilityBuildContext(
        owner_id="assurance.execution",
        contracts={
            execute_contract.contract.contract_id: execute_contract,
            run_contract.contract.contract_id: run_contract,
        },
        attempt_factory=factory,
    )
    try:
        bundle = build_execution_graphs(context)
        payload = execution_graph_input()
        with pytest.raises(RuntimeError, match="crash after promotion"):
            await bundle.execute.ainvoke(payload, config=_invoke_config(entrypoint="execute"))
        result = await bundle.execute.ainvoke(payload, config=_invoke_config(entrypoint="execute"))
        assert result["status"] == "passed"
        assert result["rounds_budget"] == 2
        assert writer.calls == 1
        assert workspace.promote_calls == 1
        assert (project / "tests" / "a.py").is_file()

        first_rerun = execution_graph_input(activation={"kind": "round", "value": "0"})
        second_rerun = execution_graph_input(activation={"kind": "round", "value": "1"})
        assert first_rerun["mapping"] == second_rerun["mapping"]
        await bundle.rerun.ainvoke(first_rerun, config=_invoke_config(entrypoint="rerun"))
        await bundle.rerun.ainvoke(second_rerun, config=_invoke_config(entrypoint="rerun"))
        assert writer.calls == 3

        selected = AGENT_JOB_CONTRACTS["run"].input_model.model_validate(
            {key: first_rerun[key] for key in AGENT_JOB_CONTRACTS["run"].input_model.model_fields}
        )
        first_key = derive_attempt_key(
            invocation_id="inv-1",
            graph_revision=_revision(),
            public_entrypoint="rerun",
            semantic_node_id="execution.run",
            business_activation=BusinessActivation.for_round(0),
            contract_id=_RUN_ID,
            validated_input=selected,
        )
        second_key = derive_attempt_key(
            invocation_id="inv-1",
            graph_revision=_revision(),
            public_entrypoint="rerun",
            semantic_node_id="execution.run",
            business_activation=BusinessActivation.for_round(1),
            contract_id=_RUN_ID,
            validated_input=selected,
        )
        assert first_key != second_key
    finally:
        store.close()
