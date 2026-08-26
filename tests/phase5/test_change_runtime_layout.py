from __future__ import annotations

import json
from pathlib import Path

from graph_engine.plugin_api import TaskOutcome
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostExecuteCall
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    common_lifecycle_args,
    parse_json_output,
    scripted_engine_factory,
    source_args,
    write_product_input,
)
from tests.phase5.composition_harness import InstalledSources, request_for
from tests.phase5.product_runner import _ScriptedTaskHost

_FORBIDDEN_TREE_NAMES = frozenset({"workspace", "trees", "attempts", "HEAD.json"})


def _change_root(project_dir: Path, change_id: str) -> Path:
    return project_dir / "qa" / "changes" / change_id


def _existing_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    installed_sources: InstalledSources,
    secret: str,
) -> list[str]:
    return [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--secret",
        secret,
        "--json",
    ]


def _assert_no_tree_store(root: Path) -> None:
    if not root.exists():
        return
    names = {path.name for path in root.rglob("*")}
    assert names.isdisjoint(_FORBIDDEN_TREE_NAMES)


def _failing_engine_factory():
    class _FailingHost(_ScriptedTaskHost):
        def __init__(self) -> None:
            super().__init__(
                execution_sequence=(),
                coverage_sequence=(),
                threshold=0.90,
                coverage_rounds=1,
                review_decision="pass",
                healing_decision="allowed",
            )

        async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
            del call
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.failed(
                    "invalid_output",
                    "scripted change-local failure",
                    retryable=False,
                ),
            )

    def factory(root: Path, authorization: InvocationRuntimeAuthorization) -> Engine:
        del authorization
        return Engine(root, host=_FailingHost())

    return factory


def test_two_changes_are_independently_discoverable(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    first_args, project_dir, first_change = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-change-a",
        change_id="CH-A-001",
    )
    second_input = write_product_input(
        tmp_path / "input-b.json",
        composition,
        change_id="CH-B-001",
    )
    second_args = [
        "--project-dir",
        str(project_dir),
        "--change",
        "CH-B-001",
        "--invocation-id",
        "inv-change-b",
        *source_args(installed_sources),
        "--entrypoint",
        "intake",
        "--input",
        str(second_input),
        "--secret",
        first_args[first_args.index("--secret") + 1],
        "--json",
    ]

    first = cli_runner.invoke(app, ["start", *first_args])
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(app, ["start", *second_args])
    assert second.exit_code == 0, second.output

    first_status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=first_change,
                invocation_id="inv-change-a",
                installed_sources=installed_sources,
                secret=first_args[first_args.index("--secret") + 1],
            ),
        ],
    )
    second_status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id="CH-B-001",
                invocation_id="inv-change-b",
                installed_sources=installed_sources,
                secret=first_args[first_args.index("--secret") + 1],
            ),
        ],
    )
    assert first_status.exit_code == 0, first_status.output
    assert second_status.exit_code == 0, second_status.output
    first_doc = parse_json_output(first_status.stdout)
    second_doc = parse_json_output(second_status.stdout)
    assert first_doc["invocation_id"] == "inv-change-a"
    assert second_doc["invocation_id"] == "inv-change-b"
    assert first_doc["change"]["change_id"] == "CH-A-001"
    assert second_doc["change"]["change_id"] == "CH-B-001"
    assert (_change_root(project_dir, "CH-A-001") / ".runtime" / "invocations" / "inv-change-a").is_dir()
    assert (_change_root(project_dir, "CH-B-001") / ".runtime" / "invocations" / "inv-change-b").is_dir()
    assert not (_change_root(project_dir, "CH-A-001") / ".runtime" / "invocations" / "inv-change-b").exists()
    assert not (_change_root(project_dir, "CH-B-001") / ".runtime" / "invocations" / "inv-change-a").exists()


def test_status_and_resume_authenticate_the_change_local_ledger(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-auth-001",
        change_id="CH-AUTH-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    secret = args[args.index("--secret") + 1]

    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-auth-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    assert parse_json_output(status.stdout)["invocation_id"] == "inv-auth-001"

    missing_change = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id="CH-MISSING-001",
                invocation_id="inv-auth-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
        ],
    )
    assert missing_change.exit_code == 40, missing_change.output

    crossed = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-other-001",
                installed_sources=installed_sources,
                secret=secret,
            ),
            "--action",
            "approve",
            "--reason",
            "accepted",
        ],
    )
    assert crossed.exit_code == 40, crossed.output


def test_failed_run_exposes_status_and_events_but_not_staged_files(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product import cli
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(cli, "create_engine", _failing_engine_factory())
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-fail-001",
        change_id="CH-FAIL-001",
    )
    result = cli_runner.invoke(app, ["run", *args])
    assert result.exit_code == 40, result.output

    change = _change_root(project_dir, change_id)
    status_path = change / "status.json"
    events_path = change / "events.jsonl"
    assert status_path.is_file()
    assert events_path.is_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["invocation_id"] == "inv-fail-001"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
    assert events

    staged = change / ".staging" / "intake" / "1" / "leaked.py"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("not canonical\n", encoding="utf-8")
    generated = change / "generated"
    assert not generated.exists() or not any(generated.rglob("*"))
    assert not (change / "leaked.py").exists()
    assert "leaked.py" not in status_path.read_text(encoding="utf-8")
    assert "leaked.py" not in events_path.read_text(encoding="utf-8")


def test_start_does_not_create_tree_store_directories(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(
        "assurance_product.cli.create_engine",
        scripted_engine_factory(),
    )
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-layout-001",
        change_id="CH-LAYOUT-001",
    )
    result = cli_runner.invoke(app, ["run", *args])
    assert result.exit_code == 0, result.output

    change = _change_root(project_dir, change_id)
    runtime = change / ".runtime"
    assert runtime.is_dir()
    assert (runtime / "invocations" / "inv-layout-001").is_dir()
    assert (change / ".staging").is_dir()
    assert (change / "status.json").is_file()
    assert (change / "events.jsonl").is_file()
    _assert_no_tree_store(project_dir)
    _assert_no_tree_store(change)
    assert not (tmp_path / "engine-root").exists()


def test_projections_are_not_used_to_advance_execution(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-proj-001",
        change_id="CH-PROJ-001",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    change = _change_root(project_dir, change_id)
    (change / "status.json").write_text('{"status":"forged"}\n', encoding="utf-8")
    (change / "events.jsonl").write_text('{"kind":"forged"}\n', encoding="utf-8")

    status = cli_runner.invoke(
        app,
        [
            "status",
            *_existing_args(
                project_dir=project_dir,
                change_id=change_id,
                invocation_id="inv-proj-001",
                installed_sources=installed_sources,
                secret=args[args.index("--secret") + 1],
            ),
        ],
    )
    assert status.exit_code == 0, status.output
    document = parse_json_output(status.stdout)
    assert document["status"] != "forged"
    assert document["invocation_id"] == "inv-proj-001"
    rewritten = json.loads((change / "status.json").read_text(encoding="utf-8"))
    assert rewritten["status"] != "forged"
    assert rewritten["invocation_id"] == "inv-proj-001"
    events = (change / "events.jsonl").read_text(encoding="utf-8")
    assert "forged" not in events
