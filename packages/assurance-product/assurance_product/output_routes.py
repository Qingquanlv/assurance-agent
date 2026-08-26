from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from assurance_product.change_workspace import safe_change_id, safe_relative_path
from assurance_product.models import PREPARE_IDS


def execute_alias_for_prepare(prepare_id: str) -> str:
    return prepare_id.removesuffix(".prepare") + ".execute"


def _change(change_id: str, *parts: str) -> str:
    return "/".join(("qa/changes", safe_change_id(change_id), *parts))


def _sorted_paths(*paths: str) -> tuple[str, ...]:
    normalized = tuple(safe_relative_path(path).as_posix() for path in paths)
    return tuple(sorted(set(normalized)))


def _plan_outputs(change_id: str, family: str) -> tuple[str, ...]:
    names = {
        "api": (
            "api-plan.md",
            "api-test-data-plan.md",
            "api-codegen-plan.md",
            "api-codegen-mapping.json",
            "m3-review-summary.md",
        ),
        "e2e": (
            "e2e-plan.md",
            "e2e-test-data-plan.md",
            "e2e-codegen-plan.md",
            "e2e-codegen-mapping.json",
            "m4-review-summary.md",
        ),
        "fuzz": (
            "fuzz-plan.md",
            "fuzz-codegen-plan.md",
            "fuzz-codegen-mapping.json",
            "fuzz-review-summary.md",
        ),
        "performance": (
            "performance-plan.md",
            "performance-codegen-plan.md",
            "performance-codegen-mapping.json",
            "performance-review-summary.md",
        ),
    }[family]
    return _sorted_paths(*(_change(change_id, "plans", name) for name in names))


def _review_outputs(change_id: str, family: str) -> tuple[str, ...]:
    return _sorted_paths(
        _change(change_id, "review", f"{family}-plan-review.json"),
        _change(change_id, "review", f"{family}-plan-review-summary.md"),
    )


def _codegen_outputs(change_id: str, family: str, *, fix: bool = False) -> tuple[str, ...]:
    suffix = "-fix" if fix else ""
    return _sorted_paths(
        _change(change_id, "codegen", f"{family}-codegen{suffix}-summary.md"),
        _change(change_id, "codegen", f"{family}-generated-files.json"),
    )


