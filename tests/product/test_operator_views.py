from __future__ import annotations
from dataclasses import replace
from tests.attempt_checkpoints import checkpoint, completed_checkpoint
from graph_engine.attempts.checkpoint import AttemptPhase, AttemptResult
from graph_engine.canonical import canonical_digest

import json
from pathlib import Path

from assurance_product.bootstrap.contracts import BootstrapPhase, BootstrapStatusV1
from assurance_product.bootstrap.status import write_bootstrap_status, write_run_manifest
from assurance_product.task_records import define_task


def test_command_attempt_has_no_fabricated_session() -> None:
    from assurance_product.operator_views import NodeAttemptV1

    node = NodeAttemptV1(
        node_id="execute",
        activation_id="activation-1",
        attempt_key="attempt-1",
        order=4,
        label="Execute tests",
        execution_kind="command",
        state="completed",
        session_id=None,
        parent_session_id=None,
        outputs=(),
    )
    assert node.session_id is None
    assert node.attempt_key == "attempt-1"


def test_managed_operator_advertises_captured_historical_outputs() -> None:
    from assurance_product.operator_views import capabilities

    assert capabilities()["historical_outputs"] is True


def test_concurrent_attempt_projections_publish_complete_json(tmp_path: Path, monkeypatch) -> None:
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from assurance_product.operator_views import write_attempt_projection

    barrier = Barrier(2)
    replace = os.replace

    def simultaneous_replace(source, destination, *args, **kwargs):
        if Path(destination).name == "attempts.json":
            barrier.wait(timeout=5)
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", simultaneous_replace)
    records = [
        completed_checkpoint(
            terminal=AttemptResult(
                resolution_kind="committed", receipt_id="receipt", receipt_digest="f" * 64
            ),
            invocation_id="run",
            public_entrypoint="full",
            semantic_node_id="intake.intake",
        )
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(write_attempt_projection, tmp_path, records, "run") for _ in range(2)]
        for future in futures:
            future.result()
    rows = json.loads((tmp_path / "attempts.json").read_text())
    assert [(row["node_id"], row["state"]) for row in rows] == [("intake.intake", "completed")]


def test_terminal_run_view_survives_a_later_change_in_the_same_task(tmp_path: Path) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.operator_views import read_run_view

    task = _task(tmp_path)
    _run(task, change_id="BOOT-OLD", number=1, phase="terminal", exit_code=0, root=None)
    _run(task, change_id="BOOT-NEW", number=2, phase="terminal", exit_code=0, root=None)
    ChangeWorkspace.prepare(task, "BOOT-OLD")
    database = task / "qa/.runtime/langgraph/checkpoints.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    (task / ".aa/runs/BOOT-OLD/attempts.json").write_text(
        json.dumps(
            [
                {
                    "node_id": "intake.intake",
                    "activation_id": "old-activation",
                    "attempt_key": None,
                    "order": 1,
                    "label": "Intake",
                    "execution_kind": "agent",
                    "state": "completed",
                    "activity_reference": {"session_id": "old-session"},
                }
            ]
        )
    )
    (task / "qa/status.json").write_text(json.dumps({"change": {"change_id": "BOOT-NEW"}}))

    view = read_run_view(task, "BOOT-OLD")

    assert view.change_id == "BOOT-OLD"
    assert view.nodes[0].session_id == "old-session"


