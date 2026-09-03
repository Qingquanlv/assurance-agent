from __future__ import annotations

from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    close_improvement_task,
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
    SignalDocumentV3,
    WorkflowEvidenceSlice,
)
from assurance_improvement.operations.retro import (
    AssembleRetroContextHandler,
    ReconcileImprovementsHandler,
    RetroCollectHandler,
    RetroCollectInput,
)
from tests.product.test_change_local_output_routing import execute_task

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    RETRO_ID,
    as_object,
    candidate_payload,
    json_value,
)

_LIFECYCLE_ONLY: dict[str, object] = {
    "change_id": "CH-RETRO-002",
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": ["qa/archive"],
    "evidence_refs": [{"path": "qa/archive/CH-RETRO-002/inspect/inspection.json", "digest": "b" * 64}],
    "lifecycle_state": "evaluating",
}
_WINDOW = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": ["CH-DEMO-001"]}


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


def _slice_model(
    domain: Literal["issue", "workflow", "eval"],
) -> IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice:
    raw = _empty_slice(domain)
    if domain == "issue":
        return IssueEvidenceSlice.model_validate(raw)
    if domain == "workflow":
        return WorkflowEvidenceSlice.model_validate(raw)
    return EvalEvidenceSlice.model_validate(raw)


def complete_collect_payload() -> dict[str, object]:
    return {
        "retro_id": RETRO_ID,
        "window": _WINDOW,
        "issue_slice": _empty_slice("issue"),
        "workflow_slice": _empty_slice("workflow"),
        "eval_slice": _empty_slice("eval"),
    }


def test_lifecycle_only_public_payload_is_rejected_by_collect_selector() -> None:
    with pytest.raises((ValidationError, ValueError)):
        select_retro_collect(_LIFECYCLE_ONLY)


@pytest.mark.asyncio
async def test_lifecycle_only_public_payload_fails_production_collect() -> None:
    outcome = await execute_task(RetroCollectHandler(), json_value(_LIFECYCLE_ONLY))
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_lifecycle_only_payload_fails_analyses_and_reconcile() -> None:
    assemble = await execute_task(AssembleRetroContextHandler(), json_value(_LIFECYCLE_ONLY))
    assert assemble.status == "failed"
    assert assemble.failure is not None
    assert assemble.failure.kind == "invalid_input"
    reconcile = await execute_task(ReconcileImprovementsHandler(), json_value(_LIFECYCLE_ONLY))
    assert reconcile.status == "failed"
    assert reconcile.failure is not None
    assert reconcile.failure.kind == "invalid_input"
    with pytest.raises((ValidationError, ValueError)):
        select_analysis_slice(cast(Any, _LIFECYCLE_ONLY), domain="eval")
    with pytest.raises((ValidationError, ValueError)):
        select_retro_reconcile(
            context=cast(Any, _LIFECYCLE_ONLY),
            candidates=(),
            current=cast(Any, _LIFECYCLE_ONLY),
            ts="2026-08-22T00:00:00Z",
        )


def test_collect_requires_authenticated_window_and_all_three_slices() -> None:
    collected = select_retro_collect(complete_collect_payload())
    assert collected.retro_id == RETRO_ID
    assert collected.window.change_ids == ("CH-DEMO-001",)
    assert collected.issue_slice.domain == "issue"
    assert collected.workflow_slice.domain == "workflow"
    assert collected.eval_slice.domain == "eval"
    missing_eval = dict(complete_collect_payload())
    del missing_eval["eval_slice"]
    with pytest.raises((ValidationError, ValueError)):
        select_retro_collect(missing_eval)


def test_eval_analysis_receives_only_the_authenticated_eval_slice() -> None:
    collected = select_retro_collect(complete_collect_payload())
    slice_ = select_analysis_slice(collected, domain="eval")
    assert isinstance(slice_, EvalEvidenceSlice)
    assert slice_ == collected.eval_slice
    dumped = slice_.model_dump(mode="json")
    assert dumped["domain"] == "eval"
    assert "issue_slice" not in dumped
    assert "workflow_slice" not in dumped
    assert dumped["retro_id"] == collected.retro_id
    assert dumped["window"] == collected.window.model_dump(mode="json")


