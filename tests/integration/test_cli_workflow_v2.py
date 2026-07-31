"""CLI surface tests for GraphRuntime cutover (Task 15)."""

from __future__ import annotations

import json
import shutil
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
from assurance_agent.workflow.graph.runtime import GraphRuntimeError
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

_SUBGRAPH_MINIMAL_SCHEMA = textwrap.dedent(
    """\
    schema_version: "2"
    name: cli-subgraph-min
    params:
      run_mode: {type: enum, values: [full], default: full}
    entrypoints:
      full: {graph: main, allow: "params.run_mode == 'full'"}
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
          spawn:
            uses: graph:child
            retry: never
            timeout: local
        edges:
          - {from: START, to: spawn}
          - {from: spawn, to: END}
      child:
        max_supersteps: 5
        nodes:
          work:
            uses: operation:no-op
            retry: never
            timeout: local
        edges:
          - {from: START, to: work}
          - {from: work, to: END}
    gates: {}
    """
)


def _run_minimal_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    change_id: str = "CH-1",
    schema: str = _MINIMAL_SCHEMA,
    entrypoint: str = "full",
) -> str:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / change_id).mkdir(parents=True)
    (tmp_path / ".aa" / "workflow-schema.yaml").write_text(schema, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "run",
            "--change",
            change_id,
            "--entrypoint",
            entrypoint,
            "--params",
            '{"run_mode":"full"}',
            "--adapter",
            "headless",
            "--agent-cmd",
            "true",
        ],
    )
    assert result.exit_code == EXIT_COMPLETED, result.output
    status = json.loads(
        CliRunner().invoke(main, ["workflow", "status", "--change", change_id, "--json"]).stdout
    )
    invocation_id = status.get("invocation_id")
    assert invocation_id
    return str(invocation_id)


def _child_invocation_id(change_dir: Path) -> str:
    from assurance_agent.workflow.core.events import read_events_strict

    started = [
        event for event in read_events_strict(change_dir) if event.get("type") == "graph_invocation_started"
    ]
    root = next(event for event in started if event.get("parent_invocation_id") is None)
    child = next(event for event in started if event.get("parent_invocation_id") == root["invocation_id"])
    return str(child["invocation_id"])


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


def test_workflow_run_writes_result_json_on_all_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    mapping = [
        (EXIT_COMPLETED, "done", False),
        (EXIT_STOPPED, "stopped", False),
        (EXIT_HUMAN_REVIEW, "interrupt", False),
        (EXIT_ERROR, "boom", False),
        (EXIT_ERROR, "drive failed", True),
    ]
    for code, reason, started_new_root in mapping:

        def _loop(
            *,
            _code: int = code,
            _reason: str = reason,
            _started: bool = started_new_root,
            on_root_bound=None,
            **kwargs: object,
        ) -> LoopResult:
            if on_root_bound is not None and _started:
                on_root_bound("inv-bound", "full", True)
            return LoopResult(
                _code,
                _reason,
                invocation_id="inv-bound" if _started else "inv-resume",
                started_new_root=_started,
            )

        monkeypatch.setattr(wf, "run_workflow_loop", _loop)
        with CliRunner().isolated_filesystem():
            result_path = Path("workflow-result.json")
            result = CliRunner().invoke(
                main,
                [
                    "workflow",
                    "run",
                    "--change",
                    "CH-1",
                    "--entrypoint",
                    "full",
                    "--result-json",
                    str(result_path),
                ],
            )
            assert result.exit_code == code
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            assert payload == {
                "schema_version": "1",
                "change_id": "CH-1",
                "entrypoint": "full",
                "root_invocation_id": "inv-bound" if started_new_root else "inv-resume",
                "started_new_root": started_new_root,
            }


def test_workflow_resume_exact_invocation_overrides_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MagicMock()
    runtime.latest_root_invocation.return_value = "inv-latest"
    runtime.resume.return_value = _run_result(EXIT_COMPLETED, "completed", "resumed")
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))
    monkeypatch.setattr(
        wf,
        "_validate_root_invocation",
        lambda *args, **kwargs: None,
    )
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
                "--invocation",
                "inv-exact",
                "--entrypoint",
                "full",
            ],
        )
        assert result.exit_code == EXIT_COMPLETED
        runtime.resume.assert_called_once_with("inv-exact", None)


