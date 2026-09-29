from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

_L1 = """\
# canonical domain knowledge
version: 1
journeys:
  - dept_management_crud
entities: {}
"""


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    target = project / ".aa" / "data-knowledge.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(_L1, encoding="utf-8")
    return project


def test_help_exposes_knowledge_promote(cli_runner: CliRunner) -> None:
    from assurance_product.cli import app
    from tests.product.cli_support import command_names, nested_command_names

    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "knowledge" in command_names(result.stdout)
    assert nested_command_names(cli_runner, app, "knowledge") == {"promote"}


def test_promote_from_file_writes_l1(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    proposal = tmp_path / "delta.yaml"
    proposal.write_text(
        "schema_version: '1'\nmode: delta\nentities:\n  user:\n    required_fields: [username]\n",
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        [
            "knowledge",
            "promote",
            "--project-dir",
            str(project),
            "--from",
            str(proposal),
            "--yes",
            "--json",
        ],
    )
    loaded = YAML(typ="safe").load((project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert json.loads(result.stdout)["written"] is True
    assert loaded["entities"]["user"]["required_fields"] == ["username"]
    assert loaded["journeys"] == ["dept_management_crud"]


def test_promote_conflict_exits_30(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    (project / ".aa" / "data-knowledge.yaml").write_text(
        "version: 1\nentities:\n  user:\n    required_fields: [username]\n",
        encoding="utf-8",
    )
    proposal = tmp_path / "delta.yaml"
    proposal.write_text(
        "schema_version: '1'\nmode: delta\nentities:\n  user:\n    required_fields: [email]\n",
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        ["knowledge", "promote", "--project-dir", str(project), "--from", str(proposal), "--yes", "--json"],
    )

    assert result.exit_code == 30
    assert json.loads(result.stdout)["conflicts"] == ["entities.user"]
    assert (project / "qa" / "improvements" / "promote-conflicts.json").is_file()


def test_promote_from_ledger_improvement(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    project = _project(tmp_path)
    ledger = project / "qa" / "improvements" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "last_seq": 1,
                "improvements": {
                    "IMP-1": {
                        "improvement_id": "IMP-1",
                        "fingerprint": "f" * 64,
                        "kind": "domain_knowledge",
                        "delivery": "knowledge_delta",
                        "source_refs": {"problem_ids": ["PROB-1"]},
                        "target": ".aa/data-knowledge.yaml",
                        "rationale": "missing user",
                        "proposed_change": "add entities.user",
                        "knowledge_delta": {
                            "schema_version": "1",
                            "mode": "delta",
                            "entities": {"user": {"required_fields": ["username"]}},
                        },
                        "verification": {"success_criteria": "user leaf exists"},
                        "risk": "low",
                        "confidence": "high",
                        "state": "proposed",
                        "version": 1,
                        "proposed_by_retro_ids": ["retro-1"],
                        "last_event_id": "IMPEVT-1",
                    }
                },
                "by_fingerprint": {},
            }
        ),
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        [
            "knowledge",
            "promote",
            "--project-dir",
            str(project),
            "--improvement",
            "IMP-1",
            "--yes",
            "--json",
        ],
    )
    loaded = YAML(typ="safe").load((project / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert loaded["entities"]["user"]["required_fields"] == ["username"]
    assert json.loads(ledger.read_text(encoding="utf-8"))["improvements"]["IMP-1"]["state"] == "proposed"


def test_promote_requires_one_source(cli_runner: CliRunner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    result = cli_runner.invoke(
        app,
        ["knowledge", "promote", "--project-dir", str(_project(tmp_path)), "--yes", "--json"],
    )
    assert result.exit_code == 40
