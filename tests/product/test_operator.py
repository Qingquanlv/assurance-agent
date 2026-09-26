from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.status import (
    read_bootstrap_status,
    read_run_manifest,
    write_bootstrap_status,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "sut"
    aa = project / ".aa"
    aa.mkdir(parents=True)
    (aa / "policy.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text("version: 1\n", encoding="utf-8")
    (aa / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {"name": "demo", "type": "backend"},
                "urls": {
                    "backend": "http://127.0.0.1:9999",
                    "frontend": "http://127.0.0.1:3100",
                },
                "execution": {"model_routing": {"default": "deepseek/deepseek-flash"}},
            }
        ),
        encoding="utf-8",
    )
    return project


def _spec_config(**execution: object) -> dict[str, object]:
    return {
        "project": {"name": "demo", "type": "backend"},
        "urls": {"backend": "http://127.0.0.1:9999"},
        "execution": execution,
    }


def test_effective_spec_uses_the_configured_default_model():
    from assurance_product.operator import build_effective_spec

    spec = build_effective_spec(
        _spec_config(model_routing={"default": "deepseek/deepseek-flash"}),
        requirement="Cover user CRUD.",
        families=("api",),
    )
    assert spec.routes.provider_model == "deepseek/deepseek-flash"


def test_effective_spec_rejects_a_missing_default_model():
    import pytest

    from assurance_product.operator import OperatorError, build_effective_spec

    with pytest.raises(OperatorError, match="execution.model_routing.default"):
        build_effective_spec(
            _spec_config(),
            requirement="Cover user CRUD.",
            families=("api",),
        )


def _start_args(project: Path, *extra: str) -> list[str]:
    return [
        "operator",
        "start",
        "--json",
        "--project-dir",
        str(project),
        "--requirement",
        "Cover user CRUD.",
        "--family",
        "api",
        "--opencode-endpoint",
        "http://127.0.0.1:4096",
        *extra,
    ]


def _finish(run_dir: Path, change_id: str) -> None:
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(phase="terminal", change_id=change_id, exit_code=0),
    )


