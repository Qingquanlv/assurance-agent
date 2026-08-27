"""Review commit validator: subject/version/assessment or path-only plugin instances."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_improvement.contracts.review import (
    ImprovementAutoReviewAssessment,
    ImprovementReviewSubject,
)
from assurance_improvement.validators.documents import bytes_match_digest, load_json, rejected
from assurance_improvement.validators.paths import canonical_relative, under_root

_ROOTS = ("improvements/", "qa/improvements/")
_OUTSIDE = "improvement review may write only review subject and assessment paths"
_REQUIRED = {
    "subject": "improvements/review-subjects/subject.json",
    "assessment": "improvements/reviews/assessment.json",
}
_MISSING = {key: f"improvement review is missing the authenticated {key} document" for key in _REQUIRED}
_MISMATCH = {
    key: f"improvement review {key} digest does not match the authenticated document" for key in _REQUIRED
}
_BYTES = "improvement review candidate bytes are not authenticated"
_IDENTITY = "improvement review subject or version does not match"


class ReviewValidator:
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

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        listed = {item.path: item.after_sha256 for item in staged.files}
        for key, path in _REQUIRED.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason=_MISSING[key])
        if not self._expected:
            return ValidationResult(
                accepted=False, reason="improvement review source digests are not authenticated"
            )
        if any(key not in self._expected for key in _REQUIRED):
            return ValidationResult(
                accepted=False, reason="improvement review expected digest map is incomplete"
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
            subject = ImprovementReviewSubject.model_validate(
                load_json(self._file_bytes, _REQUIRED["subject"])
            )
            assessment = ImprovementAutoReviewAssessment.model_validate(
                load_json(self._file_bytes, _REQUIRED["assessment"])
            )
        except ValidationError as error:
            return rejected(str(error))
        if subject.improvement_id != assessment.improvement_id or assessment.expected_improvement_version < 1:
            return rejected(_IDENTITY)
        return ValidationResult(accepted=True)
