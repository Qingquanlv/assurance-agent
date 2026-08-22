"""Trace projection commit validator — exact catalog closure."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.validators.paths import canonical_relative, under_root

_ALLOWED = ("inspect/",)
_OUTSIDE = "quality candidate may write only inspect trace paths"
_CLOSED = "trace capability is not a frozen typed leaf"


class TraceValidator:
    def __init__(
        self,
        *,
        capability_leafs: frozenset[str] | None = None,
        case_ids: frozenset[str] | None = None,
        plan_ids: frozenset[str] | None = None,
        test_ids: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._capability_leafs = capability_leafs
        self._case_ids = case_ids
        self._plan_ids = plan_ids
        self._test_ids = test_ids
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ALLOWED):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if self._capability_leafs is None:
            return ValidationResult(accepted=False, reason=_CLOSED)
        listed = {item.path for item in candidate.files}
        for path, payload in self._file_bytes.items():
            if path not in listed:
                return ValidationResult(accepted=False, reason="quality candidate contains an unlisted file")
            try:
                raw = json.loads(payload.decode("utf-8"))
                TraceProjectionV2.model_validate(
                    raw,
                    context={
                        key: value
                        for key, value in {
                            "capability_leafs": self._capability_leafs,
                            "case_ids": self._case_ids,
                            "plan_ids": self._plan_ids,
                            "test_ids": self._test_ids,
                        }.items()
                        if value is not None
                    },
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
                return ValidationResult(accepted=False, reason=str(error) or _CLOSED)
        return ValidationResult(accepted=True)
