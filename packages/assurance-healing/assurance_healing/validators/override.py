"""Override-token commit validator."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import ValidationError

from graph_engine.plugin_api import PathWriteSet, ValidationContext, ValidationResult

from assurance_healing.contracts.safety import HealingOverrideTokenV1
from assurance_healing.contracts.wire import override_token_digest
from assurance_healing.validators.paths import canonical_relative, under_root

_ALLOWED = ("healing/", "qa/changes/")
_OUTSIDE = "override candidate may write only healing token paths"
_TOKEN = "override token does not match policy and candidate"
_MISSING = "override token is required"


class OverrideValidator:
    def __init__(
        self,
        *,
        expected_change_id: str | None = None,
        policy_digest: str | None = None,
        candidate_digest: str | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        path_only: bool = False,
    ) -> None:
        self._change_id = expected_change_id
        self._policy_digest = policy_digest
        self._candidate_digest = candidate_digest
        self._file_bytes = dict(file_bytes or {})
        self._path_only = path_only

    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in staged.files:
            if not canonical_relative(item.path) or not under_root(item.path, _ALLOWED):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
        if self._path_only:
            return ValidationResult(accepted=True)
        if not self._file_bytes:
            return ValidationResult(accepted=False, reason=_MISSING)
        for path, payload in self._file_bytes.items():
            del path
            try:
                token = HealingOverrideTokenV1.model_validate(json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
                return ValidationResult(accepted=False, reason=str(error))
            if self._change_id is None and self._policy_digest is None and self._candidate_digest is None:
                continue
            if self._change_id is None or self._policy_digest is None or self._candidate_digest is None:
                return ValidationResult(accepted=False, reason=_TOKEN)
            expected = override_token_digest(
                change_id=self._change_id,
                policy_digest=self._policy_digest,
                candidate_digest=self._candidate_digest,
            )
            if token.change_id != self._change_id or token.token_digest != expected:
                return ValidationResult(accepted=False, reason=_TOKEN)
        return ValidationResult(accepted=True)
