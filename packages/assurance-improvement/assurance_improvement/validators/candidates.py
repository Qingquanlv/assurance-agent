"""Candidate commit validator: source/evidence identity or path-only plugin instances."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_improvement.contracts.delivery import artifact_digest, same_digest
from assurance_improvement.contracts.retro import (
    ImprovementCandidateDocumentV3,
    RetroContextV3,
    RetroSourceManifestV3,
)
from assurance_improvement.validators.documents import bytes_match_digest, load_json, rejected
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
_BYTES = "improvement candidate bytes are not authenticated"
_IDENTITY = "improvement candidate source or identity does not match"


class CandidatesValidator:
    def __init__(
        self,
        *,
        expected: Mapping[str, str] | None = None,
        path_only: bool = False,
        file_bytes: Mapping[str, bytes] | None = None,
    ) -> None:
        self._expected = dict(expected or {})
        self._path_only = path_only
        self._file_bytes = dict(file_bytes or {})

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
        return self._validate_documents(listed)

    def _validate_documents(self, listed: Mapping[str, str | None]) -> ValidationResult:
        if not self._file_bytes:
            return rejected(_BYTES)
        for key, path in _REQUIRED.items():
            raw = self._file_bytes.get(path)
            if raw is None or not bytes_match_digest(raw, listed.get(path)):
                return rejected(_BYTES)
        try:
            manifest = RetroSourceManifestV3.model_validate(
                load_json(self._file_bytes, _REQUIRED["manifest"])
            )
            context = RetroContextV3.model_validate(load_json(self._file_bytes, _REQUIRED["context"]))
            document = ImprovementCandidateDocumentV3.model_validate(
                load_json(self._file_bytes, _REQUIRED["candidates"]),
                context={"retro_manifest": manifest},
            )
        except (ValidationError, ValueError) as error:
            return rejected(str(error))
        if document.retro_id != context.retro_id:
            return rejected(_IDENTITY)
        if not same_digest(document.context_sha256, artifact_digest(context)):
            return rejected(_IDENTITY)
        return ValidationResult(accepted=True)
