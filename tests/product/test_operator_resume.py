from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from assurance_product.bootstrap.contracts import BootstrapStatusV1
from assurance_product.bootstrap.driver import resume_bootstrap
from assurance_product.bootstrap.status import (
    read_bootstrap_status,
    write_bootstrap_status,
    write_effective_spec,
    write_run_manifest,
    write_stop_request,
)
from tests.product.test_bootstrap_contracts import _spec
from tests.product.test_bootstrap_driver import _prepared


def _paused_run(tmp_path: Path, *, reason: str | None = None) -> Path:
    task = tmp_path / "task"
    task.mkdir()
    run_dir = task / ".aa" / "runs" / "BOOT-resume"
    write_effective_spec(run_dir, _spec())
    write_run_manifest(
        run_dir,
        {
            "project_dir": str(task),
            "task_directory": str(task),
            "change_id": "BOOT-resume",
            "ownership": "shared",
            "requested_opencode_endpoint": "http://127.0.0.1:4096",
        },
    )
    write_bootstrap_status(
        run_dir,
        BootstrapStatusV1(
            phase="terminal",
            change_id="BOOT-resume",
            exit_code=20 if reason else 30,
            status={
                "status": "blocked" if reason else "interrupted",
                "pending_interrupt": {"reason_category": reason or "approval_required"},
            },
        ),
    )
    return run_dir


