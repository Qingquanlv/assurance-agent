"""Repair-candidate commit validator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary
from assurance_healing.contracts.safety import CodegenFixApplySummaryV1
from assurance_healing.validators.paths import canonical_relative, under_root

_TEST_ROOTS = ("qa/tests/", "qa/results/healing/")
_PRODUCT_ROOTS = ("app/", "src/", "web/src/")
_OUTSIDE = "repair candidate may write only approved test paths"
_UNNAMED = "modified file is not named by an approved proposal"
_PRODUCT = "product changes require approval"
_UNAPPROVED = "codegen fix proposal is not approved"
_SUMMARY = "apply summary does not authenticate the claimed files"


class RepairCandidateValidator:
    def __init__(
        self,
        *,
        approved_proposal: Mapping[str, Any] | None = None,
        require_approval: bool = True,
        path_only: bool = False,
        apply_summary: Mapping[str, Any] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
    ) -> None:
        self._proposal = dict(approved_proposal) if approved_proposal else None
        self._require_approval = require_approval
        self._path_only = path_only
        self._apply_summary = dict(apply_summary) if apply_summary else None
        self._file_bytes = dict(file_bytes or {})

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        named = _named_files(self._proposal or {})
        approved = self._proposal is not None and self._proposal.get("status") == "approved"
        for item in staged.files:
            path = item.path
            if not canonical_relative(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if under_root(path, _PRODUCT_ROOTS):
                return ValidationResult(accepted=False, reason=_PRODUCT)
            if not under_root(path, _TEST_ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if self._path_only:
                continue
            if self._require_approval and not approved:
                return ValidationResult(accepted=False, reason=_UNAPPROVED)
            if path not in named:
                return ValidationResult(accepted=False, reason=_UNNAMED)
        if self._path_only:
            return ValidationResult(accepted=True)
        try:
            summary = self._apply_summary
            loaded = _load_apply_summary(self._file_bytes)
            if loaded is not None:
                summary = loaded
        except _SummaryLoadError as error:
            return ValidationResult(accepted=False, reason=str(error))
        if summary is None:
            return ValidationResult(accepted=True)
        return _authenticate_apply_summary(summary, tuple(item.path for item in staged.files))


class _SummaryLoadError(ValueError):
    """Present apply-summary bytes failed to parse."""


def _is_apply_summary_name(path: str) -> bool:
    name = PurePosixPath(path).name
    return name == "apply-summary.json" or name.endswith("-apply-summary.json")


def _load_apply_summary(file_bytes: Mapping[str, bytes]) -> dict[str, Any] | None:
    for path, payload in file_bytes.items():
        if not _is_apply_summary_name(path):
            continue
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _SummaryLoadError(str(error)) from error
        if not isinstance(raw, dict):
            raise _SummaryLoadError("apply summary must be an object")
        return {str(key): value for key, value in raw.items()}
    return None


def _authenticate_apply_summary(summary: Mapping[str, Any], paths: tuple[str, ...]) -> ValidationResult:
    try:
        codegen = CodegenFixApplySummaryV1.model_validate(summary)
        named = set(codegen.claimed_modified_paths)
        if codegen.outcome == "applied" and not named:
            return ValidationResult(accepted=False, reason=_SUMMARY)
        for path in paths:
            if path not in named:
                return ValidationResult(accepted=False, reason=_SUMMARY)
        return ValidationResult(accepted=True)
    except ValidationError:
        pass
    try:
        repair = CoverageRepairApplySummary.model_validate(summary)
    except ValidationError as error:
        return ValidationResult(accepted=False, reason=str(error))
    named = set(repair.files_modified)
    for path in paths:
        if path not in named:
            return ValidationResult(accepted=False, reason=_SUMMARY)
    return ValidationResult(accepted=True)


def _named_files(proposal: Mapping[str, Any]) -> set[str]:
    named: set[str] = set()
    items = proposal.get("proposals")
    if not isinstance(items, list):
        return named
    for item in items:
        if not isinstance(item, dict):
            continue
        files = item.get("files_to_modify")
        if isinstance(files, list):
            named.update(str(path) for path in files)
    return named
