from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

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
    "skills/aa-e2e-codegen/SKILL.md",
    "skills/aa-fuzz-codegen/SKILL.md",
    "skills/aa-performance-codegen/SKILL.md",
    "personas/test-author.md",
    "personas/reviewer.md",
    "result-contracts/plan.v1.schema.json",
    "result-contracts/plan-review.v1.schema.json",
    "result-contracts/codegen.v1.schema.json",
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


@pytest.mark.parametrize(
    "path",
    [
        "skills/aa-api-codegen-fixer/SKILL.md",
        "skills/aa-e2e-codegen-fixer/SKILL.md",
        "result-contracts/codegen-fix.v1.schema.json",
    ],
)
def test_removed_codegen_fix_resources_are_unloadable(path: str) -> None:
    with pytest.raises(FileNotFoundError):
        resource_bytes(path)


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
    assert "Never read `.env`, `*.env`, or credential-bearing benchmark environment files" in normalized
    assert "Use environment variable names and non-secret defaults only" in normalized

    codegen = " ".join(resource_text("skills/aa-performance-codegen/SKILL.md").split())
    assert "Never read `.env`, `*.env`, or credential-bearing benchmark environment files" in codegen
    assert "Use environment variable names and non-secret defaults only" in codegen


def test_performance_codegen_uses_valid_locust_task_and_request_names() -> None:
    skill = " ".join(resource_text("skills/aa-performance-codegen/SKILL.md").split())

    assert "Never pass `name=` to the `@task` decorator" in skill
    assert "put the stable statistics label on `self.client.get(..., name=...)`" in skill
    assert "Import the generated Locust module in the configured runtime before returning" in skill
    assert "import and call that exact symbol instead of reimplementing it" in skill
    assert (
        "`create-if-missing` is permission to create an absent helper, not evidence that it is absent"
        in skill
    )
    assert "Before any glob or directory discovery, convert every declared Python symbol" in skill
    assert "The write whitelist does not limit imports or reads" in skill
    assert "controlled execution does not promise inherited token environment variables" in skill
    assert "call the declared authentication capability" in skill
    assert "exactly equal the reviewed plan's `required_capabilities`" in skill
    assert "Every mapped function is named `test_" not in skill


def test_performance_codegen_uses_locust_stats_name_then_method_order() -> None:
    skill = " ".join(resource_text("skills/aa-performance-codegen/SKILL.md").split())

    assert "`environment.stats.get(stable_name, method)`" in skill
    assert "the statistics name is the first argument and the HTTP method is the second" in skill


def test_performance_plan_and_review_reuse_non_writable_installed_helpers() -> None:
    planner = " ".join(resource_text("skills/aa-performance-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-performance-plan-reviewer/SKILL.md").split())

    assert "A helper outside the codegen write whitelist can still be imported and reused" in planner
    assert "must not instruct codegen to update it" in planner
    assert "A helper outside the codegen write whitelist can still be imported and reused" in reviewer
    assert "reject a plan that requires codegen to modify such a helper" in reviewer


def test_performance_plan_review_closes_complete_runtime_inventory_every_round() -> None:
    reviewer = " ".join(resource_text("skills/aa-performance-plan-reviewer/SKILL.md").split())

    assert "Do not stop the review after finding the first defect" in reviewer
    assert "Before the first decision in every round, close this runtime inventory" in reviewer
    assert "resolve each consumed dotted Python symbol to its `.py` module" in reviewer
    assert "Read implementations of every mapped helper and adapter" in reviewer
    assert "A repaired artifact does not narrow the next review" in reviewer
    assert "A finding locator authorizes exactly one artifact and key/section" in reviewer
    assert "emit one finding per target" in reviewer


def test_fuzz_codegen_uses_the_installed_schemathesis_v4_api() -> None:
    skill = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())

    assert "schemathesis.openapi.from_dict(document)" in skill
    assert "Never pass `base_url` to `schemathesis.openapi.from_dict`" in skill
    assert "pass `base_url` to `case.call` or `case.call_and_validate`" in skill
    assert "construct an explicit case with `operation.Case(...)`" in skill
    assert "`operation.make_case(...)` does not exist in Schemathesis v4" in skill


def test_api_codegen_distinguishes_sync_clients_from_async_helpers() -> None:
    skill = " ".join(resource_text("skills/aa-api-codegen/SKILL.md").split())
    assert "`httpx.Client`" in skill
    assert "`httpx.AsyncClient`" in skill
    assert "A test being `async def` does not make its fixtures asynchronous" in skill
    assert "`response = api_client.get(...)`" in skill
    assert "`await cleanup_dept(...)`" in skill


