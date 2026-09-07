from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.verification import (
    VerificationEvidenceV1,
    VerifiedExecutionResultV1,
)
from assurance_execution.contracts.workflow import (
    VerifiedBridgeDefectResultV1,
    VerifiedIncompleteExecutionV1,
)
from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_execution.graphs.nodes import (
    activation_execute,
    activation_rerun,
    publish_execution,
    select_execute,
    select_rerun,
)
from assurance_execution.operations.agent_skills import _execution_id
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from assurance_generation.contracts.admission import (
    admit_verified_generation,
    diagnose_verified_bridge_defect,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    TestRepairResultV1 as RepairAgentResultV1,
)
from assurance_healing.graphs.factory import HealingGraphs
from assurance_healing.graphs.nodes import publish_applied_repair, select_application
from assurance_healing.operations.application import ApplyTestRepairFinalizeHandler
from assurance_healing.operations.keys import derive_approval_id
from assurance_healing.contracts.application import RepairAuthorizationV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.loop_history import build_loop_round_history
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_product.graphs.execute import build_execute_graph
from assurance_product.graphs.factory import ProductFeatureBundles
from assurance_product.graphs.state import ProductState
from assurance_product.models import ProductInputV1, VerificationHostConfigV1
from assurance_product.repair_authorization import RepairAuthorizationIssuer
from assurance_product.verification_execution import (
    ProfiledExecutionExecutor,
    VerificationConfiguration,
)
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import (
    FinalizedInspectionV1,
    InspectionOutcomeV1,
)
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.verification import VerificationVerdictV1
from assurance_quality.graphs.factory import QualityGraphs
from assurance_quality.graphs.nodes import publish_inspect, select_materialize_assessment
from assurance_quality.operations.assessment import materialize_assessment_inputs
from assurance_quality.operations.verification import verification_failure_facts
from graph_engine.attempts import AttemptKey, derive_attempt_key
from graph_engine.attempts.events import (
    ActivityPrepared,
    ActivityTerminalObserved,
    AttemptOpened,
    AttemptTerminated,
    CommitPrepared,
    ResourcesAuthorized,
    ResourcesReleased,
    WorkspacePromoted,
)
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.plugin_api import TaskOutcome
from agent_runtime_contracts import AgentRunResult
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import execute_task
from tests.product.test_product_input import valid_product_input
from tests.verified_generation_fixture import accepted_verified_execution_input

INVOCATION_ID = "invocation"
ENTRYPOINT = "execute"
REVISION = "f" * 64
CONTRACT_ID = "assurance.execution.task.execute.v1"


