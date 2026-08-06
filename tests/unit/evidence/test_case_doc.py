import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import TraceGap
from assurance_agent.evidence.case_doc import EvidenceCaseEntry, load_case_entries


def _write_case_yaml(change_dir: Path, rel: str, body: str) -> Path:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_full_field_capture_including_flattened_performance_capability(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/performance/case.yaml",
        """
        schema_version: '1.0'
        added:
        - case_id: TC_PERF_001
          module: system.performance
          type: Performance
          assertions:
          - p95 latency under threshold
          - error rate under threshold
          automation:
            required: true
            performance:
              scenario:
                capability: GET /api/v1/api/list
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert gaps == []
    assert len(entries) == 1
    entry = entries[0]
    assert entry.case_id == "TC_PERF_001"
    assert entry.module == "system.performance"
    assert entry.type == "Performance"
    assert entry.assertions == ("p95 latency under threshold", "error rate under threshold")
    assert entry.automation_required is True
    assert entry.perf_capability == "GET /api/v1/api/list"


def test_defaults_apply_when_automation_and_assertions_absent(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added:
        - case_id: TC_API_001
          module: system.api
          type: API
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert gaps == []
    assert len(entries) == 1
    entry = entries[0]
    assert entry.assertions == ()
    assert entry.automation_required is False
    assert entry.perf_capability is None


def test_removed_entries_are_skipped(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added: []
        modified: []
        removed:
        - case_id: TC_API_999
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert gaps == []


def test_modified_entries_are_captured(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added: []
        modified:
        - case_id: TC_API_002
          module: system.api
          type: API
          assertions:
          - still valid
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert gaps == []
    assert [e.case_id for e in entries] == ["TC_API_002"]


@pytest.mark.parametrize("bad_required", ["true", "false", "1", 1, 0])
def test_non_bool_automation_required_produces_case_unreadable_gap_not_exception(
    tmp_path: Path, bad_required: object
) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        f"""
        schema_version: '1.0'
        added:
        - case_id: TC_API_003
          module: system.api
          type: API
          automation:
            required: {bad_required!r}
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert len(gaps) == 1
    gap = gaps[0]
    assert isinstance(gap, TraceGap)
    assert gap.code == "case_unreadable"
    assert gap.source == "cases/system/api/case.yaml"


@pytest.mark.parametrize(
    "automation_yaml",
    [
        "automation:\n            performance: not a mapping\n",
        "automation:\n            performance:\n              scenario: not a mapping\n",
        "automation:\n            performance:\n              scenario:\n                capability: 123\n",
    ],
    ids=["performance_not_mapping", "scenario_not_mapping", "capability_not_string"],
)
def test_present_malformed_nested_performance_keys_produce_case_unreadable_gap(
    tmp_path: Path, automation_yaml: str
) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/performance/case.yaml",
        f"""
        schema_version: '1.0'
        added:
        - case_id: TC_PERF_002
          module: system.performance
          type: Performance
          {automation_yaml}
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert len(gaps) == 1
    assert gaps[0].code == "case_unreadable"


def test_absent_nested_performance_keys_default_to_none_not_a_gap(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added:
        - case_id: TC_API_010
          module: system.api
          type: API
          automation:
            required: true
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert gaps == []
    assert len(entries) == 1
    assert entries[0].perf_capability is None


def test_present_falsy_malformed_bucket_produces_case_unreadable_gap(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added: ''
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert len(gaps) == 1
    assert gaps[0].code == "case_unreadable"


def test_non_list_assertions_produces_case_unreadable_gap_not_exception(tmp_path: Path) -> None:
    _write_case_yaml(
        tmp_path,
        "cases/system/api/case.yaml",
        """
        schema_version: '1.0'
        added:
        - case_id: TC_API_004
          module: system.api
          type: API
          assertions: not a list
        modified: []
        removed: []
        """,
    )
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert len(gaps) == 1
    assert gaps[0].code == "case_unreadable"


def test_unparseable_yaml_produces_case_unreadable_gap_for_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "cases" / "system" / "api" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("added:\n- case_id: [unterminated\n", encoding="utf-8")
    entries, gaps = load_case_entries(tmp_path)
    assert entries == []
    assert len(gaps) == 1
    assert gaps[0].code == "case_unreadable"


def test_evidence_case_entry_extra_fields_are_ignored() -> None:
    entry = EvidenceCaseEntry.model_validate(
        {
            "case_id": "TC_X_001",
            "module": "system.x",
            "type": "API",
            "title": "an on-disk field this DTO does not consume",
        }
    )
    assert entry.case_id == "TC_X_001"


def test_evidence_case_entry_strict_bool_rejects_string_and_int() -> None:
    with pytest.raises(ValidationError):
        EvidenceCaseEntry.model_validate(
            {"case_id": "TC_X_002", "module": "m", "type": "API", "automation_required": "true"}
        )
    with pytest.raises(ValidationError):
        EvidenceCaseEntry.model_validate(
            {"case_id": "TC_X_003", "module": "m", "type": "API", "automation_required": 1}
        )
