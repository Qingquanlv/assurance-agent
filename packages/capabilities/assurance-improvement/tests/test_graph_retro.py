from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.attempts import (
    select_analysis_slice,
    select_retro_agent,
    select_retro_collect,
    select_retro_reconcile,
)
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.improvements import ImprovementLedgerProjection
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroContextV3,
    WorkflowEvidenceSlice,
)
from assurance_improvement.graphs.factory import build_improvement_graphs
from assurance_improvement.graphs.nodes import select_reconcile, select_retro
from assurance_improvement.graphs.state import (
    replace_eval_analysis,
    replace_issue_analysis,
    replace_workflow_analysis,
)
from graph_engine.attempts.resolutions import RejectedTaskResult
from graph_engine.testing import GraphHarness, committed

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    RETRO_ID,
    candidate_payload,
    improvement_projection,
)
from test_improvement_graph_factory import (  # type: ignore[import-not-found]
    TASK_COLLECT_ID,
    TASK_RECONCILE_ID,
    _RETRO_EVAL_ID,
    _RETRO_ID,
    _RETRO_ISSUE_ID,
    _RETRO_WORKFLOW_ID,
    _receipt,
    archive_agent_output,
    archive_graph_input,
    improvement_contracts,
    skill_graph_fields,
)

_WINDOW = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": ["CH-DEMO-001"]}
_TS = "2026-08-22T00:00:00Z"


def _empty_slice(domain: str) -> dict[str, object]:
    sources = (
        [
            {
                "kind": "project_problem_ledger",
                "change_id": None,
                "head_event_id": "evt-1",
                "sha256": "abc",
                "evidence_ids": ["PROB-1", "OCC-1"],
            }
        ]
        if domain == "issue"
        else []
    )
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "window": _WINDOW,
        "sources": sources,
        "integrity": {"status": "complete", "reasons": []},
        "deterministic_signals": [],
        "entries": [],
    }


def complete_collect_payload() -> dict[str, object]:
    return {
        "retro_id": RETRO_ID,
        "window": _WINDOW,
        "issue_slice": _empty_slice("issue"),
        "workflow_slice": _empty_slice("workflow"),
        "eval_slice": _empty_slice("eval"),
    }


def _ok_signals(domain: str, digest: str) -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "analysis_status": "ok",
        "failure_reason": None,
        "analyzer": f"aa-retro-{domain}-analysis",
        "signals": [],
        "slice_sha256": digest,
    }


def retro_context_payload() -> dict[str, object]:
    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "generated_at": _TS,
        "dry_run": False,
        "window": _WINDOW,
        "source_manifest": {
            "issue_slice_sha256": artifact_digest(issue),
            "workflow_slice_sha256": artifact_digest(workflow),
            "eval_slice_sha256": artifact_digest(evaluation),
            "issue_sources": issue.sources,
            "workflow_sources": workflow.sources,
            "eval_sources": evaluation.sources,
        },
        "integrity": {"status": "complete", "reasons": []},
        "domain_status": {
            "issue": {"status": "ok"},
            "workflow": {"status": "ok"},
            "eval": {"status": "ok"},
        },
        "signals": {"issue": [], "workflow": [], "eval": []},
        "signal_count": 0,
    }


def empty_ledger() -> dict[str, object]:
    return {"schema_version": "1", "last_seq": 0, "improvements": {}, "by_fingerprint": {}}


def retro_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **skill_graph_fields(),
        **complete_collect_payload(),
        "context": retro_context_payload(),
        "current": empty_ledger(),
        "ts": _TS,
        "candidates": (),
    }
    payload.update(overrides)
    return payload


def analysis_agent_output(domain: Literal["issue", "workflow", "eval"]) -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "analysis_status": "ok",
        "failure_reason": None,
        "analyzer": f"aa-retro-{domain}-analysis",
        "signals": [],
        "candidates": [candidate_payload()] if domain == "issue" else [],
    }


def _task_failure_signal(*, signal_id: str, node_id: str) -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "summary": f"Task failed at {node_id}",
        "occurrence_count": 1,
        "recommended_change": "Investigate the failure",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "confidence": "high",
        "signal_type": "task_failure",
        "node_id": node_id,
        "error_kind": "timeout",
        "message_fingerprint": "fp-1",
    }


