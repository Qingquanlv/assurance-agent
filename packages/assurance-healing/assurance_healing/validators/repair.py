"""Repair-candidate commit validator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_healing.validators.paths import canonical_relative, under_root

_TEST_ROOTS = ("tests/", "qa/changes/", "healing/")
_PRODUCT_ROOTS = ("app/", "src/", "web/src/")
_OUTSIDE = "repair candidate may write only approved test paths"
_UNNAMED = "modified file is not named by an approved proposal"
_PRODUCT = "product changes require approval"
_UNAPPROVED = "codegen fix proposal is not approved"


class RepairCandidateValidator:
    def __init__(
        self,
        *,
        approved_proposal: Mapping[str, Any] | None = None,
        require_approval: bool = True,
        path_only: bool = False,
    ) -> None:
        self._proposal = dict(approved_proposal or {})
        self._require_approval = require_approval
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        named = _named_files(self._proposal)
        approved = self._proposal.get("status") == "approved" or bool(named)
        for item in candidate.files:
            path = item.path
            if not canonical_relative(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if under_root(path, _PRODUCT_ROOTS):
                return ValidationResult(accepted=False, reason=_PRODUCT)
            if not under_root(path, _TEST_ROOTS):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if self._path_only:
                continue
            if (
                self._require_approval
                and self._proposal
                and self._proposal.get("status") not in {None, "approved"}
            ):
                return ValidationResult(accepted=False, reason=_UNAPPROVED)
            if named and path not in named:
                return ValidationResult(accepted=False, reason=_UNNAMED)
            if self._require_approval and not approved and named:
                return ValidationResult(accepted=False, reason=_UNAPPROVED)
        return ValidationResult(accepted=True)


def _named_files(proposal: Mapping[str, Any]) -> set[str]:
    named: set[str] = set()
    items = proposal.get("proposals")
    if not isinstance(items, list):
        return named
    for item in items:
        if not isinstance(item, dict):
            continue
        files = item.get("files_to_modify")
        if isinstance(files, list):
            named.update(str(path) for path in files)
    return named
