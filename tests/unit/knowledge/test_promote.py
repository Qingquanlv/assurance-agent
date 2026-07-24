from pathlib import Path

import yaml
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.knowledge.promote import promote_knowledge
from tests.helpers_aa import write_aa_config

FIXTURES = Path(__file__).resolve().parents[1] / "artifacts" / "fixtures" / "data_knowledge"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_promote_merges_new_leaf_and_preserves_header_comment(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    header = "# keep this comment block\n"
    _write(
        tmp_path,
        ".aa/data-knowledge.yaml",
        header + (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"),
    )
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    proposal = yaml.safe_load((FIXTURES / "l2_valid.api.yaml").read_text(encoding="utf-8"))
    proposal["auth"] = {
        "api_admin_token": {
            "method": "token",
            "symbol": "tests.api.conftest.admin_token",
        }
    }
    _write(change_dir, "plans/data-knowledge.proposal.api.yaml", yaml.safe_dump(proposal, sort_keys=False))

    outcome = promote_knowledge(tmp_path, change_id="CH-1", yes=True)
    assert outcome.changed is True
    text = (tmp_path / ".aa/data-knowledge.yaml").read_text(encoding="utf-8")
    assert text.startswith("# keep this comment block")
    assert "api_admin_token" in text


def test_promote_without_yes_is_blocked(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    proposal = yaml.safe_load((FIXTURES / "l2_valid.api.yaml").read_text(encoding="utf-8"))
    proposal["auth"] = {
        "api_admin_token": {
            "method": "token",
            "symbol": "tests.api.conftest.admin_token",
        }
    }
    _write(change_dir, "plans/data-knowledge.proposal.api.yaml", yaml.safe_dump(proposal, sort_keys=False))
    try:
        promote_knowledge(tmp_path, change_id="CH-1", yes=False)
    except Exception as err:
        assert "--yes" in str(err)
    else:
        raise AssertionError("expected promotion to require --yes")


def test_cli_promote_noop(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["knowledge", "promote", "--project-dir", str(tmp_path), "--change", "CH-1", "--yes"],
    )
    assert result.exit_code == 1 or "no proposal" in result.output.lower()
