from __future__ import annotations

from dataclasses import fields, replace
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from pydantic import BaseModel

from agent_runtime_contracts import RawAgentRuntimeOutcome
from agent_runtime_contracts.ops import InputError
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import ExecutionAttemptOutputV1
from assurance_execution.contracts.execution import ExecutionManifest
from assurance_execution.contracts.selection import SelectedTargets
from assurance_execution.graphs.factory import (
    ExecutionGraphs,
    build_execution_graphs as _build_execution_graphs,
)
from assurance_execution.contracts.agent import ExecutionPrepareInputV1, PreparedExecutionV1
from assurance_execution.operations.cycle import seal_execution
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_execution.operations.agent_skills import assemble_execution_input
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.flow.activation import activation_value
from graph_engine.flow.control import ROUTE_SENTINEL
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
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
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
from tests.acg_plan_fixture import install_plan

from graph_engine.testing.feature_bundle import compile_bundle


def build_execution_graphs(*args, **kwargs):
    return compile_bundle(_build_execution_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest="b" * 64)
_RUN_ID = "assurance.execution.run"


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
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
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
            "plan_digest": _SHA,
            "plan_ref": {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
            "batch_id": "20260822T000000Z",
            "executed_at": "2026-08-22T00:00:00Z",
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "family_outcomes": [{"family": "api", "state": "executed"}],
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
    return {contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()}


def generation_result() -> dict[str, object]:
    ref = lambda path: {"path": path, "digest": _SHA}  # noqa: E731
    plan_ref = ref(f"qa/results/plan/{_SHA}/resolved-assurance-plan.json")
    reviewed = {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [ref("qa/requirement.md"), plan_ref],
        "case_refs": [ref("qa/cases/items/case.yaml")],
        "review_ref": ref("qa/results/review/case-review.json"),
        "selection_ref": ref("qa/results/cases/epochs/2/selection.json"),
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 2,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "reviewed_case": reviewed,
        "mapping_ref": ref("qa/results/codegen/closed-mapping.json"),
        "source_refs": [ref("qa/tests/a.py")],
        "plan_refs": [ref("qa/results/plans/api-plan.md")],
        "method_plan_ref": ref("qa/results/generation/epochs/2/obligation-methods.json"),
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
        "assurance.execution.execute",
        "assurance.execution.run",
    )
    assert recording_context.compiled_subgraph_checkpointers == (None, None)


def _prepare_input(payload: dict[str, object], kind: str) -> ExecutionPrepareInputV1:
    fields = ExecutionPrepareInputV1.model_fields
    selected = {key: payload[key] for key in fields if key in payload}
    selected["execution_kind"] = "execute" if kind == "execute" else "run"
    return ExecutionPrepareInputV1.model_validate(selected)


def _attempt_key(kind: str, *, epoch: int, repair_round: int) -> AttemptKey:
    step = "execute" if kind == "execute" else "run"
    payload = execution_graph_input()
    payload.update(coverage_epoch=epoch, repair_round=repair_round)
    model = _prepare_input(payload, kind)
    return derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="rev-1",
        public_entrypoint="assurance.execution",
        semantic_node_id=f"execution.{step}",
        business_activation=BusinessActivation.for_trigger(activation_value((), step, ())),
        contract_id=f"assurance.execution.{kind if kind == 'execute' else 'run'}",
        validated_input=model,
    )


async def _rerun_input_digest(*, epoch: int, repair_round: int) -> str:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.execution", contracts=execution_contracts())
    bundle = build_execution_graphs(context)
    payload = execution_graph_input()
    payload["coverage_epoch"] = epoch
    payload["repair_round"] = repair_round
    result = await harness.run(
        bundle.rerun,
        input=payload,
        script={"execution.run": [committed(_sealed("run", payload), _RECEIPT)]},
    )
    assert result.semantic_calls
    return result.semantic_calls[0].input_digest


async def test_second_repair_round_rerun_does_not_reuse_the_first_attempt() -> None:
    first = await _rerun_input_digest(epoch=1, repair_round=1)
    second = await _rerun_input_digest(epoch=1, repair_round=2)
    other_epoch = await _rerun_input_digest(epoch=2, repair_round=1)
    assert first != second
    assert first != other_epoch
    assert _attempt_key("run", epoch=1, repair_round=1) != _attempt_key("run", epoch=1, repair_round=2)


