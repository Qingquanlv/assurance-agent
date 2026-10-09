from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_engine.canonical import JSONValue, canonical_digest

from tests.product.test_achieved_terminal import (
    CHANGE_ID,
    _ready_change,
    _status_snapshot,
)


from graph_engine.attempts.orchestration.checkpoint import AttemptResult
from tests.attempt_checkpoints import completed_checkpoint


def _committed():
    return AttemptResult(
        resolution_kind="committed",
        output={"status": "completed"},
        receipt_id="receipt-committed",
        receipt_digest="f" * 64,
    )


def _permanent_failure():
    return AttemptResult(resolution_kind="permanent", failure_kind="invalid_output", message="bad output")


def _completed(node: str, *, invocation_id: str = "inv-terminal-full-001", terminal=None, **data):
    return completed_checkpoint(
        terminal=terminal or _committed(),
        invocation_id=invocation_id,
        semantic_node_id=node,
        public_entrypoint="full",
        **data,
    )


def test_terminal_status_projects_only_committed_attempts_as_completed_steps(tmp_path: Path) -> None:
    from assurance_product.status import render_status_from_langgraph

    status = render_status_from_langgraph(
        invocation_id="inv-terminal-full-001",
        lock_digest="a" * 64,
        root_input_digest="b" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="completed",
        snapshot=SimpleNamespace(
            next=(),
            interrupts=(),
            values={
                "terminal": {"status": "completed", "reason": "achieved"},
                "selected_test_families": ["api"],
            },
        ),
        attempt_checkpoints=(
            _completed("quality.report", invocation_id="inv-other"),
            _completed("intake.intake"),
            _completed("quality.report"),
            _completed("intake.intake"),
            _completed("quality.inspect", terminal=_permanent_failure()),
        ),
        project_root=tmp_path,
    )

    assert {node.node_id for node in status.node_states if node.state in {"succeeded", "stopped"}} == {
        "intake.intake/finalize",
        "quality.report/finalize",
    }


def test_terminal_status_does_not_treat_intake_review_rounds_as_coverage_progress(tmp_path: Path) -> None:
    from assurance_product.status import render_status_from_langgraph

    status = render_status_from_langgraph(
        invocation_id="inv-terminal-full-001",
        lock_digest="a" * 64,
        root_input_digest="b" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="completed",
        snapshot=SimpleNamespace(
            next=(),
            interrupts=(),
            values={
                "rounds_used": 2,
                "rounds_budget": 2,
                "terminal": {"status": "stopped", "reason": "not_achieved"},
            },
        ),
        project_root=tmp_path,
    )

    assert status.coverage_progress is None
    assert status.terminal_reason == "not_achieved"
    assert status.change.state == "stopped"


def test_terminal_status_rejects_quality_coverage_without_round_progress(tmp_path: Path) -> None:
    from assurance_product.status import render_status_from_langgraph

    project = _ready_change(tmp_path)
    with pytest.raises(ValueError, match="coverage progress is incomplete"):
        render_status_from_langgraph(
            invocation_id="inv-terminal-full-001",
            lock_digest="a" * 64,
            root_input_digest="b" * 64,
            entrypoint="full",
            change_id=CHANGE_ID,
            status="completed",
            snapshot=_status_snapshot(project, extra={"budgets": {}}),
            project_root=project,
        )


def test_status_projects_the_bound_opencode_session_as_adapter_evidence(tmp_path: Path) -> None:

    from assurance_product.status import render_status_from_langgraph

    reference: dict[str, JSONValue] = {
        "adapter_version": "0.1.0",
        "expected_message_id": "msg-terminal",
        "session_id": "ses_terminal_001",
    }
    reference_digest = canonical_digest(reference)
    status = render_status_from_langgraph(
        invocation_id="inv-terminal-full-001",
        lock_digest="a" * 64,
        root_input_digest="b" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="running",
        snapshot=SimpleNamespace(next=("execute",), interrupts=(), values={}),
        attempt_checkpoints=(
            _completed(
                "intake.explore",
                invocation_id="inv-other",
                activity_id="activity-other",
                activity_reference={**reference, "session_id": "ses_other"},
                activity_reference_digest=canonical_digest({**reference, "session_id": "ses_other"}),
            ),
            _completed(
                "intake.intake",
                activity_id="activity-terminal-001",
                activity_reference=reference,
                activity_reference_digest=reference_digest,
            ),
        ),
        project_root=tmp_path,
    )

    assert tuple(item.model_dump(mode="json") for item in status.adapter_evidence) == (
        {
            "activation_id": "activity-terminal-001",
            "activity_id": "ses_terminal_001",
            "reference_digest": reference_digest,
            "terminal_receipt_digest": None,
        },
    )