def test_operator_human_resume_resolves_before_continuing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product.operator import resolve_graph_interrupt

    run_dir = _paused_run(tmp_path)
    snapshot = {"status": "interrupted"}

    def resume(**kwargs: object) -> tuple[object, str, int]:
        assert kwargs["action"] == "approve"
        assert kwargs["reason"] == "owner reviewed the cases"
        assert kwargs["stop_file"] == run_dir / "stop-request.json"
        assert "reuse_directory" not in kwargs
        snapshot["status"] = "running"
        return object(), "running", 0

    def run(**kwargs: object) -> tuple[object, str]:
        assert snapshot["status"] == "running"
        snapshot["status"] = "completed"
        return object(), "completed"

    monkeypatch.setattr("assurance_product.cli._resume_invocation", resume)
    monkeypatch.setattr("assurance_product.bootstrap.driver._default_run_invocation", run)
    monkeypatch.setattr("assurance_product.bootstrap.driver._default_start_invocation", lambda **kwargs: {})
    monkeypatch.setattr(
        "assurance_product.bootstrap.driver._default_read_status", lambda **kwargs: dict(snapshot)
    )
    monkeypatch.setattr(
        "assurance_product.bootstrap.composition.prepare_composition", lambda **kwargs: _prepared(run_dir)
    )
    monkeypatch.setattr("assurance_product.bootstrap.driver.wait_http_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr("assurance_product.bootstrap.driver.time.sleep", lambda seconds: None)

    status = resolve_graph_interrupt(
        run_dir=run_dir,
        action="approve",
        reason="owner reviewed the cases",
        environ={},
    )

    assert status.phase == "terminal"
    assert status.exit_code == 0
    assert status.status["status"] == "completed"
    assert read_bootstrap_status(run_dir).model_dump(mode="json") == status.model_dump(mode="json")


def test_restart_clears_a_confirmed_operator_pause(tmp_path: Path) -> None:
    run_dir = _paused_run(tmp_path, reason="operator_stop")
    write_stop_request(run_dir, change_id="BOOT-resume")

    status = resume_bootstrap(
        run_dir,
        environ={},
        prepare_composition=lambda **kwargs: _prepared(run_dir),
        start_invocation=lambda **kwargs: {},
        run_invocation=lambda **kwargs: (object(), "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda *args, **kwargs: None,
    )

    assert status.exit_code == 0
    assert not (run_dir / "stop-request.json").exists()


@pytest.mark.parametrize("known_snapshot", [True, False])
def test_restart_reconciles_an_unresolved_stop_before_clearing_it(
    tmp_path: Path, known_snapshot: bool
) -> None:
    run_dir = _paused_run(tmp_path, reason="effect_pending")
    prior = read_bootstrap_status(run_dir)
    snapshot = dict(prior.status) if known_snapshot else {}
    write_bootstrap_status(run_dir, prior.model_copy(update={"status": snapshot}))
    write_stop_request(run_dir, change_id="BOOT-resume")
    before = (run_dir / "stop-request.json").read_bytes()

    def reconcile(**kwargs: object) -> tuple[object, str]:
        assert kwargs["stop_file"] == run_dir / "stop-request.json"
        assert (run_dir / "stop-request.json").read_bytes() == before
        snapshot.update(
            status="blocked",
            pending_interrupt={"reason_category": "operator_stop"},
        )
        return object(), "blocked"

    restart = partial(
        resume_bootstrap,
        run_dir,
        environ={},
        prepare_composition=lambda **kwargs: _prepared(run_dir),
        start_invocation=lambda **kwargs: {},
        read_status=lambda **kwargs: dict(snapshot),
        wait_ready=lambda *args, **kwargs: None,
    )
    paused = restart(run_invocation=reconcile)

    assert paused.phase == "terminal"
    assert paused.exit_code == 20
    assert paused.status["pending_interrupt"] == {"reason_category": "operator_stop"}
    assert (run_dir / "stop-request.json").read_bytes() == before

    def continue_run(**kwargs: object) -> tuple[object, str]:
        assert not (run_dir / "stop-request.json").exists()
        snapshot.update(status="completed", pending_interrupt=None)
        return object(), "completed"

    completed = restart(run_invocation=continue_run)
    assert completed.exit_code == 0
    assert completed.status["status"] == "completed"


def test_invalid_human_action_leaves_a_terminal_run_that_can_be_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _paused_run(tmp_path)
    snapshot = dict(read_bootstrap_status(run_dir).status)

    def resume(**kwargs: object) -> tuple[object, str, int]:
        if kwargs["action"] != "approve":
            raise ValueError("action is not allowed")
        snapshot["status"] = "completed"
        return object(), "completed", 0

    monkeypatch.setattr("assurance_product.cli._resume_invocation", resume)
    ports = {
        "environ": {},
        "prepare_composition": lambda **kwargs: _prepared(run_dir),
        "start_invocation": lambda **kwargs: {},
        "read_status": lambda **kwargs: dict(snapshot),
        "wait_ready": lambda *args, **kwargs: None,
    }
    with pytest.raises(ValueError, match="not allowed"):
        resume_bootstrap(run_dir, action="invalid", reason="owner decision", **ports)

    failed = read_bootstrap_status(run_dir)
    assert failed.phase == "terminal"
    assert failed.exit_code == 40
    assert failed.status["status"] == "interrupted"
    assert failed.status["pending_interrupt"] == {"reason_category": "approval_required"}

    recovered = resume_bootstrap(run_dir, action="approve", reason="owner decision", **ports)
    assert recovered.phase == "terminal"
    assert recovered.exit_code == 0


@pytest.mark.parametrize("graph_status", ["interrupted", "completed"])
def test_resume_acknowledges_a_stop_that_arrived_at_a_quiescent_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, graph_status: str
) -> None:
    run_dir = _paused_run(tmp_path)
    prior = read_bootstrap_status(run_dir)
    write_bootstrap_status(
        run_dir,
        prior.model_copy(update={"status": {**prior.status, "status": graph_status}}),
    )
    write_stop_request(run_dir, change_id="BOOT-resume")
    monkeypatch.setattr(
        "assurance_product.cli._resume_invocation", lambda **kwargs: (object(), "completed", 0)
    )

    status = resume_bootstrap(
        run_dir,
        environ={},
        action="approve" if graph_status == "interrupted" else None,
        reason="owner decision" if graph_status == "interrupted" else None,
        prepare_composition=lambda **kwargs: _prepared(run_dir),
        start_invocation=lambda **kwargs: {},
        run_invocation=lambda **kwargs: (object(), "completed"),
        read_status=lambda **kwargs: {"status": "completed"},
        wait_ready=lambda *args, **kwargs: None,
    )

    assert status.exit_code == 0
    assert not (run_dir / "stop-request.json").exists()
