from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, AgentExecutionContract
from graph_engine.boot.boot import CapabilityBuildContext
from assurance_improvement.contracts.agent import RetroAnalysisResultV3
from assurance_improvement.contracts.retro import RetroReconcileResultV1
from graph_engine.canonical import JSONValue, canonical_digest
from assurance_improvement.contracts.agent import RetroAnalysisInputV1
from assurance_improvement.contracts.attempts import (
    AGENT_JOB_CONTRACTS,
    select_analysis_slice,
    select_retro_collect,
)
from graph_engine.plugin_api import TaskHandler

from assurance_improvement.ops.retro_eval_analysis import prepare as retro_eval_prepare
from assurance_improvement.ops.retro_issue_analysis import (
    finalize as retro_issue_finalize,
    prepare as retro_issue_prepare,
)
from assurance_improvement.ops.retro_workflow_analysis import prepare as retro_workflow_prepare
from tests.product.test_change_local_output_routing import execute_task
from improvement_fixtures import BINDING, RETRO_ID, issue_signal, candidate_payload  # pyright: ignore[reportMissingImports]
from test_graph_retro import complete_collect_payload  # pyright: ignore[reportMissingImports]


def _analysis_input(domain: Literal["issue", "workflow", "eval"]) -> RetroAnalysisInputV1:
    collected = select_retro_collect(complete_collect_payload())
    return RetroAnalysisInputV1.model_validate(
        {
            "change_id": "CH-DEMO-001",
            "evidence_slice": select_analysis_slice(collected, domain=domain),
        }
    )


@pytest.mark.parametrize(
    "domain,handler",
    [
        ("eval", cast(TaskHandler, retro_eval_prepare)),
        ("issue", cast(TaskHandler, retro_issue_prepare)),
        ("workflow", cast(TaskHandler, retro_workflow_prepare)),
    ],
)
async def test_analysis_receives_real_slice_without_review_archive_fields(
    domain, handler, tmp_path: Path
) -> None:
    business = _analysis_input(domain)
    contract = AGENT_JOB_CONTRACTS[f"retro-{domain}-analysis"]
    validated = contract.input_model.model_validate(business.model_dump(mode="json"))
    outcome = await execute_task(handler, validated.model_dump(mode="json"), tmp_path, binding_data=BINDING)
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    payload = request.instructions[-1].model_dump(mode="json")["json_content"]
    assert payload["evidence_slice"]["domain"] == domain
    assert [source["evidence_ids"] for source in payload["evidence_slice"]["sources"]] == (
        [["PROB-1", "OCC-1"]] if domain == "issue" else []
    )
    assert "archive_digest" not in payload
    assert "expected_improvement_version" not in payload
    assert "subject_digest" not in payload


def agent_result(document):
    return AgentRunResult(
        result_payload=document,
        result_digest=canonical_digest(document),
        evidence_digest="a" * 64,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "fault",
    [None, "outside_slice", "candidate", "file_mismatch", "file_missing", "hard_link", "symlink_parent"],
)
async def test_analysis_finalizer_closes_slice_and_written_result(tmp_path: Path, fault) -> None:
    business = _analysis_input("issue").model_dump(mode="json")
    signal = issue_signal(source_id="unknown" if fault == "outside_slice" else "PROB-1")
    document = {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": "issue",
        "analysis_status": "ok",
        "signals": [signal],
        "candidates": [candidate_payload()] if fault == "candidate" else [],
    }
    path = tmp_path / "qa/results/retro/retro-issue-analysis.json"
    path.parent.mkdir(parents=True)
    if fault != "file_missing":
        path.write_text(json.dumps({**document, "signals": []} if fault == "file_mismatch" else document))
    if fault == "hard_link":
        (tmp_path / "linked-result.json").hardlink_to(path)
    if fault == "symlink_parent":
        target = tmp_path / "redirected-retro"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    outcome = await execute_task(
        cast(TaskHandler, retro_issue_finalize),
        {**business, "agent_result": agent_result(document)},
        tmp_path,
        write_root=tmp_path,
    )
    if fault is None:
        assert outcome.status == "succeeded", outcome.failure
        assert (
            RetroAnalysisResultV3.model_validate(outcome.output).signals[0].signal_id
            == "issue-pattern:PROB-1"
        )
    else:
        assert outcome.failure is not None
        assert outcome.failure.kind == "invalid_output"


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def synthesis_state():
    from test_graph_retro import retro_context_payload  # pyright: ignore[reportMissingImports]

    context = retro_context_payload()
    context["signals"] = {"issue": [issue_signal()], "workflow": [], "eval": []}
    context["signal_count"] = 1
    return {"change_id": "CH-DEMO-001", "context": context}


