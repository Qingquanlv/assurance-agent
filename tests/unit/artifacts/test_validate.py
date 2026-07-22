import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.validate import (
    ChangeNotFoundError,
    UnknownPhaseError,
    validate_change,
)
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe

VALID_REVIEW = {"schema_version": "1.0", "decision": "pass", "findings": []}
INVALID_REVIEW = {"schema_version": "1.0", "decision": "maybe", "findings": []}
VALID_CASE_YAML = """schema_version: "1.0"
added:
  - case_id: TC_MENU_001
    title: create menu
    status: active
    priority: P1
    severity: major
    type: API
    module: menus
modified: []
removed: []
"""
VALID_QA_YAML = """schema_version: "1.0"
schema: qa-yaml/v1
created_at: "2026-07-15T00:00:00Z"
change:
  change_id: CH-1
  requirement_id: REQ-1
  feature_name: menus
  status: in_progress
targets:
  cases:
    - module: menus
      change_case_file: cases/menus/case.yaml
      target_case_file: qa/cases/menus/case.yaml
"""


def write(change_dir: Path, rel: str, content: str) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def change_dir(tmp_path: Path) -> Path:
    root = tmp_path / "qa" / "changes" / "CH-1"
    root.mkdir(parents=True)
    return root


def test_missing_change_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ChangeNotFoundError):
        validate_change(tmp_path / "qa" / "changes" / "NOPE")


def test_full_scan_validates_known_and_skips_unknown(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "cases/menus/case.yaml", VALID_CASE_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, "proposal.md", "# free-form, never validated")

    report = validate_change(change_dir)

    assert report.ok is True
    assert [r.path for r in report.results] == [
        ".qa.yaml",
        "cases/menus/case.yaml",
        "review/case-review.json",
    ]
    assert all(r.ok and r.errors == [] for r in report.results)


def test_invalid_artifact_reports_errors_and_ok_false(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(INVALID_REVIEW))

    report = validate_change(change_dir)

    assert report.ok is False
    (result,) = report.results
    assert result.artifact_type == "review"
    assert result.ok is False
    assert any("decision" in e for e in result.errors)


def test_parse_error_reported_as_validation_failure(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", "{not json")

    report = validate_change(change_dir)

    assert report.ok is False
    assert report.results[0].errors[0].startswith("parse error:")


def test_artifact_single_file_mode(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, "review/other-review.json", json.dumps(INVALID_REVIEW))

    report = validate_change(change_dir, artifact="review/case-review.json")

    assert report.ok is True
    assert [r.path for r in report.results] == ["review/case-review.json"]


def test_artifact_missing_file_reports_not_found(change_dir: Path) -> None:
    report = validate_change(change_dir, artifact="review/case-review.json")

    assert report.ok is False
    (result,) = report.results
    assert result.errors == ["file not found"]
    assert result.artifact_type == "review"


def test_artifact_unregistered_existing_file_fails_closed(change_dir: Path) -> None:
    write(change_dir, "proposal.md", "# not a registered artifact")

    report = validate_change(change_dir, artifact="proposal.md")

    assert report.ok is False
    assert report.results[0].artifact_type == "unregistered"
    assert report.results[0].errors == ["no registered artifact contract"]


def test_full_scan_with_no_registered_artifacts_fails_closed(change_dir: Path) -> None:
    write(change_dir, "proposal.md", "# free-form artifact")

    report = validate_change(change_dir)

    assert report.ok is False
    assert report.results[0].path == "(change)"
    assert report.results[0].errors == ["no registered artifacts found"]


def test_phase_filter_uses_packaged_produces(change_dir: Path) -> None:
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))
    write(change_dir, ".qa.yaml", VALID_QA_YAML)

    class CaseReviewSchema:
        def phase_produces(self, phase_id: str) -> list[str] | None:
            if phase_id == "case-review":
                return ["review/case-review.json"]
            return None

    report = validate_change(change_dir, phase="case-review", schema=CaseReviewSchema())

    assert [r.path for r in report.results] == ["review/case-review.json"]


def test_phase_directory_produce_matches_by_prefix(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "cases/menus/case.yaml", VALID_CASE_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))

    # packaged v2 schema: case-design outputs [.qa.yaml, proposal.md, cases/]
    report = validate_change(change_dir, phase="case-design")

    assert [r.path for r in report.results] == [".qa.yaml", "cases/menus/case.yaml"]


def test_unknown_phase_raises(change_dir: Path) -> None:
    with pytest.raises(UnknownPhaseError):
        validate_change(change_dir, phase="no-such-phase")


def test_schema_provider_overrides_packaged_produces(change_dir: Path) -> None:
    write(change_dir, ".qa.yaml", VALID_QA_YAML)
    write(change_dir, "review/case-review.json", json.dumps(VALID_REVIEW))

    class FakeSchema:
        def phase_produces(self, phase_id: str) -> list[str] | None:
            return [".qa.yaml"] if phase_id == "custom-phase" else None

    report = validate_change(change_dir, phase="custom-phase", schema=FakeSchema())
    assert [r.path for r in report.results] == [".qa.yaml"]

    with pytest.raises(UnknownPhaseError):
        validate_change(change_dir, phase="case-review", schema=FakeSchema())


@pytest.mark.parametrize("value", ["", ".", "..", "../CH-1", "a/b", "/tmp/x"])
def test_change_id_rejects_path_segments(value: str) -> None:
    with pytest.raises(UnsafeIdentifierError):
        assert_change_id_safe(value)


def test_change_id_accepts_canonical_values() -> None:
    assert_change_id_safe("REQ-001.login_v2")
