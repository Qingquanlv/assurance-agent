from __future__ import annotations

from typing import Literal, cast, Any

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
from assurance_improvement.contracts.agent import RetroAnalysisInputV1, RetroSynthesisInputV1
from assurance_improvement.contracts.retro import RetroReconcileInputV1
from assurance_improvement.graphs.factory import build_improvement_graphs as _build_improvement_graphs
from graph_engine.attempts.resolutions import AttemptResolution, ReceiptRef, RejectedTaskResult
from graph_engine.testing import GraphHarness, committed

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    IMPROVEMENT_ID,
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

from graph_engine.testing.feature_bundle import compile_bundle


def build_improvement_graphs(*args, **kwargs):
    return compile_bundle(_build_improvement_graphs(*args, **kwargs))


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
        "source_refs": [],
    }
    payload.update(overrides)
    return payload


def _slice_artifacts() -> list[dict[str, str]]:
    return [
        {"path": "qa/results/retro/issue-slice.json", "digest": "b" * 64},
        {"path": "qa/results/retro/workflow-slice.json", "digest": "b" * 64},
        {"path": "qa/results/retro/eval-slice.json", "digest": "b" * 64},
    ]


def _slices(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **complete_collect_payload(),
        "discovery_slice": None,
        "coverage_gap_slice": None,
    }
    payload.update(overrides)
    return payload


def _analysis_ref(domain: str) -> dict[str, str]:
    return {"path": f"qa/results/retro/retro-{domain}-analysis.json", "digest": "a" * 64}


def _analysis_commit(
    domain: Literal["issue", "workflow", "eval"],
    receipt: ReceiptRef,
    output: dict[str, object] | None = None,
) -> AttemptResolution:
    return committed(output or analysis_agent_output(domain), receipt, artifacts=[_analysis_ref(domain)])


