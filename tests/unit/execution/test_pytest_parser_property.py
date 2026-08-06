"""Property-test identity routing in the pytest JSON parser (§5.1).

case_id wins, then ``@pytest.mark.property(...)``, else ``unmapped_tests``.
The three buckets are mutually exclusive for any single executed test.
"""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.workflow.execution.pytest_parser import parse_pytest_json


def _parse(tmp_path: Path, tests: list[dict], *, batch_id: str = "20260805-095100"):
    report = tmp_path / "api-report.json"
    report.write_text(json.dumps({"tests": tests, "exitcode": 0}), encoding="utf-8")
    return parse_pytest_json(
        change_id="CH-PROP",
        batch_id=batch_id,
        target="api",
        report_path=report,
        raw_log_path=str(tmp_path / "raw" / "api.log"),
        command="uv run pytest tests/api",
    )


def test_marker_case_unmapped_are_mutually_exclusive(tmp_path: Path) -> None:
    batch_id = "20260805-095100"
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_menu.py::test_tc_menu_api_001__create",
                "outcome": "passed",
                "markers": [{"name": "property", "args": ["entities.menu.constraints.name_unique"]}],
                "call": {"outcome": "passed", "duration": 0.01},
            },
            {
                "nodeid": "tests/api/test_props.py::test_duplicate_dept_name_rejected",
                "outcome": "passed",
                "markers": [{"name": "property", "args": ["entities.dept.constraints.name_unique"]}],
                "call": {"outcome": "passed", "duration": 0.02},
            },
            {
                "nodeid": "tests/api/test_smoke.py::test_plain_smoke",
                "outcome": "failed",
                "call": {"outcome": "failed", "duration": 0.0, "longrepr": "boom"},
            },
        ],
        batch_id=batch_id,
    )

    assert [c.case_id for c in result.cases] == ["TC_MENU_API_001"]
    assert len(result.property_tests) == 1
    prop = result.property_tests[0]
    assert prop.nodeid == "tests/api/test_props.py::test_duplicate_dept_name_rejected"
    assert prop.file == "tests/api/test_props.py"
    assert prop.constraint_keys == ("entities.dept.constraints.name_unique",)
    assert prop.outcome == "passed"
    assert prop.batch_id == batch_id
    assert len(result.unmapped_tests) == 1
    assert result.unmapped_tests[0].test_name == "test_plain_smoke"

    # Mutual exclusion: the case_id test must not also land in property_tests
    # even though it carried a property marker, and the property test must not
    # appear in unmapped_tests.
    prop_nodeids = {p.nodeid for p in result.property_tests}
    case_names = {c.test_name for c in result.cases}
    unmapped_names = {u.test_name for u in result.unmapped_tests}
    assert "test_tc_menu_api_001__create" in case_names
    assert "tests/api/test_menu.py::test_tc_menu_api_001__create" not in prop_nodeids
    assert "test_duplicate_dept_name_rejected" not in unmapped_names
    assert "test_duplicate_dept_name_rejected" not in case_names


def test_property_marker_via_keywords_alone_routes_with_empty_keys(tmp_path: Path) -> None:
    """Keywords-only identity: still routes to property_tests; keys stay empty.

    pytest-json-report keywords carry the marker name but not args, so key
    loss is expected and documented — collectors must use AST scan / dict
    markers / string ``property(...)`` forms when keys matter.
    """
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_props.py::test_required_fields",
                "outcome": "skipped",
                "keywords": ["test_required_fields", "property", "pytestmark"],
                "setup": {"outcome": "skipped", "duration": 0.0},
            }
        ],
    )
    assert result.cases == []
    assert result.unmapped_tests == []
    assert len(result.property_tests) == 1
    assert result.property_tests[0].outcome == "skipped"
    assert result.property_tests[0].constraint_keys == ()
    assert result.total == 1
    assert result.skipped == 1


def test_property_string_marker_args_are_parsed(tmp_path: Path) -> None:
    """String marker forms like ``property("k1","k2")`` must yield keys."""
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_props.py::test_combo",
                "outcome": "passed",
                "markers": [
                    'property("entities.dept.constraints.name_unique", '
                    '"entities.dept.constraints.required_fields")'
                ],
                "call": {"outcome": "passed", "duration": 0.0},
            }
        ],
    )
    assert result.property_tests[0].constraint_keys == (
        "entities.dept.constraints.name_unique",
        "entities.dept.constraints.required_fields",
    )


def test_property_dict_marker_args_are_preferred_channel(tmp_path: Path) -> None:
    """When markers carry args via the dict channel, assert those keys."""
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_props.py::test_required_fields",
                "outcome": "skipped",
                "keywords": ["test_required_fields", "property", "pytestmark"],
                "markers": [{"name": "property", "args": ["entities.dept.constraints.required_fields"]}],
                "setup": {"outcome": "skipped", "duration": 0.0},
            }
        ],
    )
    assert result.property_tests[0].constraint_keys == ("entities.dept.constraints.required_fields",)


def test_multiple_constraint_keys_on_one_property_marker(tmp_path: Path) -> None:
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_props.py::test_combo",
                "outcome": "passed",
                "markers": [
                    {
                        "name": "property",
                        "args": [
                            "entities.dept.constraints.name_unique",
                            "entities.dept.constraints.name_has_max_length",
                        ],
                    }
                ],
                "call": {"outcome": "passed", "duration": 0.0},
            }
        ],
    )
    assert result.property_tests[0].constraint_keys == (
        "entities.dept.constraints.name_unique",
        "entities.dept.constraints.name_has_max_length",
    )


def test_property_rows_count_toward_target_totals(tmp_path: Path) -> None:
    result = _parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_props.py::test_a",
                "outcome": "passed",
                "markers": [{"name": "property", "args": ["entities.dept.constraints.name_unique"]}],
                "call": {"outcome": "passed", "duration": 0.0},
            },
            {
                "nodeid": "tests/api/test_props.py::test_b",
                "outcome": "failed",
                "markers": [{"name": "property", "args": ["entities.dept.constraints.name_unique"]}],
                "call": {"outcome": "failed", "duration": 0.0, "longrepr": "x"},
            },
        ],
    )
    assert result.total == 2
    assert result.passed == 1
    assert result.failed == 1
    assert result.status == "failed"
