from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, AgentExecutionContract
from graph_engine.boot.boot import CapabilityBuildContext
from assurance_improvement.contracts.agent import RetroAnalysisResultV3
from assurance_improvement.contracts.retro import RetroReconcileResultV1
from assurance_improvement.graphs.factory import ImprovementGraphs
from graph_engine.canonical import canonical_digest
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_improvement.graphs.nodes import (
    select_eval_analysis,
    select_issue_analysis,
    select_workflow_analysis,
)
from assurance_improvement.operations.agent import (
    RetroEvalPrepareHandler,
    RetroIssuePrepareHandler,
    RetroWorkflowPrepareHandler,
    RetroIssueFinalizeHandler,
)
from tests.product.test_change_local_output_routing import execute_task
from improvement_fixtures import BINDING, RETRO_ID, issue_signal, candidate_payload  # pyright: ignore[reportMissingImports]
from test_graph_retro import complete_collect_payload  # pyright: ignore[reportMissingImports]


@pytest.mark.parametrize(
    "domain,select,handler",
    [
        ("eval", select_eval_analysis, RetroEvalPrepareHandler()),
        ("issue", select_issue_analysis, RetroIssuePrepareHandler()),
        ("workflow", select_workflow_analysis, RetroWorkflowPrepareHandler()),
    ],
)
async def test_analysis_receives_real_slice_without_review_archive_fields(
    domain, select, handler, tmp_path: Path
) -> None:
    state = {"change_id": "CH-DEMO-001", **complete_collect_payload()}
    business = select(state)
    contract = AGENT_JOB_CONTRACTS[f"retro-{domain}-analysis"]
    validated = contract.input_model.model_validate(business.model_dump(mode="json"))
    outcome = await execute_task(handler, validated.model_dump(mode="json"), tmp_path, binding_data=BINDING)
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    payload = request.instructions[2].model_dump(mode="json")["json_content"]
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
    state = {"change_id": "CH-DEMO-001", **complete_collect_payload()}
    business = select_issue_analysis(state).model_dump(mode="json")
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
        RetroIssueFinalizeHandler(),
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
    from assurance_improvement.graphs.nodes import select_retro
    from assurance_improvement.operations.agent import RetroFinalizeHandler

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
    business = select_retro(state).model_dump(mode="json")
    document = {"schema_version": "3", "retro_id": RETRO_ID, "signals": [], "candidates": [candidate]}
    path = tmp_path / "qa/results/retro/retro.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))
    outcome = await execute_task(
        RetroFinalizeHandler(),
        {**business, "agent_result": agent_result(document)},
        tmp_path,
        write_root=tmp_path,
    )
    assert outcome.status == ("succeeded" if fault is None else "failed"), outcome.failure


async def test_collect_supplies_durable_assembly_timestamp(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from assurance_improvement.operations.retro import RetroCollectHandler
    from assurance_improvement.graphs.nodes import publish_collect

    before = datetime.now(timezone.utc)
    outcome = await execute_task(RetroCollectHandler(), complete_collect_payload(), tmp_path)
    state = publish_collect({}, outcome.output, {})
    assert isinstance(state["ts"], str)
    assert before <= datetime.fromisoformat(state["ts"]) <= datetime.now(timezone.utc)


async def test_reconcile_reads_existing_store_and_stages_result_without_direct_publication(
    tmp_path: Path,
) -> None:
    from assurance_improvement.graphs.nodes import select_reconcile
    from assurance_improvement.operations.retro import ReconcileImprovementsHandler
    from tests.product.test_change_local_output_routing import dual_roots

    state = {**synthesis_state(), "candidates": [synthesized_candidate()]}
    selected = select_reconcile(state)
    project, staging = dual_roots(tmp_path)
    first = await execute_task(
        ReconcileImprovementsHandler(), selected.model_dump(mode="json"), project, write_root=staging
    )
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
    second = await execute_task(
        ReconcileImprovementsHandler(), selected.model_dump(mode="json"), project, write_root=second_stage
    )
    assert second.status == "succeeded", second.failure
    second_ledger = json.loads((second_stage / "qa/improvements/ledger.json").read_text())
    assert second_ledger == ledger
    assert len(RetroReconcileResultV1.model_validate(second.output).status.improvement_ids) == 1
    target.write_text("broken ledger")
    failed = await execute_task(
        ReconcileImprovementsHandler(),
        selected.model_dump(mode="json"),
        project,
        write_root=tmp_path / "bad-stage",
    )
    assert failed.status == "failed"
    assert target.read_text() == "broken ledger"


async def test_public_retro_runs_real_contracts_and_handlers_with_only_agent_transport_faked(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_improvement.graphs.retro import build_retro_graph
    from assurance_improvement.operations import improvement_handlers
    from tests.product.test_product_stategraph_flow import _flow_features, _product_graphs, _public_input

    contracts = {
        item.contract_id: item for item in (*AGENT_JOB_CONTRACTS.values(), *TASK_ATTEMPT_CONTRACTS.values())
    }
    handlers = improvement_handlers()
    calls = []

    def attempt(contract_id, *, semantic_node_id, activation, select, publish):
        del activation
        contract = contracts[contract_id]

        async def run(state):
            selected = select(state)
            wire_input = selected.model_dump(mode="json") if isinstance(selected, BaseModel) else selected
            business = contract.input_model.model_validate(wire_input)
            calls.append(semantic_node_id)
            if isinstance(contract, AgentExecutionContract):
                prepared = await execute_task(
                    handlers[contract.prepare_handler_id],
                    business.model_dump(mode="json"),
                    tmp_path,
                    binding_data=BINDING,
                )
                assert prepared.status == "succeeded", prepared.failure
                request = AgentRunRequest.model_validate(prepared.output)
                locked = business.model_dump(mode="json")
                domain = locked.get("evidence_slice", {}).get("domain")
                identity = locked.get("evidence_slice") or locked["context"]
                if domain is None:
                    pytest.fail("zero-signal context must not dispatch a synthesis Agent")
                failed = False
                document = {
                    "schema_version": "3",
                    "retro_id": identity["retro_id"],
                    "domain": domain,
                    "analysis_status": "failed" if failed else "ok",
                    "failure_reason": "cannot synthesize" if failed else None,
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
                result = await execute_task(
                    handlers[contract.finalize_handler_id],
                    {**locked, "agent_result": agent_result(document)},
                    tmp_path,
                )
            else:
                result = await execute_task(
                    handlers[contract.handler_id], business.model_dump(mode="json"), tmp_path
                )
            assert result.status == "succeeded", result.failure
            output = contract.output_model.model_validate(result.output)
            return publish(state, output, {})

        return run

    child = build_retro_graph(
        cast(
            CapabilityBuildContext,
            SimpleNamespace(attempt=attempt, compile_subgraph=lambda builder: builder.compile()),
        )
    )
    features = _flow_features()
    features["assurance.improvement"] = replace(
        cast(ImprovementGraphs, features["assurance.improvement"]), retro=child
    )
    result = (
        await _product_graphs(features).entrypoints["retro"].ainvoke(_public_input("retro", artifacts=[]))
    )
    assert calls[:5] == [
        "improvement.retro-build-slices",
        "improvement.retro-collect",
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    ]
    assert calls[-1] == "improvement.retro-reconcile"
    assert result["status"] == "completed"
    assert result["retro_status"]["result"] == "completed_with_gaps"
    context_file = next(tmp_path.rglob("context.json"))
    assert json.loads(context_file.read_text())["integrity"]["status"] == "incomplete"
