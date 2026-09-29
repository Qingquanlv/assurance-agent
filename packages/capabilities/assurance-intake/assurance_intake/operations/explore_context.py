"""Deterministic, bounded project-file observations for Explore."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from assurance_intake.contracts.explore import (
    REQUIREMENT_CONTEXT_BUDGET,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    ExploreAdvisoryV1,
    ExploreContextV1,
    PreparedExploreV1,
    RequirementReadFactsV1,
    SourceCatalogEntryV1,
)
from assurance_intake.contracts.impact import (
    CHANGE_EVIDENCE_PATH,
    CandidateCaseV1,
    ChangeEvidenceV1,
    HistoricalProblemV1,
    ImpactProjectionV1,
    ImpactSeedV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.operations.planning_facts import source_path_hints

_MAX_CASE_FILES = 64
_MAX_PROBLEMS = 64


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


def _source_catalog(workspace: Path) -> tuple[SourceCatalogEntryV1, ...]:
    entries: list[SourceCatalogEntryV1] = []
    requirement = _read_regular(workspace, REQUIREMENT_PATH)
    if requirement is not None:
        entries.append(
            SourceCatalogEntryV1(
                source_id="requirement",
                kind="requirement",
                artifact=EvidenceArtifactRefV1(
                    path=REQUIREMENT_PATH, digest=hashlib.sha256(requirement).hexdigest()
                ),
                quotable=True,
            )
        )
    snapshot = _read_regular(workspace, RUN_SPEC_SNAPSHOT_PATH)
    if snapshot is not None:
        entries.append(
            SourceCatalogEntryV1(
                source_id="run-spec",
                kind="decision",
                artifact=EvidenceArtifactRefV1(
                    path=RUN_SPEC_SNAPSHOT_PATH, digest=hashlib.sha256(snapshot).hexdigest()
                ),
                quotable=False,
            )
        )
    return tuple(entries)


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

    requirement_data = _read_regular(workspace, REQUIREMENT_PATH) or b""
    if requirement_data:
        requirement_text, read_facts = _utf8_prefix(requirement_data, REQUIREMENT_CONTEXT_BUDGET)
    else:
        requirement_text = ""
        read_facts = RequirementReadFactsV1(total_bytes=0, provided_bytes=0, read_state="complete")
    requirement_summary = requirement_text or None

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
        source_catalog=_source_catalog(workspace),
        requirement_read_facts=read_facts,
        degraded=bool(degraded_reasons),
        degraded_reasons=degraded_reasons,
        no_git=True,
    )


def load_exploration_document(data: bytes) -> ExploreAdvisoryV1 | PreparedExploreV1:
    """Read official PreparedExploreV1 when sealed; otherwise the authoring draft."""
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("exploration artifact is invalid") from error
    coverage = payload.get("minimum_required_coverage") if isinstance(payload, dict) else None
    first = coverage[0] if isinstance(coverage, list) and coverage else None
    try:
        if isinstance(first, dict) and "mrc_id" in first:
            return PreparedExploreV1.model_validate(payload)
        return ExploreAdvisoryV1.model_validate(payload)
    except ValueError as error:
        raise ValueError("exploration artifact is invalid") from error
