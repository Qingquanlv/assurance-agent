from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.phase4.ownership import (
    ARTIFACT_NEW_IDS,
    ASSURANCE_OWNERS,
    CALLABLE_OWNER_OVERRIDES,
    GENERATION_VERIFICATION_FILES,
    MODULE_OWNER_ROOTS,
    NON_PHASE4_MODEL_DISPOSITIONS,
    OWNERSHIP_PATH,
    QUALITY_VERIFICATION_FILES,
    OwnershipLedger,
    legacy_artifact_types,
    legacy_effect_kinds,
    legacy_hook_fields,
    legacy_operation_ids,
    legacy_persona_ids,
    legacy_runtime_resource_paths,
    legacy_skill_ids,
    legacy_validator_ids,
    load_ownership_ledger,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

OPERATION_OWNERS: dict[str, tuple[str, ...]] = {
    "assurance.execution": (
        "operation:run-tests",
        "operation:run-tests-and-collect-pr-metrics",
    ),
    "assurance.healing": (
        "operation:allocate-healing-attempt",
        "operation:fixer-authority-ready",
        "operation:record-fixer-approval",
        "operation:fixer-dispatch",
        "operation:record-codegen-fix-apply",
        "operation:combine-fixer-safety",
        "operation:record-healing-status",
        "operation:compute-coverage-repair-safety",
        "operation:allocate-coverage-repair-attempt",
        "operation:record-coverage-repair-status",
    ),
    "assurance.quality": (
        "operation:derive-plan-layer-applicability",
        "operation:inspect",
        "operation:generate-report",
        "operation:materialize-trace-projection",
        "operation:build-coverage-gap-signals",
        "operation:materialize-trace-and-coverage-gaps",
        "operation:materialize-minimum-coverage",
        "operation:collect-diff-coverage",
        "operation:compute-constraint-coverage",
        "operation:compute-auth-matrix",
        "operation:compute-journey-coverage",
        "operation:compute-threshold-slack",
        "operation:materialize-quarantine-projection",
        "operation:materialize-c-layer-metrics",
        "operation:collect-pr-metrics-batch",
        "operation:probe-coverage-repair-need",
        "operation:materialize-pr-metrics",
        "operation:load-latest-pr-metrics",
        "operation:run-mutation-sample",
        "operation:compute-assertion-strength",
        "operation:compute-baseline-drift",
        "operation:collect-adversarial-yield",
        "operation:aggregate-nightly-metrics",
        "operation:evaluate-retrospective-shortboards",
        "operation:run-nightly-metrics-pipeline",
        "operation:collect-observations",
        "operation:record-empty-issue-analysis",
        "operation:record-issue-analysis-failure",
        "operation:record-project-sync-pending",
        "operation:reconcile-issues",
        "operation:load-problem-review-context",
        "operation:apply-problem-review",
    ),
    "assurance.improvement": (
        "operation:retro-collect-v3",
        "operation:assemble-retro-context-v3",
        "operation:drain-improvement-outbox",
        "operation:finalize-retro-status",
        "operation:record-retro-pipeline-failure",
        "operation:retro-evidence-gap-fallback",
        "operation:record-analysis-failed",
        "operation:materialize-empty-retro-analysis",
        "operation:reconcile-improvements",
        "operation:load-review-subject",
        "operation:validate-improvement-review-assessment",
        "operation:apply-improvement-auto-review",
        "operation:record-improvement-auto-review-error",
        "operation:record-auto-review-orchestration-error",
        "operation:select-current-retro-auto-review-items",
        "operation:summarize-auto-review-batch",
        "operation:load-improvement-review-context",
        "operation:apply-improvement-review",
        "operation:load-improvement-delivery",
        "operation:evaluate-memory-improvement",
        "operation:apply-memory-improvement",
        "operation:rollback-memory-improvement",
        "operation:export-change-improvement",
        "operation:record-change-improvement-applied",
        "operation:export-knowledge-improvement",
        "operation:record-knowledge-improvement-applied",
    ),
}

REPLACE_PHASE5_OPERATIONS = frozenset({"operation:no-op", "operation:stop", "operation:skill-registry-check"})
DELETE_PHASE6_OPERATIONS = frozenset({"operation:retro-accept"})

SKILL_OWNERS: dict[str, tuple[str, ...]] = {
    "assurance.intake": ("aa-intake", "aa-explore", "aa-case-design", "aa-case-reviewer"),
    "assurance.generation": (
        "aa-api-plan",
        "aa-api-plan-reviewer",
        "aa-api-codegen",
        "aa-e2e-plan",
        "aa-e2e-plan-reviewer",
        "aa-e2e-codegen",
        "aa-fuzz-plan",
        "aa-fuzz-plan-reviewer",
        "aa-fuzz-codegen",
        "aa-performance-plan",
        "aa-performance-plan-reviewer",
        "aa-performance-codegen",
    ),
    "assurance.execution": ("aa-execute", "aa-run"),
    "assurance.healing": ("aa-fix-proposal", "aa-coverage-repair"),
    "assurance.quality": (
        "aa-fact-baseline",
        "aa-inspect",
        "aa-issue-analyzer",
        "aa-issue-triage-advisor",
        "aa-report-generator",
        "aa-dashboard",
    ),
    "assurance.improvement": (
        "aa-retro",
        "aa-retro-eval-analysis",
        "aa-retro-issue-analysis",
        "aa-retro-workflow-analysis",
        "aa-improvement-reviewer",
        "aa-archive",
    ),
}

PERSONA_OWNERS = {
    "aa-intake-host": "assurance.intake",
    "aa-explorer": "assurance.intake",
    "aa-doc-author": "assurance.intake",
    "aa-test-author": "assurance.generation",
    "aa-reviewer": "assurance.generation",
    "aa-reporter": "assurance.quality",
    "aa-archiver": "assurance.improvement",
}

PERSONA_NEW_IDS = {
    "aa-intake-host": "assurance.intake.persona.intake-host.v1",
    "aa-explorer": "assurance.intake.persona.explorer.v1",
    "aa-doc-author": "assurance.intake.persona.doc-author.v1",
    "aa-test-author": "assurance.generation.persona.test-author.v1",
    "aa-reviewer": "assurance.generation.persona.reviewer.v1",
    "aa-reporter": "assurance.quality.persona.reporter.v1",
    "aa-archiver": "assurance.improvement.persona.archiver.v1",
}

VALIDATOR_NEW_IDS = {
    "generated_files_candidate/v1": "assurance.generation.validator.generated-files.v1",
    "plan_mechanical_candidate/v1": "assurance.generation.validator.plan-mechanical.v1",
    "archive_integrity/v1": "assurance.improvement.validator.archive-integrity.v1",
    "problem_apply_candidate/v1": "assurance.quality.validator.problem-apply.v1",
    "cross_artifact_invariants/v1": "assurance.quality.validator.cross-artifact.v1",
}

EFFECT_NEW_IDS = {
    "healing_allocation/v2": "assurance.healing.effect.allocation.v2",
    "fixer_proposal_approved/v1": "assurance.healing.effect.proposal-approved.v1",
    "heal_record_apply/v2": "assurance.healing.effect.heal-apply.v2",
}


def _operation_live_pointer(legacy_id: str) -> str:
    return f"tests/phase4/test_ownership_live.py::test_migrate_operation_is_live_handler[{legacy_id}]"


VALIDATOR_VERIFICATION: dict[str, str] = {
    "generated_files_candidate/v1": (
        "packages/features/assurance-generation/tests/test_generated_files_validator.py"
        "::test_plugin_contributed_codegen_validators_allowlist_registered_paths"
    ),
    "plan_mechanical_candidate/v1": (
        "packages/features/assurance-generation/tests/test_plan_validator.py"
        "::test_plan_mechanical_dispatches_closed_family_table"
    ),
    "archive_integrity/v1": (
        "packages/features/assurance-improvement/tests/test_delivery.py"
        "::test_archive_integrity_requires_all_four_authenticated_inputs"
    ),
    "problem_apply_candidate/v1": (
        "packages/features/assurance-quality/tests/test_issues.py"
        "::test_problem_apply_validator_rejects_forged_review_id"
    ),
    "cross_artifact_invariants/v1": (
        "packages/features/assurance-quality/tests/test_metrics.py::test_metrics_and_cross_artifact_validators"
    ),
}

EFFECT_VERIFICATION = "packages/features/assurance-healing/tests/test_effects.py::test_effect_policies_are_frozen_and_identity_bound"

HOOK_PRIMARY_SEAMS: dict[str, tuple[str, str]] = {
    "load_product_code_roots": ("assurance.healing", "assurance.healing.validator.test-tree.v1"),
    "candidate_document_digest": ("assurance.quality", "assurance.quality.candidate-document-digest"),
    "commit_healing_allocation_ledger": (
        "assurance.healing",
        "assurance.healing.effect.allocation.v2",
    ),
    "complete_issue_analyzer_outputs": (
        "assurance.quality",
        "assurance.quality.issue-analysis.finalize",
    ),
    "complete_improvement_reviewer_outputs": (
        "assurance.improvement",
        "assurance.improvement.improvement-review.finalize",
    ),
    "complete_signal_outputs": ("assurance.improvement", "assurance.improvement.retro.finalize"),
    "complete_candidate_outputs": ("assurance.improvement", "assurance.improvement.retro.finalize"),
    "register_healing_effects": ("assurance.healing", "assurance.healing.effect.allocation.v2"),
    "project_healing_episode": ("assurance.healing", "assurance.healing.project-episode"),
    "assert_test_tree_unchanged_or_healing": (
        "assurance.healing",
        "assurance.healing.validator.test-tree.v1",
    ),
    "assert_test_changes_override_allowed": (
        "assurance.healing",
        "assurance.healing.validator.override.v1",
    ),
    "build_test_changes_override_token": (
        "assurance.healing",
        "assurance.healing.validator.override.v1",
    ),
    "load_test_changes_override_policy": (
        "assurance.healing",
        "assurance.healing.policy.test-change-policy.v1",
    ),
    "token_json_bytes": ("assurance.healing", "assurance.healing.validator.override.v1"),
    "reconcile_healing_allocation": (
        "assurance.healing",
        "assurance.healing.effect.allocation.v2",
    ),
    "reconcile_fixer_proposal_approved": (
        "assurance.healing",
        "assurance.healing.effect.proposal-approved.v1",
    ),
    "reconcile_heal_record_apply": ("assurance.healing", "assurance.healing.effect.heal-apply.v2"),
}


def _expand_root(root: str) -> tuple[str, ...]:
    path = REPO_ROOT / root
    if path.is_file():
        return (root.replace("\\", "/"),)
    if not path.is_dir():
        return ()
    return tuple(
        sorted(
            item.relative_to(REPO_ROOT).as_posix()
            for item in path.rglob("*.py")
            if "__pycache__" not in item.parts
        )
    )


def _expected_module_paths() -> dict[str, str]:
    assigned: dict[str, str] = {}

    def _claim(path: str, owner: str) -> None:
        previous = assigned.get(path)
        if previous is not None and previous != owner:
            raise AssertionError(f"overlap: {path} claimed by {previous} and {owner}")
        assigned[path] = owner

    for owner, roots in MODULE_OWNER_ROOTS.items():
        for root in roots:
            for path in _expand_root(root):
                _claim(path, owner)
    for path in GENERATION_VERIFICATION_FILES:
        _claim(path, "assurance.generation")
    for path in QUALITY_VERIFICATION_FILES:
        _claim(path, "assurance.quality")
    return assigned


def test_ledger_covers_every_legacy_operation_and_skill_exactly_once() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assert ledger.legacy_ids("operation") == legacy_operation_ids()
    assert ledger.legacy_ids("skill") == legacy_skill_ids()
    assert ledger.legacy_ids("persona") == legacy_persona_ids()
    assert ledger.legacy_ids("validator") == legacy_validator_ids()
    assert ledger.legacy_ids("effect") == legacy_effect_kinds()
    assert ledger.legacy_ids("hook") == legacy_hook_fields()
    assert ledger.legacy_ids("artifact") == legacy_artifact_types()
    live_resources = legacy_runtime_resource_paths()
    ledger_resources = ledger.legacy_ids("resource")
    if (REPO_ROOT / "assurance_agent").is_dir():
        assert ledger_resources == live_resources
    else:
        assert live_resources <= ledger_resources
    assert len(ledger.items) == len({(item.kind, item.legacy_id) for item in ledger.items})


def test_assurance_dependency_edges_are_exact_and_acyclic() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assert ledger.dependencies == {
        "assurance.intake": (),
        "assurance.generation": ("assurance.intake",),
        "assurance.execution": ("assurance.intake", "assurance.generation"),
        "assurance.healing": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
        ),
        "assurance.quality": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
            "assurance.healing",
        ),
        "assurance.improvement": (
            "assurance.intake",
            "assurance.generation",
            "assurance.execution",
            "assurance.healing",
            "assurance.quality",
        ),
    }
    assert ledger.topological_order() == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
    )