def retro_agent_output() -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": None,
        "analysis_status": "ok",
        "failure_reason": None,
        "signals": [],
        "candidates": [candidate_payload()],
    }


def reconciled_ledger() -> dict[str, object]:
    projection = improvement_projection(state="proposed", delivery="change_draft")
    return {
        "schema_version": "1",
        "last_seq": 1,
        "improvements": {str(projection["improvement_id"]): projection},
        "by_fingerprint": {str(projection["fingerprint"]): projection["improvement_id"]},
    }


def test_named_analysis_reducers_are_deterministic_and_not_a_token_list() -> None:
    first = analysis_agent_output("eval")
    second = {**analysis_agent_output("eval"), "analysis_status": "ok"}
    assert replace_eval_analysis(None, first) == first
    assert replace_eval_analysis(first, None) == first
    assert replace_eval_analysis(first, second) == second
    assert replace_eval_analysis(replace_eval_analysis(None, first), second) == replace_eval_analysis(
        first, second
    )
    issue = replace_issue_analysis(None, analysis_agent_output("issue"))
    workflow = replace_workflow_analysis(None, analysis_agent_output("workflow"))
    assert isinstance(issue, dict)
    assert isinstance(workflow, dict)
    assert issue["domain"] == "issue"
    assert workflow["domain"] == "workflow"
    from assurance_improvement.graphs.state import ImprovementState

    hints = getattr(ImprovementState, "__annotations__", {})
    assert "eval_analysis" in hints
    assert "issue_analysis" in hints
    assert "workflow_analysis" in hints
    assert "analyses" not in hints
    assert "analysis_tokens" not in hints
    assert "analysis_results" not in hints


def test_collect_selector_rejects_lifecycle_only_public_payload() -> None:
    from assurance_improvement.graphs.nodes import select_collect

    with pytest.raises((ValidationError, ValueError)):
        select_collect(
            {
                "change_id": "CH-RETRO-002",
                "capability_leafs": ["auth.session.create"],
                "allowed_artifact_paths": ["qa/archive"],
                "evidence_refs": [{"path": "qa/archive/x.json", "digest": "b" * 64}],
                "lifecycle_state": "evaluating",
            }
        )


def test_analysis_selects_receive_only_matching_authenticated_slices() -> None:
    from assurance_improvement.graphs.nodes import (
        select_eval_analysis,
        select_issue_analysis,
        select_workflow_analysis,
    )

    collected = select_retro_collect(complete_collect_payload())
    state = retro_graph_input()
    eval_slice = select_analysis_slice(collected, domain="eval")
    issue_slice = select_analysis_slice(collected, domain="issue")
    workflow_slice = select_analysis_slice(collected, domain="workflow")
    assert isinstance(eval_slice, EvalEvidenceSlice)
    assert isinstance(issue_slice, IssueEvidenceSlice)
    assert isinstance(workflow_slice, WorkflowEvidenceSlice)
    selected_eval = select_eval_analysis(state)
    selected_issue = select_issue_analysis(state)
    selected_workflow = select_workflow_analysis(state)
    assert selected_eval.change_id == "CH-DEMO-001"
    assert selected_issue.change_id == "CH-DEMO-001"
    assert selected_workflow.change_id == "CH-DEMO-001"
    missing = dict(state)
    del missing["eval_slice"]
    with pytest.raises((ValidationError, ValueError, TypeError, KeyError)):
        select_eval_analysis(missing)


def test_reconcile_and_retro_agent_receive_typed_values() -> None:
    state = retro_graph_input(candidates=(candidate_payload(),))
    selected = select_reconcile(state)
    expected = select_retro_reconcile(
        context=RetroContextV3.model_validate(state["context"]),
        candidates=(candidate_payload(),),
        current=ImprovementLedgerProjection.model_validate(state["current"]),
        ts=_TS,
    )
    assert selected.context.retro_id == expected.context.retro_id
    assert selected.ts == expected.ts
    ledger = ImprovementLedgerProjection.model_validate(reconciled_ledger())
    received = select_retro_agent(ledger)
    assert received == ledger
    retro_state = {**state, "ledger": ledger.model_dump(mode="json")}
    skill = select_retro(retro_state)
    assert skill.change_id == "CH-DEMO-001"
    assert skill.retro_id == RETRO_ID


