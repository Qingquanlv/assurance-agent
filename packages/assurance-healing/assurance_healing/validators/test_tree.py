"""Test-tree commit validator."""

from __future__ import annotations

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_healing.validators.paths import canonical_relative, under_root

_DEFAULT_TEST_ROOTS = ("tests", "qa/changes")
_DEFAULT_PRODUCT_ROOTS = ("app", "src", "web/src")
_OUTSIDE = "healing candidate may write only approved test paths"
_UNAPPROVED = "test-tree changes require approval"


class TestTreeValidator:
    __test__ = False

    def __init__(
        self,
        *,
        allowed_test_roots: tuple[str, ...] = _DEFAULT_TEST_ROOTS,
        forbidden_product_roots: tuple[str, ...] = _DEFAULT_PRODUCT_ROOTS,
        require_approval: bool = True,
        approved: bool = False,
        path_only: bool = False,
    ) -> None:
        self._allowed = allowed_test_roots
        self._forbidden = forbidden_product_roots
        self._require_approval = require_approval
        self._approved = approved
        self._path_only = path_only

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        for item in candidate.files:
            path = item.path
            if not canonical_relative(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if under_root(path, self._forbidden):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if not under_root(path, self._allowed):
                return ValidationResult(accepted=False, reason=_OUTSIDE)
            if self._path_only:
                continue
            if self._require_approval and not self._approved:
                return ValidationResult(accepted=False, reason=_UNAPPROVED)
        return ValidationResult(accepted=True)
