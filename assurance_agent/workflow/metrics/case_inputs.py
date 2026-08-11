"""Shared strict loading for case artifacts consumed by metric risk resolution."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.cases import CaseEntry, CaseYaml


class CaseArtifactError(ValueError):
    """A present case artifact is unreadable or schema-invalid."""


def load_case_entries_strict(change_dir: Path) -> list[CaseEntry]:
    """Load every case document; one malformed present artifact invalidates risk."""
    entries: list[CaseEntry] = []
    root = Path(change_dir)
    for path in sorted(root.glob("cases/**/case.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            document = CaseYaml.model_validate(raw)
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, ValidationError) as err:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            raise CaseArtifactError(f"invalid {rel}: {err}") from err
        entries.extend(document.added)
        entries.extend(document.modified)
    return entries


def touched_entities_from_cases(change_dir: Path) -> frozenset[str]:
    """Return the trailing module segment declared by this change's cases."""
    return frozenset(
        str(entry.module).strip().rsplit(".", 1)[-1]
        for entry in load_case_entries_strict(change_dir)
        if str(entry.module).strip()
    )


__all__ = ["CaseArtifactError", "load_case_entries_strict", "touched_entities_from_cases"]