async def test_retro_traces_collect_three_analyses_reconcile_then_agent() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.retro,
        input=retro_graph_input(),
        script={
            "improvement.retro-collect": [committed(complete_collect_payload(), receipt)],
            "improvement.retro-eval-analysis": [committed(analysis_agent_output("eval"), receipt)],
            "improvement.retro-issue-analysis": [committed(analysis_agent_output("issue"), receipt)],
            "improvement.retro-workflow-analysis": [committed(analysis_agent_output("workflow"), receipt)],
            "improvement.retro-reconcile": [committed(reconciled_ledger(), receipt)],
            "improvement.retro": [committed(retro_agent_output(), receipt)],
        },
    )
    calls = [call.semantic_node_id for call in result.semantic_calls]
    assert calls[0] == "improvement.retro-collect"
    assert set(calls[1:4]) == {
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    }
    assert calls[4] == "improvement.retro-reconcile"
    assert calls[5] == "improvement.retro"
    assert [call.contract_id for call in result.semantic_calls][0] == TASK_COLLECT_ID
    assert [call.contract_id for call in result.semantic_calls][4] == TASK_RECONCILE_ID
    assert [call.contract_id for call in result.semantic_calls][5] == _RETRO_ID
    analysis_ids = [call.contract_id for call in result.semantic_calls[1:4]]
    assert set(analysis_ids) == {_RETRO_EVAL_ID, _RETRO_ISSUE_ID, _RETRO_WORKFLOW_ID}
    collect_selected = result.select_values[0]
    assert isinstance(collect_selected, dict)
    assert collect_selected["retro_id"] == RETRO_ID
    assert "issue_slice" in collect_selected
    assert "workflow_slice" in collect_selected
    assert "eval_slice" in collect_selected
    reconcile_selected = result.select_values[4]
    assert isinstance(reconcile_selected, dict)
    assert reconcile_selected["context"]["retro_id"] == RETRO_ID
    assert "candidates" in reconcile_selected
    assert "current" in reconcile_selected
    terminal = result.terminal
    assert terminal is not None
    assert isinstance(terminal, dict)
    assert terminal["eval_analysis"]["domain"] == "eval"
    assert terminal["issue_analysis"]["domain"] == "issue"
    assert terminal["workflow_analysis"]["domain"] == "workflow"
    assert "analyses" not in terminal


async def test_archive_remains_an_independent_graph() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    archive = await harness.run(
        bundle.archive,
        input=archive_graph_input(),
        script={"improvement.archive": [committed(archive_agent_output(), _receipt())]},
    )
    retro = await harness.run(
        bundle.retro,
        input=retro_graph_input(),
        script={
            "improvement.retro-collect": [committed(complete_collect_payload(), _receipt())],
            "improvement.retro-eval-analysis": [committed(analysis_agent_output("eval"), _receipt())],
            "improvement.retro-issue-analysis": [committed(analysis_agent_output("issue"), _receipt())],
            "improvement.retro-workflow-analysis": [committed(analysis_agent_output("workflow"), _receipt())],
            "improvement.retro-reconcile": [committed(reconciled_ledger(), _receipt())],
            "improvement.retro": [committed(retro_agent_output(), _receipt())],
        },
    )
    assert [call.semantic_node_id for call in archive.semantic_calls] == ["improvement.archive"]
    assert all(call.semantic_node_id != "improvement.archive" for call in retro.semantic_calls)
    assert archive.terminal is not None
    assert retro.terminal is not None


def _forged_caller_context() -> dict[str, object]:
    forged = retro_context_payload()
    forged["retro_id"] = "FORGED-CALLER-CONTEXT"
    return forged


def test_assemble_builds_repaired_context_from_collect_and_named_analyses() -> None:
    from assurance_improvement.contracts.delivery import artifact_digest
    from assurance_improvement.graphs.nodes import assemble_analyses

    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    assembled = assemble_analyses(
        retro_graph_input(
            context=_forged_caller_context(),
            eval_analysis=analysis_agent_output("eval"),
            issue_analysis=analysis_agent_output("issue"),
            workflow_analysis=analysis_agent_output("workflow"),
        )
    )
    context = RetroContextV3.model_validate(assembled["context"])
    assert context.retro_id == RETRO_ID
    assert context.retro_id != "FORGED-CALLER-CONTEXT"
    assert context.source_manifest.issue_slice_sha256 == artifact_digest(issue)
    assert context.source_manifest.workflow_slice_sha256 == artifact_digest(workflow)
    assert context.source_manifest.eval_slice_sha256 == artifact_digest(evaluation)
    assert assembled["candidates"] == [candidate_payload()]


