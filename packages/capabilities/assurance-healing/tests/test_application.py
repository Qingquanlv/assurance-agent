from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.artifacts import open_artifact, stage_json_artifact
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel, TaskHandler
from pydantic import ValidationError

from assurance_execution.contracts.workflow import EXECUTION_CYCLE_PATH, ExecutionCycleDocumentV1
from assurance_generation.contracts.workflow import GENERATION_CYCLE_PATH, GenerationCycleResultV1
from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    TestRepairResultV1 as RepairAgentResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.repair_input import ApplyBoundInputV1
from assurance_healing.operations.repair_input import opened_repair_input

from assurance_healing.ops.apply_test_repair import (
    finalize as apply_test_repair_finalize,
    prepare as apply_test_repair_prepare,
)
from assurance_healing.ops.fix_proposal import finalize as fix_proposal_finalize
from assurance_healing.operations.application import expected_repair_history, repair_history_path
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from tests.capabilities.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import BINDING, execute_task
from tests.acg_plan_fixture import install_plan

CHANGE = "CH-REPAIR-1"
SOURCE = "qa/tests/api/test_users.py"
TARGET = "qa/tests/api/test_users.py"
MAPPING = "qa/results/generated/mapping.json"
PROPOSAL = "qa/results/healing/fix-proposal.json"
EXECUTION = "qa/results/execution/execute-result.json"
CASE = "qa/cases/api/case.yaml"
REVIEW = "qa/results/review/case-review.json"
PREP = "qa/results/intake/prepare.json"
SHA = "a" * 64
_POLICY = {
    "resource_id": "assurance.product.configuration.product-policy",
    "sha256": "d" * 64,
}
_EXECUTION_RECEIPT = {"receipt_id": "execute", "receipt_digest": "c" * 64}


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


def _stage_cycle(workspace: Path, relative: str, document: FrozenModel) -> dict[str, str]:
    path = workspace / relative
    if path.exists():
        path.unlink()
    ref = stage_json_artifact(workspace, relative, document)
    return {"path": ref.path, "digest": ref.digest}


def _reviewed_case(
    plan_digest: str,
    plan_ref: dict[str, str],
    *,
    prep_ref: dict[str, str],
    case_ref: dict[str, str],
    review_ref: dict[str, str],
    selection_ref: dict[str, str],
) -> dict[str, object]:
    return {
        "change_id": CHANGE,
        "coverage_epoch": 0,
        "plan_digest": plan_digest,
        "plan_ref": plan_ref,
        "preparation_refs": sorted(
            [plan_ref, prep_ref],
            key=lambda item: (item["path"], item["digest"]),
        ),
        "case_refs": [case_ref],
        "review_ref": review_ref,
        "selection_ref": selection_ref,
    }


