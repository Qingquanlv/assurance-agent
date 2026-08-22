"""Candidate commit validator: source/evidence identity or path-only plugin instances."""

from __future__ import annotations

from collections.abc import Mapping

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_improvement.validators.paths import canonical_relative, under_root

_ROOTS = ("retro/", "improvements/", "qa/retro/", "qa/improvements/")
_OUTSIDE = "improvement candidate may write only retro and improvement paths"
_REQUIRED = {
    "candidates": "retro/proposal-candidates.json",
    "context": "retro/context.json",
    "manifest": "retro/source-manifest.json",
}
_MISSING = {key: f"improvement candidates are missing the authenticated {key} document" for key in _REQUIRED}
_MISMATCH = {
    key: f"improvement candidate {key} digest does not match the authenticated document" for key in _REQUIRED
}


class CandidatesValidator:
    def __init__(
        self,
        *,
        expected: Mapping[str, str] | None = None,
        path_only: bool = False,
    ) -> None:
        self._expected = dict(expected or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        listed = {item.path: item.after_sha256 for item in candidate.files}
        for key, path in _REQUIRED.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason=_MISSING[key])
        if not self._expected:
            return ValidationResult(
                accepted=False, reason="improvement candidate source digests are not authenticated"
            )
        if any(key not in self._expected for key in _REQUIRED):
            return ValidationResult(
                accepted=False, reason="improvement candidate expected digest map is incomplete"
            )
        for key, path in _REQUIRED.items():
            if listed.get(path) != self._expected[key]:
                return ValidationResult(accepted=False, reason=_MISMATCH[key])
        return ValidationResult(accepted=True)
