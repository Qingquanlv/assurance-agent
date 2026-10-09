from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from graph_engine.artifacts import stage_json_artifact
from graph_engine.attempts import (
    AttemptExecutionContext,
    BusinessActivation,
    CommittedTaskResult,
    derive_attempt_key,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore

from assurance_intake.contracts.coverage_rework import (
    COVERAGE_REWORK_HANDOFF_PATH,
    CoverageReworkHandoffV1,
    CoverageReworkInputV1,
)
from assurance_intake.contracts.plan import (
    PREPARATION_REFS_PATH,
    ResolvePlanInputV1,
    TestFamilyPolicyV1 as FamilyPolicyV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.feature import TASK_ATTEMPT_CONTRACTS
from assurance_intake.ops.case_review.hooks import REVIEWED_CASE_PATH
from assurance_intake.plugin import IntakePlugin
from assurance_product.runtime_bindings import DeterministicTaskExecutor
from tests.acg_plan_fixture import DEFAULT_POLICY, install_plan
from tests.op_handlers import op_handler

_REVISION = canonical_digest({"revision": "intake-sealed-refs"})
_SHA = "a" * 64
_CHANGE = "CH-SEAL-REWORK"
_PLAN = "b" * 64
_CASE = "qa/cases/menus/case.yaml"


def _kernel(
    project: Path, attempts: Path, receipts: Path
) -> tuple[AssuranceAttemptKernel, TaskWorkspaceStore]:
    store = TaskWorkspaceStore(project, attempts, receipts)
    kernel = AssuranceAttemptKernel(
        checkpoints=MemoryAttemptCheckpointStore(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=_REVISION,
        validators=IntakePlugin.spec.commit_validators,
    )
    return kernel, store


def test_resolve_plan_commits_with_registered_validators(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plan, _ = install_plan(
        project,
        "CH-SEAL-RESOLVE",
        candidates=("api", "e2e"),
        proposed=("api",),
    )
    contract = TASK_ATTEMPT_CONTRACTS["resolve-plan"]
    assert set(contract.validators) <= set(IntakePlugin.spec.commit_validators)
    validated = ResolvePlanInputV1(
        change_id=plan.change_id,
        requirement_digest=plan.requirement_digest,
        candidate_test_families=plan.candidate_test_families,
        budgets=plan.resolved_budgets,
        policy_resource_id=plan.policy_resource_id,
        policy_digest=plan.policy_digest,
        family_policy=FamilyPolicyV1.model_validate(DEFAULT_POLICY["test_family_policy"]),
        exploration_ref=plan.exploration_ref,
        impact_inventory_ref=plan.impact_inventory_ref,
        source_resource_digests=plan.quality_goal.source_resource_digests,
        capability_leafs=("entities.item.constraints.name",),
    )
    executor = DeterministicTaskExecutor(
        contract.handler_id,
        op_handler(contract.handler_id),
        contract.output_model,
    )
    resolved = resolve_contract(contract, executor=executor)
    attempt_key = derive_attempt_key(
        invocation_id="inv-seal-resolve",
        graph_revision=_REVISION,
        public_entrypoint="full",
        semantic_node_id="intake.resolve-plan",
        business_activation=BusinessActivation.one_shot(),
        contract_id=contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-seal-resolve",
        public_entrypoint="full",
        semantic_node_id="intake.resolve-plan",
        attempt_key=attempt_key,
        fencing_token=1,
    )
    kernel, store = _kernel(project, tmp_path / "attempts", tmp_path / "receipts")
    try:
        result = asyncio.run(kernel.execute_or_recover(attempt_key, resolved, validated, context))
        assert isinstance(result, CommittedTaskResult)
        assert result.output.preparation_refs_ref.path == PREPARATION_REFS_PATH
        assert (project / PREPARATION_REFS_PATH).is_file()
    finally:
        store.close()


def _ref(path: str, body: bytes = b"x") -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(body).hexdigest())


def _write(root: Path, ref: EvidenceArtifactRefV1, body: bytes = b"x") -> None:
    target = root.joinpath(*ref.path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def test_coverage_rework_commits_with_registered_validators(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plan = _ref(f"qa/results/plan/{_PLAN}/resolved-assurance-plan.json", b"plan")
    reviewed = ReviewedCaseV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        plan_digest=_PLAN,
        plan_ref=plan,
        preparation_refs=(plan, _ref("qa/results/preparation/context.json", b"prep")),
        case_refs=(_ref(_CASE, b"case"),),
        review_ref=_ref("qa/results/review/case-review.json", b"review"),
        selection_ref=_ref("qa/results/cases/epochs/0/selection.json", b"selection"),
    )
    for ref, body in (
        (reviewed.plan_ref, b"plan"),
        (reviewed.preparation_refs[1], b"prep"),
        (reviewed.case_refs[0], b"case"),
        (_ref("qa/results/inspect/epochs/0/gaps.json", b"gap"), b"gap"),
    ):
        _write(project, ref, body)
    case = stage_json_artifact(project, REVIEWED_CASE_PATH, reviewed)
    handoff = stage_json_artifact(
        project,
        COVERAGE_REWORK_HANDOFF_PATH,
        CoverageReworkHandoffV1(
            source_epoch=0,
            assessment_refs=(_ref("qa/results/inspect/epochs/0/gaps.json", b"gap"),),
        ),
    )
    validated = CoverageReworkInputV1(
        change_id=_CHANGE,
        coverage_epoch=1,
        handoff_ref=EvidenceArtifactRefV1(path=handoff.path, digest=handoff.digest),
        reviewed_case_ref=EvidenceArtifactRefV1(path=case.path, digest=case.digest),
        inspect_receipt=ReceiptRef(receipt_id="inspect-0", receipt_digest=_SHA),
    )
    contract = TASK_ATTEMPT_CONTRACTS["coverage-rework"]
    assert set(contract.validators) <= set(IntakePlugin.spec.commit_validators)
    executor = DeterministicTaskExecutor(
        contract.handler_id,
        op_handler(contract.handler_id),
        contract.output_model,
    )
    resolved = resolve_contract(contract, executor=executor)
    attempt_key = derive_attempt_key(
        invocation_id="inv-seal-rework",
        graph_revision=_REVISION,
        public_entrypoint="full",
        semantic_node_id="intake.coverage-rework",
        business_activation=BusinessActivation.one_shot(),
        contract_id=contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-seal-rework",
        public_entrypoint="full",
        semantic_node_id="intake.coverage-rework",
        attempt_key=attempt_key,
        fencing_token=1,
    )
    kernel, store = _kernel(project, tmp_path / "attempts", tmp_path / "receipts")
    try:
        result = asyncio.run(kernel.execute_or_recover(attempt_key, resolved, validated, context))
        assert isinstance(result, CommittedTaskResult)
        assert result.output.rework_ref.path == "qa/results/cases/case-rework-context.json"
    finally:
        store.close()