_ROUTE_BUILDERS: Mapping[str, Callable[[str], tuple[str, ...]]] = MappingProxyType(
    {
        "assurance.intake.intake.execute": lambda change_id: _sorted_paths(
            _change(change_id, ".qa.yaml"),
            _change(change_id, "requirement.md"),
        ),
        "assurance.intake.explore.execute": lambda change_id: _sorted_paths(
            _change(change_id, "explore", "exploration.json"),
        ),
        "assurance.intake.case-design.execute": lambda change_id: _sorted_paths(
            _change(change_id, ".qa.yaml"),
            _change(change_id, "proposal.md"),
            _change(change_id, "trace", "minimum-coverage-matrix.json"),
        ),
        "assurance.intake.case-review.execute": lambda change_id: _sorted_paths(
            _change(change_id, "review", "case-review.json"),
            _change(change_id, "review", "case-review-summary.md"),
        ),
        "assurance.generation.api.plan.execute": lambda change_id: _plan_outputs(change_id, "api"),
        "assurance.generation.api.plan-review.execute": lambda change_id: _review_outputs(change_id, "api"),
        "assurance.generation.e2e.plan.execute": lambda change_id: _plan_outputs(change_id, "e2e"),
        "assurance.generation.e2e.plan-review.execute": lambda change_id: _review_outputs(change_id, "e2e"),
        "assurance.generation.fuzz.plan.execute": lambda change_id: _plan_outputs(change_id, "fuzz"),
        "assurance.generation.fuzz.plan-review.execute": lambda change_id: _review_outputs(change_id, "fuzz"),
        "assurance.generation.performance.plan.execute": lambda change_id: _plan_outputs(
            change_id, "performance"
        ),
        "assurance.generation.performance.plan-review.execute": lambda change_id: _review_outputs(
            change_id, "performance"
        ),
        "assurance.generation.api.codegen.execute": lambda change_id: _codegen_outputs(change_id, "api"),
        "assurance.generation.api.codegen-fix.execute": lambda change_id: _codegen_outputs(
            change_id, "api", fix=True
        ),
        "assurance.generation.e2e.codegen.execute": lambda change_id: _codegen_outputs(change_id, "e2e"),
        "assurance.generation.e2e.codegen-fix.execute": lambda change_id: _codegen_outputs(
            change_id, "e2e", fix=True
        ),
        "assurance.generation.fuzz.codegen.execute": lambda change_id: _codegen_outputs(change_id, "fuzz"),
        "assurance.generation.performance.codegen.execute": lambda change_id: _codegen_outputs(
            change_id, "performance"
        ),
        "assurance.execution.execute.execute": lambda change_id: _sorted_paths(
            _change(change_id, "execution", "execute-result.json"),
        ),
        "assurance.execution.run.execute": lambda change_id: _sorted_paths(
            _change(change_id, "execution", "run-result.json"),
        ),
        "assurance.healing.coverage-repair.execute": lambda change_id: _sorted_paths(
            _change(change_id, "healing", "coverage-repair.json"),
        ),
        "assurance.healing.fix-proposal.execute": lambda change_id: _sorted_paths(
            _change(change_id, "healing", "fix-proposal.json"),
        ),
        "assurance.quality.fact-baseline.execute": lambda change_id: _sorted_paths(
            _change(change_id, "facts", "fact-baseline.json"),
        ),
        "assurance.quality.inspect.execute": lambda change_id: _sorted_paths(
            _change(change_id, "inspect", "inspection.json"),
        ),
        "assurance.quality.issue-analysis.execute": lambda change_id: _sorted_paths(
            _change(change_id, "inspect", "issue-analysis.json"),
        ),
        "assurance.quality.issue-triage.execute": lambda change_id: _sorted_paths(
            _change(change_id, "inspect", "issue-triage.json"),
        ),
        "assurance.quality.report.execute": lambda change_id: _sorted_paths(
            _change(change_id, "report", "report.md"),
        ),
        "assurance.improvement.archive.execute": lambda change_id: _sorted_paths(
            _change(change_id, "archive", "archive-receipt.json"),
        ),
        "assurance.improvement.improvement-review.execute": lambda change_id: _sorted_paths(
            _change(change_id, "review", "improvement-review.json"),
        ),
        "assurance.improvement.retro.execute": lambda change_id: _sorted_paths(
            _change(change_id, "retro", "retro.json"),
        ),
        "assurance.improvement.retro-eval-analysis.execute": lambda change_id: _sorted_paths(
            _change(change_id, "retro", "retro-eval-analysis.json"),
        ),
        "assurance.improvement.retro-issue-analysis.execute": lambda change_id: _sorted_paths(
            _change(change_id, "retro", "retro-issue-analysis.json"),
        ),
        "assurance.improvement.retro-workflow-analysis.execute": lambda change_id: _sorted_paths(
            _change(change_id, "retro", "retro-workflow-analysis.json"),
        ),
    }
)


class OutputRouteCatalog:
    """Closed installed-product map from execute aliases to exact logical outputs."""

    def aliases(self) -> tuple[str, ...]:
        expected = tuple(execute_alias_for_prepare(prepare_id) for prepare_id in PREPARE_IDS)
        actual = tuple(sorted(_ROUTE_BUILDERS))
        if actual != tuple(sorted(expected)):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise ValueError(f"output route catalog drifted; missing={missing}, extra={extra}")
        return expected

    def outputs(self, capability_alias: str, change_id: str) -> tuple[str, ...]:
        builder = _ROUTE_BUILDERS.get(capability_alias)
        if builder is None:
            raise ValueError(f"unknown capability output route: {capability_alias}")
        return builder(change_id)


__all__ = [
    "OutputRouteCatalog",
    "execute_alias_for_prepare",
]
