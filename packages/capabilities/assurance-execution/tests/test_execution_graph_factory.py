from __future__ import annotations

from dataclasses import fields, replace
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from pydantic import BaseModel

from agent_runtime_contracts import RawAgentRuntimeOutcome, ResolvedRawAgentExecutor
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.execution import ExecutionManifest
from assurance_execution.contracts.selection import SelectedTargets
from assurance_execution.graphs.factory import ExecutionGraphs, build_execution_graphs
from assurance_execution.graphs.nodes import activation_execute, activation_rerun, publish_execution
from assurance_execution.operations.agent_skills import assemble_execution_input
from assurance_execution.operations.common import InputError
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
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
        payload["repair_round"] = int(activation["value"])
    return payload


def execution_evidence(*, change_id: str = "CH-DEMO-001", status: str = "passed") -> ExecutionEvidenceV1:
    return ExecutionEvidenceV1.model_validate(
        {
            "schema_version": "1",
            "status": status,
            "change_id": change_id,
            "batch_id": "20260822T000000Z",
            "executed_at": "2026-08-22T00:00:00Z",
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "mapping": _mapping(),
            "mapping_digest": _SHA,
            "baseline_tree_id": "b" * 64,
            "runner_profile_digest": "c" * 64,
            "receipt_digest": "d" * 64,
            "receipt": {
                "commands": [
                    {
                        "family": "api",
                        **{
                            "command": ["pytest", "tests/a.py"],
                            "exit_code": 0 if status == "passed" else 1,
                            "collected": 1,
                            "passed": 1 if status == "passed" else 0,
                            "failed": 0 if status == "passed" else 1,
                            "skipped": 0,
                        },
                    }
                ]
            },
            "results": [
                {
                    "test": "tests/a.py",
                    "status": status,
                    "duration_ms": 1,
                    "case_id": "TC_A",
                }
            ],
        }
    )


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


def generation_result() -> dict[str, object]:
    ref = lambda path: {"path": path, "digest": _SHA}  # noqa: E731
    reviewed = {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "preparation_refs": [ref("qa/changes/CH-DEMO-001/requirement.md")],
        "case_refs": [ref("qa/changes/CH-DEMO-001/cases/items/case.yaml")],
        "review_ref": ref("qa/changes/CH-DEMO-001/review/case-review.json"),
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "reviewed_case": reviewed,
        "mapping_ref": ref("qa/changes/CH-DEMO-001/codegen/closed-mapping.json"),
        "source_refs": [ref("qa/changes/CH-DEMO-001/generated/api/files/tests/a.py")],
        "plan_refs": [ref("qa/changes/CH-DEMO-001/plans/api-plan.md")],
    }


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


def test_execute_and_rerun_activations_bind_epoch_and_repair_round() -> None:
    assert activation_execute({"coverage_epoch": 0}) != activation_execute({"coverage_epoch": 1})
    assert activation_rerun({"coverage_epoch": 1, "repair_round": 0}) != activation_rerun(
        {"coverage_epoch": 1, "repair_round": 1}
    )