def test_execute_and_rerun_attempt_keys_follow_epoch_and_repair_round() -> None:
    assert _attempt_key("execute", epoch=0, repair_round=0) != _attempt_key(
        "execute", epoch=1, repair_round=0
    )
    assert _attempt_key("run", epoch=1, repair_round=1) != _attempt_key("run", epoch=1, repair_round=2)
    assert _attempt_key("run", epoch=1, repair_round=1) != _attempt_key("run", epoch=2, repair_round=1)


async def test_execute_and_rerun_publish_typed_public_output() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.execution",
        contracts=execution_contracts(),
    )
    bundle = build_execution_graphs(context)
    output = execution_evidence()
    evidence = output.model_dump(mode="json")
    admitted = execution_graph_input()
    admitted["coverage_epoch"] = 2
    sealed = _sealed("execute", {**admitted, "generation_result": generation_result()}, output)
    execute = await harness.run(
        bundle.execute,
        input=admitted,
        script={"execution.execute": [committed(sealed, _RECEIPT)]},
    )
    published = execute.published_update
    assert published is not None
    assert published[ROUTE_SENTINEL] == "committed"
    assert "batch_id" not in published
    assert "execution_evidence" not in published
    assert "execution_receipt" not in published
    assert sealed.batch_id == output.batch_id
    assert sealed.execution_evidence == evidence
    assert sealed.execution_digest == canonical_digest(evidence)
    assert sealed.execution_semantic_node_id == "execution.execute"
    assert sealed.execution_result is not None
    assert sealed.execution_result.family_outcomes == output.family_outcomes
    assert "receipt" not in sealed.execution_result.model_dump(mode="json")
    assert execute.terminal is not None
    assert cast(dict[str, object], execute.terminal)["status"] == "committed"
    assert execute.promotion_decision == "committed"
    assert execute.interrupt_envelope is None

    rerun_input = execution_graph_input()
    rerun_input["coverage_epoch"] = 2
    rerun_input["repair_round"] = 1
    rerun = await harness.run(
        bundle.rerun,
        input=rerun_input,
        script={
            "execution.run": [
                committed(
                    _sealed("run", {**rerun_input, "generation_result": generation_result()}, output),
                    _RECEIPT,
                )
            ]
        },
    )
    rerun_published = rerun.published_update
    assert rerun_published is not None
    assert rerun_published[ROUTE_SENTINEL] == "committed"
    assert "execution_semantic_node_id" not in rerun_published
    assert cast(dict[str, object], rerun.terminal)["status"] == "committed"
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


def _sealed(kind: str, payload: dict[str, object], evidence: ExecutionEvidenceV1 | None = None):
    model = _prepare_input(payload, kind)
    raw = payload.get("generation_result")
    generation = None if raw is None else GenerationCycleResultV1.model_validate(raw)
    return seal_execution(
        evidence or execution_evidence(),
        change_id=model.change_id,
        coverage_epoch=model.coverage_epoch,
        repair_round=model.repair_round,
        execution_kind="execute" if kind == "execute" else "run",
        generation=generation,
    )


@pytest.mark.parametrize("bad_status", [None, "", "unknown", "PASS_WITH_WARNINGS"])
def test_execution_result_rejects_unknown_final_status(bad_status: object) -> None:
    from assurance_execution.contracts.workflow import ExecutionCycleResultV1

    cycle = _sealed(
        "execute",
        {**execution_graph_input(), "coverage_epoch": 2, "generation_result": generation_result()},
    ).execution_result
    assert cycle is not None
    with pytest.raises(ValueError, match="final_status"):
        ExecutionCycleResultV1.model_validate(
            {
                **cycle.model_dump(mode="json"),
                "final_status": bad_status,
                "receipt": _RECEIPT.model_dump(mode="json"),
            }
        )


def test_failed_execution_publishes_committed_versioned_evidence() -> None:
    published = seal_execution(
        execution_evidence(status="failed"),
        change_id="CH-DEMO-001",
        coverage_epoch=2,
        repair_round=1,
        execution_kind="run",
        generation=GenerationCycleResultV1.model_validate(generation_result()),
    )
    assert published.admission == "committed"
    result = published.execution_result
    assert result is not None
    assert result.coverage_epoch == 2
    assert result.repair_round == 1
    assert result.final_status == "FAIL"
    assert "receipt" not in result.model_dump(mode="json")


