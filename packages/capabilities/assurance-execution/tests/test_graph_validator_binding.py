from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langchain_core.runnables.config import RunnableConfig

from agent_runtime_contracts import RawAgentRuntimeOutcome, ResolvedRawAgentExecutor
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.graphs.factory import build_execution_graphs
from assurance_execution.graphs.nodes import publish_execution, select_execute
from assurance_execution.graphs.state import ExecutionState
from assurance_execution.plugin import ExecutionPlugin
from assurance_product.agent_contracts import all_feature_agent_contracts
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.keys import AttemptKey, BusinessActivation
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import RejectedTaskResult
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.boot.graph_revision import GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.composition.models import AttemptContractClaim
from graph_engine.composition.registries import build_attempt_registry
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import (
    CommitValidator,
    PluginContribution,
    PathWriteSet,
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedWriteSet,
    TaskWorkspaceBinding,
    ValidationContext,
    ValidationResult,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.testing import GraphHarness, RecordingCapabilityBuildContext

_TEST_CONTRACT_ID = "test.assurance.execution.validator-parity.v1"
_EVIDENCE_VALIDATOR_ID = "assurance.execution.validator.evidence.v1"
_EXECUTE_ID = "assurance.execution.agent.execute.v1"
_ACCEPT_PATH = "tests/test_validator_parity.py"
_REJECT_PATH = "src/validator_parity.py"
_OUTSIDE_REASON = "execution candidate may write only tests and change execution paths"


class _DeferredPhase:
    async def execute(self, prepared: object, scope: object) -> RawAgentRuntimeOutcome:
        raise RuntimeError("semantic attempt phase is not driven")


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


class _StagedWriteExecutor:
    def __init__(
        self,
        workspace: _RecordingWorkspace,
        relative: str,
        output: ExecutionEvidenceV1,
    ) -> None:
        self.workspace = workspace
        self.relative = relative
        self.output = output
        self.calls = 0

    async def execute(
        self, validated_input: object, scope: object
    ) -> ExecutedAttemptResult[ExecutionEvidenceV1]:
        del validated_input, scope
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        target = binding.write_root / self.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        return ExecutedAttemptResult(output=self.output)


class _CountingValidator:
    def __init__(self, inner: CommitValidator) -> None:
        self.inner = inner
        self.calls = 0

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        self.calls += 1
        return self.inner.validate(staged, context)


def _boot_resolved_execute() -> ResolvedAttemptContract[Any, Any]:
    agent = AGENT_JOB_CONTRACTS["execute"]
    return ResolvedRawAgentExecutor(
        agent,
        prepare=cast(Any, _DeferredPhase()),
        runtime=_DeferredPhase(),
        finalize=cast(Any, _DeferredPhase()),
    ).resolve()


def _contribution() -> PluginContribution:
    return ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def _manifest() -> GraphBuildManifest:
    digest = canonical_digest(AGENT_JOB_CONTRACTS["execute"].to_task_contract().canonical_projection())
    return GraphBuildManifest(
        revision=GraphRevision.build(
            product_lock_digest="a" * 64,
            wheel_source_digests={"assurance.execution": "b" * 64},
            factory_symbols=("assurance_execution.graphs.factory:build_execution_graphs",),
            state_schema_versions={"execute": "1"},
            langgraph_version="1.2.11",
            checkpoint_contract_version="1",
        ),
        entrypoint_contract_digests={"execute": digest},
        attempt_contract_digests={_EXECUTE_ID: digest},
    )


def _parity_clone(core: ResolvedAttemptContract[Any, Any]) -> TaskAttemptContract[Any, Any]:
    return replace(
        core.contract,
        contract_id=_TEST_CONTRACT_ID,
        validators=(_EVIDENCE_VALIDATOR_ID,),
        resources=ResourceClaims(writes=("tests", "src")),
    )


def _revision() -> str:
    return canonical_digest({"revision": "execution-validator-parity"})


def _graph_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "selected_test_families": ["api"],
        "capability_leafs": ["entities.item.create"],
        "case_ids": ["TC_A"],
        "artifact_paths": [],
        "mapping": {
            "selected": ["tests/a.py"],
            "mappings": [
                {
                    "test": "tests/a.py",
                    "case_id": "TC_A",
                    "capability": "entities.item.create",
                    "layer": "api",
                }
            ],
        },
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "rounds_budget": 1,
        "rounds_used": 0,
    }


def _output() -> ExecutionEvidenceV1:
    return ExecutionEvidenceV1.model_validate(
        {
            "change_id": "CH-DEMO-001",
            "batch_id": "20260822T000000Z",
            "executed_at": "2026-08-22T00:00:00Z",
            "selected_targets": {
                "api": True,
                "e2e": False,
                "fuzz": False,
                "performance": False,
            },
            "mapping": {
                "selected": ["tests/a.py"],
                "mappings": [
                    {
                        "test": "tests/a.py",
                        "case_id": "TC_A",
                        "capability": "entities.item.create",
                        "layer": "api",
                    }
                ],
            },
            "mapping_digest": "d" * 64,
            "baseline_tree_id": "b" * 64,
            "runner_profile_digest": "c" * 64,
            "receipt_digest": "e" * 64,
            "receipt": {
                "commands": [
                    {
                        "family": "api",
                        "command": ["pytest", "tests/a.py"],
                        "exit_code": 0,
                        "collected": 1,
                        "passed": 1,
                        "failed": 0,
                        "skipped": 0,
                    }
                ]
            },
            "results": [{"test": "tests/a.py", "status": "passed", "duration_ms": 1}],
        }
    )


