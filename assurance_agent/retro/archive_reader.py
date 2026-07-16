from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState
from assurance_agent.change_location import ChangeNotFoundError, archive_root, resolve_change
from assurance_agent.retro.types import ArchivedChange, EvidenceSource

ModelT = TypeVar("ModelT", bound=BaseModel)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _read_model(path: Path, model: type[ModelT], *, yaml_input: bool = False) -> ModelT | None:
    raw = _read_yaml(path) if yaml_input else _read_json(path)
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def read_archived_change(change_dir: Path, *, source: EvidenceSource) -> ArchivedChange:
    reviews: dict[str, Review] = {}
    review_dir = change_dir / "review"
    if review_dir.is_dir():
        for file in sorted(review_dir.glob("*.json")):
            data = _read_model(file, Review)
            if data is not None:
                reviews[file.stem] = data
    apply_summaries: list[ApplySummary] = []
    healing_dir = change_dir / "healing"
    if healing_dir.is_dir():
        for file in sorted(healing_dir.glob("*-apply-summary.json")):
            data = _read_model(file, ApplySummary)
            if data is not None:
                apply_summaries.append(data)
    return ArchivedChange(
        change_id=change_dir.name,
        evidence_source=source,
        path=str(change_dir),
        events=_read_events(change_dir / "events.jsonl"),
        failure_analysis=_read_model(
            change_dir / "inspect" / "failure-analysis.json",
            FailureAnalysis,
        ),
        reviews=reviews,
        apply_summaries=apply_summaries,
        workflow_state=_read_model(
            change_dir / "workflow-state.yaml",
            WorkflowState,
            yaml_input=True,
        ),
    )


def list_archived_changes(project_root: Path) -> list[str]:
    archive = archive_root(project_root)
    if not archive.is_dir():
        return []
    return sorted(p.name for p in archive.iterdir() if p.is_dir())


def resolve_change_dir(project_root: Path, change_id: str) -> tuple[Path, EvidenceSource] | None:
    """Thin adapter: map ``ChangeLocation.source`` to retro ``EvidenceSource``.

    Archive-first resolution (``prefer="archive"``): `aa-archive` copies (never
    moves) and does not delete `qa/changes/<id>/`, so a Change under both roots
    is the normal steady state, not an error (ADR-0002). The archived copy is
    the canonical final snapshot, so it wins; an as-yet-unarchived Change falls
    back to its active copy under `qa.changes` (retro vocabulary: ``unarchived``).
    """
    try:
        loc = resolve_change(project_root, change_id, prefer="archive")
    except ChangeNotFoundError:
        return None
    source: EvidenceSource = "unarchived" if loc.source == "changes" else "archive"
    return loc.path, source