def test_managed_projection_write_failure_does_not_override_runtime_state(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import asyncio

    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.operator_views import publish_run_attempts
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer

    task = _task(tmp_path)
    _run(task, change_id="BOOT-DISPLAY", number=1, phase="terminal", exit_code=0, root=None)
    workspace = ChangeWorkspace.prepare(task, "BOOT-DISPLAY")

    async def initialize_journal() -> None:
        async with open_sqlite_checkpointer(workspace):
            pass

    asyncio.run(initialize_journal())

    def broken_projection(*args, **kwargs):
        raise OSError("display filesystem unavailable")

    monkeypatch.setattr("assurance_product.operator_views.write_attempt_projection", broken_projection)

    publish_run_attempts(task, "BOOT-DISPLAY")

    assert "display filesystem unavailable" in caplog.text


def test_history_rebuilds_stale_projection_from_journal_after_next_run(tmp_path: Path) -> None:
    import asyncio

    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.operator_views import read_run_view
    from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from graph_engine.attempts.keys import AttemptKey

    task = _task(tmp_path)
    _run(task, change_id="BOOT-OLD", number=1, phase="terminal", exit_code=0, root=None)
    _run(task, change_id="BOOT-NEW", number=2, phase="terminal", exit_code=0, root=None)
    workspace = ChangeWorkspace.prepare(task, "BOOT-NEW")

    async def record() -> None:
        async with open_sqlite_checkpointer(workspace) as backend:
            journal = SqliteAttemptCheckpointStore(backend)
            for change_id, digit in (("BOOT-OLD", "1"), ("BOOT-NEW", "2")):
                await backend.lease.acquire(change_id, owner_id="fixture")
                await journal.commit(
                    replace(
                        checkpoint(
                            AttemptKey(digest=digit * 64),
                            fencing_token=1,
                            contract_digest="b" * 64,
                            input_digest="c" * 64,
                            graph_revision="d" * 64,
                            invocation_id=change_id,
                            public_entrypoint="full",
                            semantic_node_id="intake.intake",
                        ),
                        fencing_token=1,
                        activity_id=AttemptKey(digest=digit * 64).digest,
                        phase=AttemptPhase.RELEASE,
                        activity_state="terminal_observed",
                        authorization_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        terminal=AttemptResult(
                            resolution_kind="committed", receipt_id="receipt", receipt_digest="f" * 64
                        ),
                        activity_outcome=None,
                        activity_outcome_digest=canonical_digest(None),
                        prepared_digest="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                        promotion_receipt_id="receipt",
                        promotion_receipt_digest="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                        promotion_staged_digest="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    ),
                    expected_revision=0,
                    fencing_token=1,
                )

    asyncio.run(record())
    (task / ".aa/runs/BOOT-OLD/attempts.json").write_text("[]")
    (task / "qa/status.json").write_text(json.dumps({"change": {"change_id": "BOOT-NEW"}}))

    view = read_run_view(task, "BOOT-OLD")

    assert [(node.activation_id, node.state) for node in view.nodes] == [("1" * 64, "completed")]


def test_repeated_node_keeps_distinct_attempt_keys(tmp_path: Path) -> None:
    from assurance_product.operator_views import read_run_view

    task = _task(tmp_path)
    _run(task, change_id="BOOT-A", number=1, phase="terminal", exit_code=0, root="ses_root_a")
    (task / ".aa" / "runs" / "BOOT-A" / "attempts.json").write_text(
        json.dumps(
            [
                {
                    "node_id": "case-design",
                    "activation_id": "activation-2",
                    "attempt_key": "attempt-2",
                    "order": 2,
                    "label": "Design cases",
                    "execution_kind": "agent",
                    "state": "completed",
                    "activity_reference": {"session_id": "ses_child_2", "parent_session_id": "ses_root_a"},
                },
                {
                    "node_id": "execute",
                    "activation_id": "activation-1",
                    "attempt_key": "attempt-1",
                    "order": 4,
                    "label": "Execute tests",
                    "execution_kind": "command",
                    "state": "completed",
                    "activity_reference": {"session_id": "must-not-stick", "parent_session_id": "ses_root_a"},
                },
                {
                    "node_id": "case-design",
                    "activation_id": "activation-1",
                    "attempt_key": "attempt-1",
                    "order": 2,
                    "label": "Design cases",
                    "execution_kind": "agent",
                    "state": "failed",
                    "activity_reference": {"session_id": "ses_child_1", "parent_session_id": "ses_root_a"},
                },
            ]
        ),
        encoding="utf-8",
    )
    view = read_run_view(task, "BOOT-A")
    command = next(node for node in view.nodes if node.execution_kind == "command")
    assert command.session_id is None
    keys = [node.attempt_key for node in view.nodes if node.node_id == "case-design"]
    assert keys == ["attempt-1", "attempt-2"]
    assert view.root_session_id == "ses_root_a"
    assert view.lifecycle == "completed"
    assert view.nodes[1].parent_session_id == "ses_root_a"


def test_missing_root_is_not_fabricated_and_legacy_run_is_an_issue(tmp_path: Path) -> None:
    from assurance_product.operator_views import read_run_view, read_task_history

    task = _task(tmp_path)
    _run(task, change_id="BOOT-A", number=1, phase="preparing", exit_code=None, root=None)
    view = read_run_view(task, "BOOT-A")
    assert view.root_session_id is None
    assert view.lifecycle == "preparing"
    assert view.stoppable is True
    legacy = task / ".aa" / "runs" / "legacy"
    legacy.mkdir()
    write_run_manifest(legacy, {"change_id": "legacy", "source_project_dir": str(tmp_path)})
    write_bootstrap_status(legacy, BootstrapStatusV1(phase="terminal", change_id="legacy", exit_code=0))
    history = read_task_history(task)
    assert [run.change_id for run in history.runs] == ["BOOT-A"]
    assert history.issues[0].change_id == "legacy"


def test_unknown_exit_is_stale_not_completed(tmp_path: Path) -> None:
    from assurance_product.operator_views import read_run_view

    task = _task(tmp_path)
    _run(task, change_id="BOOT-A", number=1, phase="terminal", exit_code=None, root="ses_root")
    view = read_run_view(task, "BOOT-A")
    assert view.lifecycle == "unknown"
    assert view.freshness == "stale"
    assert view.stoppable is False


def test_run_view_reads_journal_attempt_sessions_without_a_hand_written_file(tmp_path: Path) -> None:
    import asyncio

    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.operator_views import publish_run_attempts, read_run_view
    from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from graph_engine.attempts.keys import AttemptKey

    task = _task(tmp_path)
    change_id = "BOOT-J"
    _run(task, change_id=change_id, number=1, phase="terminal", exit_code=0, root=None)
    workspace = ChangeWorkspace.prepare(task, change_id)

    async def record() -> None:
        async with open_sqlite_checkpointer(workspace) as backend:
            store = SqliteAttemptCheckpointStore(backend)
            await backend.lease.acquire(change_id, owner_id="fixture")
            await backend.lease.acquire("OTHER", owner_id="fixture")
            for digit, node, activity, reference, committed in (
                (
                    "1",
                    "intake",
                    "a1",
                    {"session_id": "ses-a", "parent_session_id": None, "attempt_key": "try-1"},
                    True,
                ),
                ("2", "intake", "a2", {"session_id": "ses-b", "attempt_key": "try-2"}, False),
                ("3", "execute", "a3", {"attempt_key": "try-1"}, False),
                ("4", "intake", "a9", {"session_id": "ses-other"}, False),
            ):
                key = AttemptKey(digest=digit * 64)
                data = dict(
                    invocation_id="OTHER" if digit == "4" else change_id,
                    semantic_node_id=node,
                    activity_id=activity,
                    activity_reference=reference,
                    activity_reference_digest=canonical_digest(dict(reference)),
                    fencing_token=1,
                    revision=0,
                    terminal_fencing_token=None,
                    terminal_revision=None,
                )
                if committed:
                    candidate = completed_checkpoint(
                        key,
                        terminal=AttemptResult(
                            resolution_kind="committed", receipt_id="receipt", receipt_digest="f" * 64
                        ),
                        **data,
                    )
                else:
                    data.pop("terminal_fencing_token")
                    data.pop("terminal_revision")
                    candidate = checkpoint(
                        key,
                        phase=AttemptPhase.RECONCILE,
                        authorization_id="e" * 64,
                        activity_state="bound",
                        activity_dispatch_fingerprint={"sent": True},
                        activity_dispatch_fingerprint_digest=canonical_digest({"sent": True}),
                        **data,
                    )
                await store.commit(candidate, expected_revision=0, fencing_token=1)

    asyncio.run(record())
    assert not (task / ".aa" / "runs" / change_id / "attempts.json").exists()
    from assurance_product.run_history import capture_attempt_files

    capture_attempt_files(
        task / ".aa" / "runs" / change_id,
        "1" * 64,
        (("qa/results/intake/result.json", b'{"intake":true}'),),
    )
    capture_attempt_files(
        task / ".aa" / "runs" / change_id,
        "2" * 64,
        (("qa/results/intake/result.json", b'{"intake":false}'),),
    )
    publish_run_attempts(task, change_id)
    view = read_run_view(task, change_id)
    assert [(node.node_id, node.activation_id, node.attempt_key, node.session_id) for node in view.nodes] == [
        ("intake", "a1", "try-1", "ses-a"),
        ("intake", "a2", "try-2", "ses-b"),
        ("execute", "a3", "try-1", None),
    ]
    assert [(ref.logical_path, ref.node_id) for ref in view.nodes[0].outputs] == [
        ("qa/results/intake/result.json", "intake")
    ]
    assert view.nodes[1].outputs == ()


def test_operator_output_preview_reads_only_preserved_attempt_bytes(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from assurance_product.cli import app
    from assurance_product.run_history import preserve_run_output

    task = _task(tmp_path)
    _run(task, change_id="BOOT-A", number=1, phase="terminal", exit_code=0, root=None)
    run_dir = task / ".aa" / "runs" / "BOOT-A"
    (run_dir / "attempts.json").write_text(
        json.dumps(
            [
                {
                    "node_id": "intake.case-design",
                    "activation_id": "activation-1",
                    "attempt_key": None,
                    "order": 1,
                    "label": "Case design",
                    "execution_kind": "agent",
                    "state": "completed",
                    "activity_reference": {"session_id": "ses_case"},
                }
            ]
        ),
        encoding="utf-8",
    )
    ref = preserve_run_output(
        run_dir=run_dir,
        change_id="BOOT-A",
        node_id="intake.case-design",
        activation_id="activation-1",
        attempt_key=None,
        kind="artifact",
        logical_path="qa/cases/user/case.yaml",
        content=b"case: first\n",
    )
    result = CliRunner().invoke(
        app,
        [
            "operator",
            "output",
            "--project-dir",
            str(task),
            "--run-id",
            "BOOT-A",
            "--output-id",
            ref.output_id,
            "--preview",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["output"]["output_id"] == ref.output_id
    assert data["preview"] == "case: first\n"
    assert data["truncated"] is False


def _task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
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
    return task


def _run(
    task: Path,
    *,
    change_id: str,
    number: int,
    phase: BootstrapPhase,
    exit_code: int | None,
    root: str | None,
) -> None:
    from assurance_product.task_records import read_task

    definition = read_task(task)
    assert definition is not None
    run_dir = task / ".aa" / "runs" / change_id
    run_dir.mkdir(parents=True)
    write_run_manifest(
        run_dir,
        {
            "change_id": change_id,
            "task_id": definition.task_id,
            "task_directory": str(task.resolve()),
            "run_number": number,
            "config_source": str(task / ".aa" / "config.yaml"),
            "requested_opencode_endpoint": "http://127.0.0.1:4096",
            "baseline": [],
        },
    )
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(phase=phase, change_id=change_id, exit_code=exit_code, root_session_id=root),
    )