def test_issue_and_workflow_analyses_receive_matching_slices() -> None:
    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    assert isinstance(issue, IssueEvidenceSlice)
    assert isinstance(workflow, WorkflowEvidenceSlice)
    assert issue == collected.issue_slice
    assert workflow == collected.workflow_slice
    assert issue.domain == "issue"
    assert workflow.domain == "workflow"
    assert "eval_slice" not in issue.model_dump(mode="json")
    assert "eval_slice" not in workflow.model_dump(mode="json")


@pytest.mark.asyncio
async def test_three_analyses_assemble_context_before_reconcile() -> None:
    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    issue_digest = artifact_digest(issue)
    workflow_digest = artifact_digest(workflow)
    eval_digest = artifact_digest(evaluation)
    assert issue_digest != HEX_A
    assemble = await execute_task(
        AssembleRetroContextHandler(),
        json_value(
            {
                "generated_at": "2026-08-22T00:00:00Z",
                "window": _WINDOW,
                "issue_slice": issue.model_dump(mode="json"),
                "workflow_slice": workflow.model_dump(mode="json"),
                "eval_slice": evaluation.model_dump(mode="json"),
                "issue_signals": _ok_signals("issue", issue_digest),
                "workflow_signals": _ok_signals("workflow", workflow_digest),
                "eval_signals": _ok_signals("eval", eval_digest),
                "issue_slice_sha256": issue_digest,
                "workflow_slice_sha256": workflow_digest,
                "eval_slice_sha256": eval_digest,
            }
        ),
    )
    assert assemble.status == "succeeded"
    context = as_object(assemble.output)
    assert context["retro_id"] == RETRO_ID
    assert context["source_manifest"]["issue_slice_sha256"] == issue_digest
    assert context["source_manifest"]["workflow_slice_sha256"] == workflow_digest
    assert context["source_manifest"]["eval_slice_sha256"] == eval_digest
    empty = ImprovementLedgerProjection.model_validate(
        {"schema_version": "1", "last_seq": 0, "improvements": {}, "by_fingerprint": {}}
    )
    reconcile_input = select_retro_reconcile(
        context=context,
        candidates=(),
        current=empty,
        ts="2026-08-22T00:00:00Z",
    )
    reconcile = await execute_task(
        ReconcileImprovementsHandler(),
        json_value(reconcile_input.model_dump(mode="json")),
    )
    assert reconcile.status == "succeeded"


