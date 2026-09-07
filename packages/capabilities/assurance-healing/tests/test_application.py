from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.host_authority import HostSealingAuthority
from graph_engine.canonical import JSONValue, canonical_json_bytes
from pydantic import ValidationError

from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    TestRepairResultV1 as RepairAgentResultV1,
)
from assurance_execution.contracts.workflow import VerifiedGenerationDefectCycleV1
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.operations.agent import FixProposalFinalizeHandler
from assurance_healing.operations.application import (
    ApplyTestRepairFinalizeHandler,
    ApplyTestRepairPrepareHandler,
)
from assurance_healing.operations.keys import derive_approval_id
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import BINDING, execute_task
from tests.acg_plan_fixture import install_plan
from tests.verified_generation_fixture import (
    accepted_verified_execution_input,
    install_verified_generation_defect_cycle,
)

CHANGE = "CH-REPAIR-1"
SOURCE = f"qa/changes/{CHANGE}/generated/api/files/tests/api/test_users.py"
TARGET = "tests/api/test_users.py"
MAPPING = f"qa/changes/{CHANGE}/generated/mapping.json"
PROPOSAL = f"qa/changes/{CHANGE}/healing/fix-proposal.json"
APPROVAL = f"qa/changes/{CHANGE}/healing/approval.json"
EXECUTION = f"qa/changes/{CHANGE}/execution/execute-result.json"
CASE = f"qa/changes/{CHANGE}/cases/api/case.yaml"
REVIEW = f"qa/changes/{CHANGE}/review/case-review.json"
PREP = f"qa/changes/{CHANGE}/intake/prepare.json"
SHA = "a" * 64


def _write(root: Path, relative: str, data: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "digest": hashlib.sha256(data).hexdigest()}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": CHANGE,
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "FIX-1",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "needs_review": False,
                "files_to_modify": [SOURCE],
            }
        ],
    }


def _mapping(*, symbol: str = "test_users") -> dict[str, object]:
    return {
        "schema_version": "1",
        "selected": [f"{TARGET}::{symbol}"],
        "mappings": [
            {"case_id": "CASE_1", "test": f"{TARGET}::{symbol}", "capability": "users.read", "layer": "api"}
        ],
    }


def _execution(plan_digest: str = SHA, plan_ref: dict[str, str] | None = None) -> dict[str, Any]:
    bound_ref = plan_ref or {
        "path": f"qa/changes/{CHANGE}/plan/{plan_digest}/resolved-assurance-plan.json",
        "digest": SHA,
    }
    return {
        "schema_version": "1",
        "status": "failed",
        "change_id": CHANGE,
        "plan_digest": plan_digest,
        "plan_ref": bound_ref,
        "batch_id": "batch-1",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": {
            "schema_version": "1",
            "selected": [f"{TARGET}::test_users"],
            "mappings": [
                {
                    "test": f"{TARGET}::test_users",
                    "case_id": "CASE_1",
                    "capability": "users.read",
                    "layer": "api",
                }
            ],
        },
        "mapping_digest": SHA,
        "baseline_tree_id": "tree-1",
        "runner_profile_digest": SHA,
        "receipt_digest": SHA,
        "receipt": {
            "commands": [
                {
                    "family": "api",
                    **{
                        "command": ["pytest"],
                        "exit_code": 1,
                        "collected": 1,
                        "passed": 0,
                        "failed": 1,
                        "skipped": 0,
                    },
                }
            ]
        },
        "results": [
            {
                "test": f"{TARGET}::test_users",
                "status": "failed",
                "duration_ms": 1,
                "message": "fixture wiring error",
                "case_id": "CASE_1",
            }
        ],
    }


def _approval(proposal: dict[str, object]) -> dict[str, object]:
    proposal_digest = canonical_digest(cast(JSONValue, proposal))
    return {
        "schema_version": "1",
        "approval_id": derive_approval_id(
            owner_id="assurance.healing",
            candidate_digest="c" * 64,
            baseline_digest="b" * 64,
            policy_digest="d" * 64,
            proposal_digest=proposal_digest,
        ),
        "change_id": CHANGE,
        "owner_id": "assurance.healing",
        "root_invocation_id": "inv-1",
        "interrupt_task_id": "approval-1",
        "source_gate_attempt_id": "inspect-1",
        "source_tree_id": "tree-1",
        "target_tree_id": "tree-2",
        "proposal_digest": proposal_digest,
        "fixer_authority_digest": SHA,
        "candidate_digest": "c" * 64,
        "baseline_digest": "b" * 64,
        "policy_digest": "d" * 64,
        "targets": ["api"],
        "paths": [SOURCE],
        "action": "approve_and_apply",
    }