def test_collectors_scan_live_catalogs_not_the_ledger() -> None:
    payload = yaml.safe_load(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    assert legacy_validator_ids() == frozenset(
        item["legacy_id"] for item in payload["items"] if item["kind"] == "validator"
    )
    assert legacy_effect_kinds() == frozenset(
        item["legacy_id"] for item in payload["items"] if item["kind"] == "effect"
    )
    assert legacy_hook_fields() == frozenset(
        item["legacy_id"] for item in payload["items"] if item["kind"] == "hook"
    )
    artifact_ids = frozenset(item["legacy_id"] for item in payload["items"] if item["kind"] == "artifact")
    assert legacy_artifact_types() is ARTIFACT_NEW_IDS
    assert legacy_artifact_types() == artifact_ids
    assert legacy_artifact_types() is not artifact_ids
    yaml_ids = frozenset(
        item["legacy_id"]
        for item in yaml.safe_load(OWNERSHIP_PATH.read_text(encoding="utf-8"))["items"]
        if item["kind"] == "operation"
    )
    assert legacy_operation_ids() == yaml_ids
    assert legacy_operation_ids() is not yaml_ids
    skill_ids = frozenset(
        item["legacy_id"]
        for item in yaml.safe_load(OWNERSHIP_PATH.read_text(encoding="utf-8"))["items"]
        if item["kind"] == "skill"
    )
    persona_ids = frozenset(
        item["legacy_id"]
        for item in yaml.safe_load(OWNERSHIP_PATH.read_text(encoding="utf-8"))["items"]
        if item["kind"] == "persona"
    )
    assert legacy_skill_ids() == skill_ids
    assert legacy_persona_ids() == persona_ids


def test_operation_skill_and_persona_dispositions_are_exact() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    by_id = {(item.kind, item.legacy_id): item for item in ledger.items}

    for owner, operations in OPERATION_OWNERS.items():
        for operation_id in operations:
            item = by_id[("operation", operation_id)]
            slug = operation_id.removeprefix("operation:")
            assert item.disposition == "migrate"
            assert item.owner == owner
            assert item.new_id == f"{owner}.{slug}"
            assert item.status == "verified"
            assert item.verification == _operation_live_pointer(operation_id)

    for operation_id in REPLACE_PHASE5_OPERATIONS:
        item = by_id[("operation", operation_id)]
        assert item.disposition == "replace_phase5"
        assert item.owner is None
        assert item.new_id is None
    for operation_id in DELETE_PHASE6_OPERATIONS:
        item = by_id[("operation", operation_id)]
        assert item.disposition == "delete_phase6"
        assert item.owner is None
        assert item.new_id is None

    for owner, skills in SKILL_OWNERS.items():
        for skill_id in skills:
            item = by_id[("skill", skill_id)]
            assert item.disposition == "migrate"
            assert item.owner == owner
            assert item.new_id == f"{owner}.skill.{skill_id}.v1"
    assert by_id[("skill", "aa-workflow")].disposition == "replace_phase5"
    assert by_id[("skill", "writing-skills")].disposition == "retain_harness"

    for persona_id, owner in PERSONA_OWNERS.items():
        item = by_id[("persona", persona_id)]
        assert item.disposition == "migrate"
        assert item.owner == owner
        assert item.new_id == PERSONA_NEW_IDS[persona_id]


def test_validator_effect_and_hook_rows_use_declared_ids() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    by_id = {(item.kind, item.legacy_id): item for item in ledger.items}

    for validator_id, new_id in VALIDATOR_NEW_IDS.items():
        item = by_id[("validator", validator_id)]
        assert item.disposition == "migrate"
        assert item.owner == ".".join(new_id.split(".")[:2])
        assert item.new_id == new_id
        assert item.status == "verified"
        assert item.verification == VALIDATOR_VERIFICATION[validator_id]

    for effect_id, new_id in EFFECT_NEW_IDS.items():
        item = by_id[("effect", effect_id)]
        assert item.disposition == "migrate"
        assert item.owner == "assurance.healing"
        assert item.new_id == new_id
        assert item.status == "verified"
        assert item.verification == EFFECT_VERIFICATION

    pins = by_id[("hook", "semantic_pins")]
    assert pins.disposition == "delete_phase6"
    assert pins.owner is None
    assert pins.new_id is None
    assert pins.status == "planned"
    assert pins.verification is None

    for hook_name, (owner, seam) in HOOK_PRIMARY_SEAMS.items():
        item = by_id[("hook", hook_name)]
        assert item.disposition == "migrate"
        assert item.owner == owner
        assert item.new_id == seam
        assert item.status == "verified"
        assert item.verification == (
            "tests/phase4/test_product_hooks_parity.py::test_legacy_and_new_hook_paths_match_on_fixture"
        )
        assert seam.startswith(f"{owner}.")


def test_module_roots_expand_to_exact_files_without_overlap() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assigned = _expected_module_paths()
    assert len(assigned) == len(set(assigned))

    kernel_models = REPO_ROOT / "packages/assurance-kernel/assurance_kernel/artifacts/models"
    wrappers = REPO_ROOT / "assurance_agent/artifacts/models"
    verification = REPO_ROOT / "assurance_agent/verification"
    live_kernel = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in kernel_models.glob("*.py")
        if path.name != "__init__.py"
    }
    live_wrappers = {path.relative_to(REPO_ROOT).as_posix() for path in wrappers.glob("*.py")}
    live_verification = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in verification.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    live_owned_workflow = {
        path
        for roots in MODULE_OWNER_ROOTS.values()
        for root in roots
        if not root.endswith(".py")
        for path in _expand_root(root)
    }

    module_items = {item.legacy_id: item for item in ledger.items if item.kind == "module"}
    callable_items = {item.legacy_id: item for item in ledger.items if item.kind == "callable"}

    missing_kernel = live_kernel - set(module_items)
    missing_wrappers = live_wrappers - set(module_items)
    missing_verification = live_verification - set(module_items)
    missing_workflow = live_owned_workflow - set(module_items)
    assert missing_kernel == set()
    assert missing_wrappers == set()
    assert missing_verification == set()
    assert missing_workflow == set()

    for path, owner in assigned.items():
        item = module_items[path]
        assert item.disposition == "migrate"
        assert item.owner == owner
        assert item.new_id is None
        assert item.status == "planned"

    for path, disposition in NON_PHASE4_MODEL_DISPOSITIONS.items():
        item = module_items[path]
        assert item.disposition == disposition
        assert item.owner is None
        assert item.new_id is None

    for wrapper in live_wrappers:
        assert module_items[wrapper].disposition == "delete_phase6"

    for path in (
        "packages/assurance-kernel/assurance_kernel/artifacts/__init__.py",
        "assurance_agent/artifacts/__init__.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/__init__.py",
        "assurance_agent/verification/__init__.py",
        "assurance_agent/verification/checks/__init__.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/batch_id.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/canonical.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/paths.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/registry.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/validate.py",
        "assurance_agent/artifacts/batch_id.py",
        "assurance_agent/artifacts/canonical.py",
        "assurance_agent/artifacts/paths.py",
        "assurance_agent/artifacts/registry.py",
        "assurance_agent/artifacts/validate.py",
    ):
        assert module_items[path].disposition == "delete_phase6"

    for path in (
        "packages/assurance-kernel/assurance_kernel/artifacts/policy.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/policy_obligations.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/repo_registry.py",
        "assurance_agent/artifacts/policy.py",
        "assurance_agent/artifacts/policy_obligations.py",
        "assurance_agent/artifacts/repo_registry.py",
    ):
        assert module_items[path].disposition == "replace_phase5"

    assert set(callable_items) == set(CALLABLE_OWNER_OVERRIDES)
    for legacy_id, owner in CALLABLE_OWNER_OVERRIDES.items():
        item = callable_items[legacy_id]
        assert item.disposition == "migrate"
        assert item.owner == owner
        assert item.new_id is None
        assert item.status == "planned"

    review_module = module_items["packages/assurance-kernel/assurance_kernel/artifacts/models/review.py"]
    assert review_module.owner == "assurance.generation"


