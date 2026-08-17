import base64
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main


def test_artifact_write_materializes_utf8_content() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        content = "title: 部门管理\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        result = runner.invoke(
            main,
            [
                "artifact",
                "write",
                "--path",
                "qa/changes/CH-1/cases/case.yaml",
                "--project-dir",
                ".",
                "--payload-base64",
                encoded,
            ],
        )

        assert result.exit_code == 0, result.output
        assert Path("qa/changes/CH-1/cases/case.yaml").read_text(encoding="utf-8") == content


def test_artifact_write_accepts_literal_content() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        content = '{"schema_version":"1.0","decision":"pass"}\n'

        result = runner.invoke(
            main,
            [
                "artifact",
                "write",
                "--path",
                "qa/changes/CH-1/review/case-review.json",
                "--content",
                content,
            ],
        )

        assert result.exit_code == 0, result.output
        assert Path("qa/changes/CH-1/review/case-review.json").read_text() == content


def test_artifact_write_requires_exactly_one_content_source() -> None:
    runner = CliRunner()

    missing = runner.invoke(main, ["artifact", "write", "--path", "artifact.txt"])
    both = runner.invoke(
        main,
        [
            "artifact",
            "write",
            "--path",
            "artifact.txt",
            "--payload-base64",
            "eA==",
            "--content",
            "x",
        ],
    )

    assert missing.exit_code == 1
    assert both.exit_code == 1
    assert "exactly one" in missing.output
    assert "exactly one" in both.output


def test_artifact_write_rejects_workflow_state() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        encoded = base64.b64encode(b"state: forged\n").decode("ascii")

        result = runner.invoke(
            main,
            [
                "artifact",
                "write",
                "--path",
                "qa/changes/CH-1/workflow-state.json",
                "--payload-base64",
                encoded,
            ],
        )

        assert result.exit_code == 1
        assert "orchestrator-owned" in result.output
        assert not Path("qa/changes/CH-1/workflow-state.json").exists()


def test_artifact_write_rejects_path_outside_project() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        encoded = base64.b64encode(b"nope\n").decode("ascii")

        result = runner.invoke(
            main,
            ["artifact", "write", "--path", "../outside.txt", "--payload-base64", encoded],
        )

        assert result.exit_code == 1
        assert "escapes project root" in result.output