async def _repairable_state(
    project: Path,
    *,
    technical_refactor: str | None = None,
) -> tuple[dict[str, object], MemoryAttemptJournal, RepairAuthorizationIssuer]:
    prepared = accepted_verified_execution_input(project)
    if technical_refactor is not None:
        prepared = _recompile_after_technical_refactor(project, prepared, technical_refactor)
    generation = prepared.generation_result
    assert generation is not None
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    bridge_path = project / bridge.path
    bridge_path.write_text("def test_tc_user_create_001__create():\n    pass\n")
    bridge = bridge.model_copy(update={"digest": hashlib.sha256(bridge_path.read_bytes()).hexdigest()})
    generation = generation.model_copy(
        update={
            "source_refs": tuple(bridge if ref.path == bridge.path else ref for ref in generation.source_refs)
        }
    )
    state = cast(dict[str, object], prepared.model_dump(mode="json"))
    state["generation_result"] = generation.model_dump(mode="json")
    selected = select_execute(state)
    contract = TASK_ATTEMPT_CONTRACTS[CONTRACT_ID]
    attempt_key = derive_attempt_key(
        invocation_id=INVOCATION_ID,
        graph_revision=REVISION,
        public_entrypoint=ENTRYPOINT,
        semantic_node_id="execution.execute",
        business_activation=activation_execute(state),
        contract_id=contract.contract_id,
        validated_input=selected,
    )
    defect = diagnose_verified_bridge_defect(
        project,
        generation=generation,
        validation_profile="api_db.v1",
        selected_test_families=prepared.selected_test_families,
        capability_leafs=prepared.capability_leafs,
        attempt_key=attempt_key,
    )
    receipt = ReceiptRef(receipt_id="kernel", receipt_digest="9" * 64)
    raw = VerifiedBridgeDefectResultV1(
        defect=defect,
        batch_id=attempt_key.digest,
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    execution = VerifiedIncompleteExecutionV1(
        **raw.model_dump(mode="python"),
        receipt=receipt,
    )
    verification_ref = EvidenceArtifactRefV1(
        path=f"qa/changes/{generation.change_id}/inspect/verification.json",
        digest="8" * 64,
    )
    inspection = InspectionOutcomeV1(
        change_id=generation.change_id,
        coverage_epoch=generation.coverage_epoch,
        batch_id=execution.batch_id,
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        disposition="repairable_execution_failure",
        inspection_receipt=ReceiptRef(receipt_id="quality", receipt_digest="7" * 64),
        reviewed_case=generation.reviewed_case,
        mapping_ref=generation.mapping_ref,
        assessment_refs=(verification_ref,),
        reason_codes=("verification.required_evidence_missing",),
        verification_ref=verification_ref,
        verification_status="INCOMPLETE",
        verification_repairable_bridge=True,
    )
    state.update(
        execution_result=execution.model_dump(mode="json"),
        inspection_outcome=inspection.model_dump(mode="json"),
    )
    raw_payload = cast(JSONValue, raw.model_dump(mode="json"))
    input_payload = cast(JSONValue, selected.model_dump(mode="json"))
    contract_payload = cast(JSONValue, contract.canonical_projection())
    journal = MemoryAttemptJournal()
    await journal.append(
        attempt_key,
        (
            AttemptOpened(
                contract_digest=canonical_digest(contract_payload),
                input_digest=canonical_digest(input_payload),
                graph_revision=REVISION,
                invocation_id=INVOCATION_ID,
                public_entrypoint=ENTRYPOINT,
                semantic_node_id="execution.execute",
            ),
            ResourcesAuthorized(authorization_id="a" * 64),
            ActivityPrepared(activity_id=attempt_key.digest),
            ActivityTerminalObserved(
                activity_id=attempt_key.digest,
                outcome=raw_payload,
                outcome_digest=canonical_digest(raw_payload),
            ),
            CommitPrepared(prepared_digest="b" * 64),
            WorkspacePromoted(
                receipt_id=receipt.receipt_id,
                receipt_digest=receipt.receipt_digest,
                staged_digest="c" * 64,
            ),
            AttemptTerminated(
                resolution_kind="committed",
                output=raw_payload,
                receipt_id=receipt.receipt_id,
                receipt_digest=receipt.receipt_digest,
            ),
            ResourcesReleased(authorization_id="a" * 64),
        ),
        expected_revision=0,
        fencing_token=1,
    )
    return (
        state,
        journal,
        RepairAuthorizationIssuer(
            journal=journal,
            invocation_id=INVOCATION_ID,
            public_entrypoint=ENTRYPOINT,
            graph_revision=REVISION,
        ),
    )


def _recompile_after_technical_refactor(
    project: Path,
    prepared: ExecutionPrepareInputV1,
    scenario: str,
) -> ExecutionPrepareInputV1:
    """Refresh authenticated technical inputs while preserving business obligations."""

    generation = prepared.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    baseline = CaseExecutionPlanSetV1.model_validate_json(
        (project / generation.case_execution_plan_ref.path).read_bytes()
    ).cases[0]
    source_ref = next(ref for ref in generation.reviewed_case.preparation_refs if ref.path == "src/app.py")
    implementations = {
        "function_relocation": b"from app.services.users import create_user\n",
        "equivalent_orm_sql": b"def create_user_with_orm(values):\n    return User.create(**values)\n",
        "helper_extraction": (
            b"def persist_user(values):\n    return User.create(**values)\n\n"
            b"def create_user(values):\n    return persist_user(values)\n"
        ),
        "refreshed_digests": b"def create_user(values):\n    return User.create(**values)\n",
    }
    source_bytes = implementations[scenario]
    (project / source_ref.path).write_bytes(source_bytes)
    refreshed_source = source_ref.model_copy(update={"digest": hashlib.sha256(source_bytes).hexdigest()})
    reviewed = generation.reviewed_case.model_copy(
        update={
            "preparation_refs": tuple(
                sorted(
                    (
                        refreshed_source if ref.path == source_ref.path else ref
                        for ref in generation.reviewed_case.preparation_refs
                    ),
                    key=lambda ref: ref.path,
                )
            )
        }
    )
    reviewed_path = project / f"qa/changes/{generation.change_id}/cases/reviewed-case.json"
    reviewed_path.write_bytes(canonical_json_bytes(cast(JSONValue, reviewed.model_dump(mode="json"))) + b"\n")
    review_inputs = tuple(sorted((*reviewed.preparation_refs, *reviewed.case_refs), key=lambda ref: ref.path))
    history = build_loop_round_history(
        change_id=generation.change_id,
        coverage_epoch=generation.coverage_epoch,
        loop_kind="case_review",
        family=None,
        round_index=0,
        outcome="pass",
        review_input_digest=canonical_digest(
            cast(JSONValue, [ref.model_dump(mode="json") for ref in review_inputs])
        ),
        source_refs=tuple(sorted((*review_inputs, reviewed.review_ref), key=lambda ref: ref.path)),
    )
    history_path = (
        project
        / f"qa/changes/{generation.change_id}/cases/reviews/epochs/{generation.coverage_epoch}/rounds/0.json"
    )
    history_path.write_bytes(canonical_json_bytes(cast(JSONValue, history.model_dump(mode="json"))) + b"\n")

    bindings_path = project / f"qa/changes/{generation.change_id}/plans/api-execution-bindings.json"
    bindings = json.loads(bindings_path.read_bytes())
    bindings["bindings"]["action.finished"]["credential_ref"] = f"credentials.{scenario}"
    bindings_bytes = json.dumps(bindings, indent=2, sort_keys=True).encode() + b"\n"
    bindings_path.write_bytes(bindings_bytes)
    technical_digest = hashlib.sha256(bindings_bytes).hexdigest()
    sut_digest = canonical_digest(cast(JSONValue, [refreshed_source.model_dump(mode="json")]))
    recompiled = baseline.model_copy(
        update={
            "reviewed_case": reviewed,
            "technical_config_digest": technical_digest,
            "sut_digest": sut_digest,
            "action": baseline.action.model_copy(update={"credential_ref": f"credentials.{scenario}"}),
            "bindings": tuple(
                item.model_copy(
                    update={
                        "actual": item.actual.model_copy(update={"credential_ref": f"credentials.{scenario}"})
                    }
                )
                if item.obligation_id == "action.finished"
                else item
                for item in baseline.bindings
            ),
        }
    )
    machine_set = CaseExecutionPlanSetV1(change_id=generation.change_id, cases=(recompiled,))
    machine_bytes = canonical_json_bytes(cast(JSONValue, machine_set.model_dump(mode="json"))) + b"\n"
    machine_path = project / generation.case_execution_plan_ref.path
    machine_path.write_bytes(machine_bytes)
    machine_ref = generation.case_execution_plan_ref.model_copy(
        update={"digest": hashlib.sha256(machine_bytes).hexdigest()}
    )

    manifest_ref = next(
        ref for ref in generation.source_refs if "/codegen/api-generated-files.json" in ref.path
    )
    baseline_manifest_digest = manifest_ref.digest
    manifest = json.loads((project / manifest_ref.path).read_bytes())
    mapping = manifest["mapping"]
    mapping["reviewed_case"] = reviewed.model_dump(mode="json")
    mapping["case_execution_plan_ref"] = machine_ref.model_dump(mode="json")
    mapping["case_execution_plan_digest"] = machine_ref.digest
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    (project / manifest_ref.path).write_bytes(manifest_bytes)
    manifest_ref = manifest_ref.model_copy(update={"digest": hashlib.sha256(manifest_bytes).hexdigest()})
    bridge_ref = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    bindings_ref = EvidenceArtifactRefV1(
        path=bindings_path.relative_to(project).as_posix(), digest=technical_digest
    )
    generation = generation.model_copy(
        update={
            "reviewed_case": reviewed,
            "source_refs": tuple(sorted((manifest_ref, bridge_ref), key=lambda ref: ref.path)),
            "plan_refs": tuple(sorted((bindings_ref, machine_ref), key=lambda ref: ref.path)),
            "case_execution_plan_ref": machine_ref,
            "case_execution_plan_digest": machine_ref.digest,
        }
    )
    admission = admit_verified_generation(
        project,
        project,
        change_id=generation.change_id,
        coverage_epoch=generation.coverage_epoch,
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        reviewed_case=reviewed,
        validation_profile="api_db.v1",
        selected_test_families=prepared.selected_test_families,
        capability_leafs=prepared.capability_leafs,
        case_execution_plan_ref=machine_ref,
    )
    assert admission.machine_plans.cases[0].obligation_projection() == baseline.obligation_projection()
    assert recompiled.technical_config_digest != baseline.technical_config_digest
    assert recompiled.sut_digest != baseline.sut_digest
    assert manifest_ref.digest != baseline_manifest_digest
    assert prepared.verification is not None
    return prepared.model_copy(
        update={
            "generation_result": generation,
            "verification": prepared.verification.model_copy(update={"case_execution_plan_ref": machine_ref}),
        }
    )


def test_product_issues_repair_authorization_from_current_checkpoint_and_attempt(
    tmp_path: Path,
) -> None:
    state, _journal, issuer = asyncio.run(_repairable_state(tmp_path))

    authorization = asyncio.run(issuer.issue(state))

    execution = VerifiedIncompleteExecutionV1.model_validate(state["execution_result"])
    assert authorization.attempt_key == execution.defect.attempt_key
    assert authorization.bridge_ref == execution.defect.bridge_ref
    assert authorization.expected_digest == execution.defect.expected_digest
    assert authorization.receipt == execution.receipt


def test_product_rejects_repair_when_checkpoint_receipt_is_not_the_committed_attempt(
    tmp_path: Path,
) -> None:
    state, _journal, issuer = asyncio.run(_repairable_state(tmp_path))
    execution = VerifiedIncompleteExecutionV1.model_validate(state["execution_result"])
    state["execution_result"] = execution.model_copy(
        update={"receipt": ReceiptRef(receipt_id="other", receipt_digest="6" * 64)}
    ).model_dump(mode="json")

    with pytest.raises(ValueError, match="committed execution Attempt"):
        asyncio.run(issuer.issue(state))


def test_product_rejects_repair_when_quality_describes_another_execution(tmp_path: Path) -> None:
    state, _journal, issuer = asyncio.run(_repairable_state(tmp_path))
    inspection = InspectionOutcomeV1.model_validate(state["inspection_outcome"])
    state["inspection_outcome"] = inspection.model_copy(update={"batch_id": "another-execution"}).model_dump(
        mode="json"
    )

    with pytest.raises(ValueError, match="same execution"):
        asyncio.run(issuer.issue(state))


def _node_graph(node: Any):
    graph = StateGraph(cast(Any, dict))
    graph.add_node("node", node)
    graph.add_edge(START, "node")
    graph.add_edge("node", END)
    return graph.compile(checkpointer=None)


def _ref(change_id: str, suffix: str, digest: str) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(
        path=f"qa/changes/{change_id}/{suffix}",
        digest=digest,
    )


def _successful_rerun(
    project: Path,
    value: ExecutionPrepareInputV1,
    attempt_key: AttemptKey,
) -> VerifiedExecutionResultV1:
    generation = value.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    plan = CaseExecutionPlanSetV1.model_validate_json(
        (project / generation.case_execution_plan_ref.path).read_bytes()
    ).cases[0]
    assert value.verification is not None
    execution_id = _execution_id(attempt_key, value.verification.nodeid)
    process_ref = _ref(
        generation.change_id,
        f"execution/{execution_id}/process_terminal.json",
        "5" * 64,
    )
    evidence = VerificationEvidenceV1.model_validate(
        {
            "execution_id": execution_id,
            "manifest_digest": "6" * 64,
            "receipt_ref": process_ref.model_dump(mode="json"),
            "observations": [],
            "host_completion": {"state": "complete"},
            "collector_completion": {"state": "not_required"},
            "state": "collected",
        }
    )
    return VerifiedExecutionResultV1(
        validation_profile="api_db.v1",
        change_id=generation.change_id,
        case_id=plan.case_id,
        reviewed_case=generation.reviewed_case,
        coverage_epoch=generation.coverage_epoch,
        repair_round=value.repair_round,
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        case_execution_plan_ref=generation.case_execution_plan_ref,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        execution_id=execution_id,
        attempt_key=attempt_key,
        batch_id="rerun-batch-1",
        mapping_digest=generation.mapping_ref.digest,
        manifest_ref=_ref(
            generation.change_id,
            f"execution/{execution_id}/manifest.json",
            "6" * 64,
        ),
        evidence_ref=_ref(
            generation.change_id,
            f"execution/{execution_id}/outcome.json",
            "7" * 64,
        ),
        execution_authority_ref=_ref(
            generation.change_id,
            f"execution/{execution_id}/execution_terminal.json",
            "8" * 64,
        ),
        raw_evidence_refs=(process_ref,),
        executed_at=datetime(2026, 9, 6, 1, tzinfo=UTC),
        completion_status="collected",
        evidence=evidence,
    )


def _write_ref(project: Path, relative: str, value: object) -> EvidenceArtifactRefV1:
    data = canonical_json_bytes(cast(JSONValue, value)) + b"\n"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _install_repair_decision(
    project: Path,
    authorization: RepairAuthorizationV1,
) -> tuple[EvidenceArtifactRefV1, EvidenceArtifactRefV1]:
    proposal = {
        "schema_version": "1",
        "change_id": authorization.bridge_ref.path.split("/")[2],
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "FIX-BRIDGE",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "needs_review": False,
                "files_to_modify": [authorization.bridge_ref.path],
            }
        ],
    }
    change_id = cast(str, proposal["change_id"])
    proposal_digest = canonical_digest(cast(JSONValue, proposal))
    approval = {
        "schema_version": "1",
        "approval_id": derive_approval_id(
            owner_id="assurance.healing",
            candidate_digest=authorization.expected_digest,
            baseline_digest=authorization.bridge_ref.digest,
            policy_digest="d" * 64,
            proposal_digest=proposal_digest,
        ),
        "change_id": change_id,
        "owner_id": "assurance.healing",
        "root_invocation_id": INVOCATION_ID,
        "interrupt_task_id": "approval-bridge",
        "source_gate_attempt_id": "quality-inspect",
        "source_tree_id": "tree-before",
        "target_tree_id": "tree-after",
        "proposal_digest": proposal_digest,
        "fixer_authority_digest": "a" * 64,
        "candidate_digest": authorization.expected_digest,
        "baseline_digest": authorization.bridge_ref.digest,
        "policy_digest": "d" * 64,
        "targets": ["api"],
        "paths": [authorization.bridge_ref.path],
        "action": "approve_and_apply",
    }
    return (
        _write_ref(project, f"qa/changes/{change_id}/healing/fix-proposal.json", proposal),
        _write_ref(project, f"qa/changes/{change_id}/healing/approval.json", approval),
    )