def test_migrate_new_ids_are_owner_qualified() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    for item in ledger.items:
        if item.disposition == "migrate":
            assert item.owner in ASSURANCE_OWNERS
            if item.new_id is not None:
                assert item.new_id.startswith(f"{item.owner}.")
        else:
            assert item.owner is None
            assert item.new_id is None
        if item.status == "planned":
            assert item.verification is None
        else:
            assert item.status == "verified"
            assert item.verification


def test_load_ownership_ledger_rejects_unknown_keys_and_bad_ids(tmp_path: Path) -> None:
    good = yaml.safe_load(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    extra_top = dict(good)
    extra_top["extra"] = True
    extra_path = tmp_path / "extra-top.yaml"
    extra_path.write_text(yaml.safe_dump(extra_top), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level"):
        load_ownership_ledger(extra_path)

    extra_item = dict(good)
    extra_item["items"] = [{**good["items"][0], "alias": "nope"}]
    extra_item_path = tmp_path / "extra-item.yaml"
    extra_item_path.write_text(yaml.safe_dump(extra_item), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown item"):
        load_ownership_ledger(extra_item_path)

    bad_id = dict(good)
    first = dict(good["items"][0])
    first["disposition"] = "migrate"
    first["owner"] = "assurance.intake"
    first["new_id"] = "assurance.generation.stolen"
    first["status"] = "planned"
    first["verification"] = None
    bad_id["items"] = [first]
    bad_id_path = tmp_path / "bad-id.yaml"
    bad_id_path.write_text(yaml.safe_dump(bad_id), encoding="utf-8")
    with pytest.raises(ValueError, match="owner prefix"):
        load_ownership_ledger(bad_id_path)

    verified = dict(good)
    first_verified = dict(good["items"][0])
    first_verified["status"] = "verified"
    first_verified["verification"] = None
    verified["items"] = [first_verified]
    verified_path = tmp_path / "verified.yaml"
    verified_path.write_text(yaml.safe_dump(verified), encoding="utf-8")
    with pytest.raises(ValueError, match="verification"):
        load_ownership_ledger(verified_path)


def test_ledger_schema_version_is_one() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    assert isinstance(ledger, OwnershipLedger)
    assert ledger.schema_version == "1"


_EXTRACTED_KINDS = frozenset(
    {
        "operation",
        "skill",
        "persona",
        "validator",
        "effect",
        "hook",
        "artifact",
        "schema",
        "resource",
    }
)

_VERIFICATION_NODE_RE = re.compile(r"^[\w./-]+\.py::[A-Za-z_][\w]*(?:\[[^\]]+\])?$")


def _verification_names_row(item: object) -> bool:
    pointer = getattr(item, "verification") or ""
    legacy_id = getattr(item, "legacy_id")
    new_id = getattr(item, "new_id")
    if legacy_id in pointer or (new_id is not None and new_id in pointer):
        return True
    path, _, _node = pointer.partition("::")
    source_path = REPO_ROOT / path
    if not source_path.is_file():
        return False
    source = source_path.read_text(encoding="utf-8")
    return legacy_id in source or (new_id is not None and new_id in source)


def test_phase4_ownership_is_fully_verified() -> None:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    for item in ledger.items:
        if item.disposition == "migrate" and item.kind in _EXTRACTED_KINDS:
            assert item.status == "verified", (item.kind, item.legacy_id)
            assert item.verification
        if item.status == "verified":
            assert item.verification
            assert _VERIFICATION_NODE_RE.fullmatch(item.verification), (item.kind, item.legacy_id)
            if item.kind in {"operation", "artifact"}:
                assert _verification_names_row(item), (item.kind, item.legacy_id, item.verification)
        if item.disposition == "migrate" and item.kind in {"module", "callable"}:
            assert item.status == "planned"
            assert item.verification is None

    pins = next(item for item in ledger.items if item.kind == "hook" and item.legacy_id == "semantic_pins")
    assert pins.disposition == "delete_phase6"
    assert pins.status == "planned"
    assert pins.owner is None
    assert pins.new_id is None
    assert pins.verification is None