def _stage_apply_cycles(
    project: Path,
    *,
    plan_digest: str,
    plan_ref: dict[str, str],
    reviewed: dict[str, object],
    mapping_ref: dict[str, str],
    source_refs: list[dict[str, str]],
    evidence_ref: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    generation = GenerationCycleResultV1.model_validate(
        {
            "change_id": CHANGE,
            "coverage_epoch": 0,
            "reviewed_case": reviewed,
            "plan_digest": plan_digest,
            "plan_ref": plan_ref,
            "mapping_ref": mapping_ref,
            "source_refs": sorted(source_refs, key=lambda item: (item["path"], item["digest"])),
            "plan_refs": [plan_ref],
            "method_plan_ref": {
                "path": "qa/results/generation/epochs/0/obligation-methods.json",
                "digest": SHA,
            },
        }
    )
    execution = ExecutionCycleDocumentV1.model_validate(
        {
            "change_id": CHANGE,
            "plan_digest": plan_digest,
            "plan_ref": plan_ref,
            "coverage_epoch": 0,
            "repair_round": 1,
            "batch_id": "batch-1",
            "executed_at": datetime(2026, 9, 5, 12, 0, 1, tzinfo=UTC),
            "final_status": "FAIL",
            "evidence_ref": evidence_ref,
            "mapping_ref": mapping_ref,
            "source_refs": sorted(source_refs, key=lambda item: (item["path"], item["digest"])),
            "family_outcomes": [{"family": "api", "state": "executed"}],
        }
    )
    return (
        _stage_cycle(project, GENERATION_CYCLE_PATH, generation),
        _stage_cycle(project, EXECUTION_CYCLE_PATH, execution),
    )


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
    evidence_ref = _write(
        project,
        EXECUTION,
        _json_bytes(_execution(plan.plan_digest, plan_ref)),
    )
    source_ref = _write(project, SOURCE, before)
    selection_ref = _write(project, "qa/results/cases/epochs/0/selection.json", b'{"schema_version":"1"}\n')
    reviewed = _reviewed_case(
        plan.plan_digest,
        plan_ref,
        prep_ref=prep_ref,
        case_ref=case_ref,
        review_ref=review_ref,
        selection_ref=selection_ref,
    )
    generation_ref, execution_ref = _stage_apply_cycles(
        project,
        plan_digest=plan.plan_digest,
        plan_ref=plan_ref,
        reviewed=reviewed,
        mapping_ref=mapping_ref,
        source_refs=[source_ref],
        evidence_ref=evidence_ref,
    )
    payload: dict[str, object] = {
        "change_id": CHANGE,
        "plan_digest": plan.plan_digest,
        "plan_ref": plan_ref,
        "capability_leafs": ["users.read"],
        "coverage_epoch": 0,
        "repair_round": 1,
        "product_policy": _POLICY,
        "generation_ref": generation_ref,
        "execution_ref": execution_ref,
        "execution_receipt": _EXECUTION_RECEIPT,
        "proposal_ref": proposal_ref,
    }
    return payload, before


def _opened(project: Path, payload: dict[str, object]) -> dict[str, Any]:
    return opened_repair_input(project, ApplyBoundInputV1.model_validate(payload), ValueError)


def _restage_apply_cycles(
    project: Path,
    payload: dict[str, object],
    *,
    mapping_ref: dict[str, str] | None = None,
    source_refs: list[dict[str, str]] | None = None,
    evidence_ref: dict[str, str] | None = None,
) -> None:
    generation = open_artifact(
        project,
        cast(dict[str, str], payload["generation_ref"]),
        model=GenerationCycleResultV1,
    )
    execution = open_artifact(
        project,
        cast(dict[str, str], payload["execution_ref"]),
        model=ExecutionCycleDocumentV1,
    )
    next_sources = source_refs or [item.model_dump(mode="json") for item in generation.source_refs]
    next_mapping = mapping_ref or generation.mapping_ref.model_dump(mode="json")
    payload["generation_ref"] = _stage_cycle(
        project,
        GENERATION_CYCLE_PATH,
        generation.model_copy(
            update={
                "mapping_ref": EvidenceArtifactRefV1.model_validate(next_mapping),
                "source_refs": tuple(EvidenceArtifactRefV1.model_validate(item) for item in next_sources),
            }
        ),
    )
    payload["execution_ref"] = _stage_cycle(
        project,
        EXECUTION_CYCLE_PATH,
        execution.model_copy(
            update={
                "mapping_ref": EvidenceArtifactRefV1.model_validate(
                    mapping_ref or execution.mapping_ref.model_dump(mode="json")
                ),
                "source_refs": tuple(
                    EvidenceArtifactRefV1.model_validate(item)
                    for item in (
                        source_refs or [row.model_dump(mode="json") for row in execution.source_refs]
                    )
                ),
                "evidence_ref": EvidenceArtifactRefV1.model_validate(
                    evidence_ref or execution.evidence_ref.model_dump(mode="json")
                ),
            }
        ),
    )


def _write_expected_repair_history(
    project: Path,
    stage: Path,
    payload: dict[str, object],
    *,
    after: bytes,
    outputs: list[str] | None = None,
) -> bytes:
    filled = _opened(project, payload)
    business = ApplyTestRepairInputV1.model_validate(
        {key: value for key, value in filled.items() if key in ApplyTestRepairInputV1.model_fields}
    )
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
        cast(TaskHandler, apply_test_repair_finalize),
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
        cast(TaskHandler, apply_test_repair_finalize),
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
async def test_finalize_proves_existing_test_bytes_changed_without_approval(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    first_history = _write_expected_repair_history(tmp_path, stage, payload, after=after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure
    output = cast(dict[str, Any], result.outcome.output)
    assert output["changed_test_refs"] == [{"path": SOURCE, "digest": hashlib.sha256(after).hexdigest()}]
    mapping_ref = _opened(tmp_path, payload)["mapping_ref"]
    assert output["mapping_ref"] == (
        mapping_ref.model_dump(mode="json") if hasattr(mapping_ref, "model_dump") else mapping_ref
    )
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
    proposal_input = {key: value for key, value in payload.items() if key != "proposal_ref"}
    proposed = await execute_task(
        cast(TaskHandler, fix_proposal_finalize),
        cast(
            JSONValue,
            {
                **proposal_input,
                "prepare": proposal_input,
                "agent_result": agent.model_dump(mode="json"),
            },
        ),
        tmp_path,
        write_root=proposal_stage,
    )
    assert proposed.status == "succeeded", proposed.failure
    payload["proposal_ref"] = _write(tmp_path, PROPOSAL, proposal_bytes)
    stage = tmp_path / ".stage"
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    _write(stage, SOURCE, after)
    _write_expected_repair_history(tmp_path, stage, payload, after=after)
    result = await _finalize(tmp_path, stage, payload, [SOURCE])
    assert result.outcome.status == "succeeded", result.outcome.failure


@pytest.mark.asyncio
async def test_repair_changes_only_the_proposed_file_in_a_two_file_generation(tmp_path: Path) -> None:
    payload, _ = _fixture(tmp_path)
    other_target = "qa/tests/api/test_other.py"
    other_source = other_target
    other_bytes = b"def test_other():\n    assert True\n"
    other_ref = _write(tmp_path, other_source, other_bytes)
    opened = _opened(tmp_path, payload)
    current_sources = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        for item in opened["source_refs"]
    ]
    source_refs = sorted([*current_sources, other_ref], key=lambda ref: (ref["path"], ref["digest"]))
    execution = _execution(str(payload["plan_digest"]), cast(dict[str, str], payload["plan_ref"]))
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
    mapping_ref = _write(tmp_path, MAPPING, _json_bytes(execution["mapping"]))
    evidence_ref = _write(tmp_path, EXECUTION, _json_bytes(execution))
    _restage_apply_cycles(
        tmp_path,
        payload,
        mapping_ref=mapping_ref,
        source_refs=source_refs,
        evidence_ref=evidence_ref,
    )
    prepared = await execute_task(
        cast(TaskHandler, apply_test_repair_prepare),
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
    _write_expected_repair_history(tmp_path, stage, payload, after=after)
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
@pytest.mark.parametrize(
    "fault", ["outside_mapping", "ineligible", "needs_review", "critical", "other_change", "changed_digest"]
)
async def test_prepare_rejects_ineligible_or_unauthenticated_proposal(tmp_path: Path, fault: str) -> None:
    payload, _ = _fixture(tmp_path)
    proposal = _proposal()
    items = cast(list[dict[str, object]], proposal["proposals"])
    if fault == "outside_mapping":
        items[0]["files_to_modify"] = ["qa/tests/api/test_other.py"]
    elif fault == "ineligible":
        items[0]["eligible"] = False
    elif fault == "needs_review":
        items[0]["needs_review"] = True
    elif fault == "critical":
        items[0]["risk_level"] = "critical"
    elif fault == "other_change":
        proposal["change_id"] = "CH-OTHER"
    else:
        proposal["summary"] = {"eligible_count": 2}
    ref = _write(tmp_path, PROPOSAL, _json_bytes(proposal))
    if fault != "changed_digest":
        payload["proposal_ref"] = ref
    result = await execute_task(
        cast(TaskHandler, apply_test_repair_prepare),
        cast(JSONValue, payload),
        tmp_path,
        binding_data=BINDING,
    )
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["no_change", "outside_proposal", "case", "product", "oracle"])
async def test_finalize_rejects_unproved_or_unsafe_patch(tmp_path: Path, mutation: str) -> None:
    payload, before = _fixture(tmp_path)
    stage = tmp_path / ".stage"
    path = SOURCE
    after = b"def test_users(client):\n    response = client.get('/users')\n    assert response.status_code == 200\n"
    if mutation == "no_change":
        after = before
    elif mutation == "outside_proposal":
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
async def test_finalize_rejects_mapping_membership_change(tmp_path: Path) -> None:
    payload, _before = _fixture(tmp_path)
    altered = _write(tmp_path, MAPPING, _json_bytes(_mapping(symbol="test_admin")))
    generation = open_artifact(
        tmp_path,
        cast(dict[str, str], payload["generation_ref"]),
        model=GenerationCycleResultV1,
    )
    payload["generation_ref"] = _stage_cycle(
        tmp_path,
        GENERATION_CYCLE_PATH,
        generation.model_copy(update={"mapping_ref": EvidenceArtifactRefV1.model_validate(altered)}),
    )
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
                "execution_ref": {"path": EXECUTION, "digest": SHA},
                "mapping_ref": {"path": MAPPING, "digest": SHA},
                "source_refs": [{"path": SOURCE, "digest": SHA}],
                "allowed_test_paths": [SOURCE],
            }
        )