def _fixture(project: Path) -> tuple[dict[str, object], bytes]:
    plan, plan_ref = install_plan(
        project,
        CHANGE,
        capability_leafs=("users.read",),
    )
    before = b"def test_users(client):\n    response = wrong_client(client)\n    assert response.status_code == 200\n"
    case_ref = _write(project, CASE, b"schema_version: '1.0'\ncase_id: CASE_1\n")
    review_ref = _write(project, REVIEW, b'{"decision":"approved"}\n')
    prep_ref = _write(project, PREP, b'{"prepared":true}\n')
    mapping_ref = _write(project, MAPPING, _json_bytes(_mapping()))
    proposal = _proposal()
    proposal_ref = _write(project, PROPOSAL, _json_bytes(proposal))
    approval_ref = _write(project, APPROVAL, _json_bytes(_approval(proposal)))
    execution_ref = _write(
        project,
        EXECUTION,
        _json_bytes(_execution(plan.plan_digest, plan_ref)),
    )
    source_ref = _write(project, SOURCE, before)
    payload: dict[str, object] = {
        "change_id": CHANGE,
        "plan_digest": plan.plan_digest,
        "plan_ref": plan_ref,
        "coverage_epoch": 0,
        "repair_round": 1,
        "reviewed_case": {
            "change_id": CHANGE,
            "coverage_epoch": 0,
            "plan_digest": plan.plan_digest,
            "plan_ref": plan_ref,
            "preparation_refs": sorted(
                [plan_ref, prep_ref],
                key=lambda item: (item["path"], item["digest"]),
            ),
            "case_refs": [case_ref],
            "review_ref": review_ref,
        },
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "execution_ref": execution_ref,
        "mapping_ref": mapping_ref,
        "source_refs": [source_ref],
        "allowed_test_paths": [SOURCE],
    }
    return payload, before


