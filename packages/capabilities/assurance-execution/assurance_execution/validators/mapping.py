"""Closed-mapping commit validator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_execution.contracts.selection import ClosedMappingV1

_OUTSIDE_REASON = "execution candidate may write only tests and change execution paths"
_MAPPING_REASON = "mapping must equal selected tests"
_ALLOWED_PREFIXES = ("qa/tests/", "qa/results/")


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _allowed_path(path: str) -> bool:
    return _canonical_relative(path) and path.startswith(_ALLOWED_PREFIXES)


def _is_test_module(path: str) -> bool:
    posix = PurePosixPath(path)
    if posix.suffix != ".py" or not path.startswith("qa/tests/"):
        return False
    return posix.stem.startswith("test_") or posix.stem.endswith("_test")


def _load_mapping(file_bytes: Mapping[str, bytes]) -> ClosedMappingV1 | None:
    for path, payload in file_bytes.items():
        name = PurePosixPath(path).name
        if name not in {"closed-mapping.json"} and not name.endswith("-closed-mapping.json"):
            continue
        try:
            return ClosedMappingV1.model_validate(json.loads(payload.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            continue
    return None


class ClosedMappingValidator:
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

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(file.path for file in staged.files)
        for path in listed:
            if not _allowed_path(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        extra = sorted(set(self._file_bytes) - set(listed))
        if extra:
            return ValidationResult(accepted=False, reason="execution candidate contains an unlisted file")
        mapping = self._mapping
        if mapping is None and self._file_bytes:
            mapping = _load_mapping(self._file_bytes)
            if mapping is not None and (self._capability_leafs is not None or self._case_ids is not None):
                try:
                    mapping = ClosedMappingV1.model_validate(
                        mapping.model_dump(mode="json"),
                        context={
                            key: value
                            for key, value in {
                                "capability_leafs": self._capability_leafs,
                                "case_ids": self._case_ids,
                            }.items()
                            if value is not None
                        },
                    )
                except ValidationError as error:
                    return ValidationResult(accepted=False, reason=str(error))
        if not self._require_mapping and mapping is None:
            return ValidationResult(accepted=True)
        allowed = frozenset(mapping.selected) if mapping is not None else frozenset()
        for path in listed:
            if _is_test_module(path) and path not in allowed:
                return ValidationResult(accepted=False, reason=_MAPPING_REASON)
        return ValidationResult(accepted=True)
