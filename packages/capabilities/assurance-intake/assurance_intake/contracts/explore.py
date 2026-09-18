from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from assurance_intake.contracts.impact import (
    CHANGE_EVIDENCE_PATH,
    INVENTORY_PATH,
    CandidateCaseV1,
    ChangeEvidenceV1,
    HistoricalProblemV1,
    ImpactProjectionV1,
    ImpactSeedV1,
)
from assurance_intake.contracts.planning_facts import source_path_hints

EXPLORATION_PATH = "qa/results/explore/exploration.json"
EXPLORE_OUTPUT_PATHS: tuple[str, ...] = (EXPLORATION_PATH, INVENTORY_PATH)
_MAX_CASE_FILES = 64
_MAX_PROBLEMS = 64


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


class TestScopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_scope: list[str]
    out_of_scope: list[str]


class LayerRecommendationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: Literal["API", "E2E", "Fuzz", "Performance"]
    recommended: bool
    rationale: str = Field(min_length=1)
    evidence_ids: list[str]


class TestStrategyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: TestScopeV1 | None
    data_focus: list[str]
    depth: Literal["smoke", "core", "exhaustive"]
    layer_recommendation: list[LayerRecommendationV1] = Field(min_length=4, max_length=4)
    approach: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _all_layers_once(self) -> TestStrategyV1:
        layers = tuple(item.layer for item in self.layer_recommendation)
        expected = ("API", "E2E", "Fuzz", "Performance")
        if layers != expected:
            raise ValueError("layer_recommendation must list API, E2E, Fuzz, Performance once")
        return self


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
    impact: ImpactProjectionV1
    case_signals: list[Any]
    test_health: list[Any]
    historical_issues: list[Any]
    evidence: list[Any]
    degraded: bool
    degraded_reasons: list[str]
    no_git: bool


def _read_regular(workspace: Path, relative: str) -> bytes | None:
    path = workspace.joinpath(*relative.split("/"))
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _seed_projection(
    workspace: Path,
    *,
    change_id: str,
    requirement_text: str,
) -> tuple[
    Literal["change-evidence", "content-snapshot"], tuple[ImpactSeedV1, ...], tuple[str, ...], list[str]
]:
    evidence_bytes = _read_regular(workspace, CHANGE_EVIDENCE_PATH)
    seeds: list[ImpactSeedV1] = []
    if evidence_bytes is not None:
        try:
            evidence = ChangeEvidenceV1.model_validate_json(evidence_bytes)
        except ValidationError as error:
            raise ValueError(f"invalid change-evidence.json: {error}") from error
        if evidence.change_id != change_id:
            raise ValueError("change-evidence.json change_id does not match the current change")
        for changed in evidence.changed_files:
            for symbol in changed.symbols or (None,):
                seeds.append(
                    ImpactSeedV1(
                        seed_id=f"CF-{len(seeds) + 1:03d}",
                        path=changed.path,
                        symbol=symbol,
                        reason="diff",
                    )
                )
        return "change-evidence", tuple(seeds), (), []

    unobserved: list[str] = []
    for hint in source_path_hints(requirement_text):
        if _read_regular(workspace, hint) is None:
            unobserved.append(hint)
            continue
        seeds.append(
            ImpactSeedV1(
                seed_id=f"CF-{len(seeds) + 1:03d}", path=hint, symbol=None, reason="requirement_hint"
            )
        )
    return (
        "content-snapshot",
        tuple(seeds),
        tuple(unobserved),
        ["no_diff: no authenticated diff projection was supplied"],
    )


