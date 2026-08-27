"""Issue, fingerprint, and problem-apply commit validators."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_quality.contracts.issue_events import CHANGE_ISSUE_EVENT_ADAPTER, PROBLEM_EVENT_ADAPTER
from assurance_quality.contracts.issues import IssueCandidateDocument, Problem
from assurance_quality.operations.identity import problem_fingerprint, review_id
from assurance_quality.validators.paths import canonical_relative, under_root

_ISSUE_ROOTS = ("issues/", "inspect/", "issue-review/")
_APPLY_ROOTS = ("issue-review/",)
_OUTSIDE = "quality candidate may write only issue and inspect paths"
_APPLY_OUTSIDE = "quality candidate may write only issue-review apply receipts"
_EVIDENCE = "issue evidence is not a frozen catalog member"
_FINGERPRINT = "canonical fingerprint does not match evidence"
_TRANSITION = "issue event transition is not allowed"
_APPLY_CLOSED = "problem apply candidate is not authenticated"
_NOT_CHANGE_ISSUE = "not a change-issue transition"
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
        event_history: tuple[str, ...] = (),
        path_only: bool = False,
    ) -> None:
        self._evidence_refs = evidence_refs
        self._file_bytes = dict(file_bytes or {})
        self._event_history = event_history
        self._path_only = path_only

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ISSUE_ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if not self._file_bytes:
            return ValidationResult(accepted=False, reason=_EVIDENCE)
        listed = {item.path for item in staged.files}
        for path, payload in self._file_bytes.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason="quality candidate contains an unlisted file")
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            reason = _check_issue_payload(raw, self._evidence_refs, self._event_history)
            if reason is not None:
                return ValidationResult(accepted=False, reason=reason)
        return ValidationResult(accepted=True)


def _check_issue_payload(
    raw: object,
    evidence_refs: frozenset[str] | None,
    event_history: tuple[str, ...] = (),
) -> str | None:
    if not isinstance(raw, dict):
        return _EVIDENCE
    if "candidates" in raw:
        try:
            document = IssueCandidateDocument.model_validate(raw)
        except ValidationError as error:
            return str(error)
        for candidate in document.candidates:
            try:
                expected = problem_fingerprint(
                    affected_surface=candidate.affected_surface,
                    fingerprint_inputs=candidate.fingerprint_inputs,
                )
            except ValueError:
                return _FINGERPRINT
            claimed = set(candidate.possible_problem_ids)
            if claimed and expected.digest not in claimed:
                return _FINGERPRINT
        if evidence_refs is not None and document.evidence_bundle_digest not in evidence_refs:
            return _EVIDENCE
        return None
    if "type" in raw:
        return _check_event_transition(raw, event_history)
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


def _check_event_transition(raw: dict[str, Any], event_history: tuple[str, ...]) -> str | None:
    try:
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(raw)
    except ValidationError as error:
        try:
            PROBLEM_EVENT_ADAPTER.validate_python(raw)
        except ValidationError:
            return str(error)
        return _NOT_CHANGE_ISSUE
    event_type = str(getattr(event, "type", ""))
    allowed = _ALLOWED_AFTER.get(event_type)
    if allowed is None:
        return _TRANSITION
    predecessor = event_history[-1] if event_history else raw.get("predecessor_type")
    if predecessor is None or predecessor == "":
        if allowed:
            return _TRANSITION
        return None
    if predecessor not in allowed:
        return _TRANSITION
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

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        receipts = [item.path for item in staged.files if item.path.endswith("/apply-receipt.json")]
        for item in staged.files:
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
        problem_id = receipt.get("problem_id")
        if not isinstance(problem_id, str) or problem_id != saved.get("problem_id"):
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        version = receipt.get("expected_problem_version", saved.get("expected_problem_version"))
        if version is None or receipt.get("review_id") is None or saved.get("review_id") is None:
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        try:
            expected = review_id(problem_id, int(version))
        except (TypeError, ValueError):
            return ValidationResult(accepted=False, reason=_APPLY_CLOSED)
        if receipt.get("review_id") != expected or saved.get("review_id") != expected:
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