def test_fuzz_codegen_keeps_httpx_out_of_schemathesis_requests_transport() -> None:
    skill = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())
    planner = " ".join(resource_text("skills/aa-fuzz-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    for document in (skill, planner, reviewer):
        assert "Schemathesis `case.call(..., session=...)` accepts only a `requests.Session`" in document
        assert "never pass an `httpx.Client` as the Schemathesis `session`" in document
    assert "omit `session` from `case.call`" in skill
    assert "keep the inherited HTTPX client for lifecycle discovery and cleanup" in skill
    assert "pass the authenticated headers or cookies explicitly to `case.call`" in skill


def test_fuzz_plan_requires_an_executable_generated_case_strategy() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    assert "execute cases generated by the exact selected operation's `operation.as_strategy()`" in planner
    assert "Finite hand-authored payloads are supplemental boundary probes" in planner
    assert (
        "must execute cases generated by the exact selected operation's `operation.as_strategy()`" in reviewer
    )
    assert (
        "Merely inspecting the operation or deriving hand-authored payload shapes from it does not qualify"
        in reviewer
    )


def test_fuzz_codegen_reuses_ancestor_fixtures_without_shadowing_them() -> None:
    skill = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())

    assert "exact-read every ancestor `conftest.py`" in skill
    assert "Never shadow an existing fixture in the mapped test module" in skill
    assert "reuse its response-envelope handling" in skill


def test_api_codegen_keeps_generated_values_within_source_backed_schema_limits() -> None:
    skill = " ".join(resource_text("skills/aa-api-codegen/SKILL.md").split())

    assert "including fixture setup and unique-name prefixes" in skill
    assert "fit every generated value within the source-backed schema constraints" in skill
    assert "Count the complete runtime value" in skill


def test_e2e_codegen_uses_compiled_patterns_for_pattern_url_assertions() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "a Python string is an exact expected URL, not a regular expression" in skill
    assert "pass a compiled `re.Pattern`" in skill


def test_e2e_codegen_requires_strict_unique_validation_locators() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "scope form-validation assertions to the current visible form or dialog" in skill
    assert "Never use an unscoped page-wide text locator when the same text can label inputs" in skill


def test_e2e_codegen_resolves_localized_default_action_names() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "do not guess the visible or accessible name from the action's meaning" in skill
    assert "resolve the active locale's exact default text" in skill


def test_e2e_plan_review_closes_complete_runtime_inventory_every_round() -> None:
    reviewer = " ".join(resource_text("skills/aa-e2e-plan-reviewer/SKILL.md").split())

    assert "Before the first decision in every round, close this runtime inventory" in reviewer
    assert "resolve each consumed dotted Python symbol to its `.py` module" in reviewer
    assert "read every ancestor `conftest.py` from the target directory through the test root" in reviewer
    assert "Verify the fixture's real name and its browser, credential, or request handoff" in reviewer
    assert "Read implementations of every mapped helper and adapter" in reviewer
    assert "A repaired artifact does not narrow the next review" in reviewer


def test_fuzz_codegen_does_not_feed_function_scoped_state_fixtures_to_hypothesis() -> None:
    skill = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())

    assert "Never pass a function-scoped fixture to an `@given` test" in skill
    assert "Do not suppress `HealthCheck.function_scoped_fixture`" in skill
    assert "perform mutable setup and cleanup inside each generated example" in skill


def test_e2e_codegen_does_not_invent_roles_for_optional_defaulted_controls() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "Do not infer an ARIA role from the component name" in skill
    assert "already satisfies the case through its initialized default" in skill


def test_codegen_skills_freeze_inputs_and_require_every_mapping_target() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        skill = resource_text(f"skills/aa-{family}-codegen/SKILL.md")
        normalized = " ".join(skill.split())

        assert "Plan, case, and review inputs are immutable" in normalized
        assert "Every closed-mapping target must appear in `files`" in normalized
        assert "Do not list plan, case, or review inputs in `files`" in normalized
        assert (
            "Read every exact product-source path cited by the approved plan before any discovery"
            in normalized
        )
        assert "A glob result of `No files found` is not evidence that product source is absent" in normalized
        assert "ignored source files remain exact-readable" in normalized
        assert "runtime `allowed_outputs` list is the exact write whitelist" in normalized
        assert "keep the fixture or helper inside an authorized mapped target" in normalized
        assert "Never attempt or declare an unlisted support file" in normalized


