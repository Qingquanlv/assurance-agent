from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.phase5.conformance import EXPECTED_25_CASE_IDS
from tests.phase5.test_behavioral_projection import (
    CASE_ID,
    CHANGED_DIGEST,
    GOVERNING_CONTRACT,
    ComparisonResultV1,
    DispositionV1,
    compare_case,
    copy_export,
    identity_disposition,
    load_manifest,
    make_export,
    project_legacy_export,
    project_new_export,
    rewrite_manifest,
)


def _difference_fields(result: ComparisonResultV1) -> set[str]:
    fields: set[str] = set()
    for bucket in (result.governed_differences, result.undisposed_differences):
        for item in bucket:
            if isinstance(item, Mapping) and isinstance(item.get("field"), str):
                fields.add(item["field"])
    return fields


def test_undisposed_difference_fails_comparison(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(current, terminal_class="stopped", terminal_reason_category="healing_disallowed")
    result = compare_case(CASE_ID, legacy, current, {})
    assert result.case_id == CASE_ID
    assert result.passed is False
    assert result.governed_differences == ()
    assert "terminal_class" in _difference_fields(result)


def test_disposed_difference_requires_governing_contract(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        DispositionV1(
            case_id=CASE_ID,
            field="terminal_class",
            mode="exact",
            classification="legacy-bug",
            predicate=None,
            governing_contract="",
        )


def test_legacy_bug_disposition_records_governed_difference(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(current, terminal_class="stopped", terminal_reason_category="healing_disallowed")
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "terminal_class": DispositionV1(
                case_id=CASE_ID,
                field="terminal_class",
                mode="exact",
                classification="legacy-bug",
                predicate=None,
                governing_contract=GOVERNING_CONTRACT,
            ),
            "terminal_reason_category": DispositionV1(
                case_id=CASE_ID,
                field="terminal_reason_category",
                mode="exact",
                classification="legacy-bug",
                predicate=None,
                governing_contract="spec §20.3 STOP",
            ),
        },
    )
    assert result.passed is True
    assert result.undisposed_differences == ()
    assert "terminal_class" in _difference_fields(result)
    governed = next(
        item
        for item in result.governed_differences
        if isinstance(item, Mapping) and item.get("field") == "terminal_class"
    )
    assert governed["classification"] == "legacy-bug"
    assert governed["governing_contract"] == GOVERNING_CONTRACT


def test_required_disposition_does_not_copy_legacy_defect(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(current, terminal_class="stopped", terminal_reason_category="healing_disallowed")
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "terminal_class": DispositionV1(
                case_id=CASE_ID,
                field="terminal_class",
                mode="exact",
                classification="required",
                predicate=None,
                governing_contract="spec §20.3 STOP",
            ),
            "terminal_reason_category": DispositionV1(
                case_id=CASE_ID,
                field="terminal_reason_category",
                mode="exact",
                classification="required",
                predicate=None,
                governing_contract="spec §20.3 STOP",
            ),
        },
    )
    assert result.passed is True
    assert all(
        isinstance(item, Mapping) and item.get("classification") == "required"
        for item in result.governed_differences
    )


def test_disposition_must_match_case_and_field(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(current, terminal_class="stopped")
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "terminal_class": DispositionV1(
                case_id="full-e2e-only-success",
                field="terminal_class",
                mode="exact",
                classification="unspecified",
                predicate=None,
                governing_contract=GOVERNING_CONTRACT,
            )
        },
    )
    assert result.passed is False
    assert "terminal_class" in _difference_fields(result)
    assert result.undisposed_differences


def test_wrong_field_disposition_does_not_cover_mismatch(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(current, terminal_class="stopped")
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "report": DispositionV1(
                case_id=CASE_ID,
                field="report",
                mode="exact",
                classification="unspecified",
                predicate=None,
                governing_contract=GOVERNING_CONTRACT,
            )
        },
    )
    assert result.passed is False
    assert "terminal_class" in {
        item["field"]
        for item in result.undisposed_differences
        if isinstance(item, Mapping) and isinstance(item.get("field"), str)
    }


