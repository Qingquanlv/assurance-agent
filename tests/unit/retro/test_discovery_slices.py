"""Materialize discovery/coverage_gap slices — isolation from issue/workflow/eval."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import cast

import pytest

from assurance_agent.artifacts.models.retro_v3 import (
    ConfirmedEscapeSignal,
    DomainEvidenceGapSignal,
    LowPromotionRateSignal,
    LowReplayStabilitySignal,
    ReopenedCoverageGapSignal,
)
from assurance_agent.retro.discovery_history import (
    CompactCoverageGapRecord,
    CompactDiscoveryRecord,
    InMemoryCoverageGapHistoryReader,
    InMemoryDiscoveryHistoryReader,
)
from assurance_agent.retro.eval_history import EvalHistoryReader
from assurance_agent.retro.slices import materialize_slices
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
                    evidence_ids=("CH-1#seq1",),
                ),
            ),
            gate_verdicts=(),
            task_failures=(),
            healing_allocations=(),
            healing_applies=(),
            skill_loaded_false=(),
        )


class _IssueReader:
    def read_window(self, selection):
        return NS(
            integrity=RetroIntegrity(status="complete"),
            sources=(
                NS(
                    kind="change_issue_ledger",
                    change_id="CH-1",
                    head_event_id="change-event-1",
                    sha256="sha256:issue",
                ),
            ),
            observations=(),
            occurrences=(),
            problem_snapshots=(),
            problem_events=(),
        )


class _EvalReader:
    def read_window(self, window):
        return NS(reports=(), integrity=RetroIntegrity(status="complete"), sources=())


def _discovery_record(**overrides: object) -> CompactDiscoveryRecord:
    payload = {
        "change_id": "CH-1",
        "campaign_id": "camp-1",
        "counterexample_ids": ("CE-1",),
        "promotion_receipt_digests": ("sha256:promo-1",),
        "replay_success": 0,
        "replay_attempts": 2,
        "replay_rate": 0.0,
        "problem_escape_refs": ("PROB-ESC-1",),
        "promoted_count": 0,
        "total_counterexamples": 2,
        "sha256": "sha256:discovery-ch1",
    }
    payload.update(overrides)
    return CompactDiscoveryRecord.model_validate(payload)


def _gap_record(**overrides: object) -> CompactCoverageGapRecord:
    payload = {
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "projection_digest": "sha256:proj-1",
        "document_digest": "sha256:gaps-1",
        "gap_identities": (
            {
                "kind": "uncovered_required_case",
                "case_id": "CASE-1",
                "constraint_key": "",
                "cell": "",
                "cluster_key": "",
            },
        ),
        "sha256": "sha256:gap-ch1",
    }
    payload.update(overrides)
    return CompactCoverageGapRecord.model_validate(payload)


def test_materialize_optional_discovery_and_coverage_gap_slices(tmp_path: Path) -> None:
    discovery = InMemoryDiscoveryHistoryReader((_discovery_record(),))
    coverage = InMemoryCoverageGapHistoryReader(
        current_records=(_gap_record(),),
        previous_by_change_id={
            "CH-1": _gap_record(
                batch_id="batch-0",
                document_digest="sha256:gaps-0",
                gap_identities=(),
                sha256="sha256:gap-prev",
            )
        },
        historically_closed_by_change_id={
            "CH-1": (("uncovered_required_case", "CASE-1", "", "", ""),),
        },
    )
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-m4",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=discovery,
        coverage_gap_history=coverage,
        write_root=tmp_path,
    )
    assert bundle.discovery is not None
    assert bundle.coverage_gap is not None
    retro = tmp_path / "qa/retro/retro-m4"
    assert (retro / "evidence/discovery-slice.json").is_file()
    assert (retro / "evidence/coverage_gap-slice.json").is_file()
    assert (retro / "signals/discovery.json").is_file()
    assert (retro / "signals/coverage_gap.json").is_file()
    # Core three domains still written
    assert (retro / "evidence/issue-slice.json").is_file()
    assert bundle.issue.integrity.status == "complete"


def test_corrupt_discovery_does_not_pollute_issue_slice(tmp_path: Path) -> None:
    discovery = InMemoryDiscoveryHistoryReader(
        records=(_discovery_record(),),
        corrupt_change_ids=frozenset({"CH-1"}),
    )
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-iso",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=discovery,
        write_root=tmp_path,
    )
    assert bundle.issue.integrity.status == "complete"
    assert bundle.workflow.integrity.status == "complete"
    assert bundle.eval.integrity.status == "complete"
    assert bundle.discovery is not None
    assert bundle.discovery.integrity.status == "incomplete"
    gap_signals = [
        s for s in bundle.discovery.deterministic_signals if isinstance(s, DomainEvidenceGapSignal)
    ]
    assert gap_signals
    assert gap_signals[0].domain == "discovery"


def test_discovery_deterministic_signals_escape_promotion_replay(tmp_path: Path) -> None:
    discovery = InMemoryDiscoveryHistoryReader((_discovery_record(),))
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-signals",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=discovery,
        write_root=tmp_path,
    )
    assert bundle.discovery is not None
    types = {s.signal_type for s in bundle.discovery.deterministic_signals}
    assert "confirmed_escape" in types
    assert "low_promotion_rate" in types
    assert "low_replay_stability" in types
    escape = next(s for s in bundle.discovery.deterministic_signals if isinstance(s, ConfirmedEscapeSignal))
    assert escape.problem_id == "PROB-ESC-1"
    promo = next(s for s in bundle.discovery.deterministic_signals if isinstance(s, LowPromotionRateSignal))
    assert promo.rate == pytest.approx(0.0)
    assert promo.denominator == 2
    replay = next(
        s for s in bundle.discovery.deterministic_signals if isinstance(s, LowReplayStabilitySignal)
    )
    assert replay.rate == pytest.approx(0.0)
    assert replay.attempts == 2


def test_missing_discovery_data_emits_gap_not_fake_rates(tmp_path: Path) -> None:
    # Missing projection → domain gap only; never invent vacuous rates.
    empty = InMemoryDiscoveryHistoryReader(records=(), missing_change_ids=frozenset({"CH-1"}))
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-gap",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=empty,
        write_root=tmp_path,
    )
    assert bundle.discovery is not None
    rate_types = {
        s.signal_type
        for s in bundle.discovery.deterministic_signals
        if s.signal_type in {"low_promotion_rate", "low_replay_stability", "confirmed_escape"}
    }
    assert rate_types == set()
    assert any(isinstance(s, DomainEvidenceGapSignal) for s in bundle.discovery.deterministic_signals)


def test_coverage_gap_reopened_signal(tmp_path: Path) -> None:
    coverage = InMemoryCoverageGapHistoryReader(
        current_records=(_gap_record(),),
        previous_by_change_id={
            "CH-1": _gap_record(
                batch_id="batch-0",
                document_digest="sha256:gaps-0",
                gap_identities=(),
                sha256="sha256:gap-prev",
            )
        },
        historically_closed_by_change_id={
            "CH-1": (("uncovered_required_case", "CASE-1", "", "", ""),),
        },
    )
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-reopen",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        coverage_gap_history=coverage,
        write_root=tmp_path,
    )
    assert bundle.coverage_gap is not None
    reopened = [
        s for s in bundle.coverage_gap.deterministic_signals if isinstance(s, ReopenedCoverageGapSignal)
    ]
    assert len(reopened) == 1
    assert reopened[0].gap_kind == "uncovered_required_case"


def test_old_three_domain_materialize_unchanged_without_new_readers(tmp_path: Path) -> None:
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-legacy",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        write_root=tmp_path,
    )
    assert bundle.discovery is None
    assert bundle.coverage_gap is None
    retro = tmp_path / "qa/retro/retro-legacy"
    assert not (retro / "evidence/discovery-slice.json").exists()
    assert (retro / "evidence/issue-slice.json").is_file()
    # DomainStatuses optional fields default absent for old ledgers
    from assurance_agent.artifacts.models.retro_v3 import DomainAnalysisStatus, DomainStatuses

    statuses = DomainStatuses(
        issue=DomainAnalysisStatus(status="ok"),
        workflow=DomainAnalysisStatus(status="ok"),
        eval=DomainAnalysisStatus(status="ok"),
    )
    assert statuses.discovery is None
    assert statuses.coverage_gap is None


def test_signal_docs_for_new_domains_are_digest_pinned(tmp_path: Path) -> None:
    discovery = InMemoryDiscoveryHistoryReader((_discovery_record(problem_escape_refs=()),))
    materialize_slices(
        tmp_path,
        retro_id="retro-digest",
        selection=RetroWindowSelection(change_ids=("CH-1",), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=discovery,
        write_root=tmp_path,
    )
    retro = tmp_path / "qa/retro/retro-digest"
    slice_bytes = (retro / "evidence/discovery-slice.json").read_bytes()
    signal = json.loads((retro / "signals/discovery.json").read_text(encoding="utf-8"))
    assert signal["domain"] == "discovery"
    assert signal["analysis_status"] == "ok"
    assert signal["slice_sha256"].startswith("sha256:")
    from assurance_agent.artifacts.canonical import sha256_bytes

    assert signal["slice_sha256"] == sha256_bytes(slice_bytes)
