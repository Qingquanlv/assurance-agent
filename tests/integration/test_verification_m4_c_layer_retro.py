"""M4 Task 5 — multi-window C-layer + retro acceptance (final).

Composes landed C1/C2/C3 feedstock, report-only ``metrics-c-layer.json``, and
retro discovery/coverage_gap slices without inventing parallel ledgers.

Scenarios:
A. Four C-layer vectors evaluated from a multi-aspect fixture + materialize path.
B. C-layer artifact never moves quality-gate / metrics-sufficiency verdicts.
C. Retro discovery + coverage_gap slices/signals; corrupt isolation; fallback.
D. C2 dual perspective — promotion rate ≠ gap-closure rate (separate fields).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.artifacts.models.c_layer import (
    C_LAYER_METRICS_REL,
    C_LAYER_VECTOR_KEYS,
    CLayerMetricsDocument,
)
from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.improvements import ImprovementKind
from assurance_agent.artifacts.models.issues import Problem, ProblemProjection
from assurance_agent.artifacts.models.metrics import METRIC_KEYS, MetricEntry, MetricScope, MetricsDocument
from assurance_agent.artifacts.models.promotion import (
    PromotionReceipt,
    RegressionCandidate,
    WriteSetEntry,
)
from assurance_agent.artifacts.models.retro_v3 import (
    ConfirmedEscapeSignal,
    DomainEvidenceGapSignal,
    LowPromotionRateSignal,
    LowReplayStabilitySignal,
    ReopenedCoverageGapSignal,
    SignalDocumentV3,
    ImprovementCandidateDocumentV3,
)
from assurance_agent.artifacts.policy import load_policy_bytes
from assurance_agent.evidence.c_layer_metrics import aggregate_c_layer_metrics
from assurance_agent.evidence.coverage_gaps import diff_coverage_gaps, gap_identity
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.retro.discovery_history import (
    CompactCoverageGapRecord,
    CompactDiscoveryRecord,
    InMemoryCoverageGapHistoryReader,
    InMemoryDiscoveryHistoryReader,
)
from assurance_agent.retro.candidates import context_sha256, validate_candidate_document
from assurance_agent.retro.eval_history import EvalHistoryReader
from assurance_agent.retro.fallback import (
    candidate_fingerprint,
    candidate_from_coverage_gaps,
)
from assurance_agent.retro.slices import materialize_slices
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.retro.workflow_history import TerminalChangeRef, WorkflowHistoryReader
from assurance_agent.workflow.discovery.replay_receipts import write_replay_attempt_receipts
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.issues.history import IssueHistoryReader
from assurance_agent.workflow.metrics.c_layer import (
    load_c_layer_metrics,
    materialize_c_layer_metrics,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.unit.artifacts.test_models_issues import make_problem

CHANGE_ID = "CH-M4-ACCEPT"
CHANGE_ID_B = "CH-M4-ACCEPT-B"
CAMPAIGN_ID = "CAM-M4-ACCEPT"
CANDIDATE_ID = "RC-M4-001"
CE_PROMOTED = "CE-M4-PROMO"
PREV_GAPS_REL = "inspect/coverage-gaps.prev.json"
COMPUTED_AT = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
TOTAL_COUNTEREXAMPLES = 4

# Expected rates for the multi-aspect fixture (Scenario A / D).
EXPECTED_ESCAPE = (1, 2, 0.5)  # human_confirmed only; llm_provisional ignored
EXPECTED_PROMO = (1, TOTAL_COUNTEREXAMPLES, 0.25)
EXPECTED_GAP_CLOSURE = (1, 2, 0.5)  # CASE-CLOSED closed; CASE-KEEP remains
EXPECTED_REPLAY = (2, 3, 2 / 3)


# ---------------------------------------------------------------------------
# Core readers (issue / workflow / eval) — complete, no pollution
# ---------------------------------------------------------------------------


class _WorkflowReader:
    def list_terminal_changes(self, change_ids=None, *, tolerate_member_errors=False):
        del tolerate_member_errors
        refs = (
            TerminalChangeRef(change_id=CHANGE_ID, terminal_ts="2026-07-01T00:00:00Z"),
            TerminalChangeRef(change_id=CHANGE_ID_B, terminal_ts="2026-07-02T00:00:00Z"),
        )
        if change_ids is None:
            return refs
        return tuple(ref for ref in refs if ref.change_id in change_ids)

    def discover_change_ids(self):
        return frozenset({CHANGE_ID, CHANGE_ID_B})

    def read_window(self, window):
        return NS(
            integrity=RetroIntegrity(status="complete"),
            sources=tuple(
                RetroSourceDescriptor(
                    kind="workflow_ledger",
                    change_id=cid,
                    sha256=f"sha256:wf-{cid}",
                    evidence_ids=(f"{cid}#seq1",),
                )
                for cid in window.change_ids
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
            sources=tuple(
                NS(
                    kind="change_issue_ledger",
                    change_id=cid,
                    head_event_id=f"change-event-{cid}",
                    sha256=f"sha256:issue-{cid}",
                )
                for cid in selection.change_ids
            ),
            observations=(),
            occurrences=(),
            problem_snapshots=(),
            problem_events=(),
        )


class _EvalReader:
    def read_window(self, window):
        return NS(reports=(), integrity=RetroIntegrity(status="complete"), sources=())


# ---------------------------------------------------------------------------
# Fixture builders — Scenario A feedstock
# ---------------------------------------------------------------------------


def _problem_escape_confirmed() -> Problem:
    return Problem.model_validate(
        make_problem(
            problem_id="PROB-ESC-M4",
            escape_analysis={
                "is_escape": True,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "qa",
            },
        )
    )


def _problem_analyzed_non_escape() -> Problem:
    return Problem.model_validate(
        make_problem(
            problem_id="PROB-OK-M4",
            escape_analysis={
                "is_escape": False,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "qa",
            },
        )
    )


def _problem_llm_provisional() -> Problem:
    return Problem.model_validate(
        make_problem(
            problem_id="PROB-DRAFT-M4",
            escape_analysis={"is_escape": True, "authority": "llm_provisional"},
        )
    )


def _gap(case_id: str, *, batch_id: str) -> CoverageGap:
    return CoverageGap(
        kind="uncovered_required_case",
        locator=CoverageGapLocator(case_id=case_id),
        layer="execution",
        batch_id=batch_id,
        evidence_refs=(f"digest-{case_id}",),
    )


def _gaps_previous() -> CoverageGapsDocument:
    # CASE-CLOSED will close; CASE-KEEP remains. CASE-REOPEN is historically closed
    # and reappears only in current (retro reopen signal; not in previous).
    return CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="batch-prev",
        projection_digest="sha256:" + ("1" * 64),
        gaps=(
            _gap("CASE-CLOSED", batch_id="batch-prev"),
            _gap("CASE-KEEP", batch_id="batch-prev"),
        ),
    )


def _gaps_current() -> CoverageGapsDocument:
    return CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id="batch-curr",
        projection_digest="sha256:" + ("2" * 64),
        gaps=(
            _gap("CASE-KEEP", batch_id="batch-curr"),
            _gap("CASE-REOPEN", batch_id="batch-curr"),
        ),
    )


def _candidate() -> RegressionCandidate:
    return RegressionCandidate(
        schema_version="1",
        candidate_id=CANDIDATE_ID,
        change_id=CHANGE_ID,
        campaign_id=CAMPAIGN_ID,
        counterexample_id=CE_PROMOTED,
        oracle_id="ORACLE-M4",
        surface="api",
        proposed_targets=("tests/api/test_m4_promo.py",),
        source_files={f"discovery/candidates/{CANDIDATE_ID}/files/test_m4_promo.py": "sha256:" + ("b" * 64)},
        minimization_status="minimized",
        purpose="Stable regression for M4 acceptance",
    )


def _receipt() -> PromotionReceipt:
    return PromotionReceipt(
        schema_version="1",
        receipt_id="PROM-RCPT-M4-001",
        improvement_id="IMP-M4-001",
        candidate_id=CANDIDATE_ID,
        applied_at="2026-08-05T06:00:00Z",
        write_set=(
            WriteSetEntry(
                path="tests/api/test_m4_promo.py",
                before_sha256=None,
                after_sha256="sha256:" + ("e" * 64),
            ),
        ),
        write_authorization=("tests/api/test_m4_promo.py",),
        status="applied",
    )


def _replay_receipts() -> tuple[ReplayAttemptReceipt, ...]:
    return (
        ReplayAttemptReceipt(
            schema_version="1",
            counterexample_id=CE_PROMOTED,
            seed=1,
            attempt_index=0,
            base_revision="deadbeef",
            oracle_set_digest="sha256:" + ("a" * 64),
            outcome="violate",
            observed_digest="sha256:" + ("b" * 64),
        ),
        ReplayAttemptReceipt(
            schema_version="1",
            counterexample_id=CE_PROMOTED,
            seed=1,
            attempt_index=1,
            base_revision="deadbeef",
            oracle_set_digest="sha256:" + ("a" * 64),
            outcome="hold",
            observed_digest="sha256:" + ("c" * 64),
        ),
        ReplayAttemptReceipt(
            schema_version="1",
            counterexample_id=CE_PROMOTED,
            seed=1,
            attempt_index=2,
            base_revision="deadbeef",
            oracle_set_digest="sha256:" + ("a" * 64),
            outcome="violate",
            observed_digest="sha256:" + ("d" * 64),
        ),
    )


def _plant_disk_fixture(project_root: Path) -> Path:
    """Plant multi-aspect feedstock under change_dir + project problems ledger."""
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "inspect").mkdir(parents=True)

    problems = ProblemProjection(
        schema_version="1.0",
        generated_at="2026-08-05T12:00:00Z",
        problems=[
            _problem_escape_confirmed(),
            _problem_analyzed_non_escape(),
            _problem_llm_provisional(),
        ],
    )
    problems_path = project_root / "qa" / "issues" / "problems.json"
    problems_path.parent.mkdir(parents=True, exist_ok=True)
    problems_path.write_text(problems.model_dump_json(indent=2) + "\n", encoding="utf-8")

    ce_root = change_dir / "discovery" / "counterexamples"
    ce_root.mkdir(parents=True)
    for i in range(TOTAL_COUNTEREXAMPLES):
        ce_id = CE_PROMOTED if i == 0 else f"CE-M4-EXTRA-{i}"
        (ce_root / f"{ce_id}.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1",
                    "counterexample_id": ce_id,
                    "campaign_id": CAMPAIGN_ID,
                    "round_id": "R1",
                    "surface": "api",
                    "technique": "boundary",
                    "obligation_ids": [],
                    "oracle_id": "ORACLE-M4",
                    "oracle_kind": "hard_oracle",
                    "environment_digest": "env-m4",
                    "generated_file_digests": {},
                    "setup": {},
                    "actions": [],
                    "observed": {},
                    "expected": {},
                    "seed": 7,
                    "minimization": {
                        "status": "minimized",
                        "parent_counterexample_id": None,
                    },
                    "replay": {"attempts": 1, "reproduced": 1, "artifact_refs": []},
                    "finding_status": "confirmed",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    cand_dir = change_dir / "discovery" / "candidates" / CANDIDATE_ID
    cand_dir.mkdir(parents=True)
    (cand_dir / "candidate.yaml").write_text(
        yaml.safe_dump(_candidate().model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    (cand_dir / "promotion-receipt.json").write_bytes(canonical_json_bytes(_receipt()))

    (change_dir / PREV_GAPS_REL).write_bytes(canonical_json_bytes(_gaps_previous()))
    (change_dir / COVERAGE_GAPS_REL).write_bytes(canonical_json_bytes(_gaps_current()))

    write_replay_attempt_receipts(change_dir, _replay_receipts())
    return change_dir


def _assert_four_vectors(doc: CLayerMetricsDocument) -> None:
    esc_n, esc_d, esc_r = EXPECTED_ESCAPE
    assert doc.escape_rate.status == "evaluated"
    assert doc.escape_rate.numerator == esc_n
    assert doc.escape_rate.denominator == esc_d
    assert doc.escape_rate.rate == pytest.approx(esc_r)

    promo_n, promo_d, promo_r = EXPECTED_PROMO
    assert doc.counterexample_promotion_rate.status == "evaluated"
    assert doc.counterexample_promotion_rate.numerator == promo_n
    assert doc.counterexample_promotion_rate.denominator == promo_d
    assert doc.counterexample_promotion_rate.rate == pytest.approx(promo_r)

    gap_n, gap_d, gap_r = EXPECTED_GAP_CLOSURE
    assert doc.coverage_gap_closure_rate.status == "evaluated"
    assert doc.coverage_gap_closure_rate.numerator == gap_n
    assert doc.coverage_gap_closure_rate.denominator == gap_d
    assert doc.coverage_gap_closure_rate.rate == pytest.approx(gap_r)

    rep_n, rep_d, rep_r = EXPECTED_REPLAY
    assert doc.seed_replay_stability.status == "evaluated"
    assert doc.seed_replay_stability.numerator == rep_n
    assert doc.seed_replay_stability.denominator == rep_d
    assert doc.seed_replay_stability.rate == pytest.approx(rep_r)

    # historically_closed reopen is orthogonal to C-layer closure tally
    reopen_id = gap_identity(_gap("CASE-REOPEN", batch_id="batch-curr"))
    diff = diff_coverage_gaps(
        _gaps_previous(),
        _gaps_current(),
        historically_closed=(reopen_id,),
    )
    assert len(diff.closed) == 1
    assert reopen_id in diff.reopened


def _metrics_doc() -> MetricsDocument:
    return MetricsDocument.model_validate(
        {
            "schema_version": "2",
            "change_id": CHANGE_ID,
            "cadence": "pr",
            "computed_at": COMPUTED_AT,
            "risk_tier": "low",
            "risk_tier_lower_bound": "low",
            "risk_tier_declared": None,
            "risk_declaration_lowered": False,
            "risk_lowered_declarations": (),
            "metrics": {
                "constraint_coverage": MetricEntry(
                    layer="api",
                    status="evaluated",
                    value=0.9,
                    declared=MetricScope.of(total=10, covered=9),
                    evidence="constraint-coverage.json",
                )
            },
            "policy_digest": "0" * 64,
        }
    )


def _api_result() -> TargetResult:
    return TargetResult(
        change_id=CHANGE_ID,
        batch_id="b1",
        target="api",
        status="passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/x.log"),
        total=1,
        passed=1,
        failed=0,
        skipped=0,
        cases=[
            CaseResult(
                case_id="TC_API_001",
                status="passed",
                file="f.py",
                test_name="t",
                duration_ms=1,
                message="",
            )
        ],
        unmapped_tests=[],
    )


def _coverage_result() -> CoverageResult:
    return CoverageResult(
        change_id=CHANGE_ID,
        batch_id="b1",
        available=True,
        line_coverage=85.0,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )


def _discovery_record(**overrides: object) -> CompactDiscoveryRecord:
    payload: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "campaign_id": CAMPAIGN_ID,
        "counterexample_ids": (CE_PROMOTED, "CE-M4-EXTRA-1"),
        "promotion_receipt_digests": ("sha256:promo-m4",),
        "replay_success": 2,
        "replay_attempts": 3,
        "replay_rate": 2 / 3,
        "problem_escape_refs": ("PROB-ESC-M4",),
        "promoted_count": 1,
        "total_counterexamples": TOTAL_COUNTEREXAMPLES,
        "sha256": "sha256:discovery-m4",
    }
    payload.update(overrides)
    return CompactDiscoveryRecord.model_validate(payload)


def _gap_record(**overrides: object) -> CompactCoverageGapRecord:
    payload: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "batch_id": "batch-curr",
        "projection_digest": "sha256:proj-m4",
        "document_digest": "sha256:gaps-m4",
        "gap_identities": (
            {
                "kind": "uncovered_required_case",
                "case_id": "CASE-KEEP",
                "constraint_key": "",
                "cell": "",
                "cluster_key": "",
            },
            {
                "kind": "uncovered_required_case",
                "case_id": "CASE-REOPEN",
                "constraint_key": "",
                "cell": "",
                "cluster_key": "",
            },
        ),
        "sha256": "sha256:gap-m4",
    }
    payload.update(overrides)
    return CompactCoverageGapRecord.model_validate(payload)


# ---------------------------------------------------------------------------
# Scenario A — C-layer four vectors
# ---------------------------------------------------------------------------


def test_scenario_a_c_layer_four_vectors_and_materialize(tmp_path: Path) -> None:
    project_root = tmp_path / "product"
    project_root.mkdir()
    change_dir = _plant_disk_fixture(project_root)

    problems = [
        _problem_escape_confirmed(),
        _problem_analyzed_non_escape(),
        _problem_llm_provisional(),
    ]
    in_memory = aggregate_c_layer_metrics(
        problems=problems,
        promotion_receipts=[_receipt()],
        candidates=[_candidate()],
        total_counterexamples=TOTAL_COUNTEREXAMPLES,
        previous_gaps=_gaps_previous(),
        current_gaps=_gaps_current(),
        replay_receipts=_replay_receipts(),
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    _assert_four_vectors(in_memory)

    written = materialize_c_layer_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        previous_gaps_rel=PREV_GAPS_REL,
    )
    assert (change_dir / C_LAYER_METRICS_REL).is_file()
    assert C_LAYER_METRICS_REL == "inspect/metrics-c-layer.json"
    loaded = load_c_layer_metrics(change_dir)
    assert loaded is not None
    _assert_four_vectors(written)
    _assert_four_vectors(loaded)
    assert loaded.cadence == "report"
    assert loaded.change_id == CHANGE_ID


# ---------------------------------------------------------------------------
# Scenario B — verdict isolation
# ---------------------------------------------------------------------------


def test_scenario_b_c_layer_never_changes_execution_verdict(tmp_path: Path) -> None:
    project_root = tmp_path / "product"
    project_root.mkdir()
    change_dir = _plant_disk_fixture(project_root)

    policy = load_policy_bytes(None, origin="packaged")
    metrics = _metrics_doc()

    before_sufficiency = evaluate_metrics_sufficiency(metrics, policy.evidence_sufficiency)
    before_gate = build_quality_gate(
        change_id=CHANGE_ID,
        batch_id="b1",
        api=_api_result(),
        e2e=None,
        coverage=_coverage_result(),
        coverage_gate_mode="warn",
    )
    before_gate_dump = before_gate.model_dump(mode="json")

    materialize_c_layer_metrics(
        change_dir=change_dir,
        project_root=project_root,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
        previous_gaps_rel=PREV_GAPS_REL,
    )
    assert (change_dir / C_LAYER_METRICS_REL).is_file()

    after_sufficiency = evaluate_metrics_sufficiency(metrics, policy.evidence_sufficiency)
    after_gate = build_quality_gate(
        change_id=CHANGE_ID,
        batch_id="b1",
        api=_api_result(),
        e2e=None,
        coverage=_coverage_result(),
        coverage_gate_mode="warn",
    )

    assert before_sufficiency.verdict == after_sufficiency.verdict
    assert before_sufficiency == after_sufficiency
    assert before_gate.final_status == after_gate.final_status
    assert before_gate_dump == after_gate.model_dump(mode="json")
    assert "metrics-c-layer" not in after_gate.model_dump_json()
    assert not hasattr(after_gate.dimensions, "c_layer")

    # C-layer keys not in METRIC_KEYS and not in packaged floors
    for key in C_LAYER_VECTOR_KEYS:
        assert key not in METRIC_KEYS
    for band in policy.evidence_sufficiency.floors.values():
        assert set(band).issubset(set(METRIC_KEYS))
        for key in C_LAYER_VECTOR_KEYS:
            assert key not in band


# ---------------------------------------------------------------------------
# Scenario C — Retro discovery / coverage_gap
# ---------------------------------------------------------------------------


def test_scenario_c_retro_slices_signals_fallback_and_isolation(tmp_path: Path) -> None:
    discovery = InMemoryDiscoveryHistoryReader(
        (
            _discovery_record(),
            _discovery_record(
                change_id=CHANGE_ID_B,
                campaign_id="CAM-M4-B",
                problem_escape_refs=(),
                promoted_count=0,
                total_counterexamples=2,
                replay_success=2,
                replay_attempts=2,
                replay_rate=1.0,
                sha256="sha256:discovery-m4-b",
            ),
        )
    )
    reopen_identity = ("uncovered_required_case", "CASE-REOPEN", "", "", "")
    coverage = InMemoryCoverageGapHistoryReader(
        current_records=(
            _gap_record(),
            _gap_record(
                change_id=CHANGE_ID_B,
                batch_id="batch-b",
                gap_identities=(),
                document_digest="sha256:gaps-b",
                sha256="sha256:gap-b",
            ),
        ),
        previous_by_change_id={
            CHANGE_ID: _gap_record(
                batch_id="batch-prev",
                document_digest="sha256:gaps-prev",
                gap_identities=(
                    {
                        "kind": "uncovered_required_case",
                        "case_id": "CASE-CLOSED",
                        "constraint_key": "",
                        "cell": "",
                        "cluster_key": "",
                    },
                    {
                        "kind": "uncovered_required_case",
                        "case_id": "CASE-KEEP",
                        "constraint_key": "",
                        "cell": "",
                        "cluster_key": "",
                    },
                ),
                sha256="sha256:gap-prev",
            ),
            CHANGE_ID_B: _gap_record(
                change_id=CHANGE_ID_B,
                batch_id="batch-b-prev",
                gap_identities=(),
                document_digest="sha256:gaps-b-prev",
                sha256="sha256:gap-b-prev",
            ),
        },
        historically_closed_by_change_id={CHANGE_ID: (reopen_identity,)},
    )

    selection = RetroWindowSelection(change_ids=(CHANGE_ID, CHANGE_ID_B), last=None)
    bundle = materialize_slices(
        tmp_path,
        retro_id="retro-m4-accept",
        selection=selection,
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=discovery,
        coverage_gap_history=coverage,
        write_root=tmp_path,
    )

    assert bundle.issue.integrity.status == "complete"
    assert bundle.workflow.integrity.status == "complete"
    assert bundle.eval.integrity.status == "complete"
    assert bundle.discovery is not None
    assert bundle.coverage_gap is not None
    assert bundle.discovery.integrity.status == "complete"
    assert bundle.coverage_gap.integrity.status == "complete"

    retro = tmp_path / "qa/retro/retro-m4-accept"
    assert (retro / "evidence/discovery-slice.json").is_file()
    assert (retro / "evidence/coverage_gap-slice.json").is_file()
    assert (retro / "signals/discovery.json").is_file()
    assert (retro / "signals/coverage_gap.json").is_file()
    assert (retro / "window.json").is_file()

    # materialize_slices writes deterministic optional-domain signal docs; pin
    # agent signal docs for issue/workflow/eval so assemble can dry-run all five.
    from assurance_agent.retro.assemble import assemble_context

    for domain in ("issue", "workflow", "eval"):
        slice_path = retro / "evidence" / f"{domain}-slice.json"
        slice_bytes = slice_path.read_bytes()
        doc = SignalDocumentV3(
            retro_id="retro-m4-accept",
            domain=domain,  # type: ignore[arg-type]
            analysis_status="ok",
            analyzer=f"aa-retro-{domain}-analysis",
            signals=(),
            slice_sha256=sha256_bytes(slice_bytes),
        )
        (retro / "signals" / f"{domain}.json").write_bytes(canonical_json_bytes(doc))

    ctx = assemble_context(retro, dry_run=True, now=COMPUTED_AT)
    assert ctx.dry_run is True
    assert ctx.integrity.status == "complete"
    assert ctx.domain_status.discovery is not None
    assert ctx.domain_status.discovery.status == "ok"
    assert ctx.domain_status.coverage_gap is not None
    assert ctx.domain_status.coverage_gap.status == "ok"
    assert ctx.signals.discovery == bundle.discovery.deterministic_signals
    assert ctx.signals.coverage_gap == bundle.coverage_gap.deterministic_signals

    disc_types = {s.signal_type for s in bundle.discovery.deterministic_signals}
    assert "confirmed_escape" in disc_types
    assert "low_promotion_rate" in disc_types  # 1/4 < 0.5
    assert "low_replay_stability" in disc_types  # 2/3 < 1.0
    escape = next(s for s in bundle.discovery.deterministic_signals if isinstance(s, ConfirmedEscapeSignal))
    assert escape.problem_id == "PROB-ESC-M4"
    promo = next(s for s in bundle.discovery.deterministic_signals if isinstance(s, LowPromotionRateSignal))
    assert promo.numerator == 1
    assert promo.denominator == TOTAL_COUNTEREXAMPLES
    replay = next(
        s for s in bundle.discovery.deterministic_signals if isinstance(s, LowReplayStabilitySignal)
    )
    assert replay.attempts == 3

    reopened = [
        s for s in bundle.coverage_gap.deterministic_signals if isinstance(s, ReopenedCoverageGapSignal)
    ]
    assert len(reopened) == 1
    assert reopened[0].case_id == "CASE-REOPEN"

    coverage_candidate = candidate_from_coverage_gaps(
        signals=(reopened[0],),
        retro_id=ctx.retro_id,
    )
    validate_candidate_document(
        ctx,
        ImprovementCandidateDocumentV3(
            retro_id=ctx.retro_id,
            context_sha256=context_sha256(ctx),
            candidates=(coverage_candidate,),
        ),
    )

    # Corrupt discovery → issue slice still complete; discovery domain gap only
    corrupt = InMemoryDiscoveryHistoryReader(
        records=(_discovery_record(),),
        corrupt_change_ids=frozenset({CHANGE_ID}),
    )
    iso = materialize_slices(
        tmp_path / "iso",
        retro_id="retro-m4-iso",
        selection=RetroWindowSelection(change_ids=(CHANGE_ID,), last=None),
        issue_history=cast(IssueHistoryReader, _IssueReader()),
        workflow_history=cast(WorkflowHistoryReader, _WorkflowReader()),
        eval_history=cast(EvalHistoryReader, _EvalReader()),
        discovery_history=corrupt,
        write_root=tmp_path / "iso",
    )
    assert iso.issue.integrity.status == "complete"
    assert iso.workflow.integrity.status == "complete"
    assert iso.eval.integrity.status == "complete"
    assert iso.discovery is not None
    assert iso.discovery.integrity.status == "incomplete"
    gap_signals = [s for s in iso.discovery.deterministic_signals if isinstance(s, DomainEvidenceGapSignal)]
    assert gap_signals
    assert gap_signals[0].domain == "discovery"

    # Fallback: coverage gap → test_improvement; fingerprint stable across two calls
    first = candidate_from_coverage_gaps(signals=(reopened[0],), retro_id="retro-a")
    second = candidate_from_coverage_gaps(signals=(reopened[0],), retro_id="retro-b")
    assert first.kind == ImprovementKind.TEST
    assert first.kind.value == "test_improvement"
    assert candidate_fingerprint(first) == candidate_fingerprint(second)
    assert first.candidate_id == second.candidate_id


# ---------------------------------------------------------------------------
# Scenario D — C2 dual perspective
# ---------------------------------------------------------------------------


def test_scenario_d_c2_dual_perspective_not_merged() -> None:
    doc = aggregate_c_layer_metrics(
        problems=[
            _problem_escape_confirmed(),
            _problem_analyzed_non_escape(),
            _problem_llm_provisional(),
        ],
        promotion_receipts=[_receipt()],
        candidates=[_candidate()],
        total_counterexamples=TOTAL_COUNTEREXAMPLES,
        previous_gaps=_gaps_previous(),
        current_gaps=_gaps_current(),
        replay_receipts=_replay_receipts(),
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    promo = doc.counterexample_promotion_rate
    gaps = doc.coverage_gap_closure_rate
    assert promo.rate == pytest.approx(0.25)
    assert gaps.rate == pytest.approx(0.5)
    assert promo.rate != gaps.rate

    dumped = doc.model_dump(mode="json")
    assert "c2_rate" not in dumped
    assert "merged_c2" not in dumped
    # Never multiplied / averaged into one field
    assert promo.rate is not None and gaps.rate is not None
    product = promo.rate * gaps.rate
    average = (promo.rate + gaps.rate) / 2
    for _key, entry in dumped.items():
        if isinstance(entry, dict) and entry.get("rate") is not None:
            assert abs(entry["rate"] - product) > 1e-9
            assert abs(entry["rate"] - average) > 1e-9
    assert dumped["counterexample_promotion_rate"]["rate"] != dumped["coverage_gap_closure_rate"]["rate"]