def _agent_result(output_files: list[str], *, change_id: str = CHANGE) -> dict[str, object]:
    result = RepairAgentResultV1(
        schema_version="1", change_id=change_id, output_files=tuple(output_files), summary="repair fixture"
    )
    raw = cast(JSONValue, result.model_dump(mode="json"))
    envelope = AgentRunResult(
        result_payload=raw,
        result_digest=canonical_digest(raw),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    return envelope.model_dump(mode="json")


async def _finalize(project: Path, stage: Path, payload: dict[str, object], outputs: list[str]):
    return await execute_task(
        ApplyTestRepairFinalizeHandler(),
        cast(
            JSONValue,
            {
                **payload,
                "agent_result": _agent_result(outputs, change_id=str(payload["change_id"])),
            },
        ),
        project,
        write_root=stage,
        capability_id="assurance.healing.apply-test-repair.finalize",
    )


def _verified_repair_fixture(
    project: Path,
    *,
    record_current: bool = True,
) -> tuple[dict[str, object], str, bytes]:
    from assurance_execution.contracts.authority import record_current_generation_defect
    from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1
    from assurance_generation.contracts.admission import diagnose_verified_bridge_defect
    from graph_engine.attempts import BusinessActivation, derive_attempt_key

    prepared = accepted_verified_execution_input(project)
    generation = prepared.generation_result
    assert generation is not None
    bridge = next(ref for ref in generation.source_refs if "/generated/api/files/" in ref.path)
    original = (
        b"from assurance_execution.bridge import execute_case\n\n"
        b"def test_tc_user_create_001__create():\n"
        b'    execute_case("TC_USER_CREATE_001")\n'
    )
    (project / bridge.path).write_text("def forged():\n    return True\n", encoding="utf-8")
    invocation_id = "inv-1"
    public_entrypoint = "phase5"
    graph_revision = "3" * 64
    contract = TASK_ATTEMPT_CONTRACTS["assurance.execution.task.execute.v1"]
    activation = BusinessActivation.for_trigger(f"coverage.{generation.coverage_epoch}.execute")
    attempt_key = derive_attempt_key(
        invocation_id=invocation_id,
        graph_revision=graph_revision,
        public_entrypoint=public_entrypoint,
        semantic_node_id="execution.execute",
        business_activation=activation,
        contract_id="assurance.execution.task.execute.v1",
        validated_input=prepared,
    )
    defect = diagnose_verified_bridge_defect(
        project,
        generation=generation,
        validation_profile="api_db.v1",
        selected_test_families=("api",),
        capability_leafs=("entities.item.create",),
        attempt_key=attempt_key,
    )
    assert hashlib.sha256(original).hexdigest() == defect.expected_digest
    proposal_path = f"qa/changes/{generation.change_id}/healing/fix-proposal.json"
    approval_path = f"qa/changes/{generation.change_id}/healing/approval.json"
    proposal = {
        "schema_version": "1",
        "change_id": generation.change_id,
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "FIX-BRIDGE-1",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "needs_review": False,
                "files_to_modify": [bridge.path],
            }
        ],
    }
    proposal_digest = canonical_digest(cast(JSONValue, proposal))
    approval = {
        "schema_version": "1",
        "approval_id": derive_approval_id(
            owner_id="assurance.healing",
            candidate_digest=defect.expected_digest,
            baseline_digest=bridge.digest,
            policy_digest="d" * 64,
            proposal_digest=proposal_digest,
        ),
        "change_id": generation.change_id,
        "owner_id": "assurance.healing",
        "root_invocation_id": "inv-verified",
        "interrupt_task_id": "approval-verified",
        "source_gate_attempt_id": "inspect-verified",
        "source_tree_id": "tree-before",
        "target_tree_id": "tree-after",
        "proposal_digest": proposal_digest,
        "fixer_authority_digest": SHA,
        "candidate_digest": defect.expected_digest,
        "baseline_digest": bridge.digest,
        "policy_digest": "d" * 64,
        "targets": ["api"],
        "paths": [bridge.path],
        "action": "approve_and_apply",
    }
    binding = ExecutionAttemptBindingV1(
        invocation_id=invocation_id,
        public_entrypoint=public_entrypoint,
        semantic_node_id="execution.execute",
        attempt_key=attempt_key,
        business_activation=activation,
        graph_revision=graph_revision,
        contract_id="assurance.execution.task.execute.v1",
        contract_digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
        input_digest=canonical_digest(cast(JSONValue, prepared.model_dump(mode="json"))),
        change_id=generation.change_id,
        coverage_epoch=generation.coverage_epoch,
        repair_round=0,
        validation_profile=defect.validation_profile,
        generation_digest=canonical_digest(cast(JSONValue, generation.model_dump(mode="json"))),
    )
    cycle = install_verified_generation_defect_cycle(
        project,
        defect,
        execution_binding=binding,
    )
    if record_current:
        record_current_generation_defect(
            project,
            cycle,
            binding,
            selection_authority=HostSealingAuthority.open_for_project(project),
        )
    payload = {
        "change_id": generation.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "coverage_epoch": generation.coverage_epoch,
        "repair_round": 1,
        "reviewed_case": generation.reviewed_case.model_dump(mode="json"),
        "proposal_ref": _write(project, proposal_path, _json_bytes(proposal)),
        "approval_ref": _write(project, approval_path, _json_bytes(approval)),
        "execution_ref": None,
        "mapping_ref": generation.mapping_ref.model_dump(mode="json"),
        "source_refs": [ref.model_dump(mode="json") for ref in generation.source_refs],
        "allowed_test_paths": [bridge.path],
        "validation_profile": "api_db.v1",
        "selected_test_families": ["api"],
        "capability_leafs": ["entities.item.create"],
        "generation_defect": cycle.model_dump(mode="json"),
        "generation_defect_execution_binding": binding.model_dump(mode="json"),
    }
    return payload, bridge.path, original


def _alternative_authenticated_cycle(
    project: Path,
    cycle: VerifiedGenerationDefectCycleV1,
    binding: object,
    *,
    invocation_id: str | None = None,
    input_digest: str = "e" * 64,
):
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1
    from assurance_generation.contracts.admission import diagnose_verified_bridge_defect

    original = ExecutionAttemptBindingV1.model_validate(binding)
    selected_invocation = invocation_id or original.invocation_id
    key = AttemptKey(
        digest=canonical_digest(
            {
                "invocation_id": selected_invocation,
                "graph_revision": original.graph_revision,
                "public_entrypoint": original.public_entrypoint,
                "semantic_node_id": original.semantic_node_id,
                "business_activation": original.business_activation.model_dump(mode="json"),
                "contract_id": original.contract_id,
                "task_input_digest": input_digest,
            }
        )
    )
    defect = diagnose_verified_bridge_defect(
        project,
        generation=cycle.attempt.defect.generation,
        validation_profile=cycle.attempt.defect.validation_profile,
        selected_test_families=("api",),
        capability_leafs=("entities.item.create",),
        attempt_key=key,
    )
    alternative = original.model_copy(
        update={
            "invocation_id": selected_invocation,
            "attempt_key": key,
            "input_digest": input_digest,
        }
    )
    installed = install_verified_generation_defect_cycle(
        project,
        defect,
        execution_binding=alternative,
    )
    return installed, alternative


def _current_index_root(project: Path, change_id: str, invocation_id: str = "inv-1") -> Path:
    return (
        project
        / "qa"
        / "changes"
        / change_id
        / ".runtime"
        / "current-generation-defects"
        / hashlib.sha256(invocation_id.encode()).hexdigest()
    )


