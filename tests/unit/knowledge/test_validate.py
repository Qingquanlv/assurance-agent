from pathlib import Path

from click.testing import CliRunner

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge, DataKnowledgeProposal
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.artifacts.repo_registry import resolve_repo_model
from assurance_agent.cli import main
from assurance_agent.knowledge.validate import validate_data_knowledge, validate_knowledge, validate_proposal
from tests.helpers_aa import write_aa_config

FIXTURES = Path(__file__).resolve().parents[1] / "artifacts" / "fixtures" / "data_knowledge"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_validate_data_knowledge_accepts_canonical_l1(tmp_path: Path) -> None:
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    result = validate_data_knowledge(tmp_path)
    assert result.ok is True
    assert result.artifact_type == "data_knowledge"


def test_validate_data_knowledge_rejects_malformed_l1_with_remediation(tmp_path: Path) -> None:
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_invalid.yaml").read_text(encoding="utf-8"))
    result = validate_data_knowledge(tmp_path)
    assert result.ok is False
    assert any("version" in err for err in result.errors)
    assert any("remediation" in err for err in result.errors)


def test_validate_proposal_accepts_per_layer_file(tmp_path: Path) -> None:
    proposal = tmp_path / "plans" / "data-knowledge.proposal.api.yaml"
    proposal.parent.mkdir(parents=True)
    proposal.write_text((FIXTURES / "l2_valid.api.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    result = validate_proposal(proposal, rel="plans/data-knowledge.proposal.api.yaml")
    assert result.ok is True


def test_validate_knowledge_with_change_collects_layer_proposals(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _write(
        change_dir,
        "plans/data-knowledge.proposal.api.yaml",
        (FIXTURES / "l2_valid.api.yaml").read_text(encoding="utf-8"),
    )
    report = validate_knowledge(tmp_path, change_id="CH-1")
    assert report.ok is True
    assert len(report.results) == 2
    assert report.results[1].path == "plans/data-knowledge.proposal.api.yaml"


def test_registry_resolution_helpers() -> None:
    assert resolve_repo_model(".aa/data-knowledge.yaml") is DataKnowledge
    spec = match_artifact("plans/data-knowledge.proposal.api.yaml")
    assert spec is not None
    assert spec.model is DataKnowledgeProposal


def test_cli_validate_success(tmp_path: Path) -> None:
    _write(tmp_path, ".aa/data-knowledge.yaml", (FIXTURES / "l1_valid.yaml").read_text(encoding="utf-8"))
    runner = CliRunner()
    result = runner.invoke(main, ["knowledge", "validate", "--project-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"ok": true' in result.output


def test_validate_rejects_unknown_auth_matrix_token(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".aa/data-knowledge.yaml",
        """
version: 1
accounts: {}
auth:
  api_admin_token:
    method: token
entities: {}
auth_matrix:
  ghost_cell:
    route: /api/v1/api/list
    method: GET
    token: ghost_token
    expected: allow
    allowed_status_codes: [200]
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
""".lstrip(),
    )
    result = validate_data_knowledge(tmp_path)
    assert result.ok is False
    assert any("unknown auth key" in err for err in result.errors)


def test_validate_proposal_accepts_parameterized_max_length(tmp_path: Path) -> None:
    proposal = tmp_path / "plans" / "data-knowledge.proposal.api.yaml"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        """
schema_version: "1"
based_on_l1_version: 1
mode: delta
entities:
  dept:
    constraints:
      name_unique: true
      name_has_max_length: 20
    required_fields: [name]
discovered_candidates: []
needs_review: []
promotion_checklist: []
""".lstrip(),
        encoding="utf-8",
    )
    result = validate_proposal(proposal, rel="plans/data-knowledge.proposal.api.yaml")
    assert result.ok is True


def test_validate_proposal_rejects_boolean_max_length(tmp_path: Path) -> None:
    proposal = tmp_path / "plans" / "data-knowledge.proposal.api.yaml"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        """
schema_version: "1"
mode: delta
entities:
  dept:
    constraints:
      name_has_max_length: true
discovered_candidates: []
needs_review: []
promotion_checklist: []
""".lstrip(),
        encoding="utf-8",
    )
    result = validate_proposal(proposal, rel="plans/data-knowledge.proposal.api.yaml")
    assert result.ok is False
    assert any("max_length" in err for err in result.errors)
