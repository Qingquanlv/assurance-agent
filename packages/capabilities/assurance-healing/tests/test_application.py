from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_json_bytes
from pydantic import ValidationError

from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    TestRepairResultV1 as RepairAgentResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.operations.agent import FixProposalFinalizeHandler
from assurance_healing.operations.application import (
    ApplyTestRepairFinalizeHandler,
    ApplyTestRepairPrepareHandler,
    expected_repair_history,
    repair_history_path,
)
from assurance_healing.operations.keys import derive_approval_id
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.capabilities.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import BINDING, execute_task
from tests.acg_plan_fixture import install_plan

CHANGE = "CH-REPAIR-1"
SOURCE = "qa/tests/api/test_users.py"
TARGET = "qa/tests/api/test_users.py"
MAPPING = "qa/results/generated/mapping.json"
PROPOSAL = "qa/results/healing/fix-proposal.json"
APPROVAL = "qa/results/healing/approval.json"
EXECUTION = "qa/results/execution/execute-result.json"
CASE = "qa/cases/api/case.yaml"
REVIEW = "qa/results/review/case-review.json"
PREP = "qa/results/intake/prepare.json"
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
        "path": f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json",
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
        "family_outcomes": [{"family": "api", "state": "executed"}],
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
    selection_ref = _write(project, "qa/results/cases/epochs/0/selection.json", b'{"schema_version":"1"}\n')
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
            "selection_ref": selection_ref,
        },
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "execution_ref": execution_ref,
        "mapping_ref": mapping_ref,
        "source_refs": [source_ref],
        "allowed_test_paths": [SOURCE],
    }
    return payload, before


def _write_expected_repair_history(
    stage: Path,
    payload: dict[str, object],
    *,
    after: bytes,
    outputs: list[str] | None = None,
) -> bytes:
    business = ApplyTestRepairInputV1.model_validate(payload)
    changed = tuple(
        EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(after).hexdigest())
        for path in (outputs or [SOURCE])
    )
    verified = VerifiedTestRepairV1(
        change_id=business.change_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        coverage_epoch=business.coverage_epoch,
        repair_round=business.repair_round,
        changed_test_refs=changed,
        mapping_ref=business.mapping_ref,
    )
    history = expected_repair_history(business, verified)
    data = canonical_json_bytes(history.model_dump(mode="json")) + b"\n"
    relative = repair_history_path(
        coverage_epoch=business.coverage_epoch,
        repair_round=business.repair_round,
    )
    _write(stage, relative, data)
    return data


def _agent_result(output_files: list[str]) -> dict[str, object]:
    result = RepairAgentResultV1(
        schema_version="1", change_id=CHANGE, output_files=tuple(output_files), summary="repair fixture"
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
        cast(JSONValue, {**payload, "agent_result": _agent_result(outputs)}),
        project,
        write_root=stage,
        capability_id="assurance.healing.apply-test-repair.finalize",
    )


def test_applied_requires_committed_changed_tests() -> None:
    with pytest.raises(ValidationError):
        AppliedTestRepairV1(
            change_id=CHANGE,
            plan_digest="d" * 64,
            plan_ref=EvidenceArtifactRefV1(path="qa/results/plan.json", digest="e" * 64),
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
    first_history = _write_expected_repair_history(stage, payload, after=after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure
    output = cast(dict[str, Any], result.outcome.output)
    assert output["changed_test_refs"] == [{"path": SOURCE, "digest": hashlib.sha256(after).hexdigest()}]
    assert output["mapping_ref"] == payload["mapping_ref"]
    history_path = stage / "qa/results/healing/epochs/0/rounds/1/repair.json"
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
    _write_expected_repair_history(stage, payload, after=after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure


@pytest.mark.asyncio
async def test_repair_changes_only_the_approved_file_in_a_two_file_generation(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    other_target = "qa/tests/api/test_other.py"
    other_source = other_target
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
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    _write_expected_repair_history(stage, payload, after=after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure
    assert (tmp_path / other_source).read_bytes() == other_bytes
    assert not (stage / other_source).exists()

    _write(stage, other_source, b"def test_other():\n    x = 1\n    assert True\n")
    extra = await _finalize(tmp_path, stage, payload, sorted([SOURCE, other_source]))
    assert extra.status == "failed"


@pytest.mark.asyncio
async def test_finalize_generates_host_repair_history(tmp_path: Path) -> None:
    from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS
    from tests.capabilities.finalize_phase import checked_finalize

    payload, _before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    result = await checked_finalize(
        AGENT_JOB_CONTRACTS["apply-test-repair"],
        stage,
        lambda: _finalize(tmp_path, stage, payload, [SOURCE]),
    )
    assert result.outcome.status == "succeeded", result.outcome.failure
    history = json.loads((stage / "qa/results/healing/epochs/0/rounds/1/repair.json").read_bytes())
    assert history["outcome"] == "applied"
    assert {"path": SOURCE, "digest": hashlib.sha256(after).hexdigest()} in history["source_refs"]


@pytest.mark.asyncio
async def test_prepare_rejects_a_proposal_outside_approval_scope(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    approval = _approval(_proposal())
    approval["paths"] = ["qa/tests/api/test_other.py"]
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
        path = "qa/tests/api/test_admin.py"
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
                    "path": f"qa/results/plan/{SHA}/resolved-assurance-plan.json",
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
                    "selection_ref": {
                        "path": "qa/results/cases/epochs/0/selection.json",
                        "digest": SHA,
                    },
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
                "path": f"qa/results/plan/{SHA}/resolved-assurance-plan.json",
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
