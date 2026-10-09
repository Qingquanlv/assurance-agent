from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState
from langgraph.graph import END, START, StateGraph
from langchain_core.runnables.config import RunnableConfig

from agent_runtime_contracts import RawAgentRuntimeOutcome
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import ExecutionAttemptOutputV1
from assurance_execution.graphs.factory import build_execution_graphs as _build_execution_graphs
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.plugin import ExecutionPlugin
from assurance_product.agent_contracts import all_feature_agent_contracts
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts.models.contracts import (
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.models.keys import AttemptKey, BusinessActivation
from graph_engine.attempts.orchestration.kernel import AssuranceAttemptKernel
from graph_engine.attempts.orchestration.node_factory import AttemptNodeFactory
from graph_engine.attempts.models.resolutions import RejectedTaskResult
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiter
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
from graph_engine.attempts.resources.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.testing import GraphHarness, RecordingCapabilityBuildContext

from graph_engine.testing.feature_bundle import compile_bundle


def build_execution_graphs(*args, **kwargs):
    return compile_bundle(_build_execution_graphs(*args, **kwargs))


_TEST_CONTRACT_ID = "test.assurance.execution.validator-parity.v1"
_EVIDENCE_VALIDATOR_ID = "assurance.execution.validator.evidence.v1"


class _ExecutionChannels(CheckpointBridgeState, total=False):
    change_id: str
    plan_digest: str
    plan_ref: dict[str, str]
    selected_test_families: list[str]
    capability_leafs: list[str]
    coverage_epoch: int
    coverage_epoch_token: str
    repair_round: int
    execution_kind: str
    generation_result: dict[str, object]
    allowed_origins: list[str]
    timeout_seconds: int
    batch_id: str
    case_ids: list[str]
    artifact_paths: list[str]
    mapping: dict[str, object]
    selected_targets: dict[str, bool]
    baseline_tree_id: str
    runner_profile_digest: str
    rounds_budget: int
    rounds_used: int
    status: str
    execution_evidence: dict[str, object]
    execution_digest: str
    execution_semantic_node_id: str
    execution_result: dict[str, object]
    execution_receipt: dict[str, str]
    family_outcomes: list[dict[str, object]]
    attempt_failure: dict[str, object]


_EXECUTE_ID = "assurance.execution.execute"
_ACCEPT_PATH = "qa/tests/test_validator_parity.py"
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
    ) -> ExecutedAttemptResult[ExecutionAttemptOutputV1]:
        del validated_input, scope
        self.calls += 1
        binding = self.workspace.binding
        assert binding is not None
        target = binding.write_root / self.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        from assurance_execution.operations.cycle import seal_execution

        return ExecutedAttemptResult(
            output=seal_execution(
                self.output,
                change_id=self.output.change_id,
                coverage_epoch=0,
                repair_round=0,
                execution_kind="execute",
                generation=None,
            )
        )


class _CountingValidator:
    def __init__(self, inner: CommitValidator) -> None:
        self.inner = inner
        self.calls = 0

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        self.calls += 1
        return self.inner.validate(staged, context)


class _NoopExecutor:
    async def execute(self, validated_input: object, scope: object) -> object:
        del validated_input, scope
        raise RuntimeError("semantic attempt is not driven")


def _boot_resolved_execute() -> ResolvedAttemptContract[Any, Any]:
    return resolve_contract(TASK_ATTEMPT_CONTRACTS["execute"], executor=cast(Any, _NoopExecutor()))


def _contribution() -> PluginContribution:
    return ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def _manifest() -> GraphBuildManifest:
    digest = canonical_digest(TASK_ATTEMPT_CONTRACTS["execute"].canonical_projection())
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
        resources=ResourceClaims(writes=("qa/tests", "src")),
    )


def _revision() -> str:
    return canonical_digest({"revision": "execution-validator-parity"})


def _graph_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": f"qa/results/plan/{'a' * 64}/resolved-assurance-plan.json",
            "digest": "a" * 64,
        },
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
            "plan_digest": "a" * 64,
            "plan_ref": {
                "path": f"qa/results/plan/{'a' * 64}/resolved-assurance-plan.json",
                "digest": "a" * 64,
            },
            "batch_id": "20260822T000000Z",
            "executed_at": "2026-08-22T00:00:00Z",
            "selected_targets": {
                "api": True,
                "e2e": False,
                "fuzz": False,
                "performance": False,
            },
            "family_outcomes": [{"family": "api", "state": "executed"}],
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


class _QaPathValidator:
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if item.path.startswith("src/"):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        return ValidationResult(accepted=True)


def _assert_shipped_inventory(contribution: PluginContribution) -> None:
    assert all(contract.validators == () for contract in TASK_ATTEMPT_CONTRACTS.values())
    assert _TEST_CONTRACT_ID not in {item.contract_id for item in contribution.attempt_contracts}
    assert _TEST_CONTRACT_ID not in all_feature_agent_contracts()
    assert len(all_feature_agent_contracts()) == 26
    assert contribution.commit_validators == {}


async def _run_parity_candidate(
    tmp_path: Path,
    *,
    relative: str,
) -> tuple[dict[str, object], _CountingValidator, _RecordingWorkspace, _StagedWriteExecutor]:
    contribution = _contribution()
    evidence = _QaPathValidator()
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
    journal = MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision=_revision(),
        validators={_EVIDENCE_VALIDATOR_ID: counter},
    )
    shipped_context = GraphHarness().recording_context(
        owner_id="assurance.execution",
        contracts={contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    )
    build_execution_graphs(shipped_context)
    assert shipped_context.bound_contract_ids == (_EXECUTE_ID, "assurance.execution.run")
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
                _TEST_CONTRACT_ID,
                semantic_node_id="execution.validator-parity",
                activation=BusinessActivation.one_shot(),
                select=lambda state: ExecutionPrepareInputV1.model_validate(
                    {
                        **{key: state[key] for key in ExecutionPrepareInputV1.model_fields if key in state},
                        "execution_kind": "execute",
                    }
                ),
                publish=lambda _state, output, _receipt: {
                    "status": getattr(output, "status", None)
                    or (output.get("status") if isinstance(output, dict) else None)
                    or "passed"
                },
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
    assert all(contract.validators == () for contract in TASK_ATTEMPT_CONTRACTS.values())
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