def test_workflow_resume_exact_invocation_rejects_mismatched_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MagicMock()
    monkeypatch.setattr(wf, "build_graph_runtime", lambda **_k: MagicMock(runtime=runtime, compiled=None))
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))
    monkeypatch.setattr(
        wf,
        "_validate_root_invocation",
        lambda *args, **kwargs: (_ for _ in ()).throw(GraphRuntimeError("entrypoint mismatch")),
    )
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
                "--invocation",
                "inv-exact",
                "--entrypoint",
                "execute",
            ],
        )
        assert result.exit_code == EXIT_ERROR
        assert "entrypoint mismatch" in result.output
        runtime.resume.assert_not_called()


def test_workflow_resume_rejects_unknown_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_minimal_workflow(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "resume",
            "--change",
            "CH-1",
            "--invocation",
            "inv-does-not-exist",
            "--entrypoint",
            "full",
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "unknown invocation" in result.output


def test_workflow_resume_rejects_non_root_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_minimal_workflow(tmp_path, monkeypatch, schema=_SUBGRAPH_MINIMAL_SCHEMA)
    child_id = _child_invocation_id(tmp_path / "qa" / "changes" / "CH-1")
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "resume",
            "--change",
            "CH-1",
            "--invocation",
            child_id,
            "--entrypoint",
            "full",
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "is not a root" in result.output


def test_workflow_resume_rejects_wrong_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_id = _run_minimal_workflow(tmp_path, monkeypatch, change_id="CH-1")
    change_one = tmp_path / "qa" / "changes" / "CH-1"
    change_two = tmp_path / "qa" / "changes" / "CH-2"
    change_two.mkdir(parents=True)
    for name in ("events.jsonl", ".graph-runtime"):
        src = change_one / name
        dst = change_two / name
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.is_file():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "resume",
            "--change",
            "CH-2",
            "--invocation",
            root_id,
            "--entrypoint",
            "full",
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "belongs to change" in result.output


def test_workflow_resume_rejects_mismatched_entrypoint_with_real_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_id = _run_minimal_workflow(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "resume",
            "--change",
            "CH-1",
            "--invocation",
            root_id,
            "--entrypoint",
            "execute",
        ],
    )
    assert result.exit_code == EXIT_ERROR
    assert "entrypoint" in result.output


def test_workflow_resume_requires_invocation_and_entrypoint_together() -> None:
    with CliRunner().isolated_filesystem():
        only_invocation = CliRunner().invoke(
            main,
            ["workflow", "resume", "--change", "CH-1", "--invocation", "inv-1"],
        )
        only_entrypoint = CliRunner().invoke(
            main,
            ["workflow", "resume", "--change", "CH-1", "--entrypoint", "full"],
        )
        assert only_invocation.exit_code == EXIT_ERROR
        assert only_entrypoint.exit_code == EXIT_ERROR
        assert "must be provided together" in only_invocation.output


def test_workflow_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_agent.commands import status_cmd

    gs = _status("interrupted", pending_tasks=("main:first",))
    monkeypatch.setattr(status_cmd, "read_latest_graph_status", lambda *a, **k: gs)
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        alias = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
        direct = CliRunner().invoke(main, ["status", "--change", "CH-1", "--json"])
        assert alias.exit_code == direct.exit_code
        assert alias.stdout == direct.stdout
        assert "deprecated" in alias.stderr
        doc = json.loads(alias.stdout)
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
    alias = CliRunner().invoke(main, ["workflow", "status", "--change", "CH-1", "--json"])
    direct = CliRunner().invoke(main, ["status", "--change", "CH-1", "--json"])
    assert alias.exit_code == direct.exit_code == 0
    assert alias.stdout == direct.stdout
    assert "deprecated" in alias.stderr
    doc = json.loads(alias.stdout)
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
    gs = _status(
        "interrupted",
        pending_tasks=("main:first",),
        pending_interrupts=(interrupt,),
    )
    monkeypatch.setattr(status_mod, "read_latest_graph_status", lambda *a, **k: gs)
    with CliRunner().isolated_filesystem():
        write_aa_config(Path.cwd())
        (Path("qa/changes/CH-1")).mkdir(parents=True)
        result = CliRunner().invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
        assert result.exit_code == EXIT_HUMAN_REVIEW
        doc = json.loads(result.stdout)
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


