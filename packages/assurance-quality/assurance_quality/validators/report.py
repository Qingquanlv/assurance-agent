"""Report commit validator: exact source digests or path-only plugin instances."""

from __future__ import annotations

from collections.abc import Mapping

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_quality.validators.paths import canonical_relative, under_root

_REPORT_ROOTS = ("report/", "inspect/", "issues/", "cases/", "plans/", "codegen/", "execution/", "healing/")
_OUTSIDE = "quality candidate may write only report and authenticated source paths"
_SOURCE_PATHS = {
    "case": "cases/source-digest",
    "plan": "plans/source-digest",
    "mapping": "codegen/source-digest",
    "execution": "execution/source-digest",
    "healing": "healing/source-digest",
    "trace": "inspect/trace-projection.json",
    "coverage": "inspect/coverage-gaps.json",
    "issue": "issues/snapshot.json",
    "metrics": "inspect/metrics.json",
}
_MISSING = {key: f"quality report is missing the authenticated {key} projection" for key in _SOURCE_PATHS}
_MISMATCH = {
    key: f"quality report source digest does not match the authenticated {key} projection"
    for key in _SOURCE_PATHS
}


class ReportValidator:
    def __init__(
        self,
        *,
        expected: Mapping[str, str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._expected = dict(expected or {})
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _REPORT_ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        listed = {item.path: item.after_sha256 for item in candidate.files}
        for key, path in _SOURCE_PATHS.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason=_MISSING[key])
        if not self._expected:
            return ValidationResult(
                accepted=False, reason="quality report source digests are not authenticated"
            )
        if any(key not in self._expected for key in _SOURCE_PATHS):
            return ValidationResult(accepted=False, reason="quality report expected digest map is incomplete")
        for key, path in _SOURCE_PATHS.items():
            if listed.get(path) != self._expected[key]:
                return ValidationResult(accepted=False, reason=_MISMATCH[key])
        return ValidationResult(accepted=True)
