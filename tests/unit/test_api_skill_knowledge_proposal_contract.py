"""Executable contract examples embedded in the API planning skill."""

import re

import yaml

from assurance_agent import resources
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal


def test_api_plan_canonical_delta_proposal_matches_runtime_schema() -> None:
    """Catch examples that teach agents a proposal shape promotion will reject."""
    skill = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    match = re.search(
        r"### Canonical delta proposal\s+```yaml\s+(.*?)\s+```",
        skill,
        re.DOTALL,
    )

    assert match is not None, "aa-api-plan must provide a canonical delta proposal"
    proposal = DataKnowledgeProposal.model_validate(yaml.safe_load(match.group(1)))

    assert proposal.mode == "delta"
    assert proposal.based_on_l1_version == 1
    assert proposal.entities["user"].required_fields == ["username"]