def synthesized_candidate():
    return candidate_payload(signal_ids=["issue-pattern:PROB-1"])


@pytest.mark.parametrize("fault", [None, "unknown_signal", "empty_lock", "cross_signal_source"])
async def test_synthesis_consumes_locked_context_without_reconciled_ledger(tmp_path: Path, fault) -> None:
    from assurance_improvement.contracts.agent import RetroSynthesisInputV1
    from assurance_improvement.ops.retro import finalize as retro_finalize
    from graph_engine.plugin_api import TaskHandler

    state = synthesis_state()
    if fault == "empty_lock":
        state["context"]["signals"] = {"issue": [], "workflow": [], "eval": []}
        state["context"]["signal_count"] = 0
    candidate = synthesized_candidate()
    if fault == "unknown_signal":
        candidate["signal_ids"] = ["unknown"]
    if fault == "cross_signal_source":
        state["context"]["signals"]["issue"][0]["source_refs"] = {"problem_ids": ["PROB-1"]}
        candidate["source_refs"] = {"occurrence_ids": ["OCC-1"]}
    business = RetroSynthesisInputV1.model_validate(
        {"change_id": state["change_id"], "context": _jsonable(state["context"])}
    ).model_dump(mode="json")
    document = {"schema_version": "3", "retro_id": RETRO_ID, "signals": [], "candidates": [candidate]}
    path = tmp_path / "qa/results/retro/retro.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))
    outcome = await execute_task(
        cast(TaskHandler, retro_finalize),
        {**business, "agent_result": agent_result(document)},
        tmp_path,
        write_root=tmp_path,
    )
    assert outcome.status == ("succeeded" if fault is None else "failed"), outcome.failure


