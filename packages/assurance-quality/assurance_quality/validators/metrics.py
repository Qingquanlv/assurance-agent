"""Metrics numeric-domain and cross-artifact digest validators."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.validators.paths import canonical_relative, under_root

_METRIC_ROOTS = ("inspect/", "report/")
_CROSS_ROOTS = ("inspect/", "report/", "issues/", "issue-review/")
_OUTSIDE = "quality candidate may write only inspect and report metric paths"
_CROSS_OUTSIDE = "quality candidate may write only quality-owned artifact paths"
_NUMERIC = "metric value is not a finite number"
_SCOPE = "metric scope identity is incomplete"
_EVIDENCE = "evaluated metric is missing evidence"
_CLOSED = "cross-artifact schema or digest is not closed"


class MetricsValidator:
    def __init__(
        self,
        *,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _METRIC_ROOTS):
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
                document = MetricsDocument.model_validate(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            reason = _check_metrics(document)
            if reason is not None:
                return ValidationResult(accepted=False, reason=reason)
        return ValidationResult(accepted=True)


def _check_metrics(document: MetricsDocument) -> str | None:
    for entry in document.metrics.values():
        if entry.value is not None and not math.isfinite(entry.value):
            return _NUMERIC
        if entry.status == "evaluated" and not entry.evidence:
            return _EVIDENCE
        if (
            entry.status == "evaluated"
            and entry.value is not None
            and entry.declared is None
            and entry.holds is None
        ):
            if entry.layer in {"api", "e2e"}:
                return _SCOPE
        if (
            entry.declared is not None
            and entry.declared.value is not None
            and not math.isfinite(entry.declared.value)
        ):
            return _NUMERIC
    return None


class CrossArtifactValidator:
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
            if not canonical_relative(item.path) or not under_root(item.path, _CROSS_ROOTS):
                return ValidationResult(accepted=False, reason=_CROSS_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if not self._expected and not self._file_bytes:
            return ValidationResult(accepted=False, reason=_CLOSED)
        listed = {item.path for item in candidate.files}
        for path, payload in self._file_bytes.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason="quality candidate contains an unlisted file")
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ValidationResult(accepted=False, reason=_CLOSED)
            digest = canonical_digest(raw) if isinstance(raw, (dict, list)) else ""
            expected = self._expected.get(path)
            if expected is not None and expected != digest:
                return ValidationResult(accepted=False, reason=_CLOSED)
            schema_id = raw.get("schema_id") if isinstance(raw, dict) else None
            if (
                isinstance(schema_id, str)
                and schema_id
                and not schema_id.startswith("assurance.quality.schema.")
            ):
                return ValidationResult(accepted=False, reason=_CLOSED)
        return ValidationResult(accepted=True)