def _candidate_cases(workspace: Path) -> tuple[tuple[CandidateCaseV1, ...], list[str]]:
    cases_root = workspace / "qa" / "cases"
    files = (
        sorted(path for path in cases_root.rglob("case.yaml") if path.is_file() and not path.is_symlink())
        if cases_root.is_dir()
        else []
    )
    if not files:
        return (), ["no_cases: qa/cases is empty or missing"]
    degraded: list[str] = []
    cases: list[CandidateCaseV1] = []
    seen: set[str] = set()
    for path in files[:_MAX_CASE_FILES]:
        relative = path.relative_to(workspace).as_posix()
        try:
            document = yaml.safe_load(path.read_bytes())
        except (OSError, yaml.YAMLError, UnicodeError):
            degraded.append(f"case_unreadable: {relative}")
            continue
        if not isinstance(document, Mapping):
            degraded.append(f"case_unreadable: {relative}")
            continue
        for section in ("added", "modified"):
            entries = document.get(section)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                case_id = entry.get("case_id")
                title = entry.get("title")
                module = entry.get("module")
                if not all(isinstance(value, str) and value.strip() for value in (case_id, title, module)):
                    continue
                if case_id in seen:
                    continue
                seen.add(str(case_id))
                cases.append(
                    CandidateCaseV1(
                        evidence_id=f"CS-{len(cases) + 1:03d}",
                        case_id=str(case_id),
                        module=str(module),
                        path=relative,
                        title=str(title),
                    )
                )
    if len(files) > _MAX_CASE_FILES:
        degraded.append(f"cases_truncated: {len(files) - _MAX_CASE_FILES} case files were not projected")
    if not cases and not degraded:
        degraded.append("no_cases: qa/cases contains no added or modified cases")
    return tuple(cases), degraded


def _historical_problems(workspace: Path) -> tuple[tuple[HistoricalProblemV1, ...], list[str]]:
    data = _read_regular(workspace, "qa/issues/problems.json")
    if data is None or not data.strip():
        return (), ["no_history: qa/issues/problems.json is missing or empty"]
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return (), ["history_unreadable: qa/issues/problems.json is not valid JSON"]
    problems = document.get("problems") if isinstance(document, Mapping) else document
    if not isinstance(problems, list):
        return (), ["history_unreadable: qa/issues/problems.json has no problems list"]
    rows: list[HistoricalProblemV1] = []
    for item in problems[:_MAX_PROBLEMS]:
        if not isinstance(item, Mapping):
            continue
        problem_id = item.get("problem_id")
        title = item.get("title")
        if not (isinstance(problem_id, str) and problem_id and isinstance(title, str) and title):
            continue
        assessment = item.get("assessment")
        classification = assessment.get("classification") if isinstance(assessment, Mapping) else None
        status = item.get("status")
        rows.append(
            HistoricalProblemV1(
                evidence_id=f"HI-{len(rows) + 1:03d}",
                problem_id=problem_id,
                title=title,
                classification=classification if isinstance(classification, str) else None,
                status=status if isinstance(status, str) else None,
            )
        )
    if not rows:
        return (), ["no_history: qa/issues/problems.json lists no problems"]
    return tuple(rows), []


def build_explore_context(
    workspace: Path,
    *,
    change_id: str,
    capability_leafs: tuple[str, ...],
) -> ExploreContextV1:
    """Build an honest content-deterministic context without consulting ambient state.

    Raises ``ValueError`` when a sealed ``change-evidence.json`` is present but invalid or
    belongs to another change; a forged diff projection must fail prepare, not degrade.
    """

    requirement_text = ""
    requirement = workspace / "qa" / "requirement.md"
    if requirement.is_file() and not requirement.is_symlink():
        requirement_text = requirement.read_text(encoding="utf-8")
    requirement_summary = requirement_text[:2000] if requirement_text else None

    diff_base, seeds, unobserved, seed_degraded = _seed_projection(
        workspace, change_id=change_id, requirement_text=requirement_text
    )
    candidate_cases, case_degraded = _candidate_cases(workspace)
    historical_problems, history_degraded = _historical_problems(workspace)
    archives: list[str] = []

    degraded_reasons = [
        "no_git: attempt workspace contains content only",
        *seed_degraded,
        *case_degraded,
        *history_degraded,
    ]
    if not archives:
        degraded_reasons.append("no_archives: historical archive projection is empty or missing")

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
        impact=ImpactProjectionV1(
            diff_base=diff_base,
            seeds=seeds,
            unobserved_hints=unobserved,
            candidate_cases=candidate_cases,
            historical_problems=historical_problems,
            factory_leafs=tuple(
                sorted(leaf for leaf in set(capability_leafs) if leaf.startswith("capabilities."))
            ),
        ),
        case_signals=[],
        test_health=[],
        historical_issues=[],
        evidence=[],
        degraded=bool(degraded_reasons),
        degraded_reasons=degraded_reasons,
        no_git=True,
    )


__all__ = [
    "EXPLORATION_PATH",
    "EXPLORE_OUTPUT_PATHS",
    "ExploreAdvisoryV1",
    "ExploreContextV1",
    "build_explore_context",
]
