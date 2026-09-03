from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.product.cli_support import SECRET_ENV, SECRET_VALUE, common_lifecycle_args, parse_json_output
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture(autouse=True)
def _reset_runtime_ports() -> None:
    yield
    try:
        from assurance_product.runtime_ports import ProductRuntimePorts

        ProductRuntimePorts.test_kernel_resolutions = None
        ProductRuntimePorts._last_scripted_committed = None
    except ImportError:
        return


def _existing_args(project_dir: Path, change_id: str, invocation_id: str, args: list[str]) -> list[str]:
    return [
        "--json",
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        "--product",
        args[args.index("--product") + 1],
        "--binding-dist",
        args[args.index("--binding-dist") + 1],
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        args[args.index("--binding-declaration") + 1],
        "--config-tree",
        args[args.index("--config-tree") + 1],
        "--secret",
        args[args.index("--secret") + 1],
    ]


def test_cli_sqlite_system_interrupt_survives_reopen_and_replays_ordinal(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts
    from graph_engine.attempts.events import (
        SystemInterruptCompletionCheckpointed,
        SystemInterruptIssuanceAnchored,
    )
    from graph_engine.attempts.resolutions import (
        CommittedTaskResult,
        PendingTaskResult,
        ReceiptRef,
        SystemReference,
    )

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-sqlite-interrupt",
        entrypoint="archive",
    )
    resolutions = [
        PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")),
        CommittedTaskResult(
            output={"status": "completed"},
            receipt=ReceiptRef(receipt_id="r1", receipt_digest="a" * 64),
        ),
    ]
    monkeypatch.setattr(ProductRuntimePorts, "test_kernel_resolutions", resolutions)
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    first = cli_runner.invoke(
        app, ["run", *_existing_args(project_dir, change_id, "inv-sqlite-interrupt", args)]
    )
    assert first.exit_code in {20, 30}, first.output
    assert parse_json_output(first.stdout)["status"] in {"blocked", "interrupted"}
    pending_status = cli_runner.invoke(
        app, ["status", *_existing_args(project_dir, change_id, "inv-sqlite-interrupt", args)]
    )
    assert pending_status.exit_code == 0, pending_status.output
    pending_document = parse_json_output(pending_status.stdout)
    assert pending_document["pending_interrupt"] is not None or pending_document["status"] in {
        "blocked",
        "interrupted",
    }
    assert isinstance(pending_document["graph_hierarchy"], list)
    assert isinstance(pending_document["adapter_evidence"], list)
    resume_file = tmp_path / "resume.json"
    resume_file.write_text(
        json.dumps({"wakeup": {"reference_id": "wake-1"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resumed = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(project_dir, change_id, "inv-sqlite-interrupt", args),
            "--resume-file",
            str(resume_file),
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    assert parse_json_output(resumed.stdout)["status"] == "completed"
    events = ProductRuntimePorts.last_journal_events()
    kinds = {type(event).__name__ for event in events}
    assert "SystemInterruptIssuanceAnchored" in kinds
    assert "SystemInterruptCompletionCheckpointed" in kinds
    assert any(isinstance(event, SystemInterruptIssuanceAnchored) for event in events)
    assert any(isinstance(event, SystemInterruptCompletionCheckpointed) for event in events)
    assert ProductRuntimePorts.last_active_generations() == 0


def test_cli_sqlite_completion_pending_write_replays_before_aput(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts
    from graph_engine.attempts.resolutions import (
        CommittedTaskResult,
        PendingTaskResult,
        ReceiptRef,
        SystemReference,
    )
    from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-sqlite-replay",
        entrypoint="archive",
    )
    monkeypatch.setattr(
        ProductRuntimePorts,
        "test_kernel_resolutions",
        [
            PendingTaskResult(wakeup=SystemReference(reference_id="wake-2")),
            CommittedTaskResult(
                output={"status": "completed"},
                receipt=ReceiptRef(receipt_id="r2", receipt_digest="b" * 64),
            ),
        ],
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    blocked = cli_runner.invoke(
        app, ["run", *_existing_args(project_dir, change_id, "inv-sqlite-replay", args)]
    )
    assert blocked.exit_code in {20, 30}, blocked.output
    original_aput = AnchoredCheckpointer.aput
    calls = {"count": 0}

    async def crash_before_second_aput(self, *aput_args, **aput_kwargs):
        calls["count"] += 1
        if calls["count"] >= 2:
            raise RuntimeError("crash after completion pending write")
        return await original_aput(self, *aput_args, **aput_kwargs)

    monkeypatch.setattr(AnchoredCheckpointer, "aput", crash_before_second_aput)
    resume_file = tmp_path / "resume-replay.json"
    resume_file.write_text(
        json.dumps({"wakeup": {"reference_id": "wake-2"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    crashed = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(project_dir, change_id, "inv-sqlite-replay", args),
            "--resume-file",
            str(resume_file),
        ],
    )
    assert crashed.exit_code == 40, crashed.output
    monkeypatch.setattr(AnchoredCheckpointer, "aput", original_aput)
    replayed = cli_runner.invoke(
        app,
        [
            "resume",
            *_existing_args(project_dir, change_id, "inv-sqlite-replay", args),
            "--resume-file",
            str(resume_file),
        ],
    )
    assert replayed.exit_code == 0, replayed.output
    assert ProductRuntimePorts.last_replayed_ordinals()
