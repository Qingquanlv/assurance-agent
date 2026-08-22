from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / (
    ".superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly"
)

PREPARE_IDS = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)

ALL_BINDING_IDS = tuple(
    alias
    for prepare_id in PREPARE_IDS
    for alias in (
        "assurance.product.agent."
        + prepare_id.removeprefix("assurance.").removesuffix(".prepare")
        + phase
        for phase in (".prepare", ".execute", ".finalize")
    )
)

EXPECTED_25_CASE_IDS = (
    "full-api-only-success",
    "full-e2e-only-success",
    "full-fuzz-only-success",
    "full-performance-only-success",
    "full-all-four-family-success",
    "intake-review-needs-fix-then-pass",
    "plan-review-invalid-output-bounded-retry",
    "codegen-validation-failure-bounded-fix",
    "execution-closed-mapping-no-stale-test",
    "coverage-insufficient-repair-reexecution-pass",
    "coverage-repair-no-progress-exhausted",
    "healing-disallowed-business-stop",
    "report-generation-required-outputs",
    "issue-analysis-reconcile-path",
    "archive-durable-effect-replay",
    "retro-collect-analyze-propose-reconcile",
    "improvement-review-evaluate-export-apply",
    "improvement-rollback",
    "human-interrupt-exact-resume",
    "transient-local-retry",
    "opencode-ambiguous-create-recovery",
    "cursor-unknown-process-indeterminate",
    "engine-crash-after-provider-terminal-receipt",
    "config-model-graph-source-drift-rejection",
    "replay-after-provider-state-removal",
)


def load_yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value
