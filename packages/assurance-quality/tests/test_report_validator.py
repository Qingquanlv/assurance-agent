from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import CandidateWriteSet, ValidationResult

from assurance_quality.plugin import QualityPlugin
from assurance_quality.validators.report import ReportValidator
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    HEX_B,
    validation_context,
    write_set,
)

SOURCE_PATHS = {
    "case": "cases/source-digest",
    "plan": "plans/source-digest",
    "mapping": "codegen/source-digest",
    "execution": "execution/source-digest",
    "healing": "healing/source-digest",
    "trace": "inspect/trace-projection.json",
    "coverage": "inspect/coverage-gaps.json",
    "issue": "issues/snapshot.json",
    "metrics": "inspect/metrics.json",
}


def candidate_report(*, missing: str | None = None) -> CandidateWriteSet:
    paths = ["report/quality-report.json"]
    paths.extend(path for key, path in SOURCE_PATHS.items() if key != missing)
    return write_set(*paths)


def test_report_validator_requires_every_source_digest() -> None:
    for key in SOURCE_PATHS:
        result = ReportValidator().validate(candidate_report(missing=key), validation_context())
        assert result.accepted is False
        assert "authenticated" in (result.reason or "")
        assert key in (result.reason or "")


def test_report_validator_default_fails_closed_without_source_files() -> None:
    result = ReportValidator().validate(write_set("report/quality-report.json"), validation_context())
    assert result.accepted is False
    assert result.reason


def test_report_validator_default_fails_closed_without_expected_digests() -> None:
    result = ReportValidator().validate(candidate_report(), validation_context())
    assert result.accepted is False
    assert result.reason == "quality report source digests are not authenticated"


def test_report_validator_rejects_digest_mismatch() -> None:
    expected = {key: HEX_A for key in SOURCE_PATHS}
    expected["metrics"] = HEX_B
    validator = ReportValidator(expected=expected)
    files = write_set(
        "report/quality-report.json",
        *SOURCE_PATHS.values(),
        digest=HEX_A,
    )
    result = validator.validate(files, validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="quality report source digest does not match the authenticated metrics projection",
    )


def test_report_validator_rejects_incomplete_expected_digest_map() -> None:
    validator = ReportValidator(expected={"metrics": HEX_A})
    result = validator.validate(candidate_report(), validation_context())
    assert result.accepted is False
    assert "incomplete" in (result.reason or "")


def test_report_validator_compares_every_authenticated_source() -> None:
    expected = {key: HEX_A for key in SOURCE_PATHS}
    result = ReportValidator(expected=expected).validate(candidate_report(), validation_context())
    assert result.accepted is True
    mismatched = {**expected, "case": HEX_B}
    rejected = ReportValidator(expected=mismatched).validate(candidate_report(), validation_context())
    assert rejected.accepted is False
    assert "case" in (rejected.reason or "")


def test_plugin_report_validator_is_path_only() -> None:
    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validator = contribution.commit_validators["assurance.quality.validator.report.v1"]
    accepted = validator.validate(write_set("report/quality-report.json"), validation_context())
    rejected = validator.validate(write_set("src/app.py"), validation_context())
    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.reason