def analysis_agent_output(domain: Literal["issue", "workflow", "eval"]) -> dict[str, object]:
    return {
        "schema_version": "3",
        "retro_id": RETRO_ID,
        "domain": domain,
        "analysis_status": "ok",
        "failure_reason": None,
        "analyzer": f"aa-retro-{domain}-analysis",
        "signals": [_task_failure_signal(signal_id="SIG-1", node_id="quality.inspect")]
        if domain == "issue"
        else [],
        "candidates": [],
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


def _flow_script(
    receipt: ReceiptRef,
    *,
    retro: dict[str, object] | None = None,
    eval_resolution: AttemptResolution | None = None,
    synthesize_route: str = "synthesize",
) -> dict[str, list[AttemptResolution]]:
    return {
        "improvement.retro-build-slices": [committed(_slices(), receipt, artifacts=_slice_artifacts())],
        "improvement.retro-collect": [
            committed(
                {**_slices(), "generated_at": _TS},
                receipt,
                artifacts=[{"path": "qa/results/retro/collected.json", "digest": "c" * 64}],
            )
        ],
        "improvement.retro-eval-analysis": [
            eval_resolution if eval_resolution is not None else _analysis_commit("eval", receipt)
        ],
        "improvement.retro-issue-analysis": [_analysis_commit("issue", receipt)],
        "improvement.retro-workflow-analysis": [_analysis_commit("workflow", receipt)],
        "improvement.retro-synthesize": [
            committed(
                {"route": synthesize_route, "context": retro_context_payload(), "candidates": []},
                receipt,
                artifacts=[{"path": "qa/results/retro/context.json", "digest": "d" * 64}],
            )
        ],
        "improvement.retro": [
            committed(
                retro if retro is not None else retro_agent_output(),
                receipt,
                artifacts=[{"path": "qa/results/retro/candidates.json", "digest": "e" * 64}],
            )
        ],
        "improvement.retro-reconcile": [committed(reconcile_result(), receipt)],
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


def reconcile_result() -> dict[str, object]:
    ledger = reconciled_ledger()
    return {
        "reconciliation": {**ledger, "improvement_ids": [IMPROVEMENT_ID], "events": []},
        "status": {"retro_id": RETRO_ID, "result": "completed", "improvement_ids": [IMPROVEMENT_ID]},
        "artifact_refs": [],
    }


def test_reconcile_output_retains_events_and_publishes_only_typed_ledger() -> None:
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS

    contract = TASK_ATTEMPT_CONTRACTS["assurance.improvement.reconcile-improvements"]
    payload = {
        **cast(dict[str, Any], reconcile_result()["reconciliation"]),
        "events": [{"type": "improvement_proposed", "improvement_id": IMPROVEMENT_ID, "seq": 1}],
    }
    result = contract.output_model.model_validate({**reconcile_result(), "reconciliation": payload})
    assert result.reconciliation.model_dump(mode="json")["events"] == payload["events"]
    with pytest.raises(ValidationError):
        contract.output_model.model_validate(reconciled_ledger())


def test_analysis_inputs_receive_only_matching_authenticated_slices() -> None:
    collected = select_retro_collect(complete_collect_payload())
    state = retro_graph_input()
    eval_slice = select_analysis_slice(collected, domain="eval")
    issue_slice = select_analysis_slice(collected, domain="issue")
    workflow_slice = select_analysis_slice(collected, domain="workflow")
    assert isinstance(eval_slice, EvalEvidenceSlice)
    assert isinstance(issue_slice, IssueEvidenceSlice)
    assert isinstance(workflow_slice, WorkflowEvidenceSlice)
    selected_eval = RetroAnalysisInputV1.model_validate(
        {"change_id": state["change_id"], "evidence_slice": eval_slice}
    )
    selected_issue = RetroAnalysisInputV1.model_validate(
        {"change_id": state["change_id"], "evidence_slice": issue_slice}
    )
    selected_workflow = RetroAnalysisInputV1.model_validate(
        {"change_id": state["change_id"], "evidence_slice": workflow_slice}
    )
    assert selected_eval.change_id == "CH-DEMO-001"
    assert selected_issue.evidence_slice == issue_slice
    assert selected_workflow.evidence_slice == workflow_slice


def test_reconcile_and_retro_agent_receive_typed_values() -> None:
    state = retro_graph_input(candidates=(candidate_payload(),))
    selected = RetroReconcileInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "context": state["context"],
            "candidates": state["candidates"],
        }
    )
    expected = select_retro_reconcile(
        context=RetroContextV3.model_validate(state["context"]),
        candidates=(candidate_payload(),),
        current=ImprovementLedgerProjection.model_validate(state["current"]),
        ts=_TS,
    )
    assert selected.context.retro_id == expected.context.retro_id
    assert selected.context.generated_at == expected.ts
    ledger = ImprovementLedgerProjection.model_validate(reconciled_ledger())
    received = select_retro_agent(ledger)
    assert received == ledger
    retro_state = {**state, "ledger": ledger.model_dump(mode="json")}
    skill = RetroSynthesisInputV1.model_validate(
        {"change_id": retro_state["change_id"], "context": retro_state["context"]}
    )
    assert skill.context is not None
    assert skill.change_id == "CH-DEMO-001"
    assert skill.context.retro_id == RETRO_ID


