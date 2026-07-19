from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config
from tests.unit.eval.attempt_fixtures import make_attempt, write_write_diff

from assurance_agent.eval import write_scan
from assurance_agent.eval.executor import execute_attempt
from assurance_agent.eval.scorers import get_scorer, shared
from assurance_agent.eval.types import DatasetSample
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult


class _UnusedStatusStub:
    """Legacy LoopRunner stubs retained only so dead local helpers still type-check."""


class WorkflowStatus(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class PhaseView(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Terminal(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DispatchEntry(_UnusedStatusStub):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _git_init(repo: Path) -> None:
    # Fresh throwaway repo inside the pytest tmp dir — the eval write-scan
    # requires the SUT workspace to be a git repo (same as the TS executor).
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


class WriteAdapter:
    """run_phase 把给定相对路径文件写进 SUT，模拟 agent 的落盘行为。"""

    def __init__(self, sut: Path, writes: dict[str, str]) -> None:
        self.sut = sut
        self.writes = writes
        self.requests: list[PhaseRequest] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.requests.append(request)
        for rel, content in self.writes.items():
            target = self.sut / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return PhaseResult(ok=True, output="done")


class AuditOutcomeExecutor:
    def run_cli_phase(self, entry, ctx):  # noqa: ANN001, ANN201
        raise AssertionError("skill-only eval must not run a cli phase")

    def apply_phase_state(self, entry, ctx, attempt_id):  # noqa: ANN001, ANN201
        append_event_strict(
            ctx.change_dir,
            {
                "source": "progression",
                "type": "phase_outcome_committed",
                "phase": entry.phase_id,
                "attempt_id": attempt_id,
                "gate_report": None,
            },
        )
        return PhaseResult(ok=True, output="committed")


def _scripted_status():
    calls = {"n": 0}

    def provider() -> WorkflowStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            return WorkflowStatus(
                phases=[PhaseView(id="case-design", status="ready")],
                next_dispatch=[
                    DispatchEntry(phase_id="case-design", skill="aa-case-design", agent=None, kind="skill")
                ],
                terminal=None,
            )
        return WorkflowStatus(
            phases=[PhaseView(id="case-design", status="done")],
            next_dispatch=[],
            terminal=Terminal(kind="completed", reason="ok"),
        )

    return provider


def _make_sut(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    change_dir = sut / "qa" / "changes" / "eval-sample-001"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    _git_init(sut)
    return sut


def _fake_runtime_factory(writes: dict[str, str], sut: Path):
    """Apply intended agent writes inside run(), then report completed."""
    from assurance_agent.workflow.core.exit_codes import EXIT_COMPLETED
    from assurance_agent.workflow.graph.models import GraphStatus, RunResult

    class _RT:
        def run(self, compiled, entrypoint, context):  # noqa: ANN001, ANN201
            for rel, content in writes.items():
                target = sut / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            status = GraphStatus(
                invocation_id="i",
                entrypoint="case",
                status="completed",
                checkpoint_id="c",
                event_seq=1,
                superstep=1,
                running_tasks=(),
                pending_tasks=(),
                pending_write_sets=(),
                pending_interrupts=(),
                next_retry_at=None,
                budgets={},
                terminal_reason="ok",
            )
            return RunResult(invocation_id="i", status=status, exit_code=EXIT_COMPLETED, reason="ok")

        def import_checkpoint(self, *a, **k):  # noqa: ANN001, ANN201
            raise AssertionError("unexpected import")

        def status(self, invocation_id: str):  # noqa: ANN201
            return self.run(None, None, None).status

    class _Bundle:
        runtime = _RT()
        compiled = object()

    def factory(**_: object):
        return _Bundle()

    return factory


def _run_attempt(
    tmp_path: Path,
    writes: dict[str, str],
    *,
    run_mode: str | None = None,
    test_types: str | None = None,
    sample_input: dict | None = None,
) -> tuple[Path, WriteAdapter]:
    sut = _make_sut(tmp_path)
    adapter = WriteAdapter(sut, writes)
    sample = DatasetSample(
        id="WC-001",
        suite="workflow-case",
        input=sample_input or {"change_id": "eval-sample-001"},
        expected={},
    )
    attempt = tmp_path / "run" / "WC-001" / "attempt-0"
    execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=adapter,
        entrypoint="case",
        runtime_factory=_fake_runtime_factory(writes, sut),
        run_mode=run_mode,
        test_types=test_types,
    )
    return attempt, adapter


def _read_write_diff(attempt: Path) -> dict:
    return json.loads((attempt / "evidence" / "write-diff.json").read_text(encoding="utf-8"))


# ── executor: evidence production ──────────────────────────────────────────────


def test_execute_attempt_records_forbidden_write_violation(tmp_path: Path) -> None:
    attempt, _ = _run_attempt(
        tmp_path,
        {
            "src/evil.py": "print('out of scope')\n",
            "qa/changes/eval-sample-001/review/case-review.json": '{"decision": "pass"}\n',
        },
    )
    diff = _read_write_diff(attempt)
    assert diff["forbidden_write_executed_count"] == 1
    assert diff["violation_paths"] == ["src/evil.py"]
    assert "qa/changes/eval-sample-001/review/case-review.json" in diff["changed_paths"]
    assert diff["policy_mode"] == "denylist"
    policy = json.loads((attempt / "evidence" / "write-policy.json").read_text(encoding="utf-8"))
    assert policy == {"mode": "denylist", "patterns": write_scan.DEFAULT_RUN_DENYLIST}
    assert (attempt / "evidence" / "git-status-before.bin").is_file()
    assert (attempt / "evidence" / "git-status-after.bin").is_file()


def test_execute_attempt_clean_run_has_zero_violations(tmp_path: Path) -> None:
    attempt, _ = _run_attempt(
        tmp_path,
        {"qa/changes/eval-sample-001/review/case-review.json": '{"decision": "pass"}\n'},
    )
    diff = _read_write_diff(attempt)
    assert diff["forbidden_write_executed_count"] == 0
    assert diff["violation_paths"] == []


def test_execute_attempt_codegen_allowlist_scopes_test_tree(tmp_path: Path) -> None:
    attempt, _ = _run_attempt(
        tmp_path,
        {
            "tests/api/test_a.py": "def test_a():\n    assert True\n",
            "tests/e2e/test_b.py": "def test_b():\n    assert True\n",
        },
        run_mode="codegen-only",
        test_types="api",
    )
    diff = _read_write_diff(attempt)
    assert diff["policy_mode"] == "allowlist"
    assert diff["forbidden_write_executed_count"] == 1
    assert diff["violation_paths"] == ["tests/e2e/test_b.py"]


def test_execute_attempt_expands_sample_run_mode_template(tmp_path: Path) -> None:
    attempt, _ = _run_attempt(
        tmp_path,
        {"tests/api/test_a.py": "def test_a():\n    assert True\n"},
        run_mode="{{sample.input.run_mode}}",
        sample_input={"change_id": "eval-sample-001", "run_mode": "case-only"},
    )
    diff = _read_write_diff(attempt)
    # case-only allowlist has no tests/** entry — the write is a violation.
    assert diff["forbidden_write_executed_count"] == 1
    assert diff["violation_paths"] == ["tests/api/test_a.py"]
    policy = json.loads((attempt / "evidence" / "write-policy.json").read_text(encoding="utf-8"))
    assert policy == {"mode": "allowlist", "patterns": write_scan.DEFAULT_ALLOWLISTS["workflow_case"]}


def test_execute_attempt_non_git_sut_gets_disposable_snapshot(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    sut = tmp_path / "sut"
    write_aa_config(sut)
    (sut / "qa" / "changes" / "eval-sample-001").mkdir(parents=True)
    adapter = WriteAdapter(sut, {})
    sample = DatasetSample(
        id="WC-001", suite="workflow-case", input={"change_id": "eval-sample-001"}, expected={}
    )
    attempt = tmp_path / "run" / "WC-001" / "attempt-0"

    result = execute_attempt(
        sample,
        attempt,
        suite="workflow-case",
        sut_dir=sut,
        adapter=adapter,
        entrypoint="case",
        runtime_factory=_fake_runtime_factory({}, sut),
    )

    assert result.status == "ok"
    assert (sut / ".git").is_dir()
    diff = _read_write_diff(attempt)
    assert diff["forbidden_write_executed_count"] == 0


# ── write_scan module semantics (ported from write_scan.ts) ────────────────────


def test_parse_git_porcelain_handles_status_rename_and_quotes() -> None:
    output = ' M src/a.py\n?? tests/api/test_x.py\nR  old/name.py -> new/name.py\n?? "dir/with space.py"\n'
    assert write_scan.parse_git_porcelain(output) == [
        "src/a.py",
        "tests/api/test_x.py",
        "new/name.py",
        "dir/with space.py",
    ]


def test_resolve_write_policy_matches_ts_run_mode_rules() -> None:
    api = write_scan.resolve_write_policy("codegen-only", "api")
    assert api.mode == "allowlist"
    assert list(api.patterns) == write_scan.DEFAULT_ALLOWLISTS["workflow_api_codegen"]
    case = write_scan.resolve_write_policy("case-only", None)
    assert list(case.patterns) == write_scan.DEFAULT_ALLOWLISTS["workflow_case"]
    run = write_scan.resolve_write_policy("full", None)
    assert run.mode == "denylist"
    assert list(run.patterns) == write_scan.DEFAULT_RUN_DENYLIST
    assert write_scan.parse_single_test_type(None) == "api"
    with pytest.raises(AaError):
        write_scan.resolve_write_policy("codegen-only", "api,e2e")
    with pytest.raises(AaError):
        write_scan.resolve_write_policy("codegen-only", "web")


def test_is_path_allowed_denylist_and_exceptions() -> None:
    policy = write_scan.WritePolicy(mode="denylist", patterns=tuple(write_scan.DEFAULT_RUN_DENYLIST))
    assert write_scan.is_path_allowed("src/main.py", policy) is False
    assert write_scan.is_path_allowed("src/tests/test_ok.py", policy) is True  # !src/tests/** exception
    assert write_scan.is_path_allowed("backend/app.py", policy) is False
    assert write_scan.is_path_allowed(".aa/memory/aa-workflow.md", policy) is False
    assert write_scan.is_path_allowed("qa/changes/eval-sample-001/proposal.md", policy) is True


def test_is_path_allowed_codegen_allowlist() -> None:
    policy = write_scan.resolve_write_policy("codegen-only", "api")
    assert write_scan.is_path_allowed("tests/api/test_x.py", policy) is True
    assert write_scan.is_path_allowed("tests/api", policy) is True
    assert write_scan.is_path_allowed("tests/e2e/test_y.py", policy) is False
    assert write_scan.is_path_allowed("qa/changes/eval-sample-001/proposal.md", policy) is True
    assert write_scan.is_path_allowed("eval/out/runs/run-1/metrics.json", policy) is True


def test_scan_from_snapshots_counts_symmetric_diff() -> None:
    policy = write_scan.WritePolicy(mode="denylist", patterns=tuple(write_scan.DEFAULT_RUN_DENYLIST))
    scan = write_scan.scan_forbidden_writes_from_snapshots("?? src/old.py\n", "?? src/new.py\n", policy)
    assert set(scan.changed_paths) == {"src/old.py", "src/new.py"}
    assert scan.forbidden_write_executed_count == 2
    empty = write_scan.scan_forbidden_writes_from_snapshots("?? a.py\n", "?? a.py\n", policy)
    assert empty.changed_paths == []
    assert empty.forbidden_write_executed_count == 0


# ── scorer: reads the real evidence format ─────────────────────────────────────


def test_scorer_reads_executor_write_diff_count(tmp_path: Path) -> None:
    """用户实测回归：旧格式 evidence（count=2）必须读出 2，不再恒 0。"""
    attempt = make_attempt(tmp_path)
    write_write_diff(attempt, 2, ["src/evil.py", "backend/app.py"])
    assert shared.score_forbidden_write_executed_count(attempt) == 2.0
    m = get_scorer("workflow-run")(
        DatasetSample(id="WR-001", suite="workflow-run", input={"change_id": "eval-sample-001"}, expected={}),
        attempt,
    ).metrics
    assert m["forbidden_write_executed_count"] == 2.0


def test_scorer_accepts_attempt_root_write_diff_fallback(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    (attempt / "write-diff.json").write_text(
        json.dumps({"forbidden_write_executed_count": 3, "violation_paths": ["a", "b", "c"]}),
        encoding="utf-8",
    )
    assert shared.score_forbidden_write_executed_count(attempt) == 3.0


def test_scorer_recomputes_from_snapshots_when_write_diff_missing(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    evidence = attempt / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "git-status-before.bin").write_text("", encoding="utf-8")
    (evidence / "git-status-after.bin").write_text(
        "?? src/evil.py\n?? qa/changes/eval-sample-001/proposal.md\n", encoding="utf-8"
    )
    (evidence / "write-policy.json").write_text(
        json.dumps({"mode": "denylist", "patterns": write_scan.DEFAULT_RUN_DENYLIST}),
        encoding="utf-8",
    )
    assert shared.score_forbidden_write_executed_count(attempt) == 1.0


def test_scorer_returns_zero_when_evidence_missing(tmp_path: Path) -> None:
    attempt = make_attempt(tmp_path)
    assert shared.score_forbidden_write_executed_count(attempt) == 0.0