def _install_unsigned_current_index(
    project: Path,
    cycle: VerifiedGenerationDefectCycleV1,
    binding: object,
) -> Path:
    from assurance_execution.contracts.authority import CurrentGenerationDefectAuthorityV1
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1

    typed_binding = ExecutionAttemptBindingV1.model_validate(binding)
    record = CurrentGenerationDefectAuthorityV1(
        binding=typed_binding,
        cycle=cycle,
        terminal_receipt=cycle.attempt.authority_receipt,
        promotion_receipt=cycle.execution_provenance.promotion_receipt,
    )
    path = _current_index_root(project, typed_binding.change_id, typed_binding.invocation_id) / (
        f"{typed_binding.attempt_key.digest}.json"
    )
    path.write_bytes(canonical_json_bytes(record.model_dump(mode="json")))
    path.chmod(0o400)
    return path


def _host_selection_receipt_path(project: Path, change_id: str) -> Path:
    root = (
        project
        / "qa"
        / "changes"
        / change_id
        / ".runtime"
        / "activities"
        / "inv-1"
        / "receipts"
        / ".selections"
    )
    matches = list(root.glob("*/*.json"))
    assert len(matches) == 1
    return matches[0]


@pytest.mark.asyncio
async def test_verified_bridge_repair_reauthenticates_defect_and_preserves_machine_plan(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "succeeded", result.outcome.failure
    output = cast(dict[str, Any], result.outcome.output)
    assert output["changed_test_refs"] == [
        {"path": bridge_path, "digest": hashlib.sha256(expected).hexdigest()}
    ]
    history = (
        stage
        / f"qa/changes/{payload['change_id']}/healing/epochs/{payload['coverage_epoch']}/rounds/1/repair.json"
    )
    first_history = history.read_bytes()

    resumed = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert resumed.outcome.status == "succeeded", resumed.outcome.failure
    assert history.read_bytes() == first_history


@pytest.mark.asyncio
async def test_verified_bridge_repair_requires_independent_current_execution_record(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path, record_current=False)
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "current execution" in result.outcome.failure.message


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_caller_advanced_round_projection(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    binding = cast(dict[str, object], payload["generation_defect_execution_binding"])
    payload["generation_defect_execution_binding"] = {**binding, "repair_round": 1}
    payload["repair_round"] = 2
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "current execution" in result.outcome.failure.message


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_authentic_same_invocation_foreign_attempt(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    current = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    foreign_cycle, foreign_binding = _alternative_authenticated_cycle(
        tmp_path,
        current,
        payload["generation_defect_execution_binding"],
    )
    payload["generation_defect"] = foreign_cycle.model_dump(mode="json")
    payload["generation_defect_execution_binding"] = foreign_binding.model_dump(mode="json")
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "current execution" in result.outcome.failure.message


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_replaced_index_for_another_authentic_attempt(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    current = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    foreign_cycle, foreign_binding = _alternative_authenticated_cycle(
        tmp_path,
        current,
        payload["generation_defect_execution_binding"],
    )
    index_root = _current_index_root(tmp_path, str(payload["change_id"]))
    for path in index_root.iterdir():
        path.unlink()
    _install_unsigned_current_index(tmp_path, foreign_cycle, foreign_binding)
    payload["generation_defect"] = foreign_cycle.model_dump(mode="json")
    payload["generation_defect_execution_binding"] = foreign_binding.model_dump(mode="json")
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "selection" in result.outcome.failure.message
    assert not (
        stage
        / f"qa/changes/{payload['change_id']}/healing/epochs/{payload['coverage_epoch']}/rounds/1/repair.json"
    ).exists()


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_publicly_recomputed_selection_for_authentic_attempt(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    current = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    foreign_cycle, foreign_binding = _alternative_authenticated_cycle(
        tmp_path,
        current,
        payload["generation_defect_execution_binding"],
    )
    index_root = _current_index_root(tmp_path, str(payload["change_id"]))
    for path in index_root.iterdir():
        path.unlink()
    _install_unsigned_current_index(tmp_path, foreign_cycle, foreign_binding)
    selection_path = _host_selection_receipt_path(tmp_path, str(payload["change_id"]))
    document = cast(dict[str, object], json.loads(selection_path.read_bytes()))
    selection: dict[str, object] = {
        "schema_version": "1",
        "attempt_key": foreign_binding.attempt_key.model_dump(mode="json"),
        "binding_digest": canonical_digest(cast(JSONValue, foreign_binding.model_dump(mode="json"))),
        "cycle_digest": canonical_digest(cast(JSONValue, foreign_cycle.model_dump(mode="json"))),
        "execution_id": None,
        "terminal_receipt": foreign_cycle.attempt.authority_receipt.model_dump(mode="json"),
        "promotion_receipt": foreign_cycle.execution_provenance.promotion_receipt.model_dump(mode="json"),
    }
    document["selection"] = selection
    document["selection_digest"] = canonical_digest(cast(JSONValue, selection))
    selection_path.chmod(0o600)
    selection_path.write_bytes(canonical_json_bytes(cast(JSONValue, document)))
    selection_path.chmod(0o400)
    payload["generation_defect"] = foreign_cycle.model_dump(mode="json")
    payload["generation_defect_execution_binding"] = foreign_binding.model_dump(mode="json")
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "selection" in result.outcome.failure.message
    assert not (
        stage
        / f"qa/changes/{payload['change_id']}/healing/epochs/{payload['coverage_epoch']}/rounds/1/repair.json"
    ).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["delete", "payload_mismatch"])
async def test_verified_bridge_repair_requires_intact_host_current_selection(
    tmp_path: Path,
    attack: str,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    selection_path = _host_selection_receipt_path(tmp_path, str(payload["change_id"]))
    if attack == "delete":
        selection_path.unlink()
    else:
        document = json.loads(selection_path.read_bytes())
        document["selection_digest"] = "f" * 64
        selection_path.chmod(0o600)
        selection_path.write_bytes(canonical_json_bytes(cast(JSONValue, document)))
        selection_path.chmod(0o400)
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "selection" in result.outcome.failure.message


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_ambiguous_current_execution_records(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    current = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    competing_cycle, competing_binding = _alternative_authenticated_cycle(
        tmp_path,
        current,
        payload["generation_defect_execution_binding"],
        input_digest="f" * 64,
    )
    _install_unsigned_current_index(tmp_path, competing_cycle, competing_binding)
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "ambiguous" in result.outcome.failure.message


def test_generation_defect_current_selection_publication_is_idempotent_and_cas(
    tmp_path: Path,
) -> None:
    from assurance_execution.contracts.authority import (
        load_current_generation_defect,
        record_current_generation_defect,
    )
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1

    payload, _bridge_path, _expected = _verified_repair_fixture(tmp_path)
    current_cycle = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    current_binding = ExecutionAttemptBindingV1.model_validate(payload["generation_defect_execution_binding"])

    authority = HostSealingAuthority.open_for_project(tmp_path)
    replayed = record_current_generation_defect(
        tmp_path,
        current_cycle,
        current_binding,
        selection_authority=authority,
    )

    assert replayed.cycle == current_cycle
    assert (
        load_current_generation_defect(
            tmp_path,
            change_id=current_binding.change_id,
            invocation_id=current_binding.invocation_id,
            public_entrypoint=current_binding.public_entrypoint,
        ).binding
        == current_binding
    )
    competing_cycle, competing_binding = _alternative_authenticated_cycle(
        tmp_path,
        current_cycle,
        current_binding,
        input_digest="f" * 64,
    )
    with pytest.raises(ValueError, match="publication failed"):
        record_current_generation_defect(
            tmp_path,
            competing_cycle,
            competing_binding,
            selection_authority=authority,
        )


def test_generation_defect_current_selection_rejects_cross_project_copy(tmp_path: Path) -> None:
    from assurance_execution.contracts.authority import load_current_generation_defect
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1

    first = tmp_path / "first"
    first.mkdir()
    payload, _bridge_path, _expected = _verified_repair_fixture(first)
    binding = ExecutionAttemptBindingV1.model_validate(payload["generation_defect_execution_binding"])
    copied = tmp_path / "copied"
    shutil.copytree(first, copied)

    with pytest.raises(ValueError, match="coordinates"):
        load_current_generation_defect(
            copied,
            change_id=binding.change_id,
            invocation_id=binding.invocation_id,
            public_entrypoint=binding.public_entrypoint,
        )


@pytest.mark.parametrize("field", ["repair_round", "business_activation", "contract_id"])
def test_generation_defect_authority_validates_the_complete_attempt_projection(
    tmp_path: Path,
    field: str,
) -> None:
    from assurance_execution.contracts.authority import authenticate_generation_defect_cycle
    from assurance_execution.contracts.workflow import ExecutionAttemptBindingV1
    from graph_engine.attempts import BusinessActivation

    payload, _bridge_path, _expected = _verified_repair_fixture(tmp_path)
    cycle = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    binding = ExecutionAttemptBindingV1.model_validate(payload["generation_defect_execution_binding"])
    replacement: object
    if field == "repair_round":
        replacement = 1
    elif field == "business_activation":
        replacement = BusinessActivation.for_trigger("coverage.2.repair.1.rerun")
    else:
        replacement = "assurance.execution.task.run.v1"
    attacked = binding.model_copy(update={field: replacement})

    with pytest.raises(ValueError, match="expected execution attempt|repair round"):
        authenticate_generation_defect_cycle(tmp_path, cycle, attacked)


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["forged_proof", "wrong_bridge"])
async def test_verified_bridge_repair_rejects_untrusted_or_noncanonical_candidate(
    tmp_path: Path, attack: str
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    stage = tmp_path / ".stage"
    if attack == "forged_proof":
        defect = cast(
            dict[str, object],
            cast(dict[str, object], payload["generation_defect"])["attempt"],
        )["defect"]
        defect = cast(dict[str, object], defect)
        defect["expected_digest"] = "9" * 64
        candidate = expected
    else:
        candidate = b"from assurance_execution.bridge import execute_case\n\ndef test_forged():\n    execute_case('forged')\n"
    _write(stage, bridge_path, candidate)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert result.outcome.failure.kind in {"invalid_input", "invalid_output"}


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["attempt_replay", "receipt_substitution"])
async def test_verified_bridge_repair_rejects_cross_attempt_or_receipt_substitution(
    tmp_path: Path, attack: str
) -> None:
    from assurance_execution.contracts.workflow import VerifiedGenerationDefectCycleV1
    from graph_engine.canonical import canonical_digest

    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    cycle = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    if attack == "attempt_replay":
        identity = cycle.attempt.authority_identity.model_copy(
            update={"invocation_id": "inv-replayed-by-caller"}
        )
        authority_ref = cycle.attempt.authority_receipt.model_copy(
            update={"identity_digest": canonical_digest(identity.model_dump(mode="json"))}
        )
        attempt = cycle.attempt.model_copy(
            update={"authority_identity": identity, "authority_receipt": authority_ref}
        )
        provenance = cycle.execution_provenance.model_copy(
            update={
                "invocation_id": identity.invocation_id,
                "source_terminal_receipt": authority_ref,
                "output_digest": canonical_digest(attempt.model_dump(mode="json")),
            }
        )
        forged = VerifiedGenerationDefectCycleV1(
            attempt=attempt,
            execution_provenance=provenance,
        )
    else:
        provenance = cycle.execution_provenance.model_copy(
            update={
                "promotion_receipt": cycle.execution_provenance.promotion_receipt.model_copy(
                    update={"receipt_digest": "f" * 64}
                )
            }
        )
        forged = VerifiedGenerationDefectCycleV1(
            attempt=cycle.attempt,
            execution_provenance=provenance,
        )
    payload["generation_defect"] = forged.model_dump(mode="json")
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "authority" in result.outcome.failure.message or "promotion" in result.outcome.failure.message


@pytest.mark.asyncio
async def test_verified_bridge_repair_rejects_complete_old_invocation_authority_tuple(
    tmp_path: Path,
) -> None:
    payload, bridge_path, expected = _verified_repair_fixture(tmp_path)
    current = VerifiedGenerationDefectCycleV1.model_validate(payload["generation_defect"])
    old_cycle, old_binding = _alternative_authenticated_cycle(
        tmp_path,
        current,
        payload["generation_defect_execution_binding"],
        invocation_id="inv-old-complete-tuple",
    )
    payload["generation_defect"] = old_cycle.model_dump(mode="json")
    payload["generation_defect_execution_binding"] = old_binding.model_dump(mode="json")
    stage = tmp_path / ".stage"
    _write(stage, bridge_path, expected)

    result = await _finalize(tmp_path, stage, payload, [bridge_path])

    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert "current invocation" in result.outcome.failure.message


def test_verified_generation_defect_rejects_substituted_old_execution_evidence(tmp_path: Path) -> None:
    payload, _bridge_path, _expected = _verified_repair_fixture(tmp_path)
    attempt = cast(dict[str, object], payload["generation_defect"])["attempt"]
    defect = cast(dict[str, object], cast(dict[str, object], attempt)["defect"])
    generation = defect["generation"]
    old_ref = cast(dict[str, object], generation)["mapping_ref"]
    payload["execution_ref"] = old_ref

    with pytest.raises(ValidationError, match="pre-dispatch generation defect"):
        ApplyTestRepairInputV1.model_validate(payload)


def test_applied_requires_committed_changed_tests() -> None:
    with pytest.raises(ValidationError):
        AppliedTestRepairV1(
            change_id=CHANGE,
            plan_digest="d" * 64,
            plan_ref=EvidenceArtifactRefV1(path="qa/changes/CH-1/plan.json", digest="e" * 64),
            coverage_epoch=0,
            repair_round=1,
            status="applied",
            changed_test_refs=(),
            mapping_ref=None,
            receipt=None,
        )


def test_proposal_result_cannot_parse_as_applied_repair() -> None:
    with pytest.raises(ValidationError):
        AppliedTestRepairV1.model_validate(_proposal())
    with pytest.raises(ValidationError):
        AppliedTestRepairV1.model_validate({"change_id": CHANGE})


@pytest.mark.asyncio
async def test_repair_finalizer_rejects_wrapped_input(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    result = await execute_task(
        ApplyTestRepairFinalizeHandler(),
        cast(
            JSONValue,
            {
                "validated_input": payload,
                "prepared": None,
                "agent_result": _agent_result([SOURCE]),
            },
        ),
        tmp_path,
        write_root=tmp_path / ".stage",
        capability_id="assurance.healing.apply-test-repair.finalize",
    )
    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_finalize_proves_existing_test_bytes_changed(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure
    output = cast(dict[str, Any], result.outcome.output)
    assert output["changed_test_refs"] == [{"path": SOURCE, "digest": hashlib.sha256(after).hexdigest()}]
    assert output["mapping_ref"] == payload["mapping_ref"]
    history_path = stage / f"qa/changes/{CHANGE}/healing/epochs/0/rounds/1/repair.json"
    first_history = history_path.read_bytes()
    history = json.loads(first_history)
    assert history["loop_kind"] == "implementation_repair"
    assert [ref["path"] for ref in history["source_refs"]].count(SOURCE) == 1

    resumed = await _finalize(tmp_path, stage, payload, [SOURCE])

    assert resumed.outcome.status == "succeeded"
    assert history_path.read_bytes() == first_history


@pytest.mark.asyncio
async def test_proposal_and_application_accept_the_same_generated_source_path(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    proposal = FixProposalResultV1.model_validate(_proposal()).model_dump(mode="json")
    proposal_stage = tmp_path / ".proposal-stage"
    proposal_bytes = canonical_json_bytes(proposal) + b"\n"
    _write(proposal_stage, PROPOSAL, proposal_bytes)
    agent = AgentRunResult(
        result_payload=proposal,
        result_digest=canonical_digest(proposal),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    proposal_input = {
        "change_id": CHANGE,
        "plan_digest": payload["plan_digest"],
        "plan_ref": payload["plan_ref"],
        "owner_id": "assurance.healing",
        "capability_leafs": ["users.read"],
        "allowed_paths": [SOURCE],
        "allowed_roots": ["qa"],
        "mapping_paths": [SOURCE],
        "baseline_digest": "b" * 64,
        "candidate_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "execution_evidence_digest": "e" * 64,
    }
    proposed = await execute_task(
        FixProposalFinalizeHandler(),
        {
            **proposal_input,
            "prepare": proposal_input,
            "agent_result": agent.model_dump(mode="json"),
        },
        tmp_path,
        write_root=proposal_stage,
    )
    assert proposed.status == "succeeded", proposed.failure
    payload["proposal_ref"] = _write(tmp_path, PROPOSAL, proposal_bytes)
    payload["approval_ref"] = _write(tmp_path, APPROVAL, _json_bytes(_approval(proposal)))
    stage = tmp_path / ".stage"
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure


@pytest.mark.asyncio
async def test_repair_changes_only_the_approved_file_in_a_two_file_generation(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    other_target = "tests/api/test_other.py"
    other_source = f"qa/changes/{CHANGE}/generated/api/files/{other_target}"
    other_bytes = b"def test_other():\n    assert True\n"
    other_ref = _write(tmp_path, other_source, other_bytes)
    source_refs = cast(list[dict[str, str]], payload["source_refs"])
    payload["source_refs"] = sorted([*source_refs, other_ref], key=lambda ref: ref["path"])
    payload["allowed_test_paths"] = sorted([SOURCE, other_source])
    execution = _execution()
    execution["mapping"]["selected"].append(f"{other_target}::test_other")
    execution["mapping"]["mappings"].append(
        {
            "case_id": "CASE_2",
            "test": f"{other_target}::test_other",
            "capability": "users.read",
            "layer": "api",
        }
    )
    execution["results"].append(
        {"test": f"{other_target}::test_other", "status": "passed", "duration_ms": 1, "case_id": "CASE_2"}
    )
    execution["receipt"]["commands"][0].update(collected=2, passed=1)
    payload["mapping_ref"] = _write(tmp_path, MAPPING, _json_bytes(execution["mapping"]))
    payload["execution_ref"] = _write(tmp_path, EXECUTION, _json_bytes(execution))
    prepared = await execute_task(
        ApplyTestRepairPrepareHandler(),
        cast(JSONValue, payload),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    assert request.workspace.allowed_outputs == (SOURCE,)
    stage = tmp_path / ".stage"
    _write(
        stage,
        SOURCE,
        b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n",
    )
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure
    assert (tmp_path / other_source).read_bytes() == other_bytes
    assert not (stage / other_source).exists()

    _write(stage, other_source, b"def test_other():\n    x = 1\n    assert True\n")
    extra = await _finalize(tmp_path, stage, payload, sorted([SOURCE, other_source]))
    assert extra.status == "failed"


@pytest.mark.asyncio
async def test_prepare_rejects_a_proposal_outside_approval_scope(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    approval = _approval(_proposal())
    approval["paths"] = [TARGET]
    payload["approval_ref"] = _write(tmp_path, APPROVAL, _json_bytes(approval))
    result = await execute_task(
        ApplyTestRepairPrepareHandler(),
        cast(JSONValue, payload),
        tmp_path,
        binding_data=BINDING,
    )
    assert result.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["no_change", "unapproved", "case", "product", "oracle"])
async def test_finalize_rejects_unproved_or_unsafe_patch(tmp_path: Path, mutation: str) -> None:
    payload, before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    path = SOURCE
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    if mutation == "no_change":
        after = before
    elif mutation == "unapproved":
        path = f"qa/changes/{CHANGE}/generated/api/files/tests/api/test_admin.py"
    elif mutation == "case":
        path = CASE
    elif mutation == "product":
        path = "src/users.py"
    elif mutation == "oracle":
        after = b"def test_users(client):\n    response = wrong_client(client)\n    assert response.status_code == 500\n"
    _write(stage, path, after)
    result = await _finalize(tmp_path, stage, payload, [path])
    assert result.outcome.status == "failed"
    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_finalize_rejects_rejected_or_missing_approval(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    _write(
        stage,
        SOURCE,
        b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n",
    )
    payload["approval_ref"] = None
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "failed"
    rejected = tmp_path / APPROVAL
    rejected.write_text('{"action":"reject"}\n', encoding="utf-8")
    payload["approval_ref"] = _write(tmp_path, APPROVAL, rejected.read_bytes())
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "failed"


@pytest.mark.asyncio
async def test_finalize_rejects_mapping_membership_change(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    altered = _write(tmp_path, MAPPING, _json_bytes(_mapping(symbol="test_admin")))
    payload["mapping_ref"] = altered
    stage = tmp_path / ".stage"
    _write(
        stage,
        SOURCE,
        b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n",
    )
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "failed"


def test_input_binds_reviewed_case_epoch() -> None:
    with pytest.raises(ValidationError):
        ApplyTestRepairInputV1.model_validate(
            {
                "change_id": CHANGE,
                "plan_digest": SHA,
                "plan_ref": {
                    "path": f"qa/changes/{CHANGE}/plan/{SHA}/resolved-assurance-plan.json",
                    "digest": SHA,
                },
                "coverage_epoch": 1,
                "repair_round": 1,
                "reviewed_case": {
                    "change_id": CHANGE,
                    "coverage_epoch": 0,
                    "preparation_refs": [{"path": PREP, "digest": SHA}],
                    "case_refs": [{"path": CASE, "digest": SHA}],
                    "review_ref": {"path": REVIEW, "digest": SHA},
                },
                "proposal_ref": {"path": PROPOSAL, "digest": SHA},
                "approval_ref": None,
                "execution_ref": {"path": EXECUTION, "digest": SHA},
                "mapping_ref": {"path": MAPPING, "digest": SHA},
                "source_refs": [{"path": SOURCE, "digest": SHA}],
                "allowed_test_paths": [SOURCE],
            }
        )


def test_publisher_adds_only_real_commit_receipt() -> None:
    from assurance_healing.contracts.application import VerifiedTestRepairV1
    from assurance_healing.graphs.nodes import publish_applied_repair

    ref = {"path": SOURCE, "digest": SHA}
    verified = VerifiedTestRepairV1.model_validate(
        {
            "change_id": CHANGE,
            "plan_digest": SHA,
            "plan_ref": {
                "path": f"qa/changes/{CHANGE}/plan/{SHA}/resolved-assurance-plan.json",
                "digest": SHA,
            },
            "coverage_epoch": 0,
            "repair_round": 1,
            "changed_test_refs": [ref],
            "mapping_ref": {"path": MAPPING, "digest": SHA},
        }
    )
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest=SHA)
    published = publish_applied_repair(
        {"kind": "failure", "rounds_used": 1, "rounds_budget": 2}, verified, receipt
    )
    repair_result = cast(dict[str, object], published["repair_result"])
    assert repair_result["status"] == "applied"
    assert repair_result["receipt"] == receipt.model_dump(mode="json")