async def test_execute_and_rerun_publish_typed_public_output() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.execution",
        contracts=execution_contracts(),
    )
    bundle = build_execution_graphs(context)
    output = execution_evidence()
    execute = await harness.run(
        bundle.execute,
        input=execution_graph_input(),
        script={"execution.execute": [committed(output, _RECEIPT)]},
    )
    assert execute.published_update == {
        "batch_id": output.batch_id,
        "execution_evidence": output.model_dump(mode="json"),
        "execution_digest": canonical_digest(output.model_dump(mode="json")),
        "execution_semantic_node_id": "execution.execute",
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
        "batch_id": output.batch_id,
        "execution_evidence": output.model_dump(mode="json"),
        "execution_digest": canonical_digest(output.model_dump(mode="json")),
        "execution_semantic_node_id": "execution.run",
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


@pytest.mark.parametrize("bad_status", [None, "", "unknown", "PASS_WITH_WARNINGS"])
def test_execution_result_rejects_unknown_final_status(bad_status: object) -> None:
    with pytest.raises(ValueError, match="final_status"):
        publish_execution(
            {"rounds_budget": 1, "rounds_used": 0},
            {"final_status": bad_status},
            None,
        )


def test_failed_execution_publishes_committed_versioned_evidence() -> None:
    published = publish_execution(
        {
            "coverage_epoch": 2,
            "repair_round": 1,
            "rounds_budget": 2,
            "rounds_used": 1,
            "generation_result": generation_result(),
        },
        execution_evidence(status="failed"),
        _RECEIPT,
    )
    assert published["status"] == "failed"
    result = published["execution_result"]
    assert isinstance(result, dict)
    assert result["coverage_epoch"] == 2
    assert result["repair_round"] == 1
    assert result["final_status"] == "FAIL"
    assert result["receipt"] == _RECEIPT.model_dump(mode="json")


def test_execution_prepare_rejects_replaced_generation_source(tmp_path: Path) -> None:
    mapping_path = "qa/changes/CH-DEMO-001/codegen/closed-mapping.json"
    source_path = "qa/changes/CH-DEMO-001/generated/api/files/tests/a.py"
    mapping_bytes = b"{}"
    source_bytes = b"original"
    for relative, content in ((mapping_path, mapping_bytes), (source_path, b"replaced")):
        path = tmp_path.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    generation = generation_result()
    generation["mapping_ref"] = {
        "path": mapping_path,
        "digest": hashlib.sha256(mapping_bytes).hexdigest(),
    }
    generation["source_refs"] = [{"path": source_path, "digest": hashlib.sha256(source_bytes).hexdigest()}]
    with pytest.raises(InputError, match="source digest changed"):
        assemble_execution_input(
            {
                "change_id": "CH-DEMO-001",
                "selected_test_families": ["api"],
                "capability_leafs": ["entities.item.create"],
                "coverage_epoch": 2,
                "generation_result": generation,
            },
            workspace=tmp_path,
            write_root=tmp_path / ".stage",
            model=AGENT_JOB_CONTRACTS["execute"].input_model,
        )


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
    def __init__(self, workspace: _RecordingWorkspace, output: ExecutionEvidenceV1) -> None:
        self.workspace = workspace
        self.output = output
        self.calls = 0

    async def execute(
        self, validated_input: BaseModel, scope: object
    ) -> ExecutedAttemptResult[ExecutionEvidenceV1]:
        del validated_input, scope
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        target = binding.write_root / "tests" / "a.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        return ExecutedAttemptResult(output=self.output)


class _DeferredPhase:
    async def execute(self, prepared: object, scope: object) -> RawAgentRuntimeOutcome:
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
    output = execution_evidence()
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
            {
                key: first_rerun[key]
                for key in AGENT_JOB_CONTRACTS["run"].input_model.model_fields
                if key in first_rerun
            }
        )
        selected_second = AGENT_JOB_CONTRACTS["run"].input_model.model_validate(
            {
                key: second_rerun[key]
                for key in AGENT_JOB_CONTRACTS["run"].input_model.model_fields
                if key in second_rerun
            }
        )
        first_key = derive_attempt_key(
            invocation_id="inv-1",
            graph_revision=_revision(),
            public_entrypoint="rerun",
            semantic_node_id="execution.run",
            business_activation=BusinessActivation.for_trigger("coverage.0.repair.0.rerun"),
            contract_id=_RUN_ID,
            validated_input=selected,
        )
        second_key = derive_attempt_key(
            invocation_id="inv-1",
            graph_revision=_revision(),
            public_entrypoint="rerun",
            semantic_node_id="execution.run",
            business_activation=BusinessActivation.for_trigger("coverage.0.repair.1.rerun"),
            contract_id=_RUN_ID,
            validated_input=selected_second,
        )
        assert first_key != second_key
    finally:
        store.close()
