from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import cast

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.retro_batch import RetroBatchScope
from assurance_agent.artifacts.models.retro_v3 import (
    BatchMemberEvidenceGapSignal,
    TaskFailureSignal,
)
from assurance_agent.retro.eval_history import EvalHistoryReader
from assurance_agent.retro.slices import RetroSliceImmutableError, materialize_slices
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.retro.workflow_history import TerminalChangeRef, WorkflowHistoryReader
from assurance_agent.workflow.issues.history import IssueHistoryReader


class _WorkflowReader:
    def list_terminal_changes(self, change_ids=None, *, tolerate_member_errors=False):
        del tolerate_member_errors
        refs = (TerminalChangeRef(change_id="CH-1", terminal_ts="2026-07-01T00:00:00Z"),)
        if change_ids is None:
            return refs
        return tuple(ref for ref in refs if ref.change_id in change_ids)

    def discover_change_ids(self):
        return frozenset({"CH-1"})

    def read_window(self, window):
        return NS(
            integrity=RetroIntegrity(status="complete"),
            sources=(
                RetroSourceDescriptor(
                    kind="workflow_ledger",
                    change_id="CH-1",
                    sha256="sha256:wf",
                    evidence_ids=("stale-id",),
                ),
            ),
            gate_verdicts=(
                NS(
                    evidence_id="CH-1#seq4",
                    change_id="CH-1",
                    gate="archive-gate",
                    verdict="stop",
                    cause="archive.execution_failed",
                    reason="failed",
                    ts="2026-07-01T00:00:04Z",
                ),
            ),
            task_failures=(
                NS(
                    evidence_id="CH-1#seq3",
                    change_id="CH-1",
                    task_id="task-1",
                    attempt_id="attempt-1",
                    node_id="execute",
                    error_kind="internal",
                    message="boom  42",
                    recovered=False,
                    ts="2026-07-01T00:00:03Z",
                ),
            ),
            healing_allocations=(),
            healing_applies=(),
            skill_loaded_false=(
                NS(
                    evidence_id="CH-1#workflow-state:explore",
                    change_id="CH-1",
                    phase="explore",
                    expected_skill=None,
                ),
            ),
        )


class _IssueReader:
    def read_window(self, selection):
        occurrence = NS(
            occurrence_id="occ-1",
            problem_id="problem-1",
            change_id="CH-1",
            batch_id="batch-1",
            observation_ids=["obs-1"],
            provisional_assessment=NS(classification="product_bug"),
        )
        problem = NS(
            problem_id="problem-1",
            title="User endpoint fails",
            fingerprint=NS(
                digest="sha256:problem",
                preimage=NS(surface_kind="endpoint", surface_identity="GET /users", symptom="500"),
            ),
        )
        return NS(
            integrity=RetroIntegrity(status="complete"),
            sources=(
                NS(
                    kind="change_issue_ledger",
                    change_id="CH-1",
                    head_event_id="change-event-2",
                    sha256="sha256:issue",
                ),
                NS(
                    kind="project_problem_ledger",
                    change_id=None,
                    head_event_id="problem-event-2",
                    sha256="sha256:problems",
                ),
            ),
            observations=(NS(observation_id="obs-1", change_id="CH-1", observed_at="2026-07-01T00:00:01Z"),),
            occurrences=(occurrence, occurrence),
            problem_snapshots=(problem,),
            problem_events=(NS(event_id="problem-event-1", problem_id="problem-1"),),
        )


class _EvalReader:
    def read_window(self, window):
        record = NS(
            run_id="run-1",
            suite="workflow-case",
            verdict="fail",
            failure_signature="threshold:coverage",
            started_at="2026-07-01T00:00:05Z",
            source_change_ids=("CH-1",),
            sample_ids=("S-1",),
            sha256="sha256:eval",
        )
        return NS(reports=(record, record), integrity=RetroIntegrity(status="complete"))


class _GapIssueReader(_IssueReader):
    def read_window(self, selection):
        result = super().read_window(selection)
        result.integrity = RetroIntegrity(
            status="incomplete",
            reasons=("batch_member_evidence_gap:CH-2:failed:issue:workspace_missing",),
        )
        return result


class _GapWorkflowReader(_WorkflowReader):
    def read_window(self, window):
        result = super().read_window(window)
        result.integrity = RetroIntegrity(
            status="incomplete",
            reasons=("batch_member_evidence_gap:CH-2:failed:workflow:ledger_corrupt",),
        )
        return result


class _HardFailureWorkflowReader(_WorkflowReader):
    def read_window(self, window):
        result = super().read_window(window)
        result.task_failures = (
            NS(
                evidence_id="CH-1#seq230",
                change_id="CH-1",
                task_id="task-analyze",
                attempt_id="attempt-1",
                node_id="analyze-issues",
                error_kind="forbidden_write",
                message="forbidden write outside authorization_writes",
                recovered=False,
                ts="2026-07-01T00:00:03Z",
            ),
            NS(
                evidence_id="CH-1#seq232",
                change_id="CH-1",
                task_id="task-inspect",
                attempt_id="attempt-1",
                node_id="inspect-with-issues",
                error_kind="internal",
                message="issue subgraph failed",
                recovered=False,
                ts="2026-07-01T00:00:04Z",
            ),
            NS(
                evidence_id="CH-1#seq234",
                change_id="CH-1",
                task_id="task-assurance",
                attempt_id="attempt-1",
                node_id="assurance",
                error_kind="internal",
                message="inspect subgraph failed",
                recovered=False,
                ts="2026-07-01T00:00:05Z",
            ),
        )
        return result


