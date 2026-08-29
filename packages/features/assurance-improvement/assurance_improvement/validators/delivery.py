"""Delivery commit validator: approved state and exact target or path-only plugin instances."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_improvement.contracts.delivery import (
    ImprovementDeliveryDocument,
    artifact_digest,
    same_digest,
)
from assurance_improvement.contracts.improvements import ImprovementProjection, ImprovementState
from assurance_improvement.validators.documents import bytes_match_digest, load_json, rejected
from assurance_improvement.validators.paths import canonical_relative, under_root

_ROOTS = ("improvements/", "qa/improvements/", ".aa/memory/")
_OUTSIDE = "improvement delivery may write only approved delivery targets"
_REQUIRED = {
    "delivery": "improvements/delivery.json",
    "target": "improvements/target-digest",
}
_MISSING = {key: f"improvement delivery is missing the authenticated {key} document" for key in _REQUIRED}
_MISMATCH = {
    key: f"improvement delivery {key} digest does not match the authenticated document" for key in _REQUIRED
}
_BYTES = "improvement delivery candidate bytes are not authenticated"
_APPROVED = "improvement delivery requires an approved improvement"
_TARGET = "improvement delivery target does not match"


class DeliveryValidator:
    def __init__(
        self,
        *,
        expected: Mapping[str, str] | None = None,
        path_only: bool = False,
        file_bytes: Mapping[str, bytes] | None = None,
        projection: ImprovementProjection | None = None,
    ) -> None:
        self._expected = dict(expected or {})
        self._path_only = path_only
        self._file_bytes = dict(file_bytes or {})
        self._projection = projection

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
                accepted=False, reason="improvement delivery source digests are not authenticated"
            )
        if any(key not in self._expected for key in _REQUIRED):
            return ValidationResult(
                accepted=False, reason="improvement delivery expected digest map is incomplete"
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
        raw_delivery = load_json(self._file_bytes, _REQUIRED["delivery"])
        try:
            document = ImprovementDeliveryDocument.model_validate(raw_delivery)
        except ValidationError as error:
            return rejected(str(error))
        projection = self._projection
        if projection is None:
            raw_projection = load_json(self._file_bytes, "improvements/projection.json")
            try:
                projection = ImprovementProjection.model_validate(raw_projection)
            except ValidationError:
                return rejected(_APPROVED)
        if projection.state is not ImprovementState.APPROVED:
            return rejected(_APPROVED)
        if projection.approval_source not in {"human", "automatic"}:
            return rejected(_APPROVED)
        if document.memory_eval is not None and document.memory_eval.outcome != "passed":
            return rejected("improvement delivery requires a passed evaluation")
        if document.memory_eval is not None:
            if (
                document.memory_eval.approved_version is None
                or document.memory_eval.approved_state_digest is None
            ):
                return rejected("improvement delivery evaluation is missing")
            if document.memory_eval.approved_version != projection.version:
                return rejected("improvement delivery evaluation is stale")
            if not same_digest(document.memory_eval.approved_state_digest, artifact_digest(projection)):
                return rejected("improvement delivery evaluation is stale")
        if (
            document.improvement_id != projection.improvement_id
            or document.delivery != projection.delivery
            or document.expected_improvement_version != projection.version
        ):
            return rejected(_TARGET)
        for receipt in (document.memory_apply, document.memory_rollback):
            if receipt is not None and receipt.target != projection.target:
                return rejected(_TARGET)
        return ValidationResult(accepted=True)