def _product_input_for_generation(
    generation: GenerationCycleResultV1,
    verification: object,
    *,
    policy_digest: str,
) -> dict[str, object]:
    payload = valid_product_input(
        change_id=generation.change_id,
        run_mode="verify",
        resolved_plan_ref=generation.plan_ref.model_dump(mode="json"),
        capability_leafs=("entities.item.create",),
        product_policy={
            "resource_id": "assurance.product.configuration.product-policy",
            "sha256": policy_digest,
        },
        validation_profile="api_db.v1",
        verification_config_digest="c" * 64,
        verification_policy={
            "resource_id": "assurance.product.configuration.verification-policy",
            "sha256": "f" * 64,
        },
    )
    state = cast(dict[str, object], ProductInputV1.model_validate(payload).model_dump(mode="json"))
    state.update(
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref.model_dump(mode="json"),
        coverage_epoch=generation.coverage_epoch,
        selected_test_families=["api"],
        generation_result=generation.model_dump(mode="json"),
        verification=verification,
        healing_rounds_used=0,
    )
    return state


def _real_task8_graph(
    project: Path,
    initial: Mapping[str, object],
    *,
    repeat_defect: bool,
):
    config = VerificationConfiguration(
        validation_profile="api_db.v1",
        host=VerificationHostConfigV1(
            managed_sut_authority_handle="sut.authority",
            credential_handle="sut.credential",
        ),
    )
    facade = ProfiledExecutionExecutor(
        config=config,
        config_digest="c" * 64,
        legacy=None,
        callable_path="assurance_execution.operations.verified_attempt:VerifiedAttemptHandler.execute",
    )
    initial_execution = VerifiedIncompleteExecutionV1.model_validate(initial["execution_result"])
    initial_results: list[VerifiedIncompleteExecutionV1] = []
    rerun_keys: list[AttemptKey] = []
    host_calls: list[str] = []
    live_journal = MemoryAttemptJournal()
    live_issuer = RepairAuthorizationIssuer(
        journal=live_journal,
        invocation_id=INVOCATION_ID,
        public_entrypoint=ENTRYPOINT,
        graph_revision=REVISION,
    )

    class RerunHost:
        async def execute(self, value: ExecutionPrepareInputV1):
            host_calls.append("rerun")
            return SimpleNamespace(
                outcome=TaskOutcome.succeeded(
                    cast(
                        JSONValue,
                        _successful_rerun(project, value, rerun_keys[-1]).model_dump(mode="json"),
                    )
                )
            )

    facade._host = RerunHost()
    facade._call = lambda value, _scope: value  # type: ignore[method-assign]

    async def execute_node(state: Mapping[str, object]) -> dict[str, object]:
        value = select_execute(state)
        result = await facade.execute(
            value,
            cast(
                Any,
                SimpleNamespace(
                    workspace=SimpleNamespace(project_root=project),
                    execution=SimpleNamespace(attempt_key=initial_execution.defect.attempt_key),
                ),
            ),
        )
        raw = VerifiedBridgeDefectResultV1.model_validate(result.output.root)
        contract = TASK_ATTEMPT_CONTRACTS[CONTRACT_ID]
        raw_payload = cast(JSONValue, raw.model_dump(mode="json"))
        selected_payload = cast(JSONValue, value.model_dump(mode="json"))
        contract_payload = cast(JSONValue, contract.canonical_projection())
        await live_journal.append(
            raw.defect.attempt_key,
            (
                AttemptOpened(
                    contract_digest=canonical_digest(contract_payload),
                    input_digest=canonical_digest(selected_payload),
                    graph_revision=REVISION,
                    invocation_id=INVOCATION_ID,
                    public_entrypoint=ENTRYPOINT,
                    semantic_node_id="execution.execute",
                ),
                ResourcesAuthorized(authorization_id="a" * 64),
                ActivityPrepared(activity_id=raw.defect.attempt_key.digest),
                ActivityTerminalObserved(
                    activity_id=raw.defect.attempt_key.digest,
                    outcome=raw_payload,
                    outcome_digest=canonical_digest(raw_payload),
                ),
                CommitPrepared(prepared_digest="b" * 64),
                WorkspacePromoted(
                    receipt_id=initial_execution.receipt.receipt_id,
                    receipt_digest=initial_execution.receipt.receipt_digest,
                    staged_digest="c" * 64,
                ),
                AttemptTerminated(
                    resolution_kind="committed",
                    output=raw_payload,
                    receipt_id=initial_execution.receipt.receipt_id,
                    receipt_digest=initial_execution.receipt.receipt_digest,
                ),
                ResourcesReleased(authorization_id="a" * 64),
            ),
            expected_revision=0,
            fencing_token=1,
        )
        published = publish_execution(state, result.output, initial_execution.receipt)
        initial_results.append(VerifiedIncompleteExecutionV1.model_validate(published["execution_result"]))
        return published

    quality_calls = {"count": 0}

    def quality_node(state: Mapping[str, object]) -> dict[str, object]:
        quality_calls["count"] += 1
        if quality_calls["count"] > 1:
            cycle = cast(
                Any,
                state["execution_result"],
            )
            execution = cast(Any, cycle)
            from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1

            verified = VerifiedExecutionCycleResultV1.model_validate(execution)
            inspection = InspectionOutcomeV1(
                change_id=verified.change_id,
                coverage_epoch=verified.coverage_epoch,
                batch_id=verified.batch_id,
                plan_digest=verified.plan_digest,
                plan_ref=verified.plan_ref,
                disposition="needs_human",
                inspection_receipt=ReceiptRef(receipt_id="quality-rerun", receipt_digest="3" * 64),
                reviewed_case=verified.reviewed_case,
                mapping_ref=verified.mapping_ref,
                assessment_refs=(_ref(verified.change_id, "inspect/rerun.json", "2" * 64),),
                reason_codes=("rerun.observed",),
            )
            return {"inspection_outcome": inspection.model_dump(mode="json"), "status": "failed"}
        request = select_materialize_assessment(state)
        assessment = materialize_assessment_inputs(
            request,
            project_root=project,
            write_root=project,
        )
        metrics = MetricsDocument.model_validate_json((project / assessment.metrics_ref.path).read_bytes())
        sufficiency = TraceSufficiencyFacts.model_validate_json(
            (project / assessment.sufficiency_ref.path).read_bytes()
        )
        assert assessment.verification_ref is not None
        verdict = VerificationVerdictV1.model_validate_json(
            (project / assessment.verification_ref.path).read_bytes()
        )
        fact_ref = _ref(request.reviewed_case.change_id, "inspect/fact-baseline.json", "1" * 64)
        agent = InspectionResultV1(
            schema_version="1.0",
            change_id=assessment.change_id,
            batch_id=assessment.batch_id,
            inspect_mode="primary",
            classification_performed=True,
            status="analyzed",
            execution_digest=assessment.execution_digest,
            healing_digest=None,
            trace_digest=assessment.trace_ref.digest,
            coverage_digest=assessment.gaps_ref.digest,
            metrics_digest=assessment.metrics_ref.digest,
        )
        finalized = FinalizedInspectionV1(
            agent_result=agent,
            assessment=assessment,
            reviewed_case=request.reviewed_case,
            mapping_ref=request.generation.mapping_ref,
            metrics=metrics,
            sufficiency=sufficiency,
            failure_facts=verification_failure_facts(verdict),
            fact_baseline_ref=fact_ref,
            reason_codes=verdict.reason_codes,
            verification=verdict,
        )
        publishing_state = {
            **state,
            "assessment_inputs": assessment.model_dump(mode="json"),
            "fact_baseline_ref": fact_ref.model_dump(mode="json"),
        }
        return {
            "assessment_inputs": assessment.model_dump(mode="json"),
            "fact_baseline_ref": fact_ref.model_dump(mode="json"),
            **publish_inspect(
                publishing_state,
                finalized,
                ReceiptRef(receipt_id="quality", receipt_digest="7" * 64),
            ),
        }

    async def healing_node(state: Mapping[str, object]) -> dict[str, object]:
        authorization = RepairAuthorizationV1.model_validate(state["repair_authorization"])
        proposal_ref, approval_ref = _install_repair_decision(project, authorization)
        expected = (
            "from assurance_execution.bridge import execute_case\n\n"
            f"def {authorization.bridge_symbol}():\n"
            f"    execute_case({json.dumps(authorization.case_id)})\n"
        ).encode()
        stage = project / ".repair-stage"
        target = stage / authorization.bridge_ref.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
        result_payload = RepairAgentResultV1(
            change_id=cast(str, state["change_id"]),
            output_files=(authorization.bridge_ref.path,),
            summary="restore deterministic bridge",
        ).model_dump(mode="json")
        envelope = AgentRunResult(
            result_payload=cast(JSONValue, result_payload),
            result_digest=canonical_digest(cast(JSONValue, result_payload)),
            evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
            adapter_id="test.fake",
            adapter_version="1.0.0",
        )
        selected = select_application(
            {
                **state,
                "proposal_ref": proposal_ref.model_dump(mode="json"),
                "approval_ref": approval_ref.model_dump(mode="json"),
            }
        )
        task = await execute_task(
            ApplyTestRepairFinalizeHandler(),
            cast(
                JSONValue,
                {
                    **selected.model_dump(mode="json"),
                    "agent_result": envelope.model_dump(mode="json"),
                },
            ),
            project,
            write_root=stage,
            capability_id="assurance.healing.apply-test-repair.finalize",
        )
        assert task.outcome.status == "succeeded", task.outcome.failure
        output = cast(dict[str, object], task.outcome.output)
        (project / authorization.bridge_ref.path).write_bytes(expected)
        return publish_applied_repair(
            state,
            output,
            ReceiptRef(receipt_id="repair", receipt_digest="4" * 64),
        )

    async def rerun_node(state: Mapping[str, object]) -> dict[str, object]:
        value = select_rerun(state)
        contract = TASK_ATTEMPT_CONTRACTS["assurance.execution.task.run.v1"]
        key = derive_attempt_key(
            invocation_id=INVOCATION_ID,
            graph_revision=REVISION,
            public_entrypoint=ENTRYPOINT,
            semantic_node_id="execution.run",
            business_activation=activation_rerun(state),
            contract_id=contract.contract_id,
            validated_input=value,
        )
        rerun_keys.append(key)
        if repeat_defect:
            generation = value.generation_result
            assert generation is not None
            bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
            bridge_path = project / bridge.path
            bridge_path.write_text(f"def {initial_execution.defect.bridge_symbol}():\n    pass\n")
            damaged_ref = bridge.model_copy(
                update={"digest": hashlib.sha256(bridge_path.read_bytes()).hexdigest()}
            )
            generation = generation.model_copy(
                update={
                    "source_refs": tuple(
                        damaged_ref if ref.path == bridge.path else ref for ref in generation.source_refs
                    )
                }
            )
            value = value.model_copy(update={"generation_result": generation})
        try:
            result = await facade.execute(
                value,
                cast(
                    Any,
                    SimpleNamespace(
                        workspace=SimpleNamespace(project_root=project),
                        execution=SimpleNamespace(attempt_key=key),
                    ),
                ),
            )
        except ValueError as error:
            return {"attempt_failure": {"message": str(error)}, "status": "failed"}
        return publish_execution(
            state,
            result.output,
            ReceiptRef(receipt_id="rerun", receipt_digest="5" * 64),
            semantic_node_id="execution.run",
        )

    generation = GenerationCycleResultV1.model_validate(initial["generation_result"])
    features = ProductFeatureBundles(
        intake=IntakeGraphs(
            prepare=_node_graph(lambda _state: {}),
            load_plan=_node_graph(lambda _state: {}),
            case=_node_graph(lambda _state: {}),
        ),
        generation=GenerationGraphs(
            generation=_node_graph(
                lambda _state: {
                    "generation_result": generation.model_dump(mode="json"),
                    "status": "passed",
                }
            ),
            api=_node_graph(lambda _state: {}),
            e2e=_node_graph(lambda _state: {}),
            fuzz=_node_graph(lambda _state: {}),
            performance=_node_graph(lambda _state: {}),
        ),
        execution=ExecutionGraphs(
            execute=_node_graph(execute_node),
            rerun=_node_graph(rerun_node),
        ),
        quality=QualityGraphs(
            assess=_node_graph(quality_node),
            issue_review=_node_graph(lambda _state: {}),
            issue_analyze=_node_graph(lambda _state: {}),
            issue_reconcile=_node_graph(lambda _state: {}),
            report=_node_graph(lambda _state: {}),
        ),
        healing=HealingGraphs(
            repair_failure=_node_graph(healing_node),
            repair_coverage=_node_graph(lambda _state: {}),
        ),
        improvement=ImprovementGraphs(
            archive=_node_graph(lambda _state: {}),
            retro=_node_graph(lambda _state: {}),
            review=_node_graph(lambda _state: {}),
            evaluate=_node_graph(lambda _state: {}),
            export=_node_graph(lambda _state: {}),
            apply=_node_graph(lambda _state: {}),
            rollback=_node_graph(lambda _state: {}),
        ),
    )
    graph = build_execute_graph(
        features,
        validate=False,
        repair_authorization_issuer=live_issuer,
    ).compile(checkpointer=None)
    return graph, rerun_keys, host_calls, initial_results


