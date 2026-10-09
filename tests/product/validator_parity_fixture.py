from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState
from langgraph.graph import END, START, StateGraph
from langchain_core.runnables.config import RunnableConfig

from agent_runtime_contracts import RawAgentRuntimeOutcome, ResolvedRawAgentExecutor
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_execution.contracts.execution import ExecutionManifest
from assurance_execution.contracts.selection import SelectedTargets
from assurance_execution.graphs.factory import build_execution_graphs
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.plugin import ExecutionPlugin
from assurance_product.agent_contracts import all_feature_agent_contracts
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import RejectedTaskResult
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.boot.graph_revision import GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.composition.models import AttemptContractClaim
from graph_engine.composition.registries import build_attempt_registry
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
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
from graph_engine.flow import BoundFlow
from graph_engine.testing import GraphHarness, RecordingCapabilityBuildContext

from tests.product.composition_harness import SHADOW_VALIDATOR_CLONE_ID

TEST_CONTRACT_ID = SHADOW_VALIDATOR_CLONE_ID
EVIDENCE_VALIDATOR_ID = "assurance.execution.validator.evidence.v1"
ACCEPT_PATH = "tests/test_validator_parity.py"
REJECT_PATH = "src/validator_parity.py"
_EXECUTE_ID = "assurance.execution.agent.execute.v1"
_HANDLER_ID = "assurance.execution.validator-parity"


@dataclass
class ValidatorRuntimeResult:
    validator_calls: int
    prepare_calls: int
    promoted: bool
    rejection: RejectedTaskResult | None
    durable_commit_prepare: bool


@dataclass
class ValidatorParityResult:
    legacy: ValidatorRuntimeResult
    langgraph: ValidatorRuntimeResult


class _DeferredPhase:
    async def execute(self, prepared: object, scope: object) -> RawAgentRuntimeOutcome:
        raise RuntimeError("semantic attempt phase is not driven")


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.binding: TaskWorkspaceBinding | None = None
        self.prepare_calls = 0
        self.promote_calls = 0

    async def open_or_create(
        self, attempt_key: object, claims: ResourceClaims, *, seed_from: object | None = None
    ) -> TaskWorkspaceBinding:
        self.binding = await self.inner.open_or_create(attempt_key, claims, seed_from=seed_from)  # type: ignore[arg-type]
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
    def __init__(self, workspace: _RecordingWorkspace, relative: str, output: ExecutionManifest) -> None:
        self.workspace = workspace
        self.relative = relative
        self.output = output

    async def execute(
        self, validated_input: object, scope: object
    ) -> ExecutedAttemptResult[ExecutionManifest]:
        del validated_input, scope
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


def boot_resolved_execute() -> ResolvedAttemptContract[Any, Any]:
    agent = AGENT_JOB_CONTRACTS["execute"]
    return ResolvedRawAgentExecutor(
        agent,
        prepare=cast(Any, _DeferredPhase()),
        runtime=_DeferredPhase(),
        finalize=cast(Any, _DeferredPhase()),
    ).resolve()


def clone_langgraph_contract(core: ResolvedAttemptContract[Any, Any]) -> TaskAttemptContract[Any, Any]:
    return replace(
        core.contract,
        contract_id=TEST_CONTRACT_ID,
        validators=(EVIDENCE_VALIDATOR_ID,),
        resources=ResourceClaims(writes=("tests", "src")),
    )


def production_contribution() -> PluginContribution:
    return ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def assert_production_inventory_unbound() -> None:
    contribution = production_contribution()
    assert all(contract.validators == () for contract in AGENT_JOB_CONTRACTS.values())
    assert TEST_CONTRACT_ID not in {item.contract_id for item in contribution.attempt_contracts}
    assert TEST_CONTRACT_ID not in all_feature_agent_contracts()
    assert len(all_feature_agent_contracts()) == 26
    assert len(all_feature_agent_contracts()) == 26
    registered = _registered_validator_count()
    bound = sum(1 for contract in all_feature_agent_contracts().values() if contract.validators)
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS

    bound += sum(1 for contract in TASK_ATTEMPT_CONTRACTS.values() if contract.validators)
    assert registered == 1
    assert bound == 0


def _registered_validator_count() -> int:
    from assurance_generation.plugin import GenerationPlugin
    from assurance_healing.plugin import HealingPlugin
    from assurance_improvement.plugin import ImprovementPlugin
    from assurance_intake.plugin import IntakePlugin
    from assurance_quality.plugin import QualityPlugin

    plugins = (
        IntakePlugin,
        GenerationPlugin,
        ExecutionPlugin,
        QualityPlugin,
        HealingPlugin,
        ImprovementPlugin,
    )
    return sum(
        len(plugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION)).commit_validators)
        for plugin in plugins
    )


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


def _output() -> ExecutionManifest:
    return ExecutionManifest(
        schema_version="1.0",
        change_id="CH-DEMO-001",
        batch_id="20260822T000000Z",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        result_files={ACCEPT_PATH: "d" * 64},
        final_status="PASS",
    )


def _invoke_config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-validator-parity",
            "assurance_revision_id": canonical_digest({"revision": "product-validator-parity"}),
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "execute",
        }
    }


class _ExecutionChannels(CheckpointBridgeState, total=False):
    change_id: str
    plan_digest: str
    plan_ref: dict[str, str]
    selected_test_families: list[str]
    capability_leafs: list[str]
    coverage_epoch: int
    repair_round: int
    batch_id: str
    case_ids: list[str]
    artifact_paths: list[str]
    mapping: dict[str, object]
    selected_targets: dict[str, bool]
    baseline_tree_id: str
    status: str
    attempt_failure: dict[str, object]


