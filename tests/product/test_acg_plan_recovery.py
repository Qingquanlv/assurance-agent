from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from assurance_generation.contracts.agent import PlanInputV1
from assurance_intake.contracts.explore import ExploreAdvisoryV1, PreparedExploreV1
from assurance_intake.contracts.plan import (
    LoadPlanInputV1,
    ResolvePlanInputV1,
    ResolvePlanOutputV1,
    TestFamilyPolicyV1 as FamilyPolicyV1,
    plan_artifact_ref,
    plan_bytes,
)
from assurance_intake.operations.plan_codec import seal_plan
from assurance_intake.operations.obligations import normalize_obligation_drafts
from assurance_intake.operations.plan_artifacts import ResolvePlanHandler, load_plan_artifact
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.runtime_bindings import DeterministicTaskExecutor
from assurance_product.sqlite_attempt_store import SqliteAttemptJournal
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from graph_engine.attempts import (
    AttemptExecutionContext,
    BusinessActivation,
    CommittedTaskResult,
    ResolvedAttemptContract,
    derive_attempt_key,
    resolve_contract,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore

from assurance_intake.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from tests.acg_plan_fixture import DEFAULT_POLICY, install_plan


_REVISION = canonical_digest({"revision": "acg-plan-recovery"})


@dataclass
class _PlanScenario:
    kernel: AssuranceAttemptKernel
    attempt_key: AttemptKey
    resolved: ResolvedAttemptContract[Any, Any]
    validated_input: ResolvePlanInputV1
    execution_context: AttemptExecutionContext
    store: TaskWorkspaceStore
    executor: DeterministicTaskExecutor
    expected_output: ResolvePlanOutputV1
    project: Path


def _plan_scenario(tmp_path: Path) -> _PlanScenario:
    project = tmp_path / "project"
    project.mkdir()
    plan, _ = install_plan(
        project,
        "CH-ACG-RECOVERY",
        candidates=("api", "e2e"),
        proposed=("api",),
    )
    ref = plan_artifact_ref(plan)
    (project / ref.path).unlink()
    exploration_path = project / plan.exploration_ref.path
    advisory = ExploreAdvisoryV1.model_validate_json(exploration_path.read_bytes())
    exploration = PreparedExploreV1.model_validate(
        {
            **advisory.model_dump(mode="json"),
            "minimum_required_coverage": normalize_obligation_drafts(
                advisory.minimum_required_coverage, resolved_quotes={}
            ),
        }
    )
    exploration_bytes = canonical_json_bytes(cast(JSONValue, exploration.model_dump(mode="json"))) + b"\n"
    exploration_path.write_bytes(exploration_bytes)
    validated_input = ResolvePlanInputV1(
        change_id=plan.change_id,
        requirement_digest=plan.requirement_digest,
        candidate_test_families=plan.candidate_test_families,
        budgets=plan.resolved_budgets,
        policy_resource_id=plan.policy_resource_id,
        policy_digest=plan.policy_digest,
        family_policy=FamilyPolicyV1.model_validate(DEFAULT_POLICY["test_family_policy"]),
        exploration_ref=plan.exploration_ref.model_copy(
            update={"digest": hashlib.sha256(exploration_bytes).hexdigest()}
        ),
        impact_inventory_ref=plan.impact_inventory_ref,
        source_resource_digests=plan.quality_goal.source_resource_digests,
        capability_leafs=("entities.item.constraints.name",),
    )
    bound_exploration = exploration.model_dump(mode="json")
    bound_exploration["minimum_required_coverage"][0]["key"] = "entities.item.constraints.name"
    bound_bytes = canonical_json_bytes(cast(JSONValue, bound_exploration)) + b"\n"
    bound_ref = plan.exploration_ref.model_copy(update={"digest": hashlib.sha256(bound_bytes).hexdigest()})
    plan = seal_plan(
        {
            **plan.model_dump(mode="json", exclude={"plan_digest"}),
            "exploration_ref": bound_ref.model_dump(mode="json"),
            "quality_goal": plan.quality_goal.model_copy(update={"obligations_ref": bound_ref}).model_dump(
                mode="json"
            ),
        }
    )
    ref = plan_artifact_ref(plan)
    contract = TASK_ATTEMPT_CONTRACTS["resolve-plan"]
    executor = DeterministicTaskExecutor(
        contract.handler_id,
        ResolvePlanHandler(),
        contract.output_model,
    )
    resolved = resolve_contract(contract, executor=executor)
    attempt_key = derive_attempt_key(
        invocation_id="inv-acg-recovery",
        graph_revision=_REVISION,
        public_entrypoint="full",
        semantic_node_id="intake.resolve-plan",
        business_activation=BusinessActivation.one_shot(),
        contract_id=contract.contract_id,
        validated_input=validated_input,
    )
    execution_context = AttemptExecutionContext(
        invocation_id="inv-acg-recovery",
        public_entrypoint="full",
        semantic_node_id="intake.resolve-plan",
        attempt_key=attempt_key,
        fencing_token=1,
    )
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=_REVISION,
    )
    return _PlanScenario(
        kernel=kernel,
        attempt_key=attempt_key,
        resolved=resolved,
        validated_input=validated_input,
        execution_context=execution_context,
        store=store,
        executor=executor,
        expected_output=ResolvePlanOutputV1(plan=plan, plan_ref=ref),
        project=project,
    )


def _execute(scenario: _PlanScenario, *, transaction_cut: object = None) -> object:
    return asyncio.run(
        scenario.kernel.execute_or_recover(
            scenario.attempt_key,
            scenario.resolved,
            scenario.validated_input,
            scenario.execution_context,
            transaction_cut=transaction_cut,
        )
    )