@pytest.mark.parametrize("repeat_defect", [False, True])
def test_real_product_graph_applies_bridge_and_reruns_with_fresh_identity(
    tmp_path: Path,
    repeat_defect: bool,
) -> None:
    issuer_state, _journal, _issuer = asyncio.run(_repairable_state(tmp_path))
    generation = GenerationCycleResultV1.model_validate(issuer_state["generation_result"])
    from assurance_intake.contracts.plan import decode_plan

    plan = decode_plan((tmp_path / generation.plan_ref.path).read_bytes(), generation.plan_ref)
    start = _product_input_for_generation(
        generation,
        issuer_state["verification"],
        policy_digest=plan.policy_digest,
    )
    graph, rerun_keys, host_calls, initial_results = _real_task8_graph(
        tmp_path,
        issuer_state,
        repeat_defect=repeat_defect,
    )

    result = asyncio.run(graph.ainvoke(cast(ProductState, start), config={"recursion_limit": 50}))

    authorization = RepairAuthorizationV1.model_validate(result["repair_authorization"])
    assert result["allowed_test_paths"] == [authorization.bridge_ref.path]
    assert result["allowed_paths"] == [authorization.bridge_ref.path]
    assert AppliedTestRepairV1.model_validate(result["repair_result"]).repair_round == 1
    assert len(initial_results) == 1
    initial_execution = initial_results[0]
    assert len(rerun_keys) == 1
    assert rerun_keys[0] != initial_execution.defect.attempt_key
    if repeat_defect:
        assert result["terminal"] == {"status": "failed", "reason": "blocked"}
        assert host_calls == []
    else:
        from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1

        rerun = VerifiedExecutionCycleResultV1.model_validate(result["execution_result"])
        assert rerun.repair_round == 1
        assert rerun.attempt_key == rerun_keys[0]
        verification = cast(Mapping[str, object], start["verification"])
        assert rerun.execution_id == _execution_id(rerun_keys[0], cast(str, verification["nodeid"]))
        assert rerun.batch_id != initial_execution.batch_id
        assert host_calls == ["rerun"]


