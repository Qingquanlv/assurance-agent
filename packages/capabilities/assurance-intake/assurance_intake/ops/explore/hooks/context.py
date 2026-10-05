"""Deterministic, bounded project-file observations for Explore."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from graph_engine.artifacts import ArtifactReadError, read_workspace_file

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.planning_facts import source_path_hints
from assurance_intake.ops.explore.models import (
    REQUIREMENT_CONTEXT_BUDGET,
    CandidateCaseV1,
    ChangeEvidenceV1,
    ExploreContextV1,
    HistoricalProblemV1,
    ImpactProjectionV1,
    ImpactSeedV1,
    RequirementReadFactsV1,
    SourceCatalogEntryV1,
)

_MAX_CASE_FILES = 64
_MAX_PROBLEMS = 64


def _read_regular(workspace: Path, relative: str) -> bytes | None:
    try:
        return read_workspace_file(workspace, relative)
    except ArtifactReadError:
        return None


def _seed_projection(
    workspace: Path,
    *,
    change_id: str,
    requirement_text: str,
    evidence: ChangeEvidenceV1 | None,
) -> tuple[
    Literal["change-evidence", "content-snapshot"], tuple[ImpactSeedV1, ...], tuple[str, ...], list[str]
]:
    seeds: list[ImpactSeedV1] = []
    if evidence is not None:
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


def _utf8_prefix(data: bytes, budget: int) -> tuple[str, RequirementReadFactsV1]:
    total = len(data)
    text = data.decode("utf-8")
    if total <= budget:
        return text, RequirementReadFactsV1(total_bytes=total, provided_bytes=total, read_state="complete")
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text[:mid].encode("utf-8")) <= budget:
            low = mid
        else:
            high = mid - 1
    provided = text[:low].encode("utf-8")
    return text[:low], RequirementReadFactsV1(
        total_bytes=total, provided_bytes=len(provided), read_state="truncated"
    )


def _source_catalog(
    requirement: bytes | None,
    snapshot: bytes | None,
    requirement_ref: EvidenceArtifactRefV1 | None,
    snapshot_ref: EvidenceArtifactRefV1 | None,
) -> tuple[SourceCatalogEntryV1, ...]:
    entries: list[SourceCatalogEntryV1] = []
    if requirement is not None and requirement_ref is not None:
        entries.append(
            SourceCatalogEntryV1(
                source_id="requirement",
                kind="requirement",
                artifact=requirement_ref,
                quotable=True,
            )
        )
    if snapshot is not None and snapshot_ref is not None:
        entries.append(
            SourceCatalogEntryV1(
                source_id="run-spec",
                kind="decision",
                artifact=snapshot_ref,
                quotable=False,
            )
        )
    return tuple(entries)


def build_explore_context(
    workspace: Path,
    *,
    change_id: str,
    capability_leafs: tuple[str, ...],
    requirement_data: bytes | None,
    snapshot_data: bytes | None,
    evidence: ChangeEvidenceV1 | None,
    requirement_ref: EvidenceArtifactRefV1 | None,
    snapshot_ref: EvidenceArtifactRefV1 | None,
) -> ExploreContextV1:
    """Build an honest content-deterministic context without consulting ambient state.

    Raises ``ValueError`` when a sealed ``change-evidence.json`` is present but invalid or
    belongs to another change; a forged diff projection must fail prepare, not degrade.
    """

    if requirement_data:
        requirement_text, read_facts = _utf8_prefix(requirement_data, REQUIREMENT_CONTEXT_BUDGET)
    else:
        requirement_text = ""
        read_facts = RequirementReadFactsV1(total_bytes=0, provided_bytes=0, read_state="complete")
    requirement_summary = requirement_text or None

    diff_base, seeds, unobserved, seed_degraded = _seed_projection(
        workspace, change_id=change_id, requirement_text=requirement_text, evidence=evidence
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
    if read_facts.read_state == "truncated":
        degraded_reasons.append("input_truncated: requirement exceeded the host read budget")

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
        source_catalog=_source_catalog(requirement_data, snapshot_data, requirement_ref, snapshot_ref),
        requirement_read_facts=read_facts,
        degraded=bool(degraded_reasons),
        degraded_reasons=degraded_reasons,
        no_git=True,
    )
