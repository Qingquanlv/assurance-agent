"""CLI surface tests for GraphRuntime cutover (Task 15)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from tests.helpers_aa import write_aa_config

import assurance_agent.commands.workflow_cmd as wf
from assurance_agent import resources
from assurance_agent.cli import main
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    LoopResult,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.models import (
    GraphStatus,
    ImportResult,
    InterruptProjection,
    ResumeCommand,
    RunResult,
)
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2

_MINIMAL_SCHEMA = textwrap.dedent(
    """\
    schema_version: "2"
    name: cli-min
    params:
      run_mode: {type: enum, values: [full], default: full}
    entrypoints:
      full: {graph: main, allow: "params.run_mode == 'full'"}
      intake: {graph: main, allow: "params.run_mode == 'full'"}
      execute: {graph: main, allow: "params.run_mode == 'full'"}
      case: {graph: main, allow: "params.run_mode == 'full'"}
    policies:
      retry:
        never: {max_attempts: 1, retry_on: []}
      timeout:
        local: {run_seconds: 60, heartbeat_seconds: 10}
      scheduler: {max_parallel_tasks: 2}
    graphs:
      main:
        max_supersteps: 5
        nodes:
          first:
            uses: operation:no-op
            retry: never
            timeout: local
        edges:
          - {from: START, to: first}
          - {from: first, to: END}
    gates: {}
    """
)


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"unexpected agent invoke: {request}")


def _status(status: str, **overrides: object) -> GraphStatus:
    payload = {
        "invocation_id": "inv-1",
        "entrypoint": "full",
        "status": status,
        "checkpoint_id": "cp-1",
        "event_seq": 2,
        "superstep": 1,
        "running_tasks": (),
        "pending_tasks": (),
        "pending_write_sets": (),
        "pending_interrupts": (),
        "next_retry_at": None,
        "budgets": {},
        "terminal_reason": status,
    }
    payload.update(overrides)
    return GraphStatus(**payload)  # type: ignore[arg-type]


def _run_result(exit_code: int, status: str, reason: str) -> RunResult:
    return RunResult(
        invocation_id="inv-1",
        status=_status(status),
        exit_code=exit_code,  # type: ignore[arg-type]
        reason=reason,
    )


def test_packaged_default_is_schema_v2() -> None:
    schema = load_workflow_v2(Path.cwd())
    assert schema.schema_version == "2"
    packaged = resources.read_text("schemas", "workflow-schema.yaml")
    assert 'schema_version: "2"' in packaged or "schema_version: '2'" in packaged
    assert "phases:" not in packaged.split("gates:")[0]


def test_workflow_run_entrypoint_params_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_loop(**kwargs):
        seen.update(kwargs)
        return LoopResult(EXIT_COMPLETED, "done")

    monkeypatch.setattr(wf, "run_workflow_loop", fake_loop)
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "run",
                "--change",
                "CH-1",
                "--entrypoint",
                "full",
                "--params",
                '{"run_mode":"full"}',
                "--adapter",
                "headless",
            ],
        )
        assert result.exit_code == EXIT_COMPLETED
        assert seen["entrypoint"] == "full"
        assert seen["params"] == {"run_mode": "full"}


def test_workflow_run_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    mapping = [
        (EXIT_COMPLETED, "done"),
        (EXIT_STOPPED, "stopped"),
        (EXIT_HUMAN_REVIEW, "interrupt"),
        (EXIT_ERROR, "boom"),
    ]
    for code, reason in mapping:

        def _loop(*, _code: int = code, _reason: str = reason, **_kwargs: object) -> LoopResult:
            return LoopResult(_code, _reason)

        monkeypatch.setattr(wf, "run_workflow_loop", _loop)
        with CliRunner().isolated_filesystem():
            result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--entrypoint", "full"])
            assert result.exit_code == code


def test_rejects_scope_flag() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "run", "--change", "CH-1", "--scope", "full"])
        assert result.exit_code == 2


def test_workflow_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-1"
    runtime.status.return_value = _status("interrupted", pending_tasks=("main:first",))
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        doc = json.loads(result.output)
        assert doc["status"] == "interrupted"
        assert doc["pending_tasks"] == ["main:first"]


def test_workflow_resume_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-1"
    runtime.resume.return_value = _run_result(EXIT_COMPLETED, "completed", "resumed")
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(main, ["workflow", "resume", "--change", "CH-1"])
        assert result.exit_code == EXIT_COMPLETED
        runtime.resume.assert_called_once_with("inv-1", None)


def test_workflow_resume_interrupt_command(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-1"
    runtime.resume.return_value = _run_result(EXIT_COMPLETED, "completed", "accepted")
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "resume",
                "--change",
                "CH-1",
                "--interrupt",
                "INT",
                "--action",
                "accept_risk",
                "--reason",
                "approved",
                "--who",
                "tester",
            ],
        )
        assert result.exit_code == EXIT_COMPLETED
        command = runtime.resume.call_args.args[1]
        assert isinstance(command, ResumeCommand)
        assert command.interrupt_id == "INT"
        assert command.action == "accept_risk"
        assert command.reason == "approved"
        assert command.who == "tester"


def test_workflow_resume_passes_domain_action_and_structured_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-1"
    runtime.resume.return_value = _run_result(EXIT_COMPLETED, "completed", "confirmed")
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "resume",
                "--change",
                "CH-1",
                "--interrupt",
                "INT-1",
                "--action",
                "confirm_assessment",
                "--reason",
                "triaged from execution evidence",
                "--who",
                "tester",
                "--payload",
                '{"expected_problem_version":2,"classification":"product_bug","severity":"high","evidence_refs":["OCC-1"]}',
            ],
        )

        assert result.exit_code == EXIT_COMPLETED
        command = runtime.resume.call_args.args[1]
        assert isinstance(command, ResumeCommand)
        assert command.action == "confirm_assessment"
        assert command.payload == {
            "expected_problem_version": 2,
            "classification": "product_bug",
            "severity": "high",
            "evidence_refs": ["OCC-1"],
        }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not-json}", "Invalid --payload JSON"),
        ("[]", "Invalid --payload JSON: expected an object"),
    ],
)
def test_workflow_resume_rejects_non_object_payload(payload: str, message: str) -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "resume",
                "--change",
                "CH-1",
                "--interrupt",
                "INT-1",
                "--action",
                "accept_risk",
                "--reason",
                "approved",
                "--payload",
                payload,
            ],
        )

        assert result.exit_code == EXIT_ERROR
        assert message in result.output


def test_workflow_resume_rejects_action_without_interrupt() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            ["workflow", "resume", "--change", "CH-1", "--action", "accept_risk", "--reason", "x"],
        )
        assert result.exit_code == EXIT_ERROR
        assert "--action requires --interrupt" in result.output


def test_workflow_resume_requires_reason() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(
            main,
            [
                "workflow",
                "resume",
                "--change",
                "CH-1",
                "--interrupt",
                "INT",
                "--action",
                "accept_risk",
            ],
        )
        assert result.exit_code == EXIT_ERROR
        assert "--reason is required" in result.output


def test_workflow_import_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MagicMock()
    runtime.import_checkpoint.return_value = ImportResult(
        invocation_id="inv-new",
        checkpoint_id="cp-new",
        imported_tasks=("main:first",),
    )
    monkeypatch.setattr(
        wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=MagicMock())
    )
    monkeypatch.setattr(wf, "runtime_context_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        wf,
        "parse_import_manifest",
        lambda _text: MagicMock(),
    )
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        Path("import.yaml").write_text("schema_version: '2'\n", encoding="utf-8")
        result = CliRunner().invoke(
            main,
            ["workflow", "import-checkpoint", "--change", "CH-1", "--manifest", "import.yaml"],
        )
        assert result.exit_code == EXIT_COMPLETED
        assert "inv-new" in result.output
        assert "cp-new" in result.output


def test_workflow_import_requires_manifest() -> None:
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "import-checkpoint", "--change", "CH-1"])
        assert result.exit_code == 2


def test_workflow_start_detached_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_start(**kwargs):
        seen.update(kwargs)
        from assurance_agent.workflow.driver.workflow_start import StartResult

        return StartResult(ok=True, message="started", pid=1)

    monkeypatch.setattr(wf, "start_workflow_detached", fake_start)
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(main, ["workflow", "start", "--change", "CH-1", "--entrypoint", "full"])
        assert result.exit_code == EXIT_COMPLETED
        assert seen["entrypoint"] == "full"


def test_real_minimal_run_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: packaged contracts + local minimal schema via .aa override."""
    write_aa_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (tmp_path / ".aa" / "workflow-schema.yaml").write_text(_MINIMAL_SCHEMA, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "run",
            "--change",
            "CH-1",
            "--entrypoint",
            "full",
            "--params",
            '{"run_mode":"full"}',
            "--adapter",
            "headless",
            "--agent-cmd",
            "true",
        ],
    )
    assert result.exit_code == EXIT_COMPLETED, result.output
    status = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
    assert status.exit_code == 0
    doc = json.loads(status.output)
    assert doc["status"] == "completed"
    assert doc["entrypoint"] == "full"


