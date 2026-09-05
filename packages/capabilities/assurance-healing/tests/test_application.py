from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue
from pydantic import ValidationError

from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    TestRepairResultV1 as RepairAgentResultV1,
)
from assurance_healing.operations.application import ApplyTestRepairFinalizeHandler
from assurance_healing.operations.keys import derive_approval_id
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import execute_task

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
                "files_to_modify": [TARGET],
            }
        ],
    }


def _mapping(*, symbol: str = "test_users") -> dict[str, object]:
    return {
        "schema_version": "1",
        "layer": "api",
        "entries": [{"case_id": "CASE_1", "symbol": symbol, "target_file": TARGET}],
    }


def _execution() -> dict[str, object]:
    return {
        "schema_version": "1",
        "status": "failed",
        "change_id": CHANGE,
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
            "command": ["pytest"],
            "exit_code": 1,
            "collected": 1,
            "passed": 0,
            "failed": 1,
            "skipped": 0,
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
        "paths": [TARGET],
        "action": "approve_and_apply",
    }


def _fixture(project: Path) -> tuple[dict[str, object], bytes]:
    before = b"def test_users(client):\n    response = wrong_client(client)\n    assert response.status_code == 200\n"
    case_ref = _write(project, CASE, b"schema_version: '1.0'\ncase_id: CASE_1\n")
    review_ref = _write(project, REVIEW, b'{"decision":"approved"}\n')
    prep_ref = _write(project, PREP, b'{"prepared":true}\n')
    mapping_ref = _write(project, MAPPING, _json_bytes(_mapping()))
    proposal = _proposal()
    proposal_ref = _write(project, PROPOSAL, _json_bytes(proposal))
    approval_ref = _write(project, APPROVAL, _json_bytes(_approval(proposal)))
    execution_ref = _write(project, EXECUTION, _json_bytes(_execution()))
    source_ref = _write(project, SOURCE, before)
    payload: dict[str, object] = {
        "change_id": CHANGE,
        "coverage_epoch": 0,
        "repair_round": 1,
        "reviewed_case": {
            "change_id": CHANGE,
            "coverage_epoch": 0,
            "preparation_refs": [prep_ref],
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
        cast(JSONValue, {"validated_input": payload, "agent_result": _agent_result(outputs)}),
        project,
        write_root=stage,
        capability_id="assurance.healing.apply-test-repair.finalize",
    )


def test_applied_requires_committed_changed_tests() -> None:
    with pytest.raises(ValidationError):
        AppliedTestRepairV1(
            change_id=CHANGE,
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