def _materialize(tmp_path: Path):
    return materialize_slices(
        tmp_path,
        retro_id="retro-1",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        write_root=tmp_path,
    )


def test_materialize_slices_maps_deduplicates_and_rebuilds_source_ids(tmp_path: Path) -> None:
    bundle = _materialize(tmp_path)

    assert tuple(entry.occurrence_id for entry in bundle.issue.entries) == ("occ-1",)
    assert bundle.issue.entries[0].surface.value == "GET /users"
    assert bundle.issue.sources[0].evidence_ids == ("obs-1", "occ-1")
    assert bundle.issue.sources[1].evidence_ids == ("problem-1", "problem-event-1")
    assert tuple(entry.evidence_id for entry in bundle.workflow.entries) == (
        "CH-1#seq3",
        "CH-1#seq4",
        "CH-1#workflow-state:explore",
    )
    assert bundle.workflow.sources[0].evidence_ids == (
        "CH-1#seq3",
        "CH-1#seq4",
        "CH-1#workflow-state:explore",
    )
    skill_entry = bundle.workflow.entries[-1]
    assert skill_entry.entry_kind == "skill_drift"
    assert skill_entry.expected_skill == "aa-explore"
    assert tuple(entry.run_id for entry in bundle.eval.entries) == ("run-1",)
    assert bundle.eval.sources[0].evidence_ids == ("run-1",)
    assert json.loads((tmp_path / "qa/retro/retro-1/window.json").read_text())["change_ids"] == ["CH-1"]


def test_materialize_slices_writes_multiline_agent_companions_without_changing_canonical_bytes(
    tmp_path: Path,
) -> None:
    bundle = _materialize(tmp_path)

    for domain, slice_ in (
        ("issue", bundle.issue),
        ("workflow", bundle.workflow),
        ("eval", bundle.eval),
    ):
        canonical_path = tmp_path / f"qa/retro/retro-1/evidence/{domain}-slice.json"
        agent_path = tmp_path / f"qa/retro/retro-1/evidence/agent/{domain}-slice.json"

        assert canonical_path.read_bytes() == canonical_json_bytes(slice_)
        agent_text = agent_path.read_text(encoding="utf-8")
        assert agent_text.endswith("\n")
        assert len(agent_text.splitlines()) > 10
        assert json.loads(agent_text) == json.loads(canonical_path.read_text(encoding="utf-8"))


def test_materialize_slices_is_idempotent_but_rejects_conflicting_rewrite(tmp_path: Path) -> None:
    first = _materialize(tmp_path)
    assert _materialize(tmp_path) == first

    path = tmp_path / "qa/retro/retro-1/evidence/eval-slice.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RetroSliceImmutableError):
        _materialize(tmp_path)


def test_batch_member_gaps_become_stable_resolvable_deterministic_signals(
    tmp_path: Path,
) -> None:
    scope = RetroBatchScope.model_validate(
        {
            "batch_id": "batch-1",
            "status": "complete",
            "members": [
                {
                    "change_id": "CH-1",
                    "execution_status": "completed",
                    "evidence_availability": "complete",
                },
                {
                    "change_id": "CH-2",
                    "execution_status": "failed",
                    "evidence_availability": "complete",
                },
            ],
        }
    )
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-batch",
        selection=RetroWindowSelection(change_ids=("CH-1", "CH-2"), batch_scope=scope),
        issue_history=cast(IssueHistoryReader, _GapIssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _GapWorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        write_root=tmp_path,
    )

    issue_gap = bundle.issue.deterministic_signals[0]
    workflow_gap = bundle.workflow.deterministic_signals[0]
    assert isinstance(issue_gap, BatchMemberEvidenceGapSignal)
    assert isinstance(workflow_gap, BatchMemberEvidenceGapSignal)
    assert issue_gap.signal_type == "batch_member_evidence_gap"
    assert issue_gap.reason_code == "workspace_missing"
    assert workflow_gap.reason_code == "ledger_corrupt"
    assert issue_gap.signal_id in bundle.issue.resolvable_ids()
    assert workflow_gap.signal_id in bundle.workflow.resolvable_ids()


def test_unrecovered_contract_failure_becomes_one_deterministic_root_signal(
    tmp_path: Path,
) -> None:
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-hard-failure",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _HardFailureWorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        write_root=tmp_path,
    )

    assert len(bundle.workflow.deterministic_signals) == 1
    signal = bundle.workflow.deterministic_signals[0]
    assert isinstance(signal, TaskFailureSignal)
    assert signal.node_id == "analyze-issues"
    assert signal.error_kind == "forbidden_write"
    assert signal.occurrence_count == 1
    assert signal.source_refs.workflow_evidence_ids == ("CH-1#seq230",)
    assert set(signal.source_refs.workflow_evidence_ids) <= bundle.workflow.resolvable_ids()