def test_top_level_status_next_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import assurance_agent.commands.status_cmd as status_mod

    interrupt = InterruptProjection(
        interrupt_id="INT-1",
        checkpoint_ns="inv-1",
        node_id="review",
        checkpoint="case-review",
        actions=("accept_risk", "fix_and_proceed", "stop"),
        audited_reads_sha256={},
    )
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-1"
    runtime.status.return_value = _status(
        "interrupted",
        pending_tasks=("main:first",),
        pending_interrupts=(interrupt,),
    )
    monkeypatch.setattr(
        status_mod, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None)
    )
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
        assert result.exit_code == EXIT_HUMAN_REVIEW
        doc = json.loads(result.output)
        assert doc["pending_tasks"] == ["main:first"]
        assert doc["pending_interrupts"][0]["interrupt_id"] == "INT-1"


def test_decide_rejects_graph_gate_actions() -> None:
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(
            main,
            [
                "decide",
                "--change",
                "CH-1",
                "--at",
                "case-review",
                "--action",
                "accept_risk",
                "--reason",
                "ok",
            ],
        )
        assert result.exit_code == 1
        assert "workflow resume" in result.output


def test_vue_fastapi_admin_acceptance_fixture_is_present() -> None:
    root = Path("tests/fixtures/issues/vue_fastapi_admin")
    assert (root / "scenario.json").is_file()
    assert (root / "initial" / "execution-manifest.yaml").is_file()
    assert (root / "healing" / "execution-manifest.yaml").is_file()
