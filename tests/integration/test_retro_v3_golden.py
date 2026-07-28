"""Retro v3 golden acceptance over the benchmark User/Role evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from tests.unit.workflow.graph.test_retro_workflow import (
    EmptyAnalysisInvoker,
    _build_runtime,
    _compile_canonical,
    _make_project,
    _retro_context,
)

# Committed snapshot of the User/Role benchmark evidence. The live
# benchmark/vue-fastapi-admin/qa tree is gitignored, so CI cannot read it;
# this fixture is the only source the golden installer may copy from.
_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "retro_v3_golden"
_CHANGE_FIXTURE_ROOT = _FIXTURE_ROOT / "qa" / "changes"
_CHANGE_IDS = (
    "RET-user-management-20260727-094743-cursor",
    "RET-role-management-20260727-094743-cursor",
)
_UNRELATED_EVAL_RUN = "eval-20260727-f603ef81"

_ADAPTER_PROBLEM_IDS = (
    "PROB-c307f3a230230d36",
    "PROB-4e0c46489d5118a2",
    "PROB-a333965d1e2ae2b8",
    "PROB-03e7d95f9dee8592",
    "PROB-cb87a83a902a0e8b",
)
_ADAPTER_OCCURRENCE_IDS = (
    "OCC-06abd1057c9e33a7",
    "OCC-4700d3e8cade1655",
    "OCC-871ee964f0141e15",
    "OCC-a9b243fb0016047e",
    "OCC-b9a6270a3b5b931a",
    "OCC-d7062aeb31628aa8",
)
_ALIGNMENT_PROBLEM_ID = "PROB-c826e3d43cad08c8"
_ALIGNMENT_OCCURRENCE_ID = "OCC-f80ecfc08aac7538"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _seed_structured_archive_cause(events_path: Path) -> None:
    """Augment the pre-C6 benchmark run with the cause its frozen state proves.

    The User/Role evidence was captured before ``gate_report.details.cause``
    existed. Everything else remains byte-for-byte sourced from that run.
    """
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    seeded = 0
    for event in events:
        report = event.get("gate_report")
        if not isinstance(report, dict):
            continue
        if report.get("gate_id") == "archive-gate" and report.get("verdict") == "stop":
            report["details"] = {"cause": "archive.execution_failed"}
            seeded += 1
    assert seeded == 1
    events_path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )


def _install_benchmark_fixture(project: Path) -> None:
    _copy_file(
        _FIXTURE_ROOT / "qa" / "issues" / "events.jsonl",
        project / "qa" / "issues" / "events.jsonl",
    )
    for change_id in _CHANGE_IDS:
        source = _CHANGE_FIXTURE_ROOT / change_id
        target = project / "qa" / "changes" / change_id
        _copy_file(source / "events.jsonl", target / "events.jsonl")
        _copy_file(source / "issues" / "events.jsonl", target / "issues" / "events.jsonl")
        _seed_structured_archive_cause(target / "events.jsonl")

    # A real legacy report with no source_change_ids exercises the production
    # explicit-change filter; it must not enter the slice or degrade integrity.
    source_run = _FIXTURE_ROOT / "eval" / "out" / "runs" / _UNRELATED_EVAL_RUN
    target_run = project / "eval" / "out" / "runs" / _UNRELATED_EVAL_RUN
    _copy_file(source_run / "report.json", target_run / "report.json")
    # An explicit empty compact-projection catalog is complete; the unrelated
    # legacy raw report above must not be consulted as a substitute.
    compact_runs = project / "qa" / "eval" / "runs"
    compact_runs.mkdir(parents=True, exist_ok=True)
    (compact_runs / ".keep").write_text("", encoding="utf-8")


def _install_healthy_fixture(project: Path) -> None:
    change_id = "CH-HEALTHY"
    change = project / "qa" / "changes" / change_id
    (change / "issues").mkdir(parents=True, exist_ok=True)
    (change / "issues" / "events.jsonl").write_text("", encoding="utf-8")
    (project / "qa" / "issues").mkdir(parents=True, exist_ok=True)
    (project / "qa" / "issues" / "events.jsonl").write_text("", encoding="utf-8")
    eval_runs = project / "qa" / "eval" / "runs"
    eval_runs.mkdir(parents=True, exist_ok=True)
    # Tree manifests do not retain empty directories; a non-report marker keeps
    # the explicit empty Eval history visible to the production reader.
    (eval_runs / ".keep").write_text("", encoding="utf-8")
    events = [
        {
            "seq": 1,
            "ts": "2026-07-25T01:00:00Z",
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "inv-healthy",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "sha256:" + "a" * 64,
            "contract_digests": {},
            "params": {},
            "params_sha256": "sha256:" + "b" * 64,
            "root_tree_id": "tree-healthy",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "inv-healthy",
            "structural_path": "/",
        },
        {
            "seq": 2,
            "ts": "2026-07-25T02:00:00Z",
            "source": "graph",
            "type": "graph_completed",
            "invocation_id": "inv-healthy",
            "checkpoint_ns": "inv-healthy",
            "reason": "END reached",
        },
    ]
    change.mkdir(parents=True, exist_ok=True)
    change.joinpath("events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


class _GoldenAgent:
    def __init__(self, retro_id: str) -> None:
        self.retro_id = retro_id

    def invoke(self, request: AgentRequest) -> AgentResult:
        retro_dir = request.workspace_root / "qa" / "retro" / self.retro_id
        if request.target.endswith("issue-analysis"):
            self._write_signal(
                retro_dir,
                "issue",
                [
                    {
                        "signal_id": "SIG-ADAPTER",
                        "signal_type": "issue_pattern",
                        "summary": "L1 adapter and domain-factory registry gap",
                        "occurrence_count": 6,
                        "recommended_change": "Align generated adapters with the domain factory registry",
                        "source_refs": {
                            "problem_ids": list(_ADAPTER_PROBLEM_IDS),
                            "occurrence_ids": list(_ADAPTER_OCCURRENCE_IDS),
                        },
                        "confidence": "high",
                        "pattern_kind": "workflow_gap",
                        "affected_surface": {"kind": "workflow", "value": "L1 adapter registry"},
                        "symptom": "l1_knowledge_missing_symbol",
                    },
                    {
                        "signal_id": "SIG-ALIGNMENT",
                        "signal_type": "issue_pattern",
                        "summary": "Test and case alignment drift",
                        "occurrence_count": 1,
                        "recommended_change": "Verify generated cases against test expectations",
                        "source_refs": {
                            "problem_ids": [_ALIGNMENT_PROBLEM_ID],
                            "occurrence_ids": [_ALIGNMENT_OCCURRENCE_ID],
                        },
                        "confidence": "high",
                        "pattern_kind": "test_gap",
                        "affected_surface": {"kind": "test", "value": "tests_api_test_user_api_py"},
                        "symptom": "plan_test_alignment_drift",
                    },
                ],
                request.target,
            )
            return AgentResult(ok=True)
        if request.target.endswith("workflow-analysis"):
            self._write_signal(
                retro_dir,
                "workflow",
                [
                    {
                        "signal_id": "SIG-ARCHIVE",
                        "signal_type": "gate_pushback",
                        "summary": "Archive stopped because execution failed",
                        "occurrence_count": 2,
                        "recommended_change": "Expose execution failure before archive retry",
                        "source_refs": {
                            "workflow_evidence_ids": [
                                f"{change_id}#seq281" for change_id in sorted(_CHANGE_IDS)
                            ]
                        },
                        "confidence": "high",
                        "gate_id": "archive-gate",
                        "cause": "archive.execution_failed",
                    }
                ],
                request.target,
            )
            return AgentResult(ok=True)
        if request.target.endswith("eval-analysis"):
            self._write_signal(retro_dir, "eval", [], request.target)
            return AgentResult(ok=True)

        _write_json(
            retro_dir / "proposal-candidates.json",
            {
                "schema_version": "3",
                "retro_id": self.retro_id,
                "candidates": [
                    {
                        "candidate_id": "CAND-ADAPTER",
                        "kind": "workflow_improvement",
                        "delivery": "change_draft",
                        "signal_ids": ["SIG-ADAPTER"],
                        "source_refs": {
                            "problem_ids": list(_ADAPTER_PROBLEM_IDS),
                            "occurrence_ids": list(_ADAPTER_OCCURRENCE_IDS),
                        },
                        "target": "assurance_agent/workflow",
                        "rationale": "The L1 registry gap recurred across User and Role changes",
                        "proposed_change": "Align adapter registration with domain factories",
                        "verification": {
                            "suites": ["workflow-full"],
                            "success_criteria": "Both User and Role adapters resolve through the registry",
                        },
                        "risk": "medium",
                        "confidence": "high",
                    }
                ],
            },
        )
        (retro_dir / "retro-summary.md").write_text("# Retro v3 golden\n", encoding="utf-8")
        return AgentResult(ok=True)

    def _write_signal(
        self,
        retro_dir: Path,
        domain: str,
        signals: list[dict[str, object]],
        target: str,
    ) -> None:
        _write_json(
            retro_dir / "signals" / f"{domain}.json",
            {
                "schema_version": "3",
                "retro_id": self.retro_id,
                "domain": domain,
                "analysis_status": "ok",
                "failure_reason": None,
                "analyzer": target.removeprefix("skill:"),
                "signals": signals,
            },
        )


def test_two_change_golden_aggregates_patterns_and_enters_review_queue(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _install_benchmark_fixture(project)
    retro_id = "retro-golden"
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, _GoldenAgent(retro_id))

    result = runtime.run(
        compiled,
        "retro",
        _retro_context(project, retro_id).model_copy(
            update={"params": {"retro_id": retro_id, "change_ids": list(_CHANGE_IDS)}}
        ),
    )

    assert result.exit_code == 0, result.reason
    retro_dir = project / "qa" / "retro" / retro_id
    context = json.loads((retro_dir / "context.json").read_text())
    issue_slice = json.loads((retro_dir / "evidence/issue-slice.json").read_text())
    eval_slice = json.loads((retro_dir / "evidence/eval-slice.json").read_text())
    assert len(issue_slice["entries"]) == 22
    assert eval_slice["entries"] == []
    assert eval_slice["integrity"] == {"reasons": [], "status": "complete"}
    assert context["signal_count"] == 3
    assert context["integrity"] == {"reasons": [], "status": "complete"}
    summaries = {
        signal["summary"] for domain_signals in context["signals"].values() for signal in domain_signals
    }
    assert summaries == {
        "L1 adapter and domain-factory registry gap",
        "Test and case alignment drift",
        "Archive stopped because execution failed",
    }
    assert json.loads((retro_dir / "accept-status.json").read_text())["result"] == "accepted"
    assert "IMP-" in (retro_dir / "review-queue.md").read_text()
    review_queue = json.loads((project / "qa/improvements/review-queue.json").read_text())
    assert len(review_queue["improvement_ids"]) == 1


def test_healthy_golden_is_a_complete_no_actionable_signals_receipt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _install_healthy_fixture(project)
    retro_id = "retro-healthy-golden"
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, EmptyAnalysisInvoker())

    result = runtime.run(
        compiled,
        "retro",
        _retro_context(project, retro_id).model_copy(
            update={"params": {"retro_id": retro_id, "change_ids": ["CH-HEALTHY"]}}
        ),
    )

    assert result.exit_code == 0, result.reason
    retro_dir = project / "qa" / "retro" / retro_id
    context = json.loads((retro_dir / "context.json").read_text())
    assert json.loads((retro_dir / "evidence/issue-slice.json").read_text())["entries"] == []
    assert context["integrity"] == {"reasons": [], "status": "complete"}
    assert context["signal_count"] == 0
    candidates = json.loads((retro_dir / "proposal-candidates.json").read_text())
    assert candidates["schema_version"] == "3"
    assert candidates["candidates"] == []
    assert "no_actionable_signals" in (retro_dir / "retro-summary.md").read_text()
