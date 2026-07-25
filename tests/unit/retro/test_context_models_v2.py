import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.retro.types import (
    EvalRetroSignals,
    IssueRetroSignals,
    RetroContext,
    RetroIntegrity,
    RetroSelectionSnapshot,
    RetroSignal,
    RetroSignalSet,
    RetroSourceDescriptor,
    RetroSourceManifest,
    RetroWindow,
    WorkflowRetroSignals,
)


def _empty_signal_set() -> RetroSignalSet:
    return RetroSignalSet(
        issue=IssueRetroSignals(),
        workflow=WorkflowRetroSignals(),
        eval=EvalRetroSignals(),
    )


def _minimal_context(**overrides: object) -> dict:
    doc: dict = {
        "retro_id": "retro-1",
        "generated_at": "2026-07-25T00:00:00Z",
        "window": {
            "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
            "change_ids": ["CH-1"],
        },
        "source_manifest": {
            "issue_slice_sha256": "sha256:slice",
            "issue_sources": [
                {
                    "kind": "change_issue_ledger",
                    "change_id": "CH-1",
                    "sha256": "sha256:issue",
                    "evidence_ids": ["ev-1"],
                }
            ],
            "workflow_sources": [],
            "eval_sources": [],
        },
        "integrity": {"status": "complete"},
        "signals": {
            "issue": {},
            "workflow": {},
            "eval": {},
        },
        "signal_count": 0,
    }
    doc.update(overrides)
    return doc


def test_retro_context_schema_version_is_v2() -> None:
    context = RetroContext.model_validate(_minimal_context())
    assert context.schema_version == "2"


def test_retro_context_rejects_legacy_flat_signal_shape() -> None:
    legacy = {
        "retro_id": "retro-1",
        "generated_at": "2026-07-25T00:00:00Z",
        "window": {"change_count": 1, "change_ids": ["CH-1"], "change_sources": []},
        "signals": {
            "failure_distribution": [],
            "gate_pushback": [],
            "healing_efficiency": {"attempts": 0, "applied": 0, "success_rate": 0.0},
        },
        "signal_count": 0,
    }
    with pytest.raises(ValidationError):
        RetroContext.model_validate(legacy)


def test_retro_signal_carries_source_refs() -> None:
    signal = RetroSignal(
        signal_id="sig-1",
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-1",)),
        metrics={"count": 3},
    )
    assert signal.source_refs.problem_ids == ("PROB-1",)


def test_retro_source_manifest_resolvable_ids() -> None:
    manifest = RetroSourceManifest(
        issue_slice_sha256="sha256:slice",
        issue_sources=(
            RetroSourceDescriptor(
                kind="change_issue_ledger",
                change_id="CH-1",
                sha256="sha256:issue",
                evidence_ids=("ev-1", "ev-2"),
            ),
        ),
        workflow_sources=(
            RetroSourceDescriptor(
                kind="workflow_ledger",
                sha256="sha256:wf",
                evidence_ids=("wf-1",),
            ),
        ),
        eval_sources=(),
    )
    assert manifest.resolvable_ids() == frozenset({"ev-1", "ev-2", "wf-1"})


def test_retro_window_requires_selection_snapshot() -> None:
    window = RetroWindow(
        selection=RetroSelectionSnapshot(mode="last", requested_last=5),
        change_ids=("CH-1", "CH-2"),
    )
    assert window.selection.requested_last == 5


def test_retro_integrity_incomplete_with_reasons() -> None:
    integrity = RetroIntegrity(status="incomplete", reasons=("missing eval history",))
    assert integrity.status == "incomplete"
    assert integrity.reasons[0].startswith("missing")