def test_set_mode_ignores_family_order(tmp_path: Path) -> None:
    legacy = make_export(
        tmp_path / "legacy",
        runtime_identity="legacy-aa",
        skipped_families=["performance", "e2e", "fuzz"],
    )
    current = make_export(
        tmp_path / "current",
        runtime_identity="legacy-aa",
        skipped_families=["e2e", "fuzz", "performance"],
    )
    result = compare_case(CASE_ID, legacy, current, {})
    assert result.passed is True
    assert result.undisposed_differences == ()
    assert project_legacy_export(legacy).skipped_families == project_new_export(current).skipped_families


def test_intentionally_different_runtime_identity(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = make_export(tmp_path / "current", runtime_identity="assurance-product")
    missing = compare_case(CASE_ID, legacy, current, {})
    assert missing.passed is False
    assert "runtime_identity" in _difference_fields(missing)
    disposed = compare_case(
        CASE_ID,
        legacy,
        current,
        {"runtime_identity": identity_disposition()},
    )
    assert disposed.passed is True
    assert disposed.undisposed_differences == ()
    assert any(
        isinstance(item, Mapping)
        and item.get("field") == "runtime_identity"
        and item.get("mode") == "intentionally-different"
        for item in disposed.governed_differences
    )


def test_intentionally_different_mode_fails_when_values_match(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {"runtime_identity": identity_disposition()},
    )
    assert result.passed is False
    assert "runtime_identity" in _difference_fields(result)


def test_predicate_mode_accepts_equivalent_report_sections(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    payload = load_manifest(current)
    payload["report"] = {
        "present": True,
        "digest": payload["report"]["digest"],
        "semantic_fields": {"decision": "pass", "prose": "model authored wording"},
    }
    (current / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    undisposed = compare_case(CASE_ID, legacy, current, {})
    assert undisposed.passed is False
    disposed = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "report": DispositionV1(
                case_id=CASE_ID,
                field="report",
                mode="predicate",
                classification="observational-noise",
                predicate="report-required-sections",
                governing_contract="spec §17.4 report required sections",
            )
        },
    )
    assert disposed.passed is True
    assert disposed.undisposed_differences == ()


def test_observational_noise_does_not_require_disposition(tmp_path: Path) -> None:
    legacy = make_export(
        tmp_path / "legacy",
        runtime_identity="same-runtime",
        noise={"session_id": "one", "token_count": 1, "log": "aaa", "event_seq": 3},
    )
    current = make_export(
        tmp_path / "current",
        runtime_identity="same-runtime",
        noise={"session_id": "two", "token_count": 99, "log": "bbb", "event_seq": 8},
    )
    result = compare_case(CASE_ID, legacy, current, {})
    assert result.passed is True
    assert result.undisposed_differences == ()


def test_comparison_case_id_must_be_one_of_the_task1_ids(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    with pytest.raises(ValueError, match="case"):
        compare_case("not-a-matrix-case", legacy, current, {})
    assert CASE_ID in EXPECTED_25_CASE_IDS


def test_unspecified_disposition_still_cites_a_contract(tmp_path: Path) -> None:
    legacy = make_export(tmp_path / "legacy", runtime_identity="legacy-aa")
    current = copy_export(legacy, tmp_path / "current")
    rewrite_manifest(
        current,
        issue_healing_decisions=[
            {"issue_class": "environment", "decision": "ignore", "evidence_digest": CHANGED_DIGEST}
        ],
    )
    result = compare_case(
        CASE_ID,
        legacy,
        current,
        {
            "issue_healing_decisions": DispositionV1(
                case_id=CASE_ID,
                field="issue_healing_decisions",
                mode="exact",
                classification="unspecified",
                predicate=None,
                governing_contract="spec §17.5 unspecified residual",
            )
        },
    )
    assert result.passed is True
    governed = next(
        item
        for item in result.governed_differences
        if isinstance(item, Mapping) and item.get("field") == "issue_healing_decisions"
    )
    assert governed["classification"] == "unspecified"
    assert governed["governing_contract"] == "spec §17.5 unspecified residual"
