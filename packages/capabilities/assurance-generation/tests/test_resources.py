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
    "skills/aa-api-codegen/SKILL.md",
    "skills/aa-api-codegen-reviewer/SKILL.md",
    "skills/aa-e2e-codegen/SKILL.md",
    "skills/aa-e2e-codegen-reviewer/SKILL.md",
    "skills/aa-fuzz-codegen/SKILL.md",
    "skills/aa-fuzz-codegen-reviewer/SKILL.md",
    "skills/aa-performance-codegen/SKILL.md",
    "skills/aa-performance-codegen-reviewer/SKILL.md",
    "skills/aa-api-codegen/SKILL.md",
    "skills/aa-e2e-codegen/SKILL.md",
    "skills/aa-fuzz-codegen/SKILL.md",
    "skills/aa-performance-codegen/SKILL.md",
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
        "skills/aa-api-plan/SKILL.md",
        "skills/aa-api-plan-reviewer/SKILL.md",
        "skills/aa-e2e-plan/SKILL.md",
        "skills/aa-e2e-plan-reviewer/SKILL.md",
        "skills/aa-fuzz-plan/SKILL.md",
        "skills/aa-fuzz-plan-reviewer/SKILL.md",
        "skills/aa-performance-plan/SKILL.md",
        "skills/aa-performance-plan-reviewer/SKILL.md",
        "result-contracts/codegen-fix.v1.schema.json",
    ],
)
def test_removed_codegen_fix_resources_are_unloadable(path: str) -> None:
    with pytest.raises(FileNotFoundError):
        resource_bytes(path)


def test_performance_plan_requires_source_backed_seed_lookup_and_runtime_host() -> None:
    skill = resource_text("skills/aa-performance-codegen/SKILL.md")
    normalized = " ".join(skill.split())

    assert "API_BASE_URL" in normalized
    assert "Do not hard-code a local port" in normalized
    assert "Inspect real response shapes" in normalized
    assert (
        "Do not require a create response to return an identifier unless the handler actually does"
        in normalized
    )
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
    planner = " ".join(resource_text("skills/aa-performance-codegen/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-performance-codegen-reviewer/SKILL.md").split())

    assert "an existing helper outside `allowed_outputs` remains reusable" in planner
    assert "A helper outside the codegen write whitelist can still be imported and reused" in reviewer


def test_performance_plan_review_closes_complete_runtime_inventory_every_round() -> None:
    reviewer = " ".join(resource_text("skills/aa-performance-codegen-reviewer/SKILL.md").split())

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
    planner = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-codegen-reviewer/SKILL.md").split())

    for document in (skill, planner, reviewer):
        assert "Schemathesis `case.call(..., session=...)` accepts only a `requests.Session`" in document
        assert "never pass an `httpx.Client` as the Schemathesis `session`" in document
    assert "omit `session` from `case.call`" in skill
    assert "keep the inherited HTTPX client for lifecycle discovery and cleanup" in skill
    assert "pass the authenticated headers or cookies explicitly to `case.call`" in skill


def test_fuzz_plan_requires_an_executable_generated_case_strategy() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-codegen-reviewer/SKILL.md").split())

    assert "`operation.as_strategy()`" in planner
    assert "`operation.as_strategy()`" in reviewer
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
    reviewer = " ".join(resource_text("skills/aa-e2e-codegen-reviewer/SKILL.md").split())

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

        assert "host-built scope and reviewed cases are immutable" in normalized
        assert "Every closed-mapping target must appear in `files`" in normalized
        assert "Do not list plan, case, or review inputs in `files`" in normalized
        assert (
            "Read every exact product-source path cited by the host scope and reviewed cases before any discovery"
            in normalized
        )
        assert "A glob result of `No files found` is not evidence that product source is absent" in normalized
        assert "ignored source files remain exact-readable" in normalized
        assert "runtime `allowed_outputs` list is the exact write whitelist" in normalized
        assert "keep the fixture or helper inside an authorized mapped target" in normalized
        assert "Never attempt or declare an unlisted support file" in normalized


