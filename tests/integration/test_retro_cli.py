"""Integration tests for the reduced ``aa retro`` CLI (canonical graph only)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from click.testing import CliRunner
import pytest

from assurance_agent.cli import main
from tests.helpers_aa import write_aa_config


def test_retro_cli_has_no_per_run_lifecycle_commands() -> None:
    help_text = CliRunner().invoke(main, ["retro", "--help"]).output
    for old in (
        "promote",
        "complete",
        "apply",
        "rollback",
        "export-issues",
        "export-knowledge",
        "nightly",
        "proposals-for-change",
    ):
        assert old not in help_text


def test_retro_cli_help_exposes_window_options() -> None:
    help_text = CliRunner().invoke(main, ["retro", "--help"]).output
    for flag in (
        "--change",
        "--since",
        "--until",
        "--last",
        "--batch-manifest",
        "--retro-id",
        "--dry-run",
    ):
        assert flag in help_text


def test_retro_requires_explicit_window() -> None:
    result = CliRunner().invoke(main, ["retro", "--retro-id", "retro-no-window"])
    assert result.exit_code == 2
    assert "explicit Retro window" in result.output


def test_retro_since_and_change_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["retro", "--since", "2026-01-01", "--change", "CH-1"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_retro_dry_run_writes_current_run_context(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setenv("AA_RETRO_AGENT_CMD", shutil.which("true") or "true")
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        write_aa_config(root)
        (root / "qa" / "changes").mkdir(parents=True)
        (root / "qa" / "archive").mkdir(parents=True)
        (root / "qa" / "issues").mkdir(parents=True)
        result = runner.invoke(
            main,
            ["retro", "--retro-id", "retro-dry", "--dry-run", "--last", "1", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["retro_id"] == "retro-dry"
        context_path = root / "qa" / "retro" / "retro-dry" / "context.json"
        assert context_path.is_file()
        context = json.loads(context_path.read_text())
        assert context["schema_version"] == "3"
        assert context["dry_run"] is True
        assert all(item["status"] == "failed" for item in context["domain_status"].values())


def test_retro_show_reads_only_explicit_current_run() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        write_aa_config(root)
        retro_dir = root / "qa" / "retro" / "retro-show"
        retro_dir.mkdir(parents=True)
        context = {
            "schema_version": "2",
            "retro_id": "retro-show",
            "generated_at": "2026-07-26T00:00:00Z",
            "signal_count": 0,
        }
        (retro_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
        (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")
        # Sibling must not be required / scanned for show.
        sibling = root / "qa" / "retro" / "retro-other"
        sibling.mkdir(parents=True)
        (sibling / "context.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(main, ["retro", "show", "--retro-id", "retro-show", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["retro_id"] == "retro-show"
        assert "context" in payload
        assert "retro-other" not in result.output


def test_retro_batch_manifest_preserves_exact_members_and_reports_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("AA_RETRO_AGENT_CMD", shutil.which("true") or "true")
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        write_aa_config(root)
        manifest = root / "batch.json"
        manifest.write_text(
            json.dumps(
                {
                    "batch_id": "batch-1",
                    "status": "incomplete",
                    "members": [
                        {
                            "change_id": "CH-MISSING",
                            "execution_status": "failed",
                            "evidence_availability": "absent",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            main,
            [
                "retro",
                "--retro-id",
                "retro-batch",
                "--batch-manifest",
                str(manifest),
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["batch_id"] == "batch-1"
        assert payload["status"] in {"completed_with_gaps", "pending_reconcile"}
        window = json.loads((root / "qa/retro/retro-batch/window.json").read_text())
        assert window["change_ids"] == ["CH-MISSING"]


def test_schema_invalid_batch_manifest_enters_preflight_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("AA_RETRO_AGENT_CMD", shutil.which("true") or "true")
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        write_aa_config(root)
        manifest = root / "bad-batch.json"
        manifest.write_text('{"batch_id":"batch-1","members":[]}', encoding="utf-8")
        result = runner.invoke(
            main,
            [
                "retro",
                "--retro-id",
                "retro-invalid-batch",
                "--batch-manifest",
                str(manifest),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["status"] == "completed_with_gaps"
        assert payload["failure_ids"]
