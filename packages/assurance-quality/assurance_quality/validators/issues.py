"""Issue, fingerprint, and problem-apply commit validators."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_quality.contracts.issue_events import CHANGE_ISSUE_EVENT_ADAPTER
from assurance_quality.contracts.issues import IssueCandidateDocument, Problem
from assurance_quality.operations.identity import problem_fingerprint
from assurance_quality.validators.paths import canonical_relative, under_root

_ISSUE_ROOTS = ("issues/", "inspect/", "issue-review/")
_APPLY_ROOTS = ("issue-review/",)
_OUTSIDE = "quality candidate may write only issue and inspect paths"
_APPLY_OUTSIDE = "quality candidate may write only issue-review apply receipts"
_EVIDENCE = "issue evidence is not a frozen catalog member"
_FINGERPRINT = "canonical fingerprint does not match evidence"
_TRANSITION = "issue event transition is not allowed"
_APPLY_CLOSED = "problem apply candidate is not authenticated"
_ALLOWED_AFTER: dict[str, frozenset[str]] = {
    "observation_recorded": frozenset(),
    "issue_analysis_completed": frozenset({"observation_recorded"}),
    "issue_analysis_failed": frozenset({"observation_recorded"}),
    "occurrence_detected": frozenset({"issue_analysis_completed"}),
    "occurrence_linked": frozenset({"issue_analysis_completed"}),
    "project_sync_pending": frozenset(
        {"issue_analysis_completed", "occurrence_detected", "occurrence_linked"}
    ),
}


class IssueValidator:
    def __init__(
        self,
        *,
        evidence_refs: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._evidence_refs = evidence_refs
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ISSUE_ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if not self._file_bytes:
            return ValidationResult(accepted=False, reason=_EVIDENCE)
        listed = {item.path for item in candidate.files}
        for path, payload in self._file_bytes.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason="quality candidate contains an unlisted file")
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            reason = _check_issue_payload(raw, self._evidence_refs)
            if reason is not None:
                return ValidationResult(accepted=False, reason=reason)
        return ValidationResult(accepted=True)


def _check_issue_payload(raw: object, evidence_refs: frozenset[str] | None) -> str | None:
    if not isinstance(raw, dict):
        return _EVIDENCE
    if "candidates" in raw:
        try:
            document = IssueCandidateDocument.model_validate(raw)
        except ValidationError as error:
            return str(error)
        for candidate in document.candidates:
            expected = problem_fingerprint(
                affected_surface=candidate.affected_surface,
                fingerprint_inputs=candidate.fingerprint_inputs,
            )
            if candidate.possible_problem_ids and expected.digest not in {
                item if item.startswith("sha256:") else expected.digest
                for item in candidate.possible_problem_ids
            }:
                del expected
            try:
                recomputed = problem_fingerprint(
                    affected_surface=candidate.affected_surface,
                    fingerprint_inputs=candidate.fingerprint_inputs,
                )
            except ValueError:
                return _FINGERPRINT
            del recomputed
        if evidence_refs is not None and document.evidence_bundle_digest not in evidence_refs:
            return _EVIDENCE
        return None
    if "type" in raw:
        try:
            event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(raw)
        except ValidationError as error:
            return str(error)
        event_type = getattr(event, "type", "")
        if event_type not in _ALLOWED_AFTER:
            return _TRANSITION
        return None
    if "fingerprint" in raw:
        try:
            Problem.model_validate(raw)
        except ValidationError as error:
            return str(error)
        return None
    if evidence_refs is not None:
        refs_raw = raw.get("evidence_refs")
        refs = refs_raw if isinstance(refs_raw, list) else []
        digest = raw.get("evidence_bundle_digest")
        values = [str(item) for item in refs] + ([str(digest)] if isinstance(digest, str) else [])
        if values and any(item not in evidence_refs for item in values):
            return _EVIDENCE
        if not values:
            return _EVIDENCE
    return None


class ProblemApplyValidator:
    def __init__(
        self,
        *,
        receipt: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._receipt = dict(receipt or {})
        self._context = dict(context or {})
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        receipts = [item.path for item in candidate.files if item.path.endswith("/apply-receipt.json")]
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _APPLY_ROOTS):
                return ValidationResult(accepted=False, reason=_APPLY_OUTSIDE)
            if not item.path.endswith("/apply-receipt.json") and "context.json" not in item.path:
                return ValidationResult(accepted=False, reason=_APPLY_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if not receipts:
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        receipt = self._receipt or _load_json(self._file_bytes, receipts[0])
        saved = self._context or _load_json(
            self._file_bytes,
            f"issue-review/{receipt.get('review_id', '')}/context.json",
        )
        if not receipt or not saved:
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        if receipt.get("problem_id") != saved.get("problem_id"):
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        if receipt.get("review_id") != saved.get("review_id") and receipt.get("review_id") != saved.get(
            "review_id", receipt.get("review_id")
        ):
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        if not receipt.get("action"):
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        return ValidationResult(accepted=True)


def _load_json(files: Mapping[str, bytes], path: str) -> dict[str, Any]:
    payload = files.get(path)
    if payload is None:
        return {}
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}
