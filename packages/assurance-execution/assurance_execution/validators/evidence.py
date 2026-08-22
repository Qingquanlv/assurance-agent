"""Execution-evidence commit validator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath

from pydantic import ValidationError

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1

_OUTSIDE_REASON = "execution candidate may write only tests and change execution paths"
_UNMAPPED_REASON = "execution evidence contains a test outside the closed mapping"
_COVER_REASON = "execution evidence must uniquely cover the closed mapping"
_ALLOWED_PREFIXES = ("tests/", "qa/changes/")


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _allowed_path(path: str) -> bool:
    return _canonical_relative(path) and path.startswith(_ALLOWED_PREFIXES)


def _is_test_module(path: str) -> bool:
    posix = PurePosixPath(path)
    if posix.suffix != ".py" or not path.startswith("tests/"):
        return False
    return posix.stem.startswith("test_") or posix.stem.endswith("_test")


def _load_evidence(
    file_bytes: Mapping[str, bytes],
    *,
    capability_leafs: frozenset[str] | None,
    case_ids: frozenset[str] | None,
) -> ExecutionEvidenceV1 | None:
    context = {
        key: value
        for key, value in {"capability_leafs": capability_leafs, "case_ids": case_ids}.items()
        if value is not None
    }
    for path, payload in file_bytes.items():
        name = PurePosixPath(path).name
        if name not in {"execution-evidence.json"} and not name.endswith("-execution-evidence.json"):
            continue
        try:
            raw = json.loads(payload.decode("utf-8"))
            return ExecutionEvidenceV1.model_validate(raw, context=context or None)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            continue
    return None


class ExecutionEvidenceValidator:
    def __init__(
        self,
        *,
        mapping: ClosedMappingV1 | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        capability_leafs: frozenset[str] | None = None,
        case_ids: frozenset[str] | None = None,
        require_mapping: bool = True,
    ) -> None:
        self._mapping = mapping
        self._file_bytes = dict(file_bytes or {})
        self._capability_leafs = capability_leafs
        self._case_ids = case_ids
        self._require_mapping = require_mapping

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(file.path for file in candidate.files)
        for path in listed:
            if not _allowed_path(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        extra = sorted(set(self._file_bytes) - set(listed))
        if extra:
            return ValidationResult(accepted=False, reason="execution candidate contains an unlisted file")
        if not self._require_mapping and not self._file_bytes and self._mapping is None:
            return ValidationResult(accepted=True)
        allowed = frozenset(self._mapping.selected) if self._mapping is not None else frozenset()
        if self._require_mapping:
            for path in listed:
                if _is_test_module(path) and path not in allowed:
                    return ValidationResult(accepted=False, reason=_UNMAPPED_REASON)
        if not self._file_bytes:
            return ValidationResult(accepted=True)
        evidence = _load_evidence(
            self._file_bytes,
            capability_leafs=self._capability_leafs,
            case_ids=self._case_ids,
        )
        if evidence is None:
            return ValidationResult(accepted=True)
        mapped = frozenset(evidence.mapping.selected)
        result_tests = tuple(item.test for item in evidence.results)
        if any(test not in mapped for test in result_tests):
            return ValidationResult(accepted=False, reason=_UNMAPPED_REASON)
        if len(result_tests) != len(set(result_tests)) or set(result_tests) != mapped:
            return ValidationResult(accepted=False, reason=_COVER_REASON)
        return ValidationResult(accepted=True)
