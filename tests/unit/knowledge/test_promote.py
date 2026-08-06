from pathlib import Path

import yaml
from click.testing import CliRunner

from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.cli import main
from assurance_agent.knowledge.extract_constraints import extract_entity_constraints_from_source
from assurance_agent.knowledge.promote import KnowledgePromoteError, promote_knowledge
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


def test_promote_extract_proposal_requires_human_yes(tmp_path: Path) -> None:
    """Cold-start proposal must not land in L1 without explicit human --yes."""
    write_aa_config(tmp_path)
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    change_dir = tmp_path / "qa" / "changes" / "CH-EXTRACT"
    source = (
        "from tortoise import fields\nclass Dept:\n    name = fields.CharField(max_length=20, unique=True)\n"
    )
    proposal = extract_entity_constraints_from_source(source, based_on_l1_version=1)
    assert isinstance(proposal, DataKnowledgeProposal)
    assert "dept" in proposal.entities

    proposal_path = change_dir / "plans" / "data-knowledge.proposal.api.yaml"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        yaml.safe_dump(proposal.model_dump(mode="python"), sort_keys=False),
        encoding="utf-8",
    )

    try:
        promote_knowledge(tmp_path, change_id="CH-EXTRACT", yes=False)
    except KnowledgePromoteError as err:
        assert "--yes" in str(err)
    else:
        raise AssertionError("expected human-approval gate before L1 mutation")

    assert "name_has_max_length" not in (tmp_path / ".aa/data-knowledge.yaml").read_text(encoding="utf-8")

    outcome = promote_knowledge(tmp_path, change_id="CH-EXTRACT", yes=True)
    assert outcome.changed is True
    assert "entities.dept" in outcome.merged_keys
    text = (tmp_path / ".aa/data-knowledge.yaml").read_text(encoding="utf-8")
    assert "name_has_max_length" in text