@pytest.mark.asyncio
async def test_reconcile_receives_context_candidates_and_current_projection() -> None:
    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    assemble = await execute_task(
        AssembleRetroContextHandler(),
        json_value(
            {
                "generated_at": "2026-08-22T00:00:00Z",
                "window": _WINDOW,
                "issue_slice": issue.model_dump(mode="json"),
                "workflow_slice": workflow.model_dump(mode="json"),
                "eval_slice": evaluation.model_dump(mode="json"),
                "issue_signals": _ok_signals("issue", artifact_digest(issue)),
                "workflow_signals": _ok_signals("workflow", artifact_digest(workflow)),
                "eval_signals": _ok_signals("eval", artifact_digest(evaluation)),
                "issue_slice_sha256": artifact_digest(issue),
                "workflow_slice_sha256": artifact_digest(workflow),
                "eval_slice_sha256": artifact_digest(evaluation),
            }
        ),
    )
    assert assemble.status == "succeeded"
    current = ImprovementLedgerProjection.model_validate(
        {"schema_version": "1", "last_seq": 0, "improvements": {}, "by_fingerprint": {}}
    )
    candidate = candidate_payload()
    selected = select_retro_reconcile(
        context=as_object(assemble.output),
        candidates=(candidate,),
        current=current,
        ts="2026-08-22T00:00:00Z",
    )
    assert selected.context.retro_id == RETRO_ID
    assert len(selected.candidates) == 1
    assert selected.current.last_seq == 0
    outcome = await execute_task(
        ReconcileImprovementsHandler(),
        json_value(selected.model_dump(mode="json")),
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["last_seq"] >= 1
    assert payload["improvements"]


@pytest.mark.asyncio
async def test_final_retro_agent_receives_the_reconciled_typed_value() -> None:
    collected = select_retro_collect(complete_collect_payload())
    issue = select_analysis_slice(collected, domain="issue")
    workflow = select_analysis_slice(collected, domain="workflow")
    evaluation = select_analysis_slice(collected, domain="eval")
    assemble = await execute_task(
        AssembleRetroContextHandler(),
        json_value(
            {
                "generated_at": "2026-08-22T00:00:00Z",
                "window": _WINDOW,
                "issue_slice": issue.model_dump(mode="json"),
                "workflow_slice": workflow.model_dump(mode="json"),
                "eval_slice": evaluation.model_dump(mode="json"),
                "issue_signals": _ok_signals("issue", artifact_digest(issue)),
                "workflow_signals": _ok_signals("workflow", artifact_digest(workflow)),
                "eval_signals": _ok_signals("eval", artifact_digest(evaluation)),
                "issue_slice_sha256": artifact_digest(issue),
                "workflow_slice_sha256": artifact_digest(workflow),
                "eval_slice_sha256": artifact_digest(evaluation),
            }
        ),
    )
    selected = select_retro_reconcile(
        context=as_object(assemble.output),
        candidates=(candidate_payload(),),
        current=ImprovementLedgerProjection.model_validate(
            {"schema_version": "1", "last_seq": 0, "improvements": {}, "by_fingerprint": {}}
        ),
        ts="2026-08-22T00:00:00Z",
    )
    outcome = await execute_task(
        ReconcileImprovementsHandler(),
        json_value(selected.model_dump(mode="json")),
    )
    assert outcome.status == "succeeded"
    ledger = ImprovementLedgerProjection.model_validate(
        {
            "schema_version": as_object(outcome.output)["schema_version"],
            "last_seq": as_object(outcome.output)["last_seq"],
            "improvements": as_object(outcome.output)["improvements"],
            "by_fingerprint": as_object(outcome.output)["by_fingerprint"],
        }
    )
    received = select_retro_agent(ledger)
    assert received == ledger
    assert received.last_seq >= 1
    assert received.improvements
    assert artifact_digest(received) == artifact_digest(ledger)


def test_digests_originate_from_authenticated_receipt_state_not_filesystem() -> None:
    collected = select_retro_collect(complete_collect_payload())
    for domain in ("issue", "workflow", "eval"):
        slice_ = select_analysis_slice(collected, domain=cast(Literal["issue", "workflow", "eval"], domain))
        digest = artifact_digest(slice_)
        signals = SignalDocumentV3.model_validate(_ok_signals(domain, digest))
        assert signals.slice_sha256 == digest
        assert signals.slice_sha256 != HEX_A
    forged = dict(complete_collect_payload())
    forged["eval_slice"] = {**_empty_slice("eval"), "retro_id": "RET-FORGED"}
    with pytest.raises((ValidationError, ValueError)):
        select_retro_collect(forged)


def test_collect_and_reconcile_contracts_stay_unbound() -> None:
    collect = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-collect-v3"]
    reconcile = TASK_ATTEMPT_CONTRACTS["assurance.improvement.reconcile-improvements"]
    assert collect.validators == ()
    assert reconcile.validators == ()
    assert collect.input_model is RetroCollectInput
    closed = close_improvement_task("assurance.improvement.retro-collect-v3")
    assert closed.handler_id == "assurance.improvement.retro-collect-v3"