def test_completed_full_run_fails_closed_without_achieved_terminal_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assurance_product import application as application_module
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.invocation_identity import (
        InvocationIdentityRecord,
        RuntimeSelectionError,
    )

    project = tmp_path / "project"
    (project / "qa").mkdir(parents=True)
    workspace = ChangeWorkspace.open(project.resolve(), CHANGE_ID)
    workspace.initialize()
    identity = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-terminal-missing-001",
        entrypoint="full",
        root_input_digest="b" * 64,
        product_lock_digest="a" * 64,
        revision_id="c" * 64,
    )
    application = AssuranceProductApplication()
    monkeypatch.setattr(application_module, "load_identity", lambda *_args: identity)
    monkeypatch.setattr(application, "_resolve_existing", lambda *_args, **_kwargs: identity)

    async def completed_run(**_kwargs: object) -> str:
        return "completed"

    async def incomplete_terminal_status(*_args: object, **_kwargs: object):
        return "completed", SimpleNamespace(next=(), interrupts=(), values={}), ()

    monkeypatch.setattr(application, "_run_langgraph", completed_run)
    monkeypatch.setattr(application, "_status_langgraph", incomplete_terminal_status)

    with pytest.raises(RuntimeSelectionError, match="terminal full checkpoint"):
        application.run(
            project_dir=project,
            change_id=CHANGE_ID,
            invocation_id=identity.invocation_id,
            composition=object(),
            authorization=object(),  # type: ignore[arg-type]
            entrypoint=None,
            input_path=None,
            workspace=workspace,
            secrets=(),
        )


def test_run_terminalizes_achieved_full_from_its_terminal_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assurance_product import application as application_module
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.invocation_identity import InvocationIdentityRecord

    project = _ready_change(tmp_path)
    workspace = ChangeWorkspace.open(project.resolve(), CHANGE_ID)
    workspace.initialize()
    snapshot = _status_snapshot(project)
    identity = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-terminal-full-001",
        entrypoint="full",
        root_input_digest=canonical_digest({"root": "input"}),
        product_lock_digest="a" * 64,
        revision_id="b" * 64,
    )
    application = AssuranceProductApplication()

    monkeypatch.setattr(application_module, "load_identity", lambda *_args: identity)
    monkeypatch.setattr(application, "_resolve_existing", lambda *_args, **_kwargs: identity)

    async def completed_run(**_kwargs: object) -> str:
        return "completed"

    async def terminal_status(*_args: object, **_kwargs: object):
        return "completed", snapshot, ()

    monkeypatch.setattr(application, "_run_langgraph", completed_run)
    monkeypatch.setattr(application, "_status_langgraph", terminal_status)

    result, mapped, code = application.run(
        project_dir=project,
        change_id=CHANGE_ID,
        invocation_id=identity.invocation_id,
        composition=object(),
        authorization=object(),  # type: ignore[arg-type]
        entrypoint=None,
        input_path=None,
        workspace=workspace,
        secrets=(),
    )

    assert (workspace.paths.qa_root / "status.json").is_file()
    assert not (workspace.paths.qa_root / "apply-manifest.json").exists()
    persisted = json.loads((workspace.paths.qa_root / "status.json").read_text(encoding="utf-8"))
    assert persisted["change"] == {"change_id": CHANGE_ID, "state": "achieved"}
    assert persisted["selected_test_families"] == ["api"]
    assert persisted["publication"]["status"] == "not_ready"
    assert result.status == mapped == "completed"
    assert code == 0
    before = (workspace.paths.qa_root / "status.json").read_bytes()

    application.run(
        project_dir=project,
        change_id=CHANGE_ID,
        invocation_id=identity.invocation_id,
        composition=object(),
        authorization=object(),  # type: ignore[arg-type]
        entrypoint=None,
        input_path=None,
        workspace=workspace,
        secrets=(),
    )

    assert before == (workspace.paths.qa_root / "status.json").read_bytes()
    assert not (workspace.paths.qa_root / "apply-manifest.json").exists()