def test_all_plan_reviews_route_bounded_defects_to_replan() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        skill = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())
        assert "Evidence-proven, bounded defects" in skill
        assert "Severity alone does not require human review" in skill


def test_plan_reviews_do_not_block_codegen_on_a_source_proven_sut_defect() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())

        assert "A source-proven SUT defect is test evidence, not a missing product decision" in reviewer
        assert "Do not require the SUT defect to be corrected before codegen" in reviewer
        assert "let execution and reporting record the failure" in reviewer

    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())
        planner = " ".join(resource_text(f"skills/aa-{family}-codegen/SKILL.md").split())

        assert "Evidence-proven, bounded defects use `route: auto_fix`" in reviewer
        assert "Severity alone does not require human review" in reviewer
        assert "missing product, policy, authorization, or safety decision" in reviewer
        assert "an array of non-empty finding ID strings" in reviewer
        assert "Never put objects, `finding_id`/`action` pairs" in reviewer
        assert "exactly the set of finding IDs present in this response" in reviewer
        assert "Remove IDs for findings that were resolved in an earlier round" in reviewer
        assert 'Use `"finding_ids": []` for `codegen`' in reviewer
        assert "does not rewrite" in reviewer
        review_path = f"review/{family}-codegen-review.json"
        assert review_path in reviewer
        del planner


def test_all_plan_reviewers_use_exact_locked_inputs_instead_of_change_globs() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())

        assert "`review_input_paths`" in reviewer
        assert "read every listed path directly" in reviewer
        assert "Do not use glob" in reviewer


def test_all_plan_reviewers_exact_read_declared_runtime_support_before_absence_findings() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())

        assert "call the native read tool on that exact path before any glob or grep" in reviewer
        assert "do not emit an absence finding" in reviewer


def test_all_planners_and_reviewers_read_attested_source_paths_before_discovery() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())
        codegen = " ".join(resource_text(f"skills/aa-{family}-codegen/SKILL.md").split())
        assert "Read `proposal.md` first" in reviewer
        assert "A glob result of `No files found` is not evidence that product source is absent" in codegen
        assert "ignored source files remain exact-readable" in codegen
        assert "Read every exact product-source path cited by the host scope" in codegen


def test_fuzz_review_routes_source_backed_schema_loader_corrections_to_replan() -> None:
    reviewer = " ".join(resource_text("skills/aa-fuzz-codegen-reviewer/SKILL.md").split())

    assert "incorrect application import, router export, schema loader" in reviewer
    assert "repository source proves the exact replacement" in reviewer
    assert "never escalate that source-backed correction to human review" in reviewer


def test_fuzz_plan_review_closes_support_module_and_schema_loader_facts_in_first_pass() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-fuzz-codegen-reviewer/SKILL.md").split())

    assert "`operation.as_strategy()`" in planner
    assert (
        "exact-read every ancestor `conftest.py`" in planner
        or "read every ancestor `conftest.py`" in reviewer
    )
    assert (
        "A repaired artifact does not narrow the next review" in reviewer
        or "Do not stop the review after finding the first defect" in reviewer
    )


def test_fuzz_plan_reuses_ancestor_runtime_fixtures_instead_of_parallel_wiring() -> None:
    planner = " ".join(resource_text("skills/aa-fuzz-codegen/SKILL.md").split())

    assert "exact-read every ancestor `conftest.py`" in planner
    assert "Never shadow an existing fixture in the mapped test module" in planner


def test_plan_reviewers_do_not_reopen_frozen_source_backed_oracles() -> None:
    api = " ".join(resource_text("skills/aa-api-codegen-reviewer/SKILL.md").split())
    fuzz = " ".join(resource_text("skills/aa-fuzz-codegen-reviewer/SKILL.md").split())

    assert "assertion_intent` is `assert_ideal" in api
    assert "mismatch is the product defect" in api
    assert "source-backed envelope as contract evidence" in fuzz
    assert "not a new product decision" in fuzz


def test_api_plan_does_not_treat_sut_operations_as_missing_adapter_capabilities() -> None:
    planner = " ".join(resource_text("skills/aa-api-codegen/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-api-codegen-reviewer/SKILL.md").split())

    assert "Do not derive conventional routes" in planner
    assert "Do not emit needs_fix for the wheel-owned pytest runner contract" in reviewer


