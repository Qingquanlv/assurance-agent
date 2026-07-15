from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from tests.unit.retro.archive_fixtures import make_archived_change


def test_retro_json_stdout_shape(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)
        result = runner.invoke(main, ["retro", "--retro-id", "retro-x", "--change", "CH-1", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["retro_id"] == "retro-x"
        assert payload["change_count"] == 1
        assert payload["signal_count"] >= 1
        assert (root / "qa" / "retro" / "retro-x" / "context.json").exists()


def test_retro_since_and_change_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["retro", "--since", "2026-01-01", "--change", "CH-1"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_retro_immutable_when_promotions_present(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[])
        retro_dir = root / "qa" / "retro" / "retro-locked"
        retro_dir.mkdir(parents=True)
        (retro_dir / "promotions.json").write_text("[]", encoding="utf-8")
        result = runner.invoke(main, ["retro", "--retro-id", "retro-locked", "--change", "CH-1"])
        assert result.exit_code != 0
        assert "immutable" in result.output


def test_retro_nightly_collect_success_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}], gate_pushbacks=1)
        agent = root / "fake-agent.sh"
        retro_glob = "qa/retro"
        agent.write_text(
            "#!/usr/bin/env bash\n"
            f'd="$(ls -1d {retro_glob}/retro-* | tail -1)"\n'
            'printf \'{"proposals":[{"id":"P-1","apply_kind":"memory_append",'
            '"body":"x","eval_suite":"s"}]}\' > "$d/proposals.json"\n'
            'printf "# summary\\n" > "$d/retro-summary.md"\n',
            encoding="utf-8",
        )
        os.chmod(agent, 0o755)
        result = runner.invoke(
            main,
            [
                "retro",
                "nightly",
                "collect",
                "--sut",
                str(root),
                "--retro-id",
                "retro-n",
                "--agent",
                f"bash {agent}",
                "--min-evidence",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (root / "qa" / "retro" / "retro-n" / "review-queue.md").exists()


def test_retro_nightly_collect_noop_exit_10(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        (root / "qa" / "archive").mkdir(parents=True)
        result = runner.invoke(
            main, ["retro", "nightly", "collect", "--sut", str(root), "--retro-id", "retro-empty"]
        )
        assert result.exit_code == 10


def test_retro_nightly_collect_dry_run_exit_0(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        root = Path(fs)
        make_archived_change(root, "CH-1", failures=[{"classification": "assertion"}])
        result = runner.invoke(
            main, ["retro", "nightly", "collect", "--sut", str(root), "--retro-id", "retro-dry", "--dry-run"]
        )
        assert result.exit_code == 0
        assert (root / "qa" / "retro" / "retro-dry" / "context.json").exists()
