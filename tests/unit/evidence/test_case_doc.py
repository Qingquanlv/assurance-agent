"""EvidenceCaseEntry captures fold-needed fields from raw case YAML."""

from pathlib import Path

from assurance_agent.evidence.case_doc import load_case_entries


def _write_case(change_dir: Path, rel: str, body: str) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_full_fields_are_captured(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/dept/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_DEPT_API_001
    module: system.dept
    type: API
    title: create
    status: active
    priority: P0
    severity: blocker
    assertions:
      - returns 200
    automation:
      required: true
modified: []
removed: []
""",
    )
    entries, gaps = load_case_entries(change)
    assert gaps == []
    assert len(entries) == 1
    assert entries[0].case_id == "TC_DEPT_API_001"
    assert entries[0].assertions == ("returns 200",)
    assert entries[0].automation_required is True
    assert entries[0].perf_capability is None


def test_defaults_when_optional_fields_absent(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/x/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_X_API_001
    module: x
    type: API
    title: t
    status: active
    priority: P1
    severity: major
modified: []
removed: []
""",
    )
    entries, gaps = load_case_entries(change)
    assert gaps == []
    assert entries[0].assertions == ()
    assert entries[0].automation_required is False


def test_removed_bucket_is_skipped(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/x/case.yaml",
        """
schema_version: "1.0"
added: []
modified: []
removed:
  - case_id: TC_X_API_999
""",
    )
    entries, gaps = load_case_entries(change)
    assert gaps == []
    assert entries == []


def test_string_required_produces_gap_not_coercion(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/x/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_X_API_001
    module: x
    type: API
    title: t
    status: active
    priority: P1
    severity: major
    automation:
      required: "true"
modified: []
removed: []
""",
    )
    entries, gaps = load_case_entries(change)
    assert entries == []
    assert len(gaps) == 1
    assert gaps[0].code == "case_unreadable"


def test_perf_capability_is_flattened(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/perf/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_DEPT_PERF_001
    module: system.dept
    type: Performance
    title: p95
    status: active
    priority: P1
    severity: major
    automation:
      required: true
      performance:
        scenario:
          capability: dept_list
          thresholds:
            p95_ms: 200
            error_rate_max: 0.01
modified: []
removed: []
""",
    )
    entries, gaps = load_case_entries(change)
    assert gaps == []
    assert entries[0].perf_capability == "dept_list"
    assert entries[0].type == "Performance"


def test_non_list_assertions_produce_gap(tmp_path: Path) -> None:
    change = tmp_path / "CH-1"
    _write_case(
        change,
        "cases/x/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_X_API_001
    module: x
    type: API
    title: t
    status: active
    priority: P1
    severity: major
    assertions: "returns 200"
modified: []
removed: []
""",
    )
    entries, gaps = load_case_entries(change)
    assert entries == []
    assert gaps[0].code == "case_unreadable"
