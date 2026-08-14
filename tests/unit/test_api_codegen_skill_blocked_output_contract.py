"""Executable graph-authority contract embedded in the API codegen skill."""

from assurance_agent import resources


def test_api_codegen_defers_progression_to_graph_gate() -> None:
    """Accepted-risk review facts must not make codegen emit an empty manifest."""
    skill = resources.read_text("skills", "aa-api-codegen", "SKILL.md")

    assert "The graph gate is the progression authority" in skill
    assert "Do not independently block codegen" in skill
    assert "an empty `files` array" in skill
    assert "invalid" in skill
    assert "### Blocked gate output" not in skill