def test_existing_task_does_not_create_a_second_worktree(cli_runner, tmp_path, monkeypatch):
    from assurance_product.cli import app

    project = _project(tmp_path)
    task = tmp_path / "task"
    task.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("nested worktree creation")

    from assurance_product.task_records import define_task

    define_task(
        project_dir=project,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )

    def prepare_workspace(**kwargs: object) -> None:
        from assurance_product.change_workspace import ChangeWorkspace

        change_id = str(kwargs["change_id"])
        ChangeWorkspace.open(task, change_id)
        _finish(task / ".aa" / "runs" / change_id, change_id)

    monkeypatch.setattr("assurance_product.operator.ensure_run_worktree", forbidden)
    monkeypatch.setattr("assurance_product.operator.launch_worker", prepare_workspace)
    result = cli_runner.invoke(
        app,
        _start_args(project, "--task-directory", str(task), "--request-id", "req-1"),
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert Path(body["worktree"]) == task.resolve()
    manifest = read_run_manifest(task / ".aa" / "runs" / body["run_id"])
    assert manifest["source_project_dir"] == str(project.resolve())
    assert manifest["task_directory"] == str(task.resolve())
    assert manifest["config_source"] == str((project / ".aa" / "config.yaml").resolve())
    assert manifest["baseline"] == []
    assert manifest["request_id"] == "req-1"
    again = cli_runner.invoke(
        app,
        _start_args(project, "--task-directory", str(task), "--request-id", "req-1"),
    )
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["run_id"] == body["run_id"]
    assert len(list((task / ".aa" / "runs").iterdir())) == 1
    assert (task / "qa").is_dir()
    (task / "repaired.txt").write_text("chat repair\n", encoding="utf-8")
    second = cli_runner.invoke(
        app,
        _start_args(project, "--task-directory", str(task), "--request-id", "req-2"),
    )
    assert second.exit_code == 0, second.output
    second_id = json.loads(second.stdout)["run_id"]
    assert second_id != body["run_id"]
    first = read_run_manifest(task / ".aa" / "runs" / body["run_id"])
    second_manifest = read_run_manifest(task / ".aa" / "runs" / second_id)
    assert first["task_directory"] == second_manifest["task_directory"] == str(task.resolve())
    assert first["change_id"] != second_manifest["change_id"]


def test_same_request_id_does_not_relaunch_after_worker_failure(cli_runner, tmp_path, monkeypatch) -> None:
    from assurance_product.cli import app
    from assurance_product.task_records import define_task

    project = _project(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    define_task(
        project_dir=project,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )
    calls = {"n": 0}

    def boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("worker down")

    monkeypatch.setattr("assurance_product.operator.launch_worker", boom)
    args = _start_args(project, "--task-directory", str(task), "--request-id", "req-fail")
    failed = cli_runner.invoke(app, args)
    assert failed.exit_code != 0
    assert "worker_failed" in failed.output
    recovered = cli_runner.invoke(app, args)
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.stdout)["phase"] == "terminal"
    assert calls["n"] == 1


def test_two_tasks_share_project_defaults_without_sharing_runs(cli_runner, tmp_path, monkeypatch) -> None:
    from assurance_product.cli import app
    from assurance_product.task_records import define_task

    project = _project(tmp_path)
    monkeypatch.setattr("assurance_product.operator.launch_worker", lambda **kwargs: None)
    ids: list[str] = []
    for name in ("a", "b"):
        task = tmp_path / name
        task.mkdir()
        define_task(
            project_dir=project,
            task_directory=task,
            name=name,
            base_ref="main",
            requirement="Cover user CRUD.",
            families=("api",),
        )
        result = cli_runner.invoke(
            app,
            _start_args(project, "--task-directory", str(task), "--request-id", f"req-{name}"),
        )
        assert result.exit_code == 0, result.output
        ids.append(json.loads(result.stdout)["run_id"])
    assert ids[0] != ids[1]


def test_unconfigured_task_start_reports_not_configured(cli_runner, tmp_path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr("assurance_product.operator.launch_worker", lambda **kwargs: None)
    result = cli_runner.invoke(
        app, _start_args(project, "--task-directory", str(task), "--request-id", "req-1")
    )
    assert result.exit_code != 0
    assert "not_configured" in result.output


def test_malformed_task_config_does_not_fall_back(cli_runner, tmp_path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    task = tmp_path / "task"
    (task / ".aa").mkdir(parents=True)
    (task / ".aa" / "config.yaml").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr("assurance_product.operator.launch_worker", lambda **kwargs: None)
    result = cli_runner.invoke(app, _start_args(project, "--task-directory", str(task)))
    assert result.exit_code != 0
    assert not (task / ".aa" / "runs").exists()


def test_missing_task_directory_is_rejected(cli_runner, tmp_path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    monkeypatch.setattr(
        "assurance_product.operator.ensure_run_worktree",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nested worktree creation")),
    )
    result = cli_runner.invoke(app, _start_args(project, "--task-directory", str(tmp_path / "missing")))
    assert result.exit_code != 0
    assert "task directory does not exist" in result.output


def test_operator_start_allocates_distinct_ids_and_rejects_bad_input(
    cli_runner, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    nonces = iter(("11111111", "22222222"))
    launched: list[str] = []

    def launch(*, run_dir: Path, change_id: str, environ: dict[str, str]) -> None:
        launched.append(change_id)
        assert environ.get("OPENCODE_ENDPOINT") == os.environ.get("OPENCODE_ENDPOINT")
        _finish(run_dir, change_id)

    monkeypatch.setattr("assurance_product.operator.launch_worker", launch)
    monkeypatch.setattr("assurance_product.operator._nonce", lambda: next(nonces))
    monkeypatch.setattr("assurance_product.operator._stamp", lambda: "20260923T000000Z")

    first = cli_runner.invoke(app, _start_args(project, "--origin-session-id", "ses_user"))
    second = cli_runner.invoke(app, _start_args(project))
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_body = json.loads(first.stdout)
    second_body = json.loads(second.stdout)
    assert first_body["run_id"] == first_body["change_id"] == "BOOT-20260923T000000Z-11111111"
    assert second_body["run_id"] == "BOOT-20260923T000000Z-22222222"
    assert first_body["run_id"] != second_body["run_id"]
    assert launched == [first_body["run_id"], second_body["run_id"]]
    manifest = read_run_manifest(tmp_path / "sut" / ".aa" / "runs" / first_body["run_id"])
    assert manifest["origin_session_id"] == "ses_user"
    assert manifest["ownership"] == "shared"
    assert manifest["requested_opencode_endpoint"] == "http://127.0.0.1:4096"

    runs_root = project / ".aa" / "runs"
    before = set(runs_root.iterdir())
    rejected = cli_runner.invoke(
        app,
        [
            "operator",
            "start",
            "--json",
            "--project-dir",
            str(project),
            "--requirement",
            "   ",
            "--family",
            "api",
            "--opencode-endpoint",
            "http://127.0.0.1:4096",
        ],
    )
    invalid_family = cli_runner.invoke(
        app,
        [
            "operator",
            "start",
            "--json",
            "--project-dir",
            str(project),
            "--requirement",
            "Cover user CRUD.",
            "--family",
            "ui",
            "--opencode-endpoint",
            "http://127.0.0.1:4096",
        ],
    )
    assert rejected.exit_code == 40
    assert json.loads(rejected.stdout)["kind"] == "invalid_input"
    assert invalid_family.exit_code == 40
    assert json.loads(invalid_family.stdout)["kind"] == "invalid_input"
    remote = cli_runner.invoke(
        app,
        [
            "operator",
            "start",
            "--json",
            "--project-dir",
            str(project),
            "--requirement",
            "Cover user CRUD.",
            "--family",
            "api",
            "--opencode-endpoint",
            "https://opencode.example",
        ],
    )
    assert remote.exit_code == 40
    assert json.loads(remote.stdout)["kind"] == "invalid_input"
    assert set(runs_root.iterdir()) == before


def test_operator_addresses_the_exact_run(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    nonces = iter(("aaaaaaa1", "bbbbbbb2"))
    monkeypatch.setattr("assurance_product.operator._nonce", lambda: next(nonces))
    monkeypatch.setattr("assurance_product.operator._stamp", lambda: "20260923T000000Z")

    def launch(*, run_dir: Path, change_id: str, environ: dict[str, str]) -> None:
        del environ
        write_bootstrap_status(
            run_dir,
            BootstrapStatusV1(phase="running", change_id=change_id),
        )

    monkeypatch.setattr("assurance_product.operator.launch_worker", launch)
    started = cli_runner.invoke(app, _start_args(project))
    assert started.exit_code == 0, started.output
    older = json.loads(started.stdout)["run_id"]
    runs_root = project / ".aa" / "runs"
    newer = "BOOT-20260923T000000Z-bbbbbbb2"
    newer_dir = runs_root / newer
    newer_dir.mkdir()
    write_bootstrap_status(
        newer_dir,
        BootstrapStatusV1(phase="running", change_id=newer),
    )
    (newer_dir / "run-manifest.json").write_text(
        json.dumps({"source_project_dir": str(project.resolve()), "change_id": newer}) + "\n",
        encoding="utf-8",
    )
    newer_before = (newer_dir / "bootstrap-status.json").read_bytes()

    status = cli_runner.invoke(
        app,
        ["operator", "status", "--json", "--project-dir", str(project), "--run-id", older],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["change_id"] == older

    def resolve_stop(run_dir: Path) -> None:
        current = read_bootstrap_status(run_dir)
        assert current.phase == "running"
        assert (run_dir / "stop-request.json").is_file()
        write_bootstrap_status(
            run_dir,
            BootstrapStatusV1(phase="terminal", change_id=older, exit_code=20),
        )

    monkeypatch.setattr("assurance_product.operator.drive_stop", resolve_stop)
    stopped = cli_runner.invoke(
        app,
        ["operator", "stop", "--json", "--project-dir", str(project), "--run-id", older],
    )
    assert stopped.exit_code == 0, stopped.output
    assert json.loads(stopped.stdout)["change_id"] == older
    assert json.loads(stopped.stdout)["phase"] == "terminal"
    assert (newer_dir / "bootstrap-status.json").read_bytes() == newer_before

    missing = cli_runner.invoke(
        app,
        ["operator", "status", "--json", "--project-dir", str(project), "--run-id", "BOOT-missing"],
    )
    assert missing.exit_code == 40
    assert json.loads(missing.stdout)["kind"] == "unknown_run"

    empty_project = _project(tmp_path / "empty")
    empty = cli_runner.invoke(
        app,
        [
            "operator",
            "status",
            "--json",
            "--project-dir",
            str(empty_project),
            "--run-id",
            "BOOT-missing",
        ],
    )
    assert empty.exit_code == 40
    assert json.loads(empty.stdout)["kind"] == "no_runs"


def test_operator_resume_modes_and_worker_failure(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    monkeypatch.setattr("assurance_product.operator._nonce", lambda: "cccccccc")
    monkeypatch.setattr("assurance_product.operator._stamp", lambda: "20260923T000000Z")

    def fail_launch(*, run_dir: Path, change_id: str, environ: dict[str, str]) -> None:
        del run_dir, change_id, environ
        raise RuntimeError("worker exited before the graph started")

    monkeypatch.setattr("assurance_product.operator.launch_worker", fail_launch)
    failed = cli_runner.invoke(app, _start_args(project))
    assert failed.exit_code == 40
    body = json.loads(failed.stdout)
    assert body["kind"] == "worker_failed"
    run_id = "BOOT-20260923T000000Z-cccccccc"
    status = read_bootstrap_status(project / ".aa" / "runs" / run_id)
    assert status.phase == "terminal"
    assert status.error is not None and "worker exited" in status.error

    resumed: list[tuple[str, str | None, str | None]] = []

    def resume_terminal(*, run_dir: Path, environ: dict[str, str]) -> BootstrapStatusV1:
        del environ
        resumed.append(("restart_terminal", None, None))
        return BootstrapStatusV1(phase="terminal", change_id=run_id, exit_code=0)

    def resolve_interrupt(
        *, run_dir: Path, action: str, reason: str, environ: dict[str, str]
    ) -> BootstrapStatusV1:
        del run_dir, environ
        resumed.append(("resolve_interrupt", action, reason))
        return BootstrapStatusV1(phase="running", change_id=run_id)

    monkeypatch.setattr("assurance_product.operator.resume_terminal_run", resume_terminal)
    monkeypatch.setattr("assurance_product.operator.resolve_graph_interrupt", resolve_interrupt)
    restart = cli_runner.invoke(
        app,
        [
            "operator",
            "resume",
            "--json",
            "--project-dir",
            str(project),
            "--run-id",
            run_id,
            "--mode",
            "restart_terminal",
        ],
    )
    decision = cli_runner.invoke(
        app,
        [
            "operator",
            "resume",
            "--json",
            "--project-dir",
            str(project),
            "--run-id",
            run_id,
            "--mode",
            "resolve_interrupt",
            "--action",
            "approve",
            "--reason",
            "owner confirmed the gap",
        ],
    )
    assert restart.exit_code == 0, restart.output
    assert decision.exit_code == 0, decision.output
    assert resumed == [
        ("restart_terminal", None, None),
        ("resolve_interrupt", "approve", "owner confirmed the gap"),
    ]
    guessed = cli_runner.invoke(
        app,
        ["operator", "resume", "--json", "--project-dir", str(project), "--run-id", run_id],
    )
    assert guessed.exit_code != 0


def test_duplicate_start_does_not_allocate_another_run(cli_runner, tmp_path: Path, monkeypatch) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    monkeypatch.setattr("assurance_product.operator._nonce", lambda: "dddddddd")
    monkeypatch.setattr("assurance_product.operator._stamp", lambda: "20260923T000000Z")

    def launch(*, run_dir: Path, change_id: str, environ: dict[str, str]) -> None:
        del environ
        write_bootstrap_status(run_dir, BootstrapStatusV1(phase="running", change_id=change_id))

    monkeypatch.setattr("assurance_product.operator.launch_worker", launch)
    first = cli_runner.invoke(app, _start_args(project))
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(app, _start_args(project))
    assert second.exit_code == 40
    assert json.loads(second.stdout)["kind"] == "conflict"
    assert list((project / ".aa" / "runs").iterdir()) == [
        project / ".aa" / "runs" / json.loads(first.stdout)["run_id"]
    ]


def test_operator_start_rejects_a_child_session_before_creating_a_run(
    cli_runner, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    monkeypatch.setattr(
        "assurance_product.operator.launch_worker",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("launch")),
    )
    result = cli_runner.invoke(
        app,
        [*_start_args(project), "--origin-parent-session-id", "ses_parent"],
    )
    assert result.exit_code == 40
    assert json.loads(result.stdout)["kind"] == "invalid_input"
    assert not (project / ".aa" / "runs").exists()


def test_bounded_leaf_agents_disable_the_assurance_tool(tmp_path: Path) -> None:
    from assurance_product.opencode_agents import bounded_agent_profiles, install_opencode_agents

    project = tmp_path / "project"
    project.mkdir()
    config_path, _plugin = install_opencode_agents(project)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    for profile in bounded_agent_profiles():
        assert document["agent"][profile]["tools"]["assurance"] is False


def _write_chain(
    project: Path,
    *,
    change_id: str,
    epoch: str,
    batch_id: str,
    plan_bytes: bytes,
    assessment: dict[str, object],
    omit_entry: bool = False,
) -> str:
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    plan_relative = f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json"
    plan_path = project / plan_relative
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(plan_bytes)
    batch = project / "qa" / "results" / "inspect" / "epochs" / epoch / "batches" / batch_id
    batch.mkdir(parents=True, exist_ok=True)
    assessment_relative = f"qa/results/inspect/epochs/{epoch}/batches/{batch_id}/obligation-assessment.json"
    encoded = (json.dumps(assessment) + "\n").encode("utf-8")
    (project / assessment_relative).write_bytes(encoded)
    entry = {
        "path": assessment_relative,
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }
    manifest = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "digest": "sha256:" + "ab" * 32,
        "entries": [] if omit_entry else [entry],
    }
    if omit_entry:
        manifest["entries"] = [{"path": "qa/results/other.json", "digest": "sha256:" + "cd" * 32}]
    (batch / "issue-evidence-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return plan_digest


def test_operator_assessment_reads_one_committed_chain_and_rejects_the_rest(
    cli_runner, tmp_path: Path
) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    plan_bytes = b'{"schema_version":"1","obligations":[]}\n'
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    plan_ref = {
        "path": f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json",
        "digest": plan_digest,
    }
    selected = {
        "schema_version": "1",
        "plan_ref": plan_ref,
        "rows": [
            {
                "plan_digest": plan_digest,
                "mrc_id": "MRC-1",
                "verdict": "refuted",
            }
        ],
    }
    other = {
        "schema_version": "1",
        "plan_ref": plan_ref,
        "rows": [{"plan_digest": plan_digest, "mrc_id": "MRC-2", "verdict": "supported"}],
    }
    _write_chain(
        project, change_id="BOOT-1", epoch="1", batch_id="batch-a", plan_bytes=plan_bytes, assessment=selected
    )
    _write_chain(
        project, change_id="BOOT-1", epoch="1", batch_id="batch-b", plan_bytes=plan_bytes, assessment=other
    )

    def read(epoch: str, batch: str, digest: str | None = None) -> dict[str, object]:
        result = cli_runner.invoke(
            app,
            [
                "operator",
                "assessment",
                "--json",
                "--project-dir",
                str(project),
                "--change-id",
                "BOOT-1",
                "--plan-digest",
                digest or plan_digest,
                "--coverage-epoch",
                epoch,
                "--batch-id",
                batch,
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert isinstance(body, dict)
        return body

    valid = read("1", "batch-a")
    assert valid["reason"] is None
    assessment = valid["assessment"]
    assert isinstance(assessment, dict)
    assert assessment["rows"][0]["verdict"] == "refuted"
    sibling = read("1", "batch-b")
    sibling_assessment = sibling["assessment"]
    assert isinstance(sibling_assessment, dict)
    assert sibling_assessment["rows"][0]["mrc_id"] == "MRC-2"
    assert read("2", "batch-a")["reason"] == "missing_ref"
    assert read("1", "batch-a", digest="ab" * 32)["reason"] == "plan_mismatch"

    changed = project / "qa/results/inspect/epochs/1/batches/batch-a/obligation-assessment.json"
    changed.write_text(changed.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert read("1", "batch-a")["reason"] == "assessment_changed"

    empty = _project(tmp_path / "manual")
    manual = cli_runner.invoke(
        app,
        [
            "operator",
            "assessment",
            "--json",
            "--project-dir",
            str(empty),
            "--change-id",
            "BOOT-1",
            "--plan-digest",
            plan_digest,
            "--coverage-epoch",
            "1",
            "--batch-id",
            "batch-a",
        ],
    )
    assert json.loads(manual.stdout)["reason"] == "not_assessed"


def test_operator_assessment_reports_a_missing_manifest_entry(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    plan_bytes = b'{"schema_version":"1"}\n'
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    assessment = {
        "schema_version": "1",
        "plan_ref": {
            "path": f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json",
            "digest": plan_digest,
        },
        "rows": [],
    }
    _write_chain(
        project,
        change_id="BOOT-1",
        epoch="1",
        batch_id="batch-a",
        plan_bytes=plan_bytes,
        assessment=assessment,
        omit_entry=True,
    )
    result = cli_runner.invoke(
        app,
        [
            "operator",
            "assessment",
            "--json",
            "--project-dir",
            str(project),
            "--change-id",
            "BOOT-1",
            "--plan-digest",
            plan_digest,
            "--coverage-epoch",
            "1",
            "--batch-id",
            "batch-a",
        ],
    )
    assert json.loads(result.stdout)["reason"] == "missing_ref"


def test_run_view_projects_journal_sessions_without_a_synthetic_attempts_file(tmp_path: Path) -> None:
    import asyncio

    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.operator_views import read_run_view
    from assurance_product.sqlite_attempt_store import SqliteAttemptJournal
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from assurance_product.task_records import define_task
    from graph_engine.attempts.events import ActivityBound, ActivityPrepared, AttemptOpened, AttemptTerminated
    from graph_engine.attempts.keys import AttemptKey

    project = _project(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    define_task(
        project_dir=project,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )
    change_id = "BOOT-journal"
    run_dir = task / ".aa" / "runs" / change_id
    run_dir.mkdir(parents=True)
    from assurance_product.bootstrap.status import write_run_manifest
    from assurance_product.task_records import read_task

    definition = read_task(task)
    assert definition is not None
    write_run_manifest(
        run_dir,
        {
            "change_id": change_id,
            "invocation_id": change_id,
            "task_id": definition.task_id,
            "task_directory": str(task.resolve()),
            "run_number": 1,
            "config_source": str((project / ".aa" / "config.yaml").resolve()),
            "requested_opencode_endpoint": "http://127.0.0.1:4096",
            "baseline": [],
        },
    )
    write_bootstrap_status(run_dir, BootstrapStatusV1(phase="terminal", change_id=change_id, exit_code=0))
    workspace = ChangeWorkspace.prepare(task, change_id)
    digest = "ab" * 32

    async def record() -> None:
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptJournal(backend)
            specs = (
                ("1" * 64, "intake", "a1", "try-1", {"session_id": "ses-a"}),
                ("2" * 64, "intake", "a2", "try-2", {"session_id": "ses-b"}),
                ("3" * 64, "execute", "a3", "try-1", None),
                ("4" * 64, "intake", "other", "try-9", {"session_id": "ses-other"}),
            )
            for key, node_id, activity_id, attempt_key, reference in specs:
                if reference is None:
                    events = (
                        AttemptOpened(
                            contract_digest=digest,
                            input_digest=digest,
                            graph_revision=digest,
                            invocation_id=change_id,
                            public_entrypoint="full",
                            semantic_node_id=node_id,
                        ),
                        ActivityPrepared(activity_id=activity_id),
                        ActivityBound(
                            activity_id=activity_id,
                            reference={"attempt_key": attempt_key},
                            reference_digest=digest,
                        ),
                        AttemptTerminated(resolution_kind="committed"),
                    )
                else:
                    invocation = "BOOT-other" if reference["session_id"] == "ses-other" else change_id
                    events = (
                        AttemptOpened(
                            contract_digest=digest,
                            input_digest=digest,
                            graph_revision=digest,
                            invocation_id=invocation,
                            public_entrypoint="full",
                            semantic_node_id=node_id,
                        ),
                        ActivityPrepared(activity_id=activity_id),
                        ActivityBound(
                            activity_id=activity_id,
                            reference={**reference, "attempt_key": attempt_key},
                            reference_digest=digest,
                        ),
                        AttemptTerminated(resolution_kind="committed"),
                    )
                await journal.append(
                    AttemptKey(digest=key),
                    events,  # type: ignore[arg-type]
                    expected_revision=0,
                    fencing_token=1,
                )

    asyncio.run(record())
    assert not (run_dir / "attempts.json").exists()
    view = read_run_view(task, change_id)
    assert [(node.node_id, node.activation_id, node.attempt_key, node.session_id) for node in view.nodes] == [
        ("intake", "a1", "try-1", "ses-a"),
        ("intake", "a2", "try-2", "ses-b"),
        ("execute", "a3", "try-1", None),
    ]
    assert view.nodes[2].execution_kind == "command"
    reopened = read_run_view(task, change_id)
    assert [(node.attempt_key, node.session_id) for node in reopened.nodes] == [
        ("try-1", "ses-a"),
        ("try-2", "ses-b"),
        ("try-1", None),
    ]