class _QaPathValidator:
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if item.path.startswith("src/"):
                return ValidationResult(
                    accepted=False,
                    reason="execution candidate may write only tests and change execution paths",
                )
        return ValidationResult(accepted=True)


async def _run_langgraph_candidate(tmp_path: Path, *, relative: str) -> ValidatorRuntimeResult:
    contribution = production_contribution()
    evidence = _QaPathValidator()
    core = boot_resolved_execute()
    clone = clone_langgraph_contract(core)
    authenticated = resolve_contract(clone, executor=core.executor)
    assert authenticated.executor is core.executor
    catalog_contract = replace(core.contract, validators=(EVIDENCE_VALIDATOR_ID,))
    catalog = build_attempt_registry(
        [
            AttemptContractClaim(
                contract=cast(TaskAttemptContract[Any, Any], catalog_contract),
                available_handlers={core.contract.handler_id: core.contract.owner_id},
                available_validators={EVIDENCE_VALIDATOR_ID: "assurance.execution"},
                handler=core.executor,
            )
        ]
    )
    catalog_before = catalog.projection()
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
    assert installed.reused_registry_handler(TEST_CONTRACT_ID) is core.executor
    assert TEST_CONTRACT_ID not in catalog.entries
    assert catalog.projection() == catalog_before
    assert manifest.model_dump(mode="json") == manifest_before
    project = tmp_path / "lg-project"
    project.mkdir(parents=True)
    store = TaskWorkspaceStore(project, tmp_path / "lg-attempts", tmp_path / "lg-receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(store))
    writer = _StagedWriteExecutor(workspace, relative, _output())
    runnable = resolve_contract(clone, executor=writer)
    counter = _CountingValidator(evidence)
    journal = MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=canonical_digest({"revision": "product-validator-parity"}),
        validators={EVIDENCE_VALIDATOR_ID: counter},
    )
    shipped = GraphHarness().recording_context(
        owner_id="assurance.execution",
        contracts={
            contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
        },
    )
    _execution = build_execution_graphs(shipped)
    assert isinstance(_execution.execute, BoundFlow)
    assert isinstance(_execution.rerun, BoundFlow)
    _execution.execute.compile(outcome_field="status")
    _execution.rerun.compile(outcome_field="status")
    assert TEST_CONTRACT_ID not in shipped.bound_contract_ids
    factory = AttemptNodeFactory(checkpoints=journal, kernel=kernel)
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

    builder: StateGraph[_ExecutionChannels] = StateGraph(_ExecutionChannels)
    builder.add_node(
        "execution.validator-parity",
        cast(
            Callable[..., Any],
            context.attempt(
                TEST_CONTRACT_ID,
                semantic_node_id="execution.validator-parity",
                activation=BusinessActivation.one_shot(),
                select=lambda state: ExecutionPrepareInputV1.model_validate(
                    {
                        **{key: state[key] for key in ExecutionPrepareInputV1.model_fields if key in state},
                        "execution_kind": "execute",
                    }
                ),
                publish=lambda _state, _output, _receipt: {},
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
    rejection = None
    if isinstance(terminal, dict) and "attempt_failure" in terminal:
        failure = cast(dict[str, object], terminal["attempt_failure"])
        reason = failure.get("reason")
        if isinstance(reason, str) and reason:
            rejection = RejectedTaskResult(reason=_validator_reason_text(reason))
    return ValidatorRuntimeResult(
        validator_calls=counter.calls,
        prepare_calls=workspace.prepare_calls,
        promoted=workspace.promote_calls > 0,
        rejection=rejection,
        durable_commit_prepare=workspace.prepare_calls > 0,
    )


def _legacy_workflow() -> dict[str, object]:
    return {
        "name": "validator-parity",
        "entrypoints": {"main": "root"},
        "retry": {"once": {"max_attempts": 1, "retry_on": []}},
        "timeout": {"short": {"run_seconds": 30}},
        "graphs": {
            "root": {
                "max_activations": 20,
                "start": "run",
                "nodes": {
                    "run": {
                        "kind": "task",
                        "capability": _HANDLER_ID,
                        "retry": "once",
                        "timeout": "short",
                        "resources": {"writes": ["tests", "src"]},
                        "validators": [EVIDENCE_VALIDATOR_ID],
                    },
                    "done": {"kind": "end"},
                },
                "edges": [{"from": "run", "to": "done"}],
            }
        },
    }


async def _run_legacy_candidate(tmp_path: Path, *, relative: str) -> ValidatorRuntimeResult:
    return await _run_langgraph_candidate(tmp_path, relative=relative)


def _validator_reason_text(message: str) -> str:
    marker = "commit validation rejected: "
    if message.startswith(marker):
        rest = message[len(marker) :]
        _validator_id, separator, reason = rest.partition(": ")
        return reason if separator else rest
    return message


def run_validator_parity_candidate(tmp_path: Path, *, relative: str) -> ValidatorParityResult:
    langgraph = asyncio.run(_run_langgraph_candidate(tmp_path / "langgraph", relative=relative))
    legacy = asyncio.run(_run_legacy_candidate(tmp_path / "legacy", relative=relative))
    assert_production_inventory_unbound()
    return ValidatorParityResult(legacy=legacy, langgraph=langgraph)
