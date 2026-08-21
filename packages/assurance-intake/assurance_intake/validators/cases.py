"""Intake commit validators over candidate case and change paths."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

import yaml
from pydantic import ValidationError

from graph_engine.plugin_api import (
    CandidateWriteSet,
    ValidationContext,
    ValidationResult,
)

from assurance_intake.contracts import CaseYamlAuthoring, QaYaml

_CHANGE_PREFIX = "qa/changes/"
_CASES_PREFIX = "qa/cases/"
_OUTSIDE_REASON = "intake candidate may write only change and cases paths"
_UNLISTED_REASON = "intake candidate contains an unlisted file"
_YAML_SUFFIXES = (".yaml", ".yml")


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _allowed_owner_path(path: str) -> bool:
    return _canonical_relative(path) and path.startswith((_CHANGE_PREFIX, _CASES_PREFIX))


class CaseCandidateValidator:
    def __init__(
        self,
        *,
        capability_leafs: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
    ) -> None:
        self._capability_leafs = capability_leafs
        self._file_bytes = dict(file_bytes or {})

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(file.path for file in candidate.files)
        for path in listed:
            if not _allowed_owner_path(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        extra = sorted(set(self._file_bytes) - set(listed))
        if extra:
            return ValidationResult(accepted=False, reason=_UNLISTED_REASON)
        leafs = self._capability_leafs
        if leafs is None or not self._file_bytes:
            return ValidationResult(accepted=True)
        for path, payload in self._file_bytes.items():
            if not path.endswith(_YAML_SUFFIXES) or "/cases/" not in f"/{path}" or path.endswith(".qa.yaml"):
                continue
            if path.endswith(".qa.yaml"):
                continue
            try:
                raw = yaml.safe_load(payload)
                CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": leafs})
            except (yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
                return ValidationResult(accepted=False, reason=str(error))
        return ValidationResult(accepted=True)


class CaseReferenceValidator:
    def __init__(self, *, file_bytes: Mapping[str, bytes] | None = None) -> None:
        self._file_bytes = dict(file_bytes or {})

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = {file.path for file in candidate.files}
        for path in listed:
            if not _allowed_owner_path(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        if not self._file_bytes:
            return ValidationResult(accepted=True)
        missing: list[str] = []
        for path, payload in self._file_bytes.items():
            if not path.endswith(_YAML_SUFFIXES):
                continue
            try:
                raw = yaml.safe_load(payload)
            except yaml.YAMLError as error:
                return ValidationResult(accepted=False, reason=str(error))
            if not isinstance(raw, Mapping):
                continue
            if path.endswith(".qa.yaml"):
                try:
                    document = QaYaml.model_validate(raw)
                except ValidationError as error:
                    return ValidationResult(accepted=False, reason=str(error))
                for target in document.targets.cases:
                    for relative in (target.change_case_file, target.target_case_file):
                        if relative not in listed and relative not in self._file_bytes:
                            missing.append(relative)
                continue
            added_raw = raw.get("added")
            modified_raw = raw.get("modified")
            added = added_raw if isinstance(added_raw, list) else []
            modified = modified_raw if isinstance(modified_raw, list) else []
            for entry in (*added, *modified):
                if not isinstance(entry, Mapping):
                    continue
                related = entry.get("related_cases")
                if not isinstance(related, list):
                    continue
                for item in related:
                    if isinstance(item, str) and item not in listed and item not in self._file_bytes:
                        missing.append(item)
        if missing:
            return ValidationResult(
                accepted=False,
                reason=f"case references resolve outside the candidate tree: {sorted(set(missing))}",
            )
        return ValidationResult(accepted=True)