def test_all_plan_reviews_route_bounded_defects_to_replan() -> None:
    persona = " ".join(resource_text("personas/reviewer.md").split())
    assert "including fuzz and performance" in persona
    assert "Severity and a blocking impact do not by themselves require human review" in persona
    assert "Fuzz and performance reviews are human-only" not in persona


def test_plan_reviews_do_not_block_codegen_on_a_source_proven_sut_defect() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-plan-reviewer/SKILL.md").split())

        assert "A source-proven SUT defect is test evidence, not a missing product decision" in reviewer
        assert "Do not require the SUT defect to be corrected before codegen" in reviewer
        assert "let execution and reporting record the failure" in reviewer

    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-plan-reviewer/SKILL.md").split())
        planner = " ".join(resource_text(f"skills/aa-{family}-plan/SKILL.md").split())

        assert "Evidence-proven, bounded defects use `needs_fix`" in reviewer
        assert "Severity alone does not require human review" in reviewer
        assert "missing product, policy, authorization, or safety decision" in reviewer
        assert "an array of non-empty finding ID strings" in reviewer
        assert "Never put objects, `finding_id`/`action` pairs" in reviewer
        assert "exactly the set of finding IDs present in this response" in reviewer
        assert "Remove IDs for findings that were resolved in an earlier round" in reviewer
        assert 'Use `"auto_fix_plan": []` for `pass`' in reviewer
        review_path = f"review/{family}-plan-review.json"
        assert review_path in planner
        assert "apply only the findings named in `auto_fix_plan`" in planner


def test_all_plan_reviewers_use_exact_locked_inputs_instead_of_change_globs() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-plan-reviewer/SKILL.md").split())

        assert "`review_input_paths`" in reviewer
        assert "read every listed path directly" in reviewer
        assert "Do not use glob" in reviewer


def test_all_plan_reviewers_exact_read_declared_runtime_support_before_absence_findings() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-plan-reviewer/SKILL.md").split())

        assert "call the native read tool on that exact path before any glob or grep" in reviewer
        assert "do not emit an absence finding" in reviewer


def test_all_planners_and_reviewers_read_attested_source_paths_before_discovery() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        for role in ("plan", "plan-reviewer"):
            skill = " ".join(resource_text(f"skills/aa-{family}-{role}/SKILL.md").split())

            assert "Read `proposal.md` first" in skill
            assert "read every listed path directly before any discovery" in skill
            assert "A glob result of `No files found` is not evidence that product source is absent" in skill
            assert "ignored source files remain exact-readable" in skill


def test_fuzz_review_routes_source_backed_schema_loader_corrections_to_replan() -> None:
    reviewer = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    assert "incorrect application import, router export, schema loader" in reviewer
    assert "repository source proves the exact replacement" in reviewer
    assert "never escalate that source-backed correction to human review" in reviewer


def test_fuzz_plan_review_closes_support_module_and_schema_loader_facts_in_first_pass() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-plan-reviewer/SKILL.md").split())

    assert "Before authoring the first plan, build a complete support/runtime inventory" in planner
    assert "exact-read every candidate support module" in planner
    assert "Never assign `missing` or `create-if-missing` from a glob result" in planner
    assert "Every schema-acquisition mention must be a complete executable expression" in planner
    assert "Do not abbreviate it to a relative-path label in a summary or mapping table" in planner
    assert "trace the authentication fixture to its exact typed capability leaf" in planner
    assert "exact-read every shared module, fixture, and helper claimed as reusable" in reviewer
    assert "A reusable claim requires the exact module and symbol to exist" in reviewer
    assert "read every ancestor `conftest.py`" in reviewer
    assert "validate schema acquisition as an executable expression" in reviewer
    assert "A repaired artifact does not narrow the next review" in reviewer
    assert "Do not defer another independently observable defect to a later round" in reviewer
    assert "exact-read every support module named by a current finding" in planner
    assert "Do not preserve a `reuse`, `existing`, or `present` claim" in planner
    assert "verify every authorized finding is no longer contradicted" in planner


def test_fuzz_plan_reuses_ancestor_runtime_fixtures_instead_of_parallel_wiring() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-plan/SKILL.md").split())

    assert "derive the ancestor directories syntactically from the mapped target" in planner
    assert "without globbing" in planner
    assert "consume those fixtures by parameter" in planner
    assert "must not define a target-local replacement fixture" in planner
    assert "must not introduce a parallel settings accessor" in planner


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