def test_execution_prepare_rejects_replaced_generation_source(tmp_path: Path) -> None:
    plan, plan_ref = install_plan(
        tmp_path,
        "CH-DEMO-001",
        capability_leafs=("entities.item.create",),
    )
    mapping_path = "qa/results/codegen/closed-mapping.json"
    source_path = "qa/tests/a.py"
    mapping_bytes = b"{}"
    source_bytes = b"original"
    for relative, content in ((mapping_path, mapping_bytes), (source_path, b"replaced")):
        path = tmp_path.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    generation = generation_result()
    generation["plan_digest"] = plan.plan_digest
    generation["plan_ref"] = plan_ref
    reviewed = cast(dict[str, object], generation["reviewed_case"])
    reviewed["plan_digest"] = plan.plan_digest
    reviewed["plan_ref"] = plan_ref
    reviewed["preparation_refs"] = [
        {"path": "qa/requirement.md", "digest": _SHA},
        plan_ref,
    ]
    generation["mapping_ref"] = {
        "path": mapping_path,
        "digest": hashlib.sha256(mapping_bytes).hexdigest(),
    }
    generation["source_refs"] = [{"path": source_path, "digest": hashlib.sha256(source_bytes).hexdigest()}]
    with pytest.raises(InputError, match="digest does not match"):
        assemble_execution_input(
            PreparedExecutionV1.model_validate(
                {
                    "change_id": "CH-DEMO-001",
                    "plan_digest": plan.plan_digest,
                    "plan_ref": plan_ref,
                    "selected_test_families": ["api"],
                    "capability_leafs": ["entities.item.create"],
                    "coverage_epoch": 2,
                    "coverage_epoch_token": "2",
                    "execution_kind": "execute",
                    "generation_result": generation,
                }
            ),
            workspace=tmp_path,
            write_root=tmp_path / ".stage",
        )


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None
        self.prepare_calls = 0
        self.promote_calls = 0

    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims, *, seed_from: AttemptKey | None = None
    ) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims, seed_from=seed_from)
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
    ) -> ExecutedAttemptResult[ExecutionAttemptOutputV1]:
        del scope
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        target = binding.write_root / "tests" / "a.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        from assurance_execution.contracts.agent import ExecutionPrepareInputV1

        prepared = ExecutionPrepareInputV1.model_validate(validated_input.model_dump(mode="json"))
        return ExecutedAttemptResult(
            output=seal_execution(
                self.output,
                change_id=prepared.change_id,
                coverage_epoch=prepared.coverage_epoch,
                repair_round=prepared.repair_round,
                execution_kind=prepared.execution_kind,
                generation=None,
            )
        )


class _DeferredPhase:
    async def execute(self, prepared: object, scope: object) -> RawAgentRuntimeOutcome:
        raise RuntimeError("semantic attempt phase is not driven")


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
    writable = ResourceClaims(writes=("tests",))
    execute_contract = resolve_contract(
        replace(TASK_ATTEMPT_CONTRACTS["execute"], resources=writable),
        executor=writer,
    )
    run_contract = resolve_contract(
        replace(TASK_ATTEMPT_CONTRACTS["run"], resources=writable),
        executor=writer,
    )
    journal = MemoryAttemptCheckpointStore()
    cuts = ["after_promotion_before_receipt"]

    def transaction_cut(name: str) -> None:
        if cuts and name == cuts[0]:
            cuts.pop(0)
            raise RuntimeError("crash after promotion")

    kernel = AssuranceAttemptKernel(
        checkpoints=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=_revision(),
        transaction_cut=transaction_cut,
    )
    factory = AttemptNodeFactory(checkpoints=journal, kernel=kernel)
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
        assert result["status"] == "failed"
        assert writer.calls == 1
        assert workspace.promote_calls == 1
        assert (project / "tests" / "a.py").is_file()

        first_rerun = execution_graph_input(activation={"kind": "round", "value": "0"})
        second_rerun = execution_graph_input(activation={"kind": "round", "value": "1"})
        assert first_rerun["mapping"] == second_rerun["mapping"]
        await bundle.rerun.ainvoke(first_rerun, config=_invoke_config(entrypoint="rerun"))
        await bundle.rerun.ainvoke(second_rerun, config=_invoke_config(entrypoint="rerun"))
        assert writer.calls == 3

        selected = _prepare_input(first_rerun, "run")
        selected_second = _prepare_input(second_rerun, "run")
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