def test_cli_resume_repairs_v5_manual_revision_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI workflow resume recovers an open revision resume prefix via real runtime."""
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.core.graph_events import GraphInterruptedEvent, ManualPlanRevisionEvent
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.driver.runtime_factory import RuntimeBundle
    from assurance_agent.workflow.graph.manual_revision import (
        RevisionPathBaseline,
        RevisionViewBinding,
        build_manual_revision_transition,
        capture_revision_candidate,
        transition_from_committed_revision,
    )
    from assurance_agent.workflow.graph.runtime import _resume_anchors_for
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        edit_recorded_revision_view,
        prepare_interrupted_v5_graph,
    )

    runtime, compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    edit_recorded_revision_view(root_id, b"# revised plan\n")
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    project = context.project_root
    interrupt_id = str(fx["interrupt_id"])

    projection = runtime._checkpoints.project(root_id)  # noqa: SLF001
    pending = projection.interrupts[interrupt_id]
    binding = RevisionViewBinding(
        interrupt_id=pending.interrupt_id,
        owner_invocation_id=pending.revision_owner_invocation_id or "",
        base_tree_id=pending.revision_base_tree_id or "",
        view_relpath=pending.revision_view or "",
        logical_paths=tuple(pending.revision_paths or ()),
        baseline=tuple(
            RevisionPathBaseline(logical_path=path, sha256=(pending.revision_before_sha256 or {})[path])
            for path in (pending.revision_paths or ())
        ),
    )
    tree_revision = capture_revision_candidate(
        change_dir=change,
        store=runtime._objects,  # noqa: SLF001
        binding=binding,
    )
    events = read_events_strict(change)
    interrupted = next(
        e for e in events if e.get("type") == "graph_interrupted" and e.get("interrupt_id") == interrupt_id
    )
    owner = runtime._checkpoints.project(pending.revision_owner_invocation_id or "")  # noqa: SLF001
    transition = build_manual_revision_transition(
        interrupted=GraphInterruptedEvent.model_validate(
            {k: v for k, v in interrupted.items() if k not in {"seq", "ts", "source"}}
        ),
        command=ResumeCommand(
            interrupt_id=interrupt_id,
            action="fix_and_proceed",
            reason="revise synth plan",
            who="reviewer",
        ),
        revision=tree_revision,
        pinned_definition_digests={
            "policy_digest": owner.policy_digest,
            "gate_semantics_digest": owner.gate_semantics_digest,
            "assurance_profile_digest": owner.assurance_profile_digest,
            "graph_digest": owner.graph_digest,
            "ir_digest": owner.ir_digest,
        },
        resume_anchors=_resume_anchors_for(pending),
    )
    with transaction(change) as txn:
        txn.append_strict(transition.revision)

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        wf,
        "build_graph_runtime",
        lambda **_k: RuntimeBundle(runtime=runtime, compiled=compiled),
    )
    monkeypatch.setattr(wf, "evaluate_start_guard", lambda _p: MagicMock(allowed=True))

    result = CliRunner().invoke(
        main,
        [
            "workflow",
            "resume",
            "--change",
            "CH-1",
            "--interrupt",
            interrupt_id,
            "--action",
            "fix_and_proceed",
            "--reason",
            "revise synth plan",
            "--who",
            "reviewer",
        ],
    )
    assert result.exit_code == EXIT_COMPLETED, result.output
    events_after = read_events_strict(change)
    revisions = [e for e in events_after if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    rebuilt = transition_from_committed_revision(
        ManualPlanRevisionEvent.model_validate(
            {k: v for k, v in revisions[0].items() if k not in {"seq", "ts", "source"}}
        )
    )
    resumes = [
        e
        for e in events_after
        if e.get("type") == "graph_resumed"
        and e.get("revision_transition_id") == rebuilt.revision.revision_transition_id
    ]
    assert len(resumes) == len(rebuilt.resumes) == 3
    assert [e.get("revision_ordinal") for e in resumes] == [0, 1, 2]