def test_api_plan_uses_a_real_pre_post_invariant_for_omitted_identity_fields() -> None:
    planner = " ".join(resource_text("skills/aa-api-plan/SKILL.md").split())

    assert "omits the field that would identify a created entity" in planner
    assert "bounded before/after collection or tree snapshot" in planner
    assert "Never invent a sentinel value that is absent from the request" in planner


def test_api_plan_review_is_exhaustive_and_locators_are_single_target() -> None:
    planner = " ".join(resource_text("skills/aa-api-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-api-plan-reviewer/SKILL.md").split())

    assert "Do not stop the review after finding the first defect" in reviewer
    assert "complete one exhaustive pass across every required plan artifact" in reviewer
    assert "Return all independently observable defects in the same review document" in reviewer
    assert "exactly the set of finding IDs present in this response" in reviewer
    assert "Remove IDs for findings that were resolved in an earlier round" in reviewer
    assert "A finding locator authorizes exactly one artifact and key/section" in reviewer
    assert "emit one finding per target" in reviewer
    assert "Trace each planned lifecycle end to end" in reviewer
    assert "unfiltered tree plus bounded recursive exact matching" in reviewer
    assert "database/session read boundary" in reviewer
    assert "Apply every listed finding in the same planner re-entry" in planner
    assert "each locator as authorizing exactly its named artifact" in planner


def test_api_plan_review_closes_fixture_and_helper_runtime_boundaries_in_first_pass() -> None:
    planner = " ".join(resource_text("skills/aa-api-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-api-plan-reviewer/SKILL.md").split())

    assert "Before authoring the first plan, close a complete support/runtime inventory" in planner
    assert "exact-read every candidate support module" in planner
    assert "read every ancestor `conftest.py`" in planner
    assert "compare each helper's real signature" in planner
    assert "unexpectedly persists" in planner
    assert "resolve each consumed dotted Python symbol to its `.py` module" in reviewer
    assert "read every ancestor `conftest.py`" in reviewer
    assert "environment variables, credentials, and database or session paths" in reviewer
    assert "A repaired artifact does not narrow the next review" in reviewer
    assert "fixture or helper implementation named by a current finding" in planner
    assert "Do not preserve an `absent`, `missing`, or `create-if-missing` claim" in planner
    assert "verify every authorized finding is no longer contradicted" in planner


def test_e2e_plan_review_and_repair_are_exhaustive_within_one_round() -> None:
    planner = " ".join(resource_text("skills/aa-e2e-plan/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-e2e-plan-reviewer/SKILL.md").split())

    assert "Do not stop the review after finding the first defect" in reviewer
    assert "complete one exhaustive pass across every required plan artifact" in reviewer
    assert "Return all independently observable defects in the same review document" in reviewer
    assert "emit one finding per target" in reviewer
    assert "Apply every listed finding in the same planner re-entry" in planner
    assert "do not return after repairing only the first finding" in planner
    assert "exact-read every required output after the edits" in planner
    assert "contradicted lifecycle statement remains" in planner


def test_plan_reentry_returns_the_complete_output_manifest() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        planner = " ".join(resource_text(f"skills/aa-{family}-plan/SKILL.md").split())

        assert "`output_files` is the complete plan-package manifest" in planner
        assert "including required files that were unchanged in this repair" in planner
        assert "Do not return only the files edited in the current repair" in planner


def test_planners_do_not_confuse_l1_declaration_with_on_disk_implementation() -> None:
    for family in ("api", "e2e"):
        planner = " ".join(resource_text(f"skills/aa-{family}-plan/SKILL.md").split())

        assert "L1 declaration identifies the contract" in planner
        assert "on-disk inspection determines implementation availability" in planner
        assert "the first selected codegen layer may mark its bounded target `create-if-missing`" in planner
        assert "later selected layers must reuse that implementation" in planner
        assert "L1 does not declare that shared symbol" not in planner
        assert "every L1-declared symbol and every later-layer reference `reuse`" not in planner


def test_e2e_planner_exact_reads_declared_helpers_before_marking_them_missing() -> None:
    planner = " ".join(resource_text("skills/aa-e2e-plan/SKILL.md").split())

    assert "For every exact L1-declared Python symbol" in planner
    assert "translate its module path to an exact `.py` path" in planner
    assert "exact-read that file before assigning `missing` or `create-if-missing`" in planner
    assert "Glob, search, and repository status do not prove an ignored helper is absent" in planner


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
