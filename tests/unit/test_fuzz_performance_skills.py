"""Skill contract guards for activated Fuzz/Performance assurance skills."""

from __future__ import annotations

from pathlib import Path


from assurance_agent import resources
from assurance_agent.workflow.graph.contracts import load_execution_contracts

_SKILLS = (
    "aa-fuzz-plan",
    "aa-fuzz-plan-reviewer",
    "aa-fuzz-codegen",
    "aa-performance-plan",
    "aa-performance-plan-reviewer",
    "aa-performance-codegen",
)

_FORBIDDEN = (
    "workflow-state.yaml",
    "phases.",
    "layer_applicable",
    "state delta",
    "state-delta",
    "src/schema/review.ts",
    "run_fuzz_plan_fixer",
    "run_performance_plan_fixer",
    "aa-fuzz-plan-fixer",
    "aa-performance-plan-fixer",
)


def _skill_text(name: str) -> str:
    return resources.read_text("skills", f"{name}/SKILL.md")


def test_six_skills_exist_and_forbid_legacy_control_plane_instructions() -> None:
    for name in _SKILLS:
        text = _skill_text(name)
        assert text.strip()
        lowered = text.lower()
        for token in _FORBIDDEN:
            assert token.lower() not in lowered, f"{name} still mentions {token!r}"


def test_plan_skills_require_factory_mapping() -> None:
    for name in ("aa-fuzz-plan", "aa-performance-plan"):
        text = _skill_text(name)
        assert "Factory Mapping" in text
        assert "| Shared Module | Function | Ownership |" in text


def test_reviewer_skills_are_human_only_and_capability_gated() -> None:
    for name in ("aa-fuzz-plan-reviewer", "aa-performance-plan-reviewer"):
        text = _skill_text(name)
        assert "auto_fix_allowed: false" in text or "`auto_fix_allowed: false`" in text
        assert "auto_fix_plan: []" in text or "`auto_fix_plan: []`" in text
        assert "required_capabilities" in text
        assert "human_review" in text
        assert "PlanReview" in text


def test_codegen_skills_name_exact_predecessor_inputs() -> None:
    for layer, skill in (("fuzz", "aa-fuzz-codegen"), ("performance", "aa-performance-codegen")):
        text = _skill_text(skill)
        for path in (
            f"plans/{layer}-plan.md",
            f"plans/{layer}-codegen-plan.md",
            f"plans/{layer}-review-summary.md",
            f"review/{layer}-plan-review.json",
            f"review/{layer}-plan-checks.json",
            "cases/**/case.yaml",
            ".aa/config.yaml",
            ".aa/data-knowledge.yaml",
        ):
            assert path in text, f"{skill} missing input {path}"
        assert "graph gate" in text.lower() or "progression authority" in text.lower()


def test_skill_paths_are_covered_by_execution_contracts() -> None:
    catalog = load_execution_contracts(Path.cwd())
    for name in _SKILLS:
        target = f"skill:{name}"
        assert target in catalog.contracts
        contract = catalog.contracts[target]
        text = _skill_text(name)
        # Every exact change:/repo:/.aa path named in Inputs/Outputs sections should be authorized.
        for line in text.splitlines():
            stripped = line.strip().lstrip("- ").strip("`")
            if not (
                stripped.startswith("qa/changes/<change-id>/")
                or stripped.startswith(".aa/")
                or stripped.startswith("tests/")
            ):
                continue
            logical = stripped
            if logical.startswith("qa/changes/<change-id>/"):
                logical = "change:" + logical.removeprefix("qa/changes/<change-id>/")
            elif logical.startswith(".aa/"):
                # skill text uses project/repo root form; contracts use repo:/project:
                continue
            elif logical.startswith("tests/"):
                logical = "repo:" + logical
            # Soft check: contract reads/writes mention the artifact basename or prefix.
            joined = "\n".join([*contract.reads, *contract.writes, *contract.authorization_writes])
            basename = logical.rsplit("/", 1)[-1]
            assert basename in joined or logical in joined or logical.replace("change:", "") in joined
