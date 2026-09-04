from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.product.cli_support import SECRET_ENV, SECRET_VALUE, common_lifecycle_args, parse_json_output
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


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


def test_cli_system_wakeup_resume_fails_closed_without_a_pending_interrupt(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-sqlite-interrupt",
        entrypoint="improvement-evaluate",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    existing = _existing_args(project_dir, change_id, "inv-sqlite-interrupt", args)
    ran = cli_runner.invoke(app, ["run", *existing])
    assert ran.exit_code in {0, 20, 30, 40}, ran.output
    pending_status = cli_runner.invoke(app, ["status", *existing])
    assert pending_status.exit_code == 0, pending_status.output
    pending_document = parse_json_output(pending_status.stdout)
    assert pending_document["lock_digest"]
    assert isinstance(pending_document["graph_hierarchy"], list)
    resume_file = tmp_path / "resume.json"
    resume_file.write_text(
        json.dumps({"wakeup": {"reference_id": "wake-1"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resumed = cli_runner.invoke(app, ["resume", *existing, "--resume-file", str(resume_file)])
    assert resumed.exit_code == 40, resumed.output
    identity = InvocationIdentityRecord.model_validate_json(
        (
            project_dir
            / "qa"
            / "changes"
            / change_id
            / ".runtime"
            / "langgraph"
            / "identities"
            / "inv-sqlite-interrupt.json"
        ).read_bytes()
    )
    assert identity.entrypoint == "improvement-evaluate"
    assert "runtime" not in identity.model_dump(mode="json")


def test_cli_sqlite_status_reopen_keeps_the_current_identity(
    cli_runner, installed_sources, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.cli import app
    from assurance_product.invocation_identity import InvocationIdentityRecord
    from assurance_product.product import resolve_assurance_composition

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    args, project_dir, change_id = common_lifecycle_args(
        tmp_path=tmp_path,
        installed_sources=installed_sources,
        composition=composition,
        invocation_id="inv-sqlite-replay",
        entrypoint="improvement-evaluate",
    )
    started = cli_runner.invoke(app, ["start", *args])
    assert started.exit_code == 0, started.output
    existing = _existing_args(project_dir, change_id, "inv-sqlite-replay", args)
    first = cli_runner.invoke(app, ["status", *existing])
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(app, ["status", *existing])
    assert second.exit_code == 0, second.output
    assert parse_json_output(first.stdout)["lock_digest"] == parse_json_output(second.stdout)["lock_digest"]
    identity = InvocationIdentityRecord.model_validate_json(
        (
            project_dir
            / "qa"
            / "changes"
            / change_id
            / ".runtime"
            / "langgraph"
            / "identities"
            / "inv-sqlite-replay.json"
        ).read_bytes()
    )
    assert identity.product_lock_digest
    assert identity.revision_id
