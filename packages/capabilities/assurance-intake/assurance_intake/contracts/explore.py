from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceCodeEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: Literal["source_code"]
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parse_confidence_cap: Literal["medium", "low"] = "medium"
    module: str | None = None


class EvidenceInventoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: list[Any]
    missing: list[Any]
    not_inspected: list[Any]


class CaseDesignGuidanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority_hints: list[Any]
    suggested_scenarios: list[Any]
    regression_focus: list[Any]


class TestStrategyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_recommendation: list[Any] = Field(min_length=1)


class ExploreAdvisoryV1(BaseModel):
    """Current Explore advisory; the read model equals the authoring contract."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    context_ref: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    executive_summary: str = Field(min_length=1)
    watchlist: list[Any]
    evidence_inventory: EvidenceInventoryV1
    source_code_evidence: list[SourceCodeEvidenceV1]
    case_design_guidance: CaseDesignGuidanceV1
    minimum_required_coverage: dict[str, Any] = Field(min_length=1)
    open_questions_for_case_design: list[Any]
    test_strategy: TestStrategyV1


class ExploreContextV1(BaseModel):
    """Deterministic graph-owned evidence inventory supplied to Explore."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    change_id: str = Field(min_length=1)
    generated_at: Literal["1970-01-01T00:00:00Z"] = "1970-01-01T00:00:00Z"
    requirement_summary: str | None = None
    aggregation_policy: dict[str, Any]
    archive_window: dict[str, Any]
    staleness: dict[str, Any]
    impact: dict[str, Any]
    case_signals: list[Any]
    test_health: list[Any]
    historical_issues: list[Any]
    evidence: list[Any]
    degraded: bool
    degraded_reasons: list[str]
    no_git: bool


def build_explore_context(workspace: Path, *, change_id: str) -> ExploreContextV1:
    """Build an honest content-deterministic MRC without consulting ambient state."""

    requirement = workspace / "qa" / "changes" / change_id / "requirement.md"
    requirement_summary = None
    if requirement.is_file() and not requirement.is_symlink():
        requirement_summary = requirement.read_text(encoding="utf-8")[:2000]

    cases_root = workspace / "qa" / "cases"
    case_files = (
        sorted(
            path.relative_to(workspace).as_posix()
            for path in cases_root.rglob("case.yaml")
            if path.is_file() and not path.is_symlink()
        )
        if cases_root.is_dir()
        else []
    )
    archive_root = workspace / "qa" / "archive"
    archives = (
        sorted(path.name for path in archive_root.iterdir() if path.is_dir() and not path.is_symlink())
        if archive_root.is_dir()
        else []
    )
    problems = workspace / "qa" / "issues" / "problems.json"
    has_problem_history = problems.is_file() and not problems.is_symlink() and problems.stat().st_size > 0

    degraded_reasons = [
        "no_git: attempt workspace contains content only",
        "no_diff: no authenticated diff projection was supplied",
    ]
    if not case_files:
        degraded_reasons.append("no_cases: qa/cases is empty or missing")
    else:
        degraded_reasons.append(
            "case_history_not_projected: case files exist but no typed projection was supplied"
        )
    if not has_problem_history:
        degraded_reasons.append("no_history: qa/issues/problems.json is missing or empty")
    else:
        degraded_reasons.append(
            "problem_history_not_projected: problem history exists but no typed projection was supplied"
        )
    if not archives:
        degraded_reasons.append("no_archives: qa/archive is empty or missing")

    return ExploreContextV1(
        change_id=change_id,
        requirement_summary=requirement_summary,
        aggregation_policy={
            "source": "graph-owned-content-snapshot",
            "layers": ["api", "e2e", "fuzz", "performance"],
            "ambient_git_forbidden": True,
        },
        archive_window={
            "depth": len(archives),
            "archives_sampled": archives,
            "newest_archive": None,
            "oldest_archive": None,
        },
        staleness={"max_age_days": None, "stale": False},
        impact={
            "diff_base": "content-snapshot",
            "changed_files": [],
            "modules": [],
            "affected_case_ids": [],
            "affected_cases_by_module": {},
            "affected_test_files": [],
        },
        case_signals=[],
        test_health=[],
        historical_issues=[],
        evidence=[],
        degraded=True,
        degraded_reasons=degraded_reasons,
        no_git=True,
    )


__all__ = ["ExploreAdvisoryV1", "ExploreContextV1", "build_explore_context"]
