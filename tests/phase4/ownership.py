"""Test-only Phase 4 ownership ledger and live legacy-catalog collectors."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_kernel.artifacts.registry import REGISTRY
from assurance_kernel.workflow.core.product_hooks import ProductHooks
from assurance_kernel.workflow.graph.durable_effects import KNOWN_DURABLE_EFFECT_KINDS
from assurance_kernel.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS

Kind = Literal[
    "module",
    "callable",
    "operation",
    "skill",
    "persona",
    "schema",
    "artifact",
    "validator",
    "effect",
    "resource",
    "hook",
]
Disposition = Literal["migrate", "replace_phase5", "retain_harness", "delete_phase6"]
Status = Literal["planned", "verified"]

ASSURANCE_OWNERS: tuple[str, ...] = (
    "assurance.intake",
    "assurance.generation",
    "assurance.execution",
    "assurance.healing",
    "assurance.quality",
    "assurance.improvement",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNERSHIP_PATH = (
    REPO_ROOT
    / ".superpowers"
    / "sdd"
    / "2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction"
    / "ownership.yaml"
)

_TOP_LEVEL_KEYS = frozenset({"schema_version", "dependencies", "items"})
_ITEM_KEYS = frozenset({"kind", "legacy_id", "disposition", "owner", "new_id", "status", "verification"})
_KINDS = frozenset(Kind.__args__)
_DISPOSITIONS = frozenset(Disposition.__args__)
_STATUSES = frozenset(Status.__args__)
_NEW_ID_RE = re.compile(
    r"^assurance\.(intake|generation|execution|healing|quality|improvement)"
    r"(\.[A-Za-z0-9][A-Za-z0-9.-]*)+$"
)
_EXECUTABLE_SUFFIXES = frozenset({".py", ".sh", ".js", ".mjs", ".ts"})
_PRODUCT_RESOURCE_PREFIXES = (
    ("schemas", "workflow-schema.yaml"),
    ("schemas", "execution-contracts.yaml"),
    ("skills",),
    ("opencode", "agents"),
)

MODULE_OWNER_ROOTS: dict[str, tuple[str, ...]] = {
    "assurance.intake": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/common.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/cases.py",
    ),
    "assurance.generation": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/assurance.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/codegen.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/generated_files.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/plan_checks.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/review.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/discovery.py",
        "assurance_agent/workflow/discovery",
    ),
    "assurance.execution": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/execution.py",
        "assurance_agent/workflow/execution",
    ),
    "assurance.healing": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/healing.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/healing_codegen.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_repair.py",
        "assurance_agent/workflow/healing",
    ),
    "assurance.quality": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_gaps.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/c_layer.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/explore.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/inspect.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/issue_events.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/issues.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/metrics.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/minimum_coverage.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/pr_metric_evidence.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/quarantine.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/report.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/sufficiency.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/trace.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/trace_sufficiency.py",
        "assurance_agent/workflow/issues",
        "assurance_agent/workflow/metrics",
        "assurance_agent/workflow/report",
        "assurance_agent/evidence",
    ),
    "assurance.improvement": (
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_outbox.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_review.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/improvements.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/declarations.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/promotion.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_batch.py",
        "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_v3.py",
        "assurance_agent/workflow/improvements",
        "assurance_agent/retro",
    ),
}

GENERATION_VERIFICATION_FILES: tuple[str, ...] = (
    "assurance_agent/verification/applicability.py",
    "assurance_agent/verification/checks/assert_ideal.py",
    "assurance_agent/verification/checks/base.py",
    "assurance_agent/verification/checks/capability_keys.py",
    "assurance_agent/verification/checks/l1_path.py",
    "assurance_agent/verification/checks/registry.py",
    "assurance_agent/verification/checks/shared_factory.py",
    "assurance_agent/verification/contract_render.py",
    "assurance_agent/verification/generated_entries.py",
    "assurance_agent/verification/generated_files.py",
    "assurance_agent/verification/manifest.py",
    "assurance_agent/verification/oracle.py",
    "assurance_agent/verification/plan_checks.py",
    "assurance_agent/verification/profile_manifest.py",
    "assurance_agent/verification/profiles.py",
    "assurance_agent/verification/property_scan.py",
)

QUALITY_VERIFICATION_FILES: tuple[str, ...] = (
    "assurance_agent/verification/assertion_class.py",
    "assurance_agent/verification/baseline_history.py",
    "assurance_agent/verification/gate_state.py",
    "assurance_agent/verification/mutation_cache.py",
    "assurance_agent/verification/mutation_runner.py",
    "assurance_agent/verification/mutation_sampling.py",
    "assurance_agent/verification/promotion_gate.py",
    "assurance_agent/verification/replay.py",
)

NON_PHASE4_MODEL_DISPOSITIONS: dict[str, Disposition] = {
    "packages/assurance-kernel/assurance_kernel/artifacts/models/data_knowledge.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/policy.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/state.py": "replace_phase5",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/eval_projection.py": "retain_harness",
}

CALLABLE_OWNER_OVERRIDES: dict[str, str] = {
    "assurance_kernel.artifacts.models.review.CaseSourceClaim": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseSourceVerification": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseMinimumCoverageReview": "assurance.intake",
    "assurance_kernel.artifacts.models.review.CaseReviewAuthoring": "assurance.intake",
}

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
        "aa-api-codegen-fixer",
        "aa-e2e-plan",
        "aa-e2e-plan-reviewer",
        "aa-e2e-codegen",
        "aa-e2e-codegen-fixer",
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

SKILL_DISPOSITIONS: dict[str, Disposition] = {
    "aa-workflow": "replace_phase5",
    "writing-skills": "retain_harness",
}

PERSONA_OWNERS: dict[str, str] = {
    "aa-intake-host": "assurance.intake",
    "aa-explorer": "assurance.intake",
    "aa-doc-author": "assurance.intake",
    "aa-test-author": "assurance.generation",
    "aa-reviewer": "assurance.generation",
    "aa-reporter": "assurance.quality",
    "aa-archiver": "assurance.improvement",
}

PERSONA_NEW_IDS: dict[str, str] = {
    "aa-intake-host": "assurance.intake.persona.intake-host.v1",
    "aa-explorer": "assurance.intake.persona.explorer.v1",
    "aa-doc-author": "assurance.intake.persona.doc-author.v1",
    "aa-test-author": "assurance.generation.persona.test-author.v1",
    "aa-reviewer": "assurance.generation.persona.reviewer.v1",
    "aa-reporter": "assurance.quality.persona.reporter.v1",
    "aa-archiver": "assurance.improvement.persona.archiver.v1",
}

VALIDATOR_NEW_IDS: dict[str, str] = {
    "generated_files_candidate/v1": "assurance.generation.validator.generated-files.v1",
    "codegen_fix_candidate/v1": "assurance.generation.validator.codegen-fix-candidate.v1",
    "plan_mechanical_candidate/v1": "assurance.generation.validator.plan-mechanical.v1",
    "archive_integrity/v1": "assurance.improvement.validator.archive-integrity.v1",
    "problem_apply_candidate/v1": "assurance.quality.validator.problem-apply.v1",
    "cross_artifact_invariants/v1": "assurance.quality.validator.cross-artifact.v1",
}

EFFECT_NEW_IDS: dict[str, str] = {
    "healing_allocation/v2": "assurance.healing.effect.allocation.v2",
    "fixer_proposal_approved/v1": "assurance.healing.effect.proposal-approved.v1",
    "heal_record_apply/v2": "assurance.healing.effect.heal-apply.v2",
}

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

DELETE_PHASE6_MODULES: tuple[str, ...] = (
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
)

REPLACE_PHASE5_ARTIFACT_MODULES: tuple[str, ...] = (
    "packages/assurance-kernel/assurance_kernel/artifacts/policy.py",
    "packages/assurance-kernel/assurance_kernel/artifacts/policy_obligations.py",
    "packages/assurance-kernel/assurance_kernel/artifacts/repo_registry.py",
    "assurance_agent/artifacts/policy.py",
    "assurance_agent/artifacts/policy_obligations.py",
    "assurance_agent/artifacts/repo_registry.py",
)

RESOURCE_LEFTOVER_OVERRIDES: dict[str, tuple[Disposition, str | None]] = {
    "assurance_agent/_resources/skills/aa-dashboard/scripts/case-center.html": (
        "delete_phase6",
        None,
    ),
    "assurance_agent/_resources/skills/aa-dashboard/scripts/server.py": ("delete_phase6", None),
    "assurance_agent/_resources/skills/aa-dashboard/scripts/start-server.sh": ("delete_phase6", None),
    "assurance_agent/_resources/skills/aa-dashboard/scripts/stop-server.sh": ("delete_phase6", None),
}

ARTIFACT_LEFTOVER_TYPES = frozenset(
    {
        "advisory",
        "discovery_generated_manifest",
        "discovery_oracle_set",
        "discovery_promotion_manifest",
        "discovery_replay_attempt_receipt",
        "discovery_round_decision",
        "improvement_reconcile_outbox_v1",
        "issue_evidence_manifest",
        "issue_reconcile_status",
        "issue_triage_advice",
        "metrics_nightly_document",
        "retro_eval_evidence_slice_v3",
        "retro_eval_signal_v3",
        "retro_issue_evidence_slice_v3",
        "retro_issue_signal_v3",
        "retro_workflow_evidence_slice_v3",
        "retro_workflow_signal_v3",
    }
)

RESOURCE_PATH_HINTS: dict[str, tuple[Disposition, str | None]] = {
    "assurance_agent/_resources/schemas/workflow-schema.yaml": ("replace_phase5", None),
    "assurance_agent/_resources/schemas/execution-contracts.yaml": ("replace_phase5", None),
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/policy-default.yaml": (
        "replace_phase5",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/ingest-artifact-catalog.yaml": (
        "replace_phase5",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/rules/failure-classification.yaml": (
        "delete_phase6",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/explore-advisory.schema.json": (
        "delete_phase6",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/explore-context.schema.json": (
        "delete_phase6",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/plugins/aa.mjs": (
        "replace_phase5",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/tools/artifact_write.ts": (
        "replace_phase5",
        None,
    ),
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/tools/workflow_start.ts": (
        "replace_phase5",
        None,
    ),
}


@dataclass(frozen=True, slots=True)
class OwnershipItem:
    kind: Kind
    legacy_id: str
    disposition: Disposition
    owner: str | None
    new_id: str | None
    status: Status
    verification: str | None


@dataclass(frozen=True, slots=True)
class OwnershipLedger:
    schema_version: Literal["1"]
    dependencies: Mapping[str, tuple[str, ...]]
    items: tuple[OwnershipItem, ...]

    def legacy_ids(self, kind: str) -> frozenset[str]:
        return frozenset(item.legacy_id for item in self.items if item.kind == kind)

    def topological_order(self) -> tuple[str, ...]:
        return _stable_topological_order(self.dependencies)


def _stable_topological_order(dependencies: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    remaining: dict[str, set[str]] = {node: set(deps) for node, deps in dependencies.items()}
    for deps in list(remaining.values()):
        for dep in deps:
            remaining.setdefault(dep, set())
    ordered: list[str] = []
    while remaining:
        ready = sorted(node for node, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError("cyclic dependency graph")
        node = ready[0]
        ordered.append(node)
        del remaining[node]
        for other_deps in remaining.values():
            other_deps.discard(node)
    return tuple(ordered)


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a string-key mapping")
    return {str(key): item for key, item in value.items()}


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, label)


def _validate_new_id(item: OwnershipItem) -> None:
    if item.disposition == "migrate":
        if item.owner not in ASSURANCE_OWNERS:
            raise ValueError(f"migrate item {item.legacy_id!r} requires one of {ASSURANCE_OWNERS}")
        if item.new_id is not None:
            if not _NEW_ID_RE.fullmatch(item.new_id):
                raise ValueError(f"non-canonical new_id: {item.new_id!r}")
            if not item.new_id.startswith(f"{item.owner}."):
                raise ValueError(f"new_id {item.new_id!r} owner prefix differs from {item.owner!r}")
    elif item.owner is not None or item.new_id is not None:
        raise ValueError(f"{item.disposition} item {item.legacy_id!r} must have null owner and new_id")
    if item.status == "verified":
        if not item.verification:
            raise ValueError(f"verified item {item.legacy_id!r} requires verification")
    elif item.verification is not None:
        raise ValueError(f"planned item {item.legacy_id!r} must have null verification")


def _parse_item(raw: object, index: int) -> OwnershipItem:
    data = _require_mapping(raw, f"items[{index}]")
    unknown = set(data) - _ITEM_KEYS
    if unknown:
        raise ValueError(f"unknown item keys: {sorted(unknown)}")
    kind = _as_str(data.get("kind"), "kind")
    if kind not in _KINDS:
        raise ValueError(f"unknown kind: {kind}")
    disposition = _as_str(data.get("disposition"), "disposition")
    if disposition not in _DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition}")
    status = _as_str(data.get("status"), "status")
    if status not in _STATUSES:
        raise ValueError(f"unknown status: {status}")
    item = OwnershipItem(
        kind=kind,  # type: ignore[arg-type]
        legacy_id=_as_str(data.get("legacy_id"), "legacy_id"),
        disposition=disposition,  # type: ignore[arg-type]
        owner=_optional_str(data.get("owner"), "owner"),
        new_id=_optional_str(data.get("new_id"), "new_id"),
        status=status,  # type: ignore[arg-type]
        verification=_optional_str(data.get("verification"), "verification"),
    )
    _validate_new_id(item)
    return item


def load_ownership_ledger(path: Path) -> OwnershipLedger:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = _require_mapping(raw, "ownership ledger")
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"unknown top-level keys: {sorted(unknown)}")
    schema_version = data.get("schema_version")
    if schema_version != "1":
        raise ValueError(f"unsupported schema_version: {schema_version!r}")
    dependencies_raw = _require_mapping(data.get("dependencies"), "dependencies")
    dependencies: dict[str, tuple[str, ...]] = {}
    for key, value in dependencies_raw.items():
        owner = _as_str(key, "dependency owner")
        if owner not in ASSURANCE_OWNERS:
            raise ValueError(f"unknown dependency owner: {owner}")
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"dependencies[{owner}] must be a list of strings")
        dependencies[owner] = tuple(str(item) for item in value)
    if tuple(sorted(dependencies)) != tuple(sorted(ASSURANCE_OWNERS)):
        raise ValueError("dependencies must name the six Assurance owners exactly")
    items_raw = data.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("items must be a list")
    items = tuple(_parse_item(item, index) for index, item in enumerate(items_raw))
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.kind, item.legacy_id)
        if key in seen:
            raise ValueError(f"duplicate ledger row: {key}")
        seen.add(key)
    return OwnershipLedger(schema_version="1", dependencies=dependencies, items=items)


def _skills_root() -> Path:
    return REPO_ROOT / "assurance_agent" / "_resources" / "skills"


def _personas_root() -> Path:
    return REPO_ROOT / "assurance_agent" / "_resources" / "opencode" / "agents"


def legacy_operation_ids() -> frozenset[str]:
    return frozenset(default_operations())


def legacy_skill_ids() -> frozenset[str]:
    return frozenset(
        path.name for path in _skills_root().iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    )


def legacy_persona_ids() -> frozenset[str]:
    return frozenset(path.stem for path in _personas_root().glob("*.md") if path.is_file())


def legacy_validator_ids() -> frozenset[str]:
    return frozenset(KNOWN_PRECOMMIT_VALIDATORS)


def legacy_effect_kinds() -> frozenset[str]:
    return frozenset(KNOWN_DURABLE_EFFECT_KINDS)


def legacy_hook_fields() -> frozenset[str]:
    return frozenset(field.name for field in fields(ProductHooks))


def legacy_artifact_types() -> frozenset[str]:
    return frozenset(spec.artifact_type for spec in REGISTRY)


def _posix(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_product_rel(parts: tuple[str, ...]) -> bool:
    for prefix in _PRODUCT_RESOURCE_PREFIXES:
        if parts[: len(prefix)] == prefix:
            return True
    return False


def _resolve_resource_parts(parts: tuple[str, ...]) -> Path:
    root = (
        REPO_ROOT / "assurance_agent" / "_resources"
        if _is_product_rel(parts)
        else REPO_ROOT / "packages" / "assurance-kernel" / "assurance_kernel" / "_resources"
    )
    return root.joinpath(*parts)


def _iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        if child.is_file() and "__pycache__" not in child.parts:
            yield child


def _excluded_primary_resource(path: Path) -> bool:
    rel = _posix(path)
    if path.name == "SKILL.md" and "/_resources/skills/" in f"/{rel}":
        return True
    if path.name == "__init__.py" and path.parent.name == "_resources":
        return True
    return "/_resources/opencode/agents/" in f"/{rel}" and path.suffix == ".md"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _constant_string_args(node: ast.Call) -> tuple[str, ...] | None:
    parts: list[str] = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            parts.append(arg.value)
        else:
            return None
    return tuple(parts)


def _scan_resource_references() -> set[tuple[str, ...]]:
    references: set[tuple[str, ...]] = set()
    roots = (
        REPO_ROOT / "assurance_agent",
        REPO_ROOT / "packages" / "assurance-kernel" / "assurance_kernel",
    )
    for root in roots:
        for py_file in root.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _call_name(node) in {
                    "read_text",
                    "read_bytes",
                    "exists",
                    "iter_children",
                }:
                    parts = _constant_string_args(node)
                    if parts:
                        references.add(parts)
                if isinstance(node, ast.Tuple) and all(
                    isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in node.elts
                ):
                    values = tuple(str(elt.value) for elt in node.elts if isinstance(elt, ast.Constant))
                    if values and values[0] in {"schemas", "skills", "opencode", "rules"}:
                        references.add(values)
    return references


def _skill_owner_map() -> dict[str, tuple[Disposition, str | None]]:
    mapping: dict[str, tuple[Disposition, str | None]] = {}
    for owner, skills in SKILL_OWNERS.items():
        for skill_id in skills:
            mapping[skill_id] = ("migrate", owner)
    for skill_id, disposition in SKILL_DISPOSITIONS.items():
        mapping[skill_id] = (disposition, None)
    return mapping


def _claim_resource(
    claimed: dict[str, tuple[Disposition, str | None]],
    path: str,
    assignment: tuple[Disposition, str | None],
) -> None:
    previous = claimed.get(path)
    if previous is not None and previous != assignment:
        raise ValueError(f"resource {path} assigned to {previous} and {assignment}")
    claimed[path] = assignment


def _collect_runtime_resources() -> dict[str, tuple[Disposition, str | None]]:
    claimed: dict[str, tuple[Disposition, str | None]] = {}
    skill_owners = _skill_owner_map()
    for skill_id, assignment in skill_owners.items():
        skill_dir = _skills_root() / skill_id
        if not skill_dir.is_dir():
            raise ValueError(f"missing skill directory: {skill_id}")
        for file_path in _iter_files(skill_dir):
            if file_path.name == "SKILL.md":
                continue
            rel = _posix(file_path)
            if rel in RESOURCE_LEFTOVER_OVERRIDES:
                _claim_resource(claimed, rel, RESOURCE_LEFTOVER_OVERRIDES[rel])
                continue
            _claim_resource(claimed, rel, assignment)

    for parts in _scan_resource_references():
        resolved = _resolve_resource_parts(parts)
        if not resolved.exists():
            raise ValueError(f"missing referenced resource: {'/'.join(parts)}")
        for file_path in _iter_files(resolved):
            if _excluded_primary_resource(file_path):
                continue
            rel = _posix(file_path)
            if rel in RESOURCE_LEFTOVER_OVERRIDES:
                _claim_resource(claimed, rel, RESOURCE_LEFTOVER_OVERRIDES[rel])
                continue
            if rel in RESOURCE_PATH_HINTS:
                _claim_resource(claimed, rel, RESOURCE_PATH_HINTS[rel])
                continue
            if "/_resources/skills/" in f"/{rel}":
                parts_rel = Path(rel).parts
                try:
                    skill_id = parts_rel[parts_rel.index("skills") + 1]
                except (ValueError, IndexError) as exc:
                    raise ValueError(f"unowned skill resource: {rel}") from exc
                if skill_id not in skill_owners:
                    raise ValueError(f"unreferenced skill resource owner: {rel}")
                _claim_resource(claimed, rel, skill_owners[skill_id])
                continue
            raise ValueError(f"unassigned referenced resource: {rel}")

    for rel, assignment in RESOURCE_PATH_HINTS.items():
        if (REPO_ROOT / rel).is_file():
            _claim_resource(claimed, rel, assignment)
    for rel, assignment in RESOURCE_LEFTOVER_OVERRIDES.items():
        if (REPO_ROOT / rel).is_file():
            claimed[rel] = assignment

    executables: set[str] = set()
    for root in (
        REPO_ROOT / "assurance_agent" / "_resources",
        REPO_ROOT / "packages" / "assurance-kernel" / "assurance_kernel" / "_resources",
    ):
        for file_path in _iter_files(root):
            if _excluded_primary_resource(file_path):
                continue
            if file_path.suffix in _EXECUTABLE_SUFFIXES:
                executables.add(_posix(file_path))
    missing_executables = executables - set(claimed)
    if missing_executables:
        raise ValueError(f"unreferenced executable resources: {sorted(missing_executables)}")
    return claimed


def legacy_runtime_resource_paths() -> frozenset[str]:
    return frozenset(_collect_runtime_resources())


def _expand_root(root: str) -> tuple[str, ...]:
    path = REPO_ROOT / root
    if path.is_file():
        return (root.replace("\\", "/"),)
    if not path.is_dir():
        raise ValueError(f"missing owner root: {root}")
    return tuple(sorted(_posix(item) for item in path.rglob("*.py") if "__pycache__" not in item.parts))


def expand_owned_modules() -> dict[str, str]:
    assigned: dict[str, str] = {}
    for owner, roots in MODULE_OWNER_ROOTS.items():
        for root in roots:
            for path in _expand_root(root):
                previous = assigned.get(path)
                if previous is not None and previous != owner:
                    raise ValueError(f"module overlap: {path} claimed by {previous} and {owner}")
                assigned[path] = owner
    for path in GENERATION_VERIFICATION_FILES:
        previous = assigned.get(path)
        if previous is not None and previous != "assurance.generation":
            raise ValueError(f"module overlap: {path}")
        assigned[path] = "assurance.generation"
    for path in QUALITY_VERIFICATION_FILES:
        previous = assigned.get(path)
        if previous is not None and previous != "assurance.quality":
            raise ValueError(f"module overlap: {path}")
        assigned[path] = "assurance.quality"
    return assigned


def _item(
    kind: Kind,
    legacy_id: str,
    disposition: Disposition,
    owner: str | None,
    new_id: str | None,
) -> OwnershipItem:
    return OwnershipItem(
        kind=kind,
        legacy_id=legacy_id,
        disposition=disposition,
        owner=owner,
        new_id=new_id,
        status="planned",
        verification=None,
    )


def _operation_owner(operation_id: str) -> tuple[Disposition, str | None, str | None]:
    if operation_id in REPLACE_PHASE5_OPERATIONS:
        return "replace_phase5", None, None
    if operation_id in DELETE_PHASE6_OPERATIONS:
        return "delete_phase6", None, None
    for owner, operations in OPERATION_OWNERS.items():
        if operation_id in operations:
            return "migrate", owner, f"{owner}.{operation_id.removeprefix('operation:')}"
    raise ValueError(f"unassigned operation: {operation_id}")


def _skill_assignment(skill_id: str) -> tuple[Disposition, str | None, str | None]:
    if skill_id in SKILL_DISPOSITIONS:
        return SKILL_DISPOSITIONS[skill_id], None, None
    for owner, skills in SKILL_OWNERS.items():
        if skill_id in skills:
            return "migrate", owner, f"{owner}.skill.{skill_id}.v1"
    raise ValueError(f"unassigned skill: {skill_id}")


def _model_file_for_type(model: type[object]) -> str:
    module = model.__module__
    if not module.startswith("assurance_kernel.artifacts.models."):
        raise ValueError(f"artifact model {model} is not a kernel artifact model")
    return "packages/assurance-kernel/" + module.replace(".", "/") + ".py"


def _artifact_assignment(artifact_type: str) -> tuple[Disposition, str | None]:
    if artifact_type in ARTIFACT_LEFTOVER_TYPES:
        return "delete_phase6", None
    specs = [spec for spec in REGISTRY if spec.artifact_type == artifact_type]
    if not specs:
        raise ValueError(f"unknown artifact type: {artifact_type}")
    owners: set[tuple[Disposition, str | None]] = set()
    module_owners = expand_owned_modules()
    for spec in specs:
        model_path = _model_file_for_type(spec.model)
        if model_path in NON_PHASE4_MODEL_DISPOSITIONS:
            owners.add((NON_PHASE4_MODEL_DISPOSITIONS[model_path], None))
            continue
        owner = module_owners.get(model_path)
        if owner is None:
            raise ValueError(f"unassigned artifact model {model_path} for {artifact_type}")
        owners.add(("migrate", owner))
    if len(owners) != 1:
        raise ValueError(f"artifact type {artifact_type} has conflicting owners: {owners}")
    return next(iter(owners))


def seed_ownership_items() -> tuple[OwnershipItem, ...]:
    items: list[OwnershipItem] = []
    module_owners = expand_owned_modules()
    for path, owner in sorted(module_owners.items()):
        items.append(_item("module", path, "migrate", owner, None))
    for path, disposition in NON_PHASE4_MODEL_DISPOSITIONS.items():
        items.append(_item("module", path, disposition, None, None))
    wrappers = REPO_ROOT / "assurance_agent" / "artifacts" / "models"
    for wrapper in sorted(wrappers.glob("*.py")):
        items.append(_item("module", _posix(wrapper), "delete_phase6", None, None))
    for path in DELETE_PHASE6_MODULES:
        items.append(_item("module", path, "delete_phase6", None, None))
    for path in REPLACE_PHASE5_ARTIFACT_MODULES:
        items.append(_item("module", path, "replace_phase5", None, None))

    for legacy_id, owner in CALLABLE_OWNER_OVERRIDES.items():
        items.append(_item("callable", legacy_id, "migrate", owner, None))

    for operation_id in sorted(legacy_operation_ids()):
        disposition, owner, new_id = _operation_owner(operation_id)
        items.append(_item("operation", operation_id, disposition, owner, new_id))

    for skill_id in sorted(legacy_skill_ids()):
        disposition, owner, new_id = _skill_assignment(skill_id)
        items.append(_item("skill", skill_id, disposition, owner, new_id))

    for persona_id in sorted(legacy_persona_ids()):
        owner = PERSONA_OWNERS[persona_id]
        items.append(_item("persona", persona_id, "migrate", owner, PERSONA_NEW_IDS[persona_id]))

    for validator_id in sorted(legacy_validator_ids()):
        new_id = VALIDATOR_NEW_IDS[validator_id]
        owner = ".".join(new_id.split(".")[:2])
        items.append(_item("validator", validator_id, "migrate", owner, new_id))

    for effect_id in sorted(legacy_effect_kinds()):
        items.append(_item("effect", effect_id, "migrate", "assurance.healing", EFFECT_NEW_IDS[effect_id]))

    for hook_name in sorted(legacy_hook_fields()):
        if hook_name == "semantic_pins":
            items.append(_item("hook", hook_name, "delete_phase6", None, None))
            continue
        owner, seam = HOOK_PRIMARY_SEAMS[hook_name]
        items.append(_item("hook", hook_name, "migrate", owner, seam))

    for artifact_type in sorted(legacy_artifact_types()):
        disposition, owner = _artifact_assignment(artifact_type)
        items.append(_item("artifact", artifact_type, disposition, owner, None))

    for path, (disposition, owner) in sorted(_collect_runtime_resources().items()):
        items.append(_item("resource", path, disposition, owner, None))

    seen: set[tuple[str, str]] = set()
    unique: list[OwnershipItem] = []
    for item in items:
        key = (item.kind, item.legacy_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


def default_dependencies() -> dict[str, tuple[str, ...]]:
    return {
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