def test_api_plan_uses_a_real_pre_post_invariant_for_omitted_identity_fields() -> None:
    planner = " ".join(resource_text("skills/aa-api-codegen/SKILL.md").split())

    assert "never assume `data.id` exists" in planner


def test_api_plan_documents_boundary_construction_proofs() -> None:
    reviewer = " ".join(resource_text("skills/aa-api-codegen-reviewer/SKILL.md").split())

    assert "An upper bound is not an exact length" in reviewer


def test_api_plan_review_closes_fixture_and_helper_runtime_boundaries_in_first_pass() -> None:
    reviewer = " ".join(resource_text("skills/aa-api-codegen-reviewer/SKILL.md").split())

    assert "Do not emit needs_fix for the wheel-owned pytest runner contract" in reviewer
    assert "asyncio_mode" in reviewer
    assert "does not drop new IDs" in reviewer
    assert "does not rewrite" in reviewer


def test_api_generation_closes_source_proven_initial_admin_credentials() -> None:
    reviewer = " ".join(resource_text("skills/aa-api-codegen-reviewer/SKILL.md").split())
    codegen = " ".join(resource_text("skills/aa-api-codegen/SKILL.md").split())

    assert "independently compare `admin_username` and `admin_password`" in reviewer
    assert "credential fallback" in reviewer
    assert "source-proven `admin_username` and `admin_password`" in codegen
    assert "Never read administrator credentials from `qa/tests/config.py`" in codegen


def test_e2e_plan_review_and_repair_are_exhaustive_within_one_round() -> None:
    reviewer = " ".join(resource_text("skills/aa-e2e-codegen-reviewer/SKILL.md").split())

    assert "Do not stop the review after finding the first defect" in reviewer
    assert "complete one exhaustive pass across every required plan artifact" in reviewer
    assert "Return all independently observable defects in the same review document" in reviewer


def test_e2e_codegen_uses_importable_support_modules_instead_of_conftest_imports() -> None:
    skill = " ".join(resource_text("skills/aa-e2e-codegen/SKILL.md").split())

    assert "`conftest.py` is pytest discovery configuration, not an importable support module" in skill
    assert "Never generate `from conftest import ...`" in skill
    assert "import that module by its package path" in skill


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_plan_and_review_skills_keep_durable_qa_tests_targets_on_the_execution_view(
    family: str,
) -> None:
    reviewer = " ".join(resource_text(f"skills/aa-{family}-codegen-reviewer/SKILL.md").split())
    codegen = " ".join(resource_text(f"skills/aa-{family}-codegen/SKILL.md").split())
    assert "Execute runs durable `qa/tests/` in place with `pythonpath=qa`" in reviewer
    assert "Fixtures and support modules live under `qa/tests/`" in reviewer
    assert "Treat a missing `qa/tests/**/conftest.py` as fixture unavailability" in reviewer
    assert "Do not look up fixtures under the SUT `tests/` tree" in reviewer
    assert "Do not retarget mapping rows to `tests/`" in reviewer
    assert "qa/tests/" in codegen


@pytest.mark.parametrize(
    ("family", "target_file"),
    (
        ("api", "qa/tests/api/test_dept.py"),
        ("e2e", "qa/tests/e2e/test_dept.py"),
        ("fuzz", "qa/tests/fuzz/test_dept.py"),
        ("performance", "qa/tests/perf/locustfile_dept.py"),
    ),
)
def test_plan_skill_mapping_examples_use_qa_tests(family: str, target_file: str) -> None:
    skill = resource_text(f"skills/aa-{family}-codegen/SKILL.md")
    assert "qa/tests/" in skill
    assert target_file.split("/")[2] in skill or "qa/tests/" in skill
    assert "target_file" in skill


def test_all_planners_use_the_result_contract_as_the_capability_whitelist() -> None:
    for family in ("api", "e2e", "fuzz", "performance"):
        planner = " ".join(resource_text(f"skills/aa-{family}-codegen/SKILL.md").split())

        assert "Capability keys must be exact typed leaves" in planner