def test_status_returns_the_authenticated_persisted_full_terminal_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.models import StatusV1
    from tests.product.test_achieved_terminal import valid_status

    project = _ready_change(tmp_path)
    workspace = ChangeWorkspace.open(project.resolve(), CHANGE_ID)
    workspace.initialize()
    identity = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-persisted-terminal-001",
        entrypoint="full",
        root_input_digest="b" * 64,
        product_lock_digest="a" * 64,
        revision_id="c" * 64,
    )
    persisted = StatusV1.model_validate(
        valid_status(
            invocation_id=identity.invocation_id,
            lock_digest=identity.product_lock_digest,
            root_input_digest=identity.root_input_digest,
            apply={"manifest_digest": "d" * 64, "file_count": 1},
            publication={"status": "published"},
        )
    )
    (workspace.paths.qa_root / "status.json").write_text(
        persisted.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    snapshot = SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "terminal": {"status": "completed", "reason": "achieved"},
            "selected_test_families": ["api"],
        },
    )
    application = AssuranceProductApplication()
    monkeypatch.setattr(application, "_resolve_existing", lambda *_args, **_kwargs: identity)

    async def terminal_status(*_args: object, **_kwargs: object):
        return "completed", snapshot, ()

    monkeypatch.setattr(application, "_status_langgraph", terminal_status)

    status = application.status(
        workspace=workspace,
        composition=object(),
        authorization=object(),  # type: ignore[arg-type]
        invocation_id=identity.invocation_id,
        change_id=CHANGE_ID,
    )

    assert status == persisted
    assert status.apply.manifest_digest == "d" * 64
    assert status.publication.status == "published"


def test_resume_uses_the_same_achieved_terminalization_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assurance_product import application as application_module
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.models import StatusV1

    project = tmp_path / "project"
    (project / "qa").mkdir(parents=True)
    workspace = ChangeWorkspace.open(project.resolve(), CHANGE_ID)
    workspace.initialize()
    identity = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-resumed-terminal-001",
        entrypoint="full",
        root_input_digest="b" * 64,
        product_lock_digest="a" * 64,
        revision_id="c" * 64,
    )
    snapshot = SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "terminal": {"status": "completed", "reason": "achieved"},
            "selected_test_families": ["api", "e2e"],
        },
    )
    application = AssuranceProductApplication()
    monkeypatch.setattr(application, "_resolve_existing", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(application_module, "_assert_langgraph_revision", lambda *_args: None)

    async def completed_resume(**_kwargs: object) -> str:
        return "completed"

    async def terminal_status(*_args: object, **_kwargs: object):
        return "completed", snapshot, ()

    finalized: list[tuple[str, tuple[str, ...], StatusV1]] = []

    def record_finalization(
        _project: Path,
        change_id: str,
        families: tuple[str, ...],
        *,
        invocation: StatusV1,
    ) -> StatusV1:
        finalized.append((change_id, families, invocation))
        return invocation

    monkeypatch.setattr(application, "_resume_langgraph", completed_resume)
    monkeypatch.setattr(application, "_status_langgraph", terminal_status)
    monkeypatch.setattr(application_module, "finalize_achieved", record_finalization)

    result, mapped, code = application.resume(
        workspace=workspace,
        composition=object(),
        authorization=object(),  # type: ignore[arg-type]
        invocation_id=identity.invocation_id,
        action="approve",
        reason=None,
        resume_file=None,
    )

    assert (result.status, mapped, code) == ("completed", "completed", 0)
    assert len(finalized) == 1
    assert finalized[0][0:2] == (CHANGE_ID, ("api", "e2e"))
    assert finalized[0][2].entrypoint == "full"