async def test_retro_traces_collect_three_analyses_synthesis_then_reconcile() -> None:
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
        script=_flow_script(receipt),
    )
    calls = [call.semantic_node_id for call in result.semantic_calls]
    assert calls[:2] == ["improvement.retro-build-slices", "improvement.retro-collect"]
    assert set(calls[2:5]) == {
        "improvement.retro-eval-analysis",
        "improvement.retro-issue-analysis",
        "improvement.retro-workflow-analysis",
    }
    assert calls[5:] == [
        "improvement.retro-synthesize",
        "improvement.retro",
        "improvement.retro-reconcile",
    ]
    by_id = {call.semantic_node_id: call.contract_id for call in result.semantic_calls}
    assert by_id["improvement.retro-collect"] == TASK_COLLECT_ID
    assert by_id["improvement.retro-reconcile"] == TASK_RECONCILE_ID
    assert by_id["improvement.retro"] == _RETRO_ID
    assert by_id["improvement.retro-synthesize"] == "assurance.improvement.task.retro-synthesize"
    analysis_ids = [call.contract_id for call in result.semantic_calls[2:5]]
    assert set(analysis_ids) == {_RETRO_EVAL_ID, _RETRO_ISSUE_ID, _RETRO_WORKFLOW_ID}
    collect_selected = result.select_values[1]
    assert isinstance(collect_selected, dict)
    assert collect_selected["retro_id"] == RETRO_ID
    assert collect_selected["issue_slice_ref"]["path"] == "qa/results/retro/issue-slice.json"
    assert collect_selected["workflow_slice_ref"]["path"] == "qa/results/retro/workflow-slice.json"
    assert collect_selected["eval_slice_ref"]["path"] == "qa/results/retro/eval-slice.json"
    reconcile_selected = result.select_values[calls.index("improvement.retro-reconcile")]
    assert isinstance(reconcile_selected, dict)
    assert reconcile_selected["context_ref"]["path"] == "qa/results/retro/context.json"
    assert reconcile_selected["candidates_ref"]["path"] == "qa/results/retro/candidates.json"
    assert "context" not in reconcile_selected
    assert "current" not in reconcile_selected
    terminal = result.terminal
    assert terminal is not None
    assert isinstance(terminal, dict)
    assert terminal["status"] == "done"
    assert "issue_analysis" not in terminal
    assert "eval_analysis" not in terminal
    assert "workflow_analysis" not in terminal
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
        script=_flow_script(_receipt()),
    )
    assert [call.semantic_node_id for call in archive.semantic_calls] == ["improvement.archive"]
    assert all(call.semantic_node_id != "improvement.archive" for call in retro.semantic_calls)
    assert archive.terminal is not None
    assert retro.terminal is not None


async def test_failed_synthesis_does_not_reconcile_when_signals_exist() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.improvement", contracts=improvement_contracts())
    result = await harness.run(
        build_improvement_graphs(context).retro,
        input=retro_graph_input(),
        script=_flow_script(
            _receipt(),
            retro={
                "retro_id": RETRO_ID,
                "analysis_status": "failed",
                "failure_reason": "cannot synthesize",
                "signals": [],
                "candidates": [],
            },
        ),
    )
    assert result.semantic_calls[-1].semantic_node_id == "improvement.retro"
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == "failed"


def _forged_caller_context() -> dict[str, object]:
    forged = retro_context_payload()
    forged["retro_id"] = "FORGED-CALLER-CONTEXT"
    return forged


def _routed(state: dict[str, object]):
    from assurance_improvement.contracts.retro import RetroSynthesizeInputV1
    from assurance_improvement.operations.retro import route_retro_synthesis

    payload = RetroSynthesizeInputV1.model_validate(
        {
            "generated_at": state["ts"],
            "dry_run": bool(state.get("dry_run") or False),
            "retro_id": state["retro_id"],
            "window": state["window"],
            "issue_slice": state["issue_slice"],
            "workflow_slice": state["workflow_slice"],
            "eval_slice": state["eval_slice"],
            "discovery_slice": state.get("discovery_slice"),
            "coverage_gap_slice": state.get("coverage_gap_slice"),
            "issue_analysis_ref": _analysis_ref("issue"),
            "workflow_analysis_ref": _analysis_ref("workflow"),
            "eval_analysis_ref": _analysis_ref("eval"),
        }
    )
    return route_retro_synthesis(
        payload,
        {
            "issue": cast(dict[str, object], state["issue_analysis"]),
            "workflow": cast(dict[str, object], state["workflow_analysis"]),
            "eval": cast(dict[str, object], state["eval_analysis"]),
        },
    )


def test_assemble_builds_repaired_context_from_collect_and_named_analyses() -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    assembled = _routed(
        retro_graph_input(
            context=_forged_caller_context(),
            eval_analysis=analysis_agent_output("eval"),
            issue_analysis=analysis_agent_output("issue"),
            workflow_analysis=analysis_agent_output("workflow"),
        )
    )
    context = assembled.context
    assert context.retro_id == RETRO_ID
    assert context.retro_id != "FORGED-CALLER-CONTEXT"
    assert context.source_manifest.issue_slice_sha256 == artifact_digest(issue)
    assert context.source_manifest.workflow_slice_sha256 == artifact_digest(workflow)
    assert context.source_manifest.eval_slice_sha256 == artifact_digest(evaluation)
    assert assembled.candidates == ()


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
        script=_flow_script(receipt),
    )
    calls = [call.semantic_node_id for call in result.semantic_calls]
    reconcile_selected = result.select_values[calls.index("improvement.retro-reconcile")]
    assert isinstance(reconcile_selected, dict)
    assert reconcile_selected["context_ref"]["path"] == "qa/results/retro/context.json"
    assert "context" not in reconcile_selected
    assert "FORGED-CALLER-CONTEXT" not in str(reconcile_selected)


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
            **_flow_script(receipt),
            "improvement.retro-collect": [
                cast(AttemptResolution, RejectedTaskResult(reason="invalid collect"))
            ],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "improvement.retro-build-slices",
        "improvement.retro-collect",
    ]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"


