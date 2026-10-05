from __future__ import annotations

from typing import Literal, cast

import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    close_improvement_task,
    select_analysis_slice,
    select_retro_collect,
)
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroBuildSlicesInputV1,
    RetroCollectAttemptInput,
    RetroCollectInput,
    SignalDocumentV3,
    WorkflowEvidenceSlice,
)
from assurance_improvement.operations.retro import (
    RetroCollectHandler,
)
from tests.product.test_change_local_output_routing import execute_task

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    RETRO_ID,
    json_value,
)

_LIFECYCLE_ONLY: dict[str, object] = {
    "change_id": "CH-RETRO-002",
    "capability_leafs": ["auth.session.create"],
    "allowed_artifact_paths": [
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    ],
    "evidence_refs": [{"path": "qa/results/inspect/inspection.json", "digest": "b" * 64}],
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
    build = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-build-slices"]
    collect = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-collect-v3"]
    reconcile = TASK_ATTEMPT_CONTRACTS["assurance.improvement.reconcile-improvements"]
    assert collect.validators == ()
    assert reconcile.validators == ()
    assert collect.input_model is RetroCollectAttemptInput
    assert build.contract_id == "assurance.improvement.retro-build-slices"
    assert build.input_model is RetroBuildSlicesInputV1
    assert build.output_model is RetroCollectInput
    closed = close_improvement_task("assurance.improvement.retro-collect-v3")
    assert closed.handler_id == "assurance.improvement.retro-collect-v3"