async def test_collect_supplies_durable_assembly_timestamp(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from assurance_improvement.contracts.retro import RetroCollectedV1
    from assurance_improvement.operations.retro import RetroCollectHandler

    before = datetime.now(timezone.utc)
    from graph_engine.artifacts import stage_json_artifact

    from assurance_improvement.contracts.handoff import SLICE_PATH
    from assurance_improvement.contracts.retro import RetroCollectInput

    collected = RetroCollectInput.model_validate(complete_collect_payload())
    attempt: dict[str, object] = {
        "retro_id": collected.retro_id,
        "window": collected.window.model_dump(mode="json"),
    }
    for name, document in (
        ("issue", collected.issue_slice),
        ("workflow", collected.workflow_slice),
        ("eval", collected.eval_slice),
        ("discovery", collected.discovery_slice),
        ("coverage_gap", collected.coverage_gap_slice),
    ):
        if document is None:
            continue
        ref = stage_json_artifact(tmp_path, SLICE_PATH[name], document)
        field = "coverage_gap_slice_ref" if name == "coverage_gap" else f"{name}_slice_ref"
        attempt[field] = {"path": ref.path, "digest": ref.digest}
    outcome = await execute_task(RetroCollectHandler(), cast(JSONValue, attempt), tmp_path)
    collected = RetroCollectedV1.model_validate(outcome.output)
    assert before <= datetime.fromisoformat(collected.generated_at) <= datetime.now(timezone.utc)


async def test_reconcile_reads_existing_store_and_stages_result_without_direct_publication(
    tmp_path: Path,
) -> None:
    from assurance_improvement.contracts.retro import RetroReconcileInputV1
    from assurance_improvement.operations.retro import ReconcileImprovementsHandler
    from tests.product.test_change_local_output_routing import dual_roots

    from graph_engine.artifacts import stage_json_artifact

    from assurance_improvement.contracts.handoff import CANDIDATES, CONTEXT
    from assurance_improvement.contracts.retro import RetroCandidatesFile

    state = {**synthesis_state(), "candidates": [synthesized_candidate()]}
    selected = RetroReconcileInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "context": _jsonable(state["context"]),
            "candidates": _jsonable(state["candidates"]),
        }
    )
    project, staging = dual_roots(tmp_path)
    context_ref = stage_json_artifact(project, CONTEXT, selected.context)
    candidates_ref = stage_json_artifact(
        project, CANDIDATES, RetroCandidatesFile(candidates=selected.candidates)
    )
    attempt = {
        "change_id": selected.change_id,
        "context_ref": {"path": context_ref.path, "digest": context_ref.digest},
        "candidates_ref": {"path": candidates_ref.path, "digest": candidates_ref.digest},
    }
    first = await execute_task(ReconcileImprovementsHandler(), attempt, project, write_root=staging)
    assert first.status == "succeeded", first.failure
    assert not (project / "qa/improvements/ledger.json").exists()
    ledger = json.loads((staging / "qa/improvements/ledger.json").read_text())
    assert ledger["last_seq"] == 1
    assert {item["state"] for item in ledger["improvements"].values()} == {"proposed"}
    status = json.loads((staging / "qa/results/retro/status.json").read_text())
    assert status["result"] == "completed"
    assert len(status["improvement_ids"]) == 1
    # Simulate the Kernel's committed publication, then reconcile in a new workspace.
    target = project / "qa/improvements/ledger.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(ledger))
    second_stage = tmp_path / "next-stage"
    second_stage.mkdir()
    second = await execute_task(ReconcileImprovementsHandler(), attempt, project, write_root=second_stage)
    assert second.status == "succeeded", second.failure
    second_ledger = json.loads((second_stage / "qa/improvements/ledger.json").read_text())
    assert second_ledger == ledger
    assert len(RetroReconcileResultV1.model_validate(second.output).status.improvement_ids) == 1
    target.write_text("broken ledger")
    failed = await execute_task(
        ReconcileImprovementsHandler(),
        attempt,
        project,
        write_root=tmp_path / "bad-stage",
    )
    assert failed.status == "failed"
    assert target.read_text() == "broken ledger"