def _invoke_config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": _revision(),
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "execute",
        }
    }


def _assert_shipped_inventory(contribution: PluginContribution) -> None:
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
    assert _TEST_CONTRACT_ID not in {item.contract_id for item in contribution.attempt_contracts}
    assert _TEST_CONTRACT_ID not in all_feature_agent_contracts()
    assert len(all_feature_agent_contracts()) == 34
    assert len(all_feature_agent_contracts()) == 34
    assert _EVIDENCE_VALIDATOR_ID in contribution.commit_validators


async def _run_parity_candidate(
    tmp_path: Path,
    *,
    relative: str,
) -> tuple[dict[str, object], _CountingValidator, _RecordingWorkspace, _StagedWriteExecutor]:
    contribution = _contribution()
    evidence = contribution.commit_validators[_EVIDENCE_VALIDATOR_ID]
    core = _boot_resolved_execute()
    clone = _parity_clone(core)
    authenticated = resolve_contract(clone, executor=core.executor)
    assert authenticated.executor is core.executor
    catalog_contract = replace(core.contract, validators=(_EVIDENCE_VALIDATOR_ID,))
    catalog = build_attempt_registry(
        [
            AttemptContractClaim(
                contract=cast(TaskAttemptContract[Any, Any], catalog_contract),
                available_handlers={core.contract.handler_id: core.contract.owner_id},
                available_validators={_EVIDENCE_VALIDATOR_ID: "assurance.execution"},
                handler=core.executor,
            )
        ]
    )
    catalog_before = catalog.projection()
    contribution_before = contribution
    manifest = _manifest()
    manifest_before = manifest.model_dump(mode="json")
    base = RecordingCapabilityBuildContext(
        owner_id="assurance.execution",
        contracts={core.contract.contract_id: core.contract},
        catalog=catalog,
        contribution=contribution,
        manifest=manifest,
    )
    installed = base.with_test_contract(authenticated)
    assert installed.reused_registry_handler(_TEST_CONTRACT_ID) is core.executor
    assert _TEST_CONTRACT_ID not in catalog.entries
    assert catalog.projection() == catalog_before
    assert contribution is contribution_before
    assert manifest.model_dump(mode="json") == manifest_before

    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = _StagedWriteExecutor(workspace, relative, _output())
    runnable = resolve_contract(clone, executor=writer)
    counter = _CountingValidator(evidence)
    journal = MemoryAttemptJournal()
    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=_revision(),
        validators={_EVIDENCE_VALIDATOR_ID: counter},
    )
    shipped_context = GraphHarness().recording_context(
        owner_id="assurance.execution",
        contracts={
            contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
        },
    )
    build_execution_graphs(shipped_context)
    assert shipped_context.bound_contract_ids == (_EXECUTE_ID, "assurance.execution.agent.run.v1")
    factory = AttemptNodeFactory(journal=journal, kernel=kernel)
    context = RecordingCapabilityBuildContext(
        owner_id="assurance.execution",
        contracts={
            core.contract.contract_id: core,
            runnable.contract.contract_id: runnable,
        },
        attempt_factory=factory,
        catalog=catalog,
        contribution=contribution,
        manifest=manifest,
    )
    builder: StateGraph[ExecutionState] = StateGraph(ExecutionState)
    builder.add_node(
        "execution.validator-parity",
        cast(
            Callable[..., Any],
            context.attempt(
                _TEST_CONTRACT_ID,
                semantic_node_id="execution.validator-parity",
                activation=BusinessActivation.one_shot(),
                select=select_execute,
                publish=publish_execution,
            ),
        ),
    )
    builder.add_edge(START, "execution.validator-parity")
    builder.add_edge("execution.validator-parity", END)
    graph = context.compile_subgraph(builder)
    try:
        terminal = await graph.ainvoke(_graph_input(), config=_invoke_config())
    finally:
        store.close()
    _assert_shipped_inventory(contribution)
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
    return cast(dict[str, object], terminal), counter, workspace, writer


async def test_validator_parity_accepts_tests_candidate_and_promotes(tmp_path: Path) -> None:
    terminal, counter, workspace, writer = await _run_parity_candidate(tmp_path, relative=_ACCEPT_PATH)
    assert terminal["status"] == "passed"
    assert "attempt_failure" not in terminal
    assert counter.calls == 1
    assert writer.calls == 1
    assert workspace.prepare_calls == 1
    assert workspace.promote_calls == 1


async def test_validator_parity_rejects_src_candidate_without_durable_prepare(
    tmp_path: Path,
) -> None:
    terminal, counter, workspace, writer = await _run_parity_candidate(tmp_path, relative=_REJECT_PATH)
    failure = cast(dict[str, object], terminal["attempt_failure"])
    assert failure["resolution_kind"] == "rejected"
    assert failure["reason"] == _OUTSIDE_REASON
    assert failure["writes_promoted"] is False
    assert isinstance(RejectedTaskResult(reason=_OUTSIDE_REASON), RejectedTaskResult)
    assert counter.calls == 1
    assert writer.calls == 1
    assert workspace.prepare_calls == 0
    assert workspace.promote_calls == 0
