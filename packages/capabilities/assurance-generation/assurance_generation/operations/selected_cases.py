"""Load authenticated selected cases without importing intake operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError

from assurance_generation.contracts.families import LayerName
from assurance_intake.contracts import CaseYamlAuthoring
from assurance_intake.contracts.case_selection import CaseSelectionV1, selection_path
from assurance_intake.contracts.cases import CaseEntryAuthoring
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1


class InputError(ValueError):
    """Raised when the current selection cannot be authenticated."""


_CASE_TYPES: dict[LayerName, str] = {
    "api": "API",
    "e2e": "E2E",
    "fuzz": "Fuzz",
    "performance": "Performance",
}


def require_selected_ids(actual: tuple[str, ...], selected: tuple[str, ...]) -> None:
    if len(actual) != len(set(actual)) or set(actual) != set(selected):
        raise InputError("generated case IDs must equal reviewed selected case IDs")


def _regular_file(workspace: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = workspace.joinpath(*PurePosixPath(ref.path).parts)
    if path.is_symlink() or not path.is_file():
        raise InputError(f"selection source is missing: {ref.path}")
    try:
        resolved_root = workspace.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise InputError(f"selection source is outside the workspace: {ref.path}") from error
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise InputError(f"selection source digest drifted: {ref.path}")
    return data


def _entry_at(document: object, locator: str, case_id: str) -> CaseEntryAuthoring:
    if not isinstance(document, dict):
        raise InputError("case source must be a mapping")
    section_name, _, index_text = locator.partition("[")
    entries = document.get(section_name)
    if not isinstance(entries, list):
        raise InputError(f"selection locator is not a list: {locator}")
    if index_text.endswith("]") and index_text[:-1].isdigit():
        index = int(index_text[:-1])
        if index >= len(entries) or not isinstance(entries[index], dict):
            raise InputError(f"selection locator is out of range: {locator}")
        raw = entries[index]
    else:
        matches = [item for item in entries if isinstance(item, dict) and item.get("case_id") == case_id]
        if len(matches) != 1:
            raise InputError(f"selection case_id is not unique in source: {case_id}")
        raw = matches[0]
    try:
        entry = CaseEntryAuthoring.model_validate(raw)
    except ValidationError as error:
        raise InputError(f"selected case is not a complete CaseEntry: {error}") from error
    if entry.case_id != case_id:
        raise InputError("selection locator does not match case_id")
    return entry


def load_selected_cases(workspace: Path, reviewed: ReviewedCaseV1) -> tuple[CaseEntryAuthoring, ...]:
    return tuple(entry for _, entry in _load_selected_rows(workspace, reviewed))


def _load_selected_rows(
    workspace: Path,
    reviewed: ReviewedCaseV1,
) -> tuple[tuple[str, CaseEntryAuthoring], ...]:
    selection_bytes = _regular_file(workspace, reviewed.selection_ref)
    if reviewed.selection_ref.path != selection_path(reviewed.coverage_epoch):
        raise InputError("selection_ref must bind the current epoch selection.json")
    try:
        selection = CaseSelectionV1.model_validate(json.loads(selection_bytes))
    except (json.JSONDecodeError, ValidationError) as error:
        raise InputError(f"invalid case selection: {error}") from error
    if selection.change_id != reviewed.change_id or selection.coverage_epoch != reviewed.coverage_epoch:
        raise InputError("selection does not belong to the reviewed case")
    if selection.plan_digest != reviewed.plan_digest:
        raise InputError("selection plan_digest does not match the reviewed case")
    _regular_file(workspace, reviewed.review_ref)
    source_paths = {item.path for item in reviewed.case_refs}
    loaded: list[tuple[str, CaseEntryAuthoring]] = []
    seen_ids: set[str] = set()
    for item in selection.cases:
        if item.source_ref.path not in source_paths:
            raise InputError("selection source is not in reviewed case_refs")
        data = _regular_file(workspace, item.source_ref)
        try:
            document = yaml.safe_load(data)
        except yaml.YAMLError as error:
            raise InputError(f"selected case source is invalid YAML: {error}") from error
        if item.case_id in seen_ids:
            raise InputError("duplicate selected case_id")
        seen_ids.add(item.case_id)
        loaded.append((item.source_ref.path, _entry_at(document, item.source_locator, item.case_id)))
    return tuple(loaded)


def load_selected_case_authoring(
    workspace: Path,
    reviewed: ReviewedCaseV1,
    *,
    family: LayerName,
    capability_leafs: tuple[str, ...],
) -> tuple[CaseYamlAuthoring, dict[str, tuple[str, ...]]]:
    """Project the authenticated selection into one family without losing reuse."""

    selected = tuple(
        (path, entry)
        for path, entry in _load_selected_rows(workspace, reviewed)
        if entry.type == _CASE_TYPES[family]
    )
    grouped: dict[str, list[str]] = {}
    for path, entry in selected:
        grouped.setdefault(path, []).append(entry.case_id)
    try:
        cases = CaseYamlAuthoring.model_validate(
            {
                "schema_version": "1.0",
                "added": [entry.model_dump(mode="json") for _, entry in selected],
                "modified": [],
                "removed": [],
            },
            context={"capability_leafs": frozenset(capability_leafs)},
        )
    except ValidationError as error:
        raise InputError(f"selected cases are invalid: {error}") from error
    return cases, {path: tuple(sorted(ids)) for path, ids in sorted(grouped.items())}


__all__ = [
    "InputError",
    "load_selected_case_authoring",
    "load_selected_cases",
    "require_selected_ids",
]
