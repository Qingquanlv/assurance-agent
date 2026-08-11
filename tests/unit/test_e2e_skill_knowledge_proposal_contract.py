"""Executable contract examples embedded in the E2E planning skill."""

import re

import yaml

from assurance_agent import resources
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal


def test_e2e_plan_canonical_delta_proposal_matches_runtime_schema() -> None:
    """Catch examples that teach agents a proposal shape promotion will reject."""
    skill = resources.read_text("skills", "aa-e2e-plan", "SKILL.md")
    match = re.search(
        r"### Canonical delta proposal\s+```yaml\s+(.*?)\s+```",
        skill,
        re.DOTALL,
    )

    assert match is not None, "aa-e2e-plan must provide a canonical delta proposal"
    proposal = DataKnowledgeProposal.model_validate(yaml.safe_load(match.group(1)))

    assert proposal.mode == "delta"
    assert proposal.based_on_l1_version == 1
    assert proposal.capabilities.adapters.e2e["role"]["make_role"].create_if_missing is False