async def test_public_retro_runs_real_contracts_and_handlers_with_only_agent_transport_faked(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_improvement.graphs.retro import build_retro_graph
    from assurance_improvement.operations import improvement_handlers
    from dataclasses import replace

    from typing import Any

    from assurance_product.graphs.entrypoints import thin_root_flows
    from assurance_product.graphs.factory import ProductFeatureBundles
    from tests.product.test_product_stategraph_flow import _build_context, _public_input
    from tests.product.test_stategraph_entrypoints import _stub_features

    contracts = {
        item.contract_id: item for item in (*AGENT_JOB_CONTRACTS.values(), *TASK_ATTEMPT_CONTRACTS.values())
    }
    handlers = improvement_handlers()
    calls = []

    def attempt(contract_id, *, semantic_node_id, activation, select, publish):
        del activation
        contract = contracts[contract_id]

        async def run(state, runtime=None):
            del runtime
            selected = select(state)
            wire_input = selected.model_dump(mode="json") if isinstance(selected, BaseModel) else selected
            business = contract.input_model.model_validate(wire_input)
            calls.append(semantic_node_id)
            committed_artifacts: list[dict[str, str]] = []
            if isinstance(contract, AgentExecutionContract):
                locked = business.model_dump(mode="json")
                if locked.get("evidence_slice") is None and isinstance(
                    locked.get("evidence_slice_ref"), dict
                ):
                    ref = locked["evidence_slice_ref"]
                    locked["evidence_slice"] = json.loads((tmp_path / ref["path"]).read_text())
                if locked.get("context") is None and isinstance(locked.get("context_ref"), dict):
                    ref = locked["context_ref"]
                    locked["context"] = json.loads((tmp_path / ref["path"]).read_text())
                prepare_handler_id = contract.prepare_handler_id
                finalize_handler_id = contract.finalize_handler_id
                domain = locked.get("evidence_slice", {}).get("domain")
                identity = locked.get("evidence_slice") or locked["context"]
                if domain is None:
                    pytest.fail("zero-signal context must not dispatch a synthesis Agent")

                async def prepare_phase(input_value, _scope):
                    prepared = await execute_task(
                        handlers[prepare_handler_id],
                        input_value.model_dump(mode="json"),
                        tmp_path,
                        binding_data=BINDING,
                        capability_id=prepare_handler_id,
                    )
                    assert prepared.status == "succeeded", prepared.failure
                    return AgentRunRequest.model_validate(prepared.output)

                async def runtime_phase(request, _scope):
                    document = {
                        "schema_version": "3",
                        "retro_id": identity["retro_id"],
                        "domain": domain,
                        "analysis_status": "ok",
                        "failure_reason": None,
                        "signals": [],
                        "candidates": [],
                    }
                    # Substitute only the external model call, honoring the real prepare output path.
                    _, stage = __import__(
                        "tests.product.test_change_local_output_routing", fromlist=["dual_roots"]
                    ).dual_roots(tmp_path)
                    destination = stage / request.workspace.allowed_outputs[0]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(document))
                    if domain is not None:
                        relative = request.workspace.allowed_outputs[0]
                        project_file = tmp_path / relative
                        project_file.parent.mkdir(parents=True, exist_ok=True)
                        payload = destination.read_bytes()
                        project_file.write_bytes(payload)
                        committed_artifacts.append(
                            {"path": relative, "digest": hashlib.sha256(payload).hexdigest()}
                        )
                    return agent_result(document)

                async def finalize_phase(bundle, _scope):
                    finalized = await execute_task(
                        handlers[finalize_handler_id],
                        {**locked, "agent_result": bundle.agent_result},
                        tmp_path,
                        capability_id=finalize_handler_id,
                    )
                    assert finalized.status == "succeeded", finalized.failure
                    return contract.output_model.model_validate(finalized.output)

                request = await prepare_phase(business, None)
                raw = await runtime_phase(request, None)
                result = await finalize_phase(SimpleNamespace(agent_result=raw), None)
            else:
                executed = await execute_task(
                    handlers[contract.handler_id], business.model_dump(mode="json"), tmp_path
                )
                assert executed.status == "succeeded", executed.failure
                roots = {spec.root for spec in contract.ledger_writes()}
                for relative, payload in executed.workspace_bytes.items():
                    if relative not in roots:
                        continue
                    destination = tmp_path / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    committed_artifacts.append(
                        {"path": relative, "digest": hashlib.sha256(payload).hexdigest()}
                    )
                result = contract.output_model.model_validate(executed.output)
            seen = {item["path"] for item in committed_artifacts}
            candidates = tmp_path / "qa/results/retro/candidates.json"
            if candidates.is_file() and "qa/results/retro/candidates.json" not in seen:
                payload = candidates.read_bytes()
                committed_artifacts.append(
                    {
                        "path": "qa/results/retro/candidates.json",
                        "digest": hashlib.sha256(payload).hexdigest(),
                    }
                )
            output = contract.output_model.model_validate(result)
            return publish(state, output, None, committed=committed_artifacts)

        return run

    child = build_retro_graph(
        cast(
            CapabilityBuildContext,
            SimpleNamespace(attempt=attempt, compile_subgraph=lambda builder: builder.compile()),
        )
    )
    features = _stub_features()
    improvement = features["assurance.improvement"]
    features["assurance.improvement"] = replace(cast(Any, improvement), retro=child)
    result = (
        await thin_root_flows(
            ProductFeatureBundles(
                **cast(Any, {key.removeprefix("assurance."): value for key, value in features.items()})
            )
        )["retro"]
        .compile(_build_context())
        .ainvoke(_public_input("retro", artifacts=[]))
    )
    assert calls[:2] == ["improvement.retro-build-slices", "improvement.retro-collect"]
    assert set(calls[2:5]) == {
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    }
    assert calls[5] == "improvement.retro-synthesize"
    assert calls[-1] == "improvement.retro-reconcile"
    assert result["status"] == "completed"
    status = json.loads(next(tmp_path.rglob("status.json")).read_text(encoding="utf-8"))
    assert status["result"] == "completed_with_gaps"
    context_file = next(tmp_path.rglob("context.json"))
    assert json.loads(context_file.read_text())["integrity"]["status"] == "incomplete"
