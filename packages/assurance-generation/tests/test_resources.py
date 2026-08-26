from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts import CodegenAuthoringV1, PlanReviewAuthoring
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.resource_loader import resource_bytes, resource_text

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_generation" / "resources"
_REQUIRED = (
    "skills/aa-api-plan/SKILL.md",
    "skills/aa-api-plan-reviewer/SKILL.md",
    "skills/aa-e2e-plan/SKILL.md",
    "skills/aa-e2e-plan-reviewer/SKILL.md",
    "skills/aa-fuzz-plan/SKILL.md",
    "skills/aa-fuzz-plan-reviewer/SKILL.md",
    "skills/aa-performance-plan/SKILL.md",
    "skills/aa-performance-plan-reviewer/SKILL.md",
    "skills/aa-api-codegen/SKILL.md",
    "skills/aa-api-codegen-fixer/SKILL.md",
    "skills/aa-e2e-codegen/SKILL.md",
    "skills/aa-e2e-codegen-fixer/SKILL.md",
    "skills/aa-fuzz-codegen/SKILL.md",
    "skills/aa-performance-codegen/SKILL.md",
    "personas/test-author.md",
    "personas/reviewer.md",
    "result-contracts/plan.v1.schema.json",
    "result-contracts/plan-review.v1.schema.json",
    "result-contracts/codegen.v1.schema.json",
    "result-contracts/codegen-fix.v1.schema.json",
)
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def test_generation_resources_forbid_legacy_and_provider_names() -> None:
    missing = [item for item in _REQUIRED if not (_RESOURCES / item).is_file()]
    assert missing == [], f"missing generation resources: {missing}"
    hits: list[str] = []
    for path in _resource_files():
        if _TOKEN.search(path.read_text(encoding="utf-8")):
            hits.append(path.relative_to(_RESOURCES).as_posix())
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(path.read_text(encoding="utf-8").lower() for path in _resource_files())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("result-contracts/plan.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, PlanResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/plan-review.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, PlanReviewAuthoring.model_json_schema())
    )
    assert resource_bytes("result-contracts/codegen.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, CodegenAuthoringV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/codegen-fix.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, CodegenAuthoringV1.model_json_schema())
    )


def test_performance_plan_requires_source_backed_seed_lookup_and_runtime_host() -> None:
    skill = resource_text("skills/aa-performance-plan/SKILL.md")
    normalized = " ".join(skill.split())

    assert "must not assume `data.id` or `data.dept_id`" in normalized
    assert "source-backed identifier lookup" in normalized
    assert "API_BASE_URL" in normalized
    assert "replace hard-coded hosts" in normalized
    assert "literal character budget" in normalized
    assert "`p<uuid8>r`" in normalized
    assert "count every concrete root, child, and grandchild seed name" in normalized


def test_codegen_skills_freeze_inputs_and_require_every_mapping_target() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        skill = resource_text(f"skills/aa-{family}-codegen/SKILL.md")
        normalized = " ".join(skill.split())

        assert "Plan, case, and review inputs are immutable" in normalized
        assert "Every closed-mapping target must appear in `files`" in normalized
        assert "Do not list plan, case, or review inputs in `files`" in normalized


def test_all_plan_reviews_route_bounded_defects_to_replan() -> None:
    persona = " ".join(resource_text("personas/reviewer.md").split())
    assert "including fuzz and performance" in persona
    assert "Severity and a blocking impact do not by themselves require human review" in persona
    assert "Fuzz and performance reviews are human-only" not in persona

    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-plan-reviewer/SKILL.md").split())
        planner = " ".join(resource_text(f"skills/aa-{family}-plan/SKILL.md").split())

        assert "Evidence-proven, bounded defects use `needs_fix`" in reviewer
        assert "Severity alone does not require human review" in reviewer
        assert "missing product, policy, authorization, or safety decision" in reviewer
        assert "an array of non-empty finding ID strings" in reviewer
        assert "Never put objects, `finding_id`/`action` pairs" in reviewer
        assert 'Use `"auto_fix_plan": []` for `pass`' in reviewer
        review_path = f"review/{family}-plan-review.json"
        assert review_path in planner
        assert "apply only the findings named in `auto_fix_plan`" in planner


def test_fuzz_review_routes_source_backed_schema_loader_corrections_to_replan() -> None:
    reviewer = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    assert "incorrect application import, router export, schema loader" in reviewer
    assert "repository source proves the exact replacement" in reviewer
    assert "never escalate that source-backed correction to human review" in reviewer


def test_plan_reviewers_do_not_reopen_frozen_source_backed_oracles() -> None:
    api = " ".join(resource_text("skills/aa-api-plan-reviewer/SKILL.md").split())
    fuzz = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    assert "assertion_intent` is `assert_ideal" in api
    assert "mismatch is the product defect" in api
    assert "source-backed envelope as contract evidence" in fuzz
    assert "not a new product decision" in fuzz


def test_api_plan_does_not_treat_sut_operations_as_missing_adapter_capabilities() -> None:
    planner = " ".join(resource_text("skills/aa-api-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-api-plan-reviewer/SKILL.md").split())

    assert "The SUT endpoint being tested is not itself a capability dependency" in planner
    assert "Do not invent a missing API adapter requirement" in planner
    assert "existing helpers private to the closed-mapping target" in planner
    assert "capabilities as consumed reusable fixtures/helpers" in reviewer
    assert "does not require a same-operation API adapter leaf" in reviewer


def test_api_plan_review_is_exhaustive_and_locators_are_single_target() -> None:
    planner = " ".join(resource_text("skills/aa-api-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-api-plan-reviewer/SKILL.md").split())

    assert "Do not stop the review after finding the first defect" in reviewer
    assert "complete one exhaustive pass across every required plan artifact" in reviewer
    assert "Return all independently observable defects in the same review document" in reviewer
    assert "A finding locator authorizes exactly one artifact and key/section" in reviewer
    assert "emit one finding per target" in reviewer
    assert "Trace each planned lifecycle end to end" in reviewer
    assert "unfiltered tree plus bounded recursive exact matching" in reviewer
    assert "database/session read boundary" in reviewer
    assert "Apply every listed finding in the same planner re-entry" in planner
    assert "each locator as authorizing exactly its named artifact" in planner


def test_e2e_codegen_uses_importable_support_modules_instead_of_conftest_imports() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "`conftest.py` is pytest discovery configuration, not an importable support module" in skill
    assert "Never generate `from conftest import ...`" in skill
    assert "import that module by its package path" in skill


def test_all_planners_use_the_result_contract_as_the_capability_whitelist() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        planner = " ".join(resource_text(f"skills/aa-{family}-plan/SKILL.md").split())

        assert "enum is the sole whitelist" in planner
        assert "never construct a key from a namespace" in planner
        assert "do not emit a virtual key" in planner
        assert "byte-for-byte present in the enum" in planner
