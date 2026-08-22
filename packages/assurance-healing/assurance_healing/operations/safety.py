"""Safety combination, coverage-repair safety, and override decisions."""

from __future__ import annotations

from typing import Literal, cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import CoverageRepairSafetyInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairSafetyCheck
from assurance_healing.contracts.safety import CodegenFixerSafetyCheckV1, SafetyCheck
from assurance_healing.operations.common import InputError, failed_input, validate_input


def override_decision(*, require_approval: bool, token_valid: bool) -> Literal["allow", "deny"]:
    if require_approval and not token_valid:
        return "deny"
    return "allow"


def _verdict(flags: dict[str, bool]) -> tuple[bool, bool]:
    needs_review = flags["skip_or_xfail_added"] or flags["high_risk_proposal_applied"]
    passed = not (
        flags["product_code_modified"]
        or flags["skip_or_xfail_added"]
        or flags["unrelated_tests_modified"]
        or flags["assertion_expected_value_changes_detected"]
        or flags["high_risk_proposal_applied"]
    )
    return passed, needs_review


class CombineFixerSafetyHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("combine-fixer-safety requires fragments")
            fragments_raw = raw.get("fragments")
            if not isinstance(fragments_raw, list) or not fragments_raw:
                raise InputError("combine-fixer-safety requires at least one fragment")
            flags = {
                "product_code_modified": False,
                "skip_or_xfail_added": False,
                "unrelated_tests_modified": False,
                "assertion_expected_value_changes_detected": False,
                "high_risk_proposal_applied": False,
            }
            count = 0
            for item in fragments_raw:
                fragment = CodegenFixerSafetyCheckV1.model_validate(item)
                flags["product_code_modified"] = (
                    flags["product_code_modified"] or fragment.product_code_modified
                )
                flags["skip_or_xfail_added"] = flags["skip_or_xfail_added"] or fragment.skip_or_xfail_added
                flags["unrelated_tests_modified"] = (
                    flags["unrelated_tests_modified"] or fragment.unrelated_tests_modified
                )
                flags["assertion_expected_value_changes_detected"] = (
                    flags["assertion_expected_value_changes_detected"]
                    or fragment.assertion_expected_value_changes_detected
                )
                flags["high_risk_proposal_applied"] = (
                    flags["high_risk_proposal_applied"] or fragment.high_risk_proposal_applied
                )
                count += fragment.applied_proposal_count
            passed, needs_review = _verdict(flags)
            check = SafetyCheck(
                schema_version="1",
                passed=passed and not needs_review,
                needs_review=needs_review,
                product_code_modified=flags["product_code_modified"],
                skip_or_xfail_added=flags["skip_or_xfail_added"],
                unrelated_tests_modified=flags["unrelated_tests_modified"],
                assertion_expected_value_changes_detected=flags["assertion_expected_value_changes_detected"],
                high_risk_proposal_applied=flags["high_risk_proposal_applied"],
            )
            output = check.model_dump(mode="json")
            output["applied_proposal_count"] = count
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except Exception as error:
            return failed_input(InputError(str(error)))


class ComputeCoverageRepairSafetyHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(CoverageRepairSafetyInputV1, request.input)
            test_changed = _diff(payload.baseline.test_files_sha256, payload.current_test_files)
            product_changed = _diff(payload.baseline.product_files_sha256, payload.current_product_files)
            declaration_changed = _diff(
                payload.baseline.declaration_files_sha256, payload.current_declaration_files
            )
            allowed = set(payload.brief.allowed_test_files)
            unbriefed = tuple(path for path in test_changed if path not in allowed)
            mechanical = set(test_changed) | set(product_changed) | set(declaration_changed)
            summary_mismatch = set(payload.summary.files_modified) != mechanical
            stale_summary = not (
                payload.summary.change_id == payload.baseline.change_id
                and payload.summary.attempt == payload.baseline.attempt
                and payload.summary.attempt_token == payload.baseline.attempt_token
            )
            product_code_modified = bool(product_changed)
            declaration_files_modified = bool(declaration_changed)
            check = CoverageRepairSafetyCheck(
                change_id=payload.change_id,
                attempt=payload.attempt,
                passed=not product_code_modified and not declaration_files_modified,
                needs_review=bool(unbriefed or summary_mismatch or stale_summary),
                test_files_changed=test_changed,
                product_code_modified=product_code_modified,
                product_files_changed=product_changed,
                declaration_files_modified=declaration_files_modified,
                declaration_files_changed=declaration_changed,
                skip_or_xfail_added=False,
                unbriefed_files_modified=unbriefed,
                summary_applied=payload.summary.applied,
                summary_files_modified=payload.summary.files_modified,
                summary_mismatch=summary_mismatch,
                stale_summary=stale_summary,
            )
            return TaskOutcome.succeeded(cast(JSONValue, check.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)


def _diff(baseline: dict[str, str], current: dict[str, str]) -> tuple[str, ...]:
    changed = [
        path for path in sorted(set(baseline) | set(current)) if baseline.get(path) != current.get(path)
    ]
    return tuple(changed)
