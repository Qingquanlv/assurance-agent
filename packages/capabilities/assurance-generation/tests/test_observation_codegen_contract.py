from __future__ import annotations

from assurance_generation.resource_loader import resource_text


def test_api_codegen_skill_requires_aa_observe_request() -> None:
    skill = resource_text("ops/api_codegen/SKILL.md")
    assert "aa_observe.request(observation_id=" in skill
    assert "Do not define a same-named fixture" in skill