@pytest.mark.parametrize(
    "technical_refactor",
    ["function_relocation", "equivalent_orm_sql", "helper_extraction", "refreshed_digests"],
)
def test_legal_technical_refactor_recompiles_then_applies_and_reruns(
    tmp_path: Path,
    technical_refactor: str,
) -> None:
    issuer_state, _journal, _issuer = asyncio.run(
        _repairable_state(tmp_path, technical_refactor=technical_refactor)
    )
    generation = GenerationCycleResultV1.model_validate(issuer_state["generation_result"])
    from assurance_intake.contracts.plan import decode_plan

    plan = decode_plan((tmp_path / generation.plan_ref.path).read_bytes(), generation.plan_ref)
    start = _product_input_for_generation(
        generation,
        issuer_state["verification"],
        policy_digest=plan.policy_digest,
    )
    graph, rerun_keys, host_calls, initial_results = _real_task8_graph(
        tmp_path,
        issuer_state,
        repeat_defect=False,
    )

    result = asyncio.run(graph.ainvoke(cast(ProductState, start), config={"recursion_limit": 50}))

    from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1

    rerun = VerifiedExecutionCycleResultV1.model_validate(result["execution_result"])
    assert len(initial_results) == len(rerun_keys) == 1
    assert rerun.attempt_key == rerun_keys[0]
    verification = cast(Mapping[str, object], start["verification"])
    assert rerun.execution_id == _execution_id(rerun_keys[0], cast(str, verification["nodeid"]))
    assert host_calls == ["rerun"]