async def test_rejected_slice_build_does_not_enter_collect() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    result = await harness.run(
        bundle.retro,
        input=retro_graph_input(),
        script={
            "improvement.retro-build-slices": [RejectedTaskResult(reason="source digest drifted")],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["improvement.retro-build-slices"]
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
        script=_flow_script(receipt, eval_resolution=RejectedTaskResult(reason="eval analysis rejected")),
    )
    calls = [call.semantic_node_id for call in result.semantic_calls]
    assert calls[:2] == ["improvement.retro-build-slices", "improvement.retro-collect"]
    assert "improvement.retro-eval-analysis" in calls
    assert "improvement.retro-synthesize" not in calls
    assert "improvement.retro" not in calls
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"


def _coverage_gap_slice(*, reasons: tuple[str, ...] = ()) -> dict[str, object]:
    payload = _empty_slice("coverage_gap")
    payload["sources"] = [
        {
            "kind": "coverage_gap_projection",
            "change_id": "CH-DEMO-001",
            "head_event_id": None,
            "sha256": "cg-1",
            "evidence_ids": ["coverage-gap-1"],
        }
    ]
    if reasons:
        payload["integrity"] = {"status": "incomplete", "reasons": list(reasons)}
    return payload


def _analysis_state(**overrides: object) -> dict[str, object]:
    state = retro_graph_input(
        eval_analysis=analysis_agent_output("eval"),
        issue_analysis=analysis_agent_output("issue"),
        workflow_analysis=analysis_agent_output("workflow"),
    )
    state.update(overrides)
    return state


def test_assemble_surfaces_coverage_gap_slice_integrity_reasons() -> None:
    assembled = _routed(
        _analysis_state(coverage_gap_slice=_coverage_gap_slice(reasons=("coverage_gap_evidence_corrupt",)))
    )
    context = assembled.context
    assert "coverage_gap_evidence_corrupt" in context.integrity.reasons
    assert context.integrity.status == "incomplete"


def test_assemble_marks_collected_coverage_gap_domain_skipped_not_absent() -> None:
    assembled = _routed(_analysis_state(coverage_gap_slice=_coverage_gap_slice()))
    context = assembled.context
    assert context.source_manifest.coverage_gap_slice_sha256 is not None
    assert len(context.source_manifest.coverage_gap_sources) == 1
    status = context.domain_status.coverage_gap
    assert status is not None
    assert status.status == "skipped"


def test_assemble_leaves_coverage_gap_domain_absent_without_a_slice() -> None:
    assembled = _routed(_analysis_state())
    context = assembled.context
    assert context.domain_status.coverage_gap is None
    assert context.source_manifest.coverage_gap_slice_sha256 is None
    assert context.source_manifest.coverage_gap_sources == ()


def test_assemble_fail_closes_on_wrong_analysis_domain() -> None:
    wrong_domain = analysis_agent_output("eval")
    with pytest.raises(ValueError, match="issue signal domain does not match"):
        _routed(_analysis_state(issue_analysis=wrong_domain))


def test_assemble_fail_closes_on_conflicting_signal_id() -> None:
    issue_slice = _empty_slice("issue")
    issue_slice["deterministic_signals"] = [_task_failure_signal(signal_id="SIG-1", node_id="node-a")]
    conflicting = analysis_agent_output("issue")
    conflicting["signals"] = [_task_failure_signal(signal_id="SIG-1", node_id="node-b")]
    with pytest.raises(ValueError, match="conflicting signal_id 'SIG-1' in issue signals"):
        _routed(_analysis_state(issue_slice=issue_slice, issue_analysis=conflicting))