def test_committed_plan_replay_does_not_resolve_again(tmp_path: Path) -> None:
    scenario = _plan_scenario(tmp_path)
    try:
        first = _execute(scenario)
        second = _execute(scenario)

        assert isinstance(first, CommittedTaskResult)
        assert isinstance(second, CommittedTaskResult)
        assert first.output == second.output == scenario.expected_output
        assert first.receipt == second.receipt
        assert scenario.executor.dispatch_count == 1
        assert (scenario.project / first.output.plan_ref.path).read_bytes() == plan_bytes(first.output.plan)
        assert (
            load_plan_artifact(
                LoadPlanInputV1(
                    **scenario.validated_input.model_dump(
                        exclude={
                            "candidate_test_families",
                            "family_policy",
                            "exploration_ref",
                            "impact_inventory_ref",
                        }
                    ),
                    resolved_plan_ref=first.output.plan_ref,
                ),
                project_root=scenario.project,
            )
            == first.output
        )
    finally:
        scenario.store.close()


def test_committed_plan_replay_survives_sqlite_restart(tmp_path: Path) -> None:
    asyncio.run(_committed_plan_replay_survives_sqlite_restart(tmp_path))


async def _committed_plan_replay_survives_sqlite_restart(tmp_path: Path) -> None:
    scenario = _plan_scenario(tmp_path)
    workspace = ChangeWorkspace.prepare(scenario.project, scenario.validated_input.change_id)
    try:
        async with open_sqlite_checkpointer(workspace) as backend:
            first_kernel = AssuranceAttemptKernel(
                journal=SqliteAttemptJournal(backend),
                arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
                workspace=TaskWorkspaceProvider(scenario.store),
                graph_revision=_REVISION,
            )
            first = await first_kernel.execute_or_recover(
                scenario.attempt_key,
                scenario.resolved,
                scenario.validated_input,
                scenario.execution_context,
            )

        async with open_sqlite_checkpointer(workspace) as reopened:
            replay_kernel = AssuranceAttemptKernel(
                journal=SqliteAttemptJournal(reopened),
                arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
                workspace=TaskWorkspaceProvider(scenario.store),
                graph_revision=_REVISION,
            )
            replay = await replay_kernel.execute_or_recover(
                scenario.attempt_key,
                scenario.resolved,
                scenario.validated_input,
                scenario.execution_context,
            )

        assert isinstance(first, CommittedTaskResult)
        assert isinstance(replay, CommittedTaskResult)
        assert replay.output == first.output == scenario.expected_output
        assert replay.receipt == first.receipt
        assert scenario.executor.dispatch_count == 1
    finally:
        scenario.store.close()


@pytest.mark.parametrize(
    "crash_point",
    ("after_observed_result", "after_prepare_before_promotion", "after_promotion_before_receipt"),
)
def test_plan_recovery_reuses_the_observed_resolution(tmp_path: Path, crash_point: str) -> None:
    scenario = _plan_scenario(tmp_path)

    def crash(name: str) -> None:
        if name == crash_point:
            raise RuntimeError(crash_point)

    try:
        with pytest.raises(RuntimeError, match=crash_point):
            _execute(scenario, transaction_cut=crash)
        if crash_point != "after_promotion_before_receipt":
            assert (
                hashlib.sha256(
                    (scenario.project / scenario.validated_input.exploration_ref.path).read_bytes()
                ).hexdigest()
                == scenario.validated_input.exploration_ref.digest
            )
            assert not (scenario.project / scenario.expected_output.plan_ref.path).exists()
        recovered = _execute(scenario)

        assert isinstance(recovered, CommittedTaskResult)
        assert recovered.output == scenario.expected_output
        assert scenario.executor.dispatch_count == 1
        assert (scenario.project / recovered.output.plan_ref.path).read_bytes() == plan_bytes(
            recovered.output.plan
        )
        assert (
            hashlib.sha256(
                (scenario.project / recovered.output.plan.exploration_ref.path).read_bytes()
            ).hexdigest()
            == recovered.output.plan.exploration_ref.digest
        )
    finally:
        scenario.store.close()


def test_downstream_attempt_identity_binds_the_imported_plan(tmp_path: Path) -> None:
    first_plan, _ = install_plan(
        tmp_path / "first",
        "CH-PLAN-KEY",
        candidates=("api", "e2e"),
        proposed=("api",),
    )
    second_plan, _ = install_plan(
        tmp_path / "second",
        "CH-PLAN-KEY",
        candidates=("api", "e2e"),
        proposed=("e2e",),
    )
    common = {
        "change_id": "CH-PLAN-KEY",
        "capability_leafs": ("entities.item.constraints.name",),
        "artifact_paths": (),
    }
    first_input = PlanInputV1(
        **common,
        plan_digest=first_plan.plan_digest,
        plan_ref=plan_artifact_ref(first_plan),
    )
    second_input = PlanInputV1(
        **common,
        plan_digest=second_plan.plan_digest,
        plan_ref=plan_artifact_ref(second_plan),
    )
    key_args = {
        "invocation_id": "inv-acg",
        "graph_revision": "a" * 64,
        "public_entrypoint": "full",
        "semantic_node_id": "generation.api.codegen",
        "business_activation": BusinessActivation.for_trigger("coverage.0.plan"),
        "contract_id": "assurance.generation.agent.api.codegen.v1",
    }

    first_key = derive_attempt_key(**key_args, validated_input=first_input)
    assert first_key == derive_attempt_key(**key_args, validated_input=first_input)
    assert first_key != derive_attempt_key(**key_args, validated_input=second_input)
    assert first_key != derive_attempt_key(
        **{**key_args, "invocation_id": "inv-acg-imported"},
        validated_input=first_input,
    )