async def test_reconcile_receives_assembled_context_not_caller_supplied() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.retro,
        input=retro_graph_input(context=_forged_caller_context()),
        script={
            "improvement.retro-collect": [committed(complete_collect_payload(), receipt)],
            "improvement.retro-eval-analysis": [committed(analysis_agent_output("eval"), receipt)],
            "improvement.retro-issue-analysis": [committed(analysis_agent_output("issue"), receipt)],
            "improvement.retro-workflow-analysis": [committed(analysis_agent_output("workflow"), receipt)],
            "improvement.retro-reconcile": [committed(reconciled_ledger(), receipt)],
            "improvement.retro": [committed(retro_agent_output(), receipt)],
        },
    )
    reconcile_selected = result.select_values[4]
    assert isinstance(reconcile_selected, dict)
    assert reconcile_selected["context"]["retro_id"] == RETRO_ID
    assert reconcile_selected["context"]["retro_id"] != "FORGED-CALLER-CONTEXT"
    collected = select_retro_collect(complete_collect_payload())
    from assurance_improvement.contracts.delivery import artifact_digest

    assert reconcile_selected["context"]["source_manifest"]["issue_slice_sha256"] == artifact_digest(
        select_analysis_slice(collected, domain="issue")
    )


async def test_rejected_collect_fail_closes_without_later_agents() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.retro,
        input=retro_graph_input(),
        script={
            "improvement.retro-collect": [RejectedTaskResult(reason="invalid collect")],
            "improvement.retro-eval-analysis": [committed(analysis_agent_output("eval"), receipt)],
            "improvement.retro-issue-analysis": [committed(analysis_agent_output("issue"), receipt)],
            "improvement.retro-workflow-analysis": [committed(analysis_agent_output("workflow"), receipt)],
            "improvement.retro-reconcile": [committed(reconciled_ledger(), receipt)],
            "improvement.retro": [committed(retro_agent_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["improvement.retro-collect"]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"


async def test_rejected_analysis_fail_closes_without_later_agents() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.retro,
        input=retro_graph_input(),
        script={
            "improvement.retro-collect": [committed(complete_collect_payload(), receipt)],
            "improvement.retro-eval-analysis": [RejectedTaskResult(reason="eval analysis rejected")],
            "improvement.retro-issue-analysis": [committed(analysis_agent_output("issue"), receipt)],
            "improvement.retro-workflow-analysis": [committed(analysis_agent_output("workflow"), receipt)],
            "improvement.retro-reconcile": [committed(reconciled_ledger(), receipt)],
            "improvement.retro": [committed(retro_agent_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "improvement.retro-collect",
        "improvement.retro-eval-analysis",
    ]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"


def test_assemble_fail_closes_on_wrong_analysis_domain() -> None:
    from assurance_improvement.graphs.nodes import assemble_analyses

    wrong_domain = analysis_agent_output("eval")
    with pytest.raises(ValueError, match="issue signal domain does not match"):
        assemble_analyses(
            retro_graph_input(
                eval_analysis=analysis_agent_output("eval"),
                issue_analysis=wrong_domain,
                workflow_analysis=analysis_agent_output("workflow"),
            )
        )


def test_assemble_fail_closes_on_conflicting_signal_id() -> None:
    from assurance_improvement.graphs.nodes import assemble_analyses

    issue_slice = _empty_slice("issue")
    issue_slice["deterministic_signals"] = [_task_failure_signal(signal_id="SIG-1", node_id="node-a")]
    conflicting = analysis_agent_output("issue")
    conflicting["signals"] = [_task_failure_signal(signal_id="SIG-1", node_id="node-b")]
    with pytest.raises(ValueError, match="conflicting signal_id 'SIG-1' in issue signals"):
        assemble_analyses(
            retro_graph_input(
                issue_slice=issue_slice,
                eval_analysis=analysis_agent_output("eval"),
                issue_analysis=conflicting,
                workflow_analysis=analysis_agent_output("workflow"),
            )
        )
