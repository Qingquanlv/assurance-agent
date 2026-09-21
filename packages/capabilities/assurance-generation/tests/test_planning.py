from __future__ import annotations

import pytest

from assurance_generation.operations.planning import plan_outputs, plan_review_outputs
from assurance_generation.resource_loader import resource_text
from planning_fixtures import FAMILIES  # pyright: ignore[reportMissingImports]


def test_e2e_codegen_skill_reentry_reads_family_prefixed_review() -> None:
    skill = resource_text("skills/aa-e2e-codegen-reviewer/SKILL.md")
    assert "review/e2e-codegen-review.json" in skill
    assert "review/plan-review.json" not in skill


def test_codegen_review_outputs_use_family_prefixed_review_paths() -> None:
    assert plan_review_outputs("CH-DEMO-001", "api") == (
        "qa/results/review/api-codegen-review-summary.md",
        "qa/results/review/api-codegen-review.json",
    )
    for family in FAMILIES:
        outputs = plan_review_outputs("CH-DEMO-001", family)
        assert all(path.startswith("qa/results/") for path in outputs)
        assert any(path.endswith(f"{family}-codegen-review.json") for path in outputs)
        assert len(outputs) == 2
        assert not any(
            "/".join(("qa", "changes")) + "/" in path
            for path in (*plan_outputs("CH-DEMO-001", family), *plan_review_outputs("CH-DEMO-001", family))
        )


@pytest.mark.parametrize("family", FAMILIES)
def test_deleted_plan_skills_are_not_the_registered_generation_jobs(family: str) -> None:
    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS

    assert f"{family}.plan" not in AGENT_JOB_CONTRACTS
    assert f"{family}.codegen" in AGENT_JOB_CONTRACTS
    assert f"{family}.codegen-review" in AGENT_JOB_CONTRACTS
    assert AGENT_JOB_CONTRACTS[f"{family}.codegen"].skill_id == f"aa-{family}-codegen"
    assert AGENT_JOB_CONTRACTS[f"{family}.codegen-review"].skill_id == f"aa-{family}-codegen-reviewer"
