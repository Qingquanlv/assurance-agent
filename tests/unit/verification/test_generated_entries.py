"""Structural mapping extraction for D16 generated-file authority."""

from __future__ import annotations

import pytest

from assurance_agent.verification.generated_entries import (
    MappingExtractionError,
    extract_layer_mapping,
    mapped_case_ids_for_path,
)


def _cases(*case_ids: str, case_type: str = "API") -> list[dict[str, object]]:
    return [
        {
            "added": [
                {
                    "case_id": case_id,
                    "type": case_type,
                    "automation": {"required": True},
                }
                for case_id in case_ids
            ],
            "modified": [],
        }
    ]


def _api_plan(*, rows: str) -> str:
    return f"""# API Codegen Plan

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
{rows}
"""


def test_api_mapping_extracts_case_function_file() -> None:
    plan = _api_plan(
        rows="| API_001 | `test_api_001` | `tests/api/test_a.py` |\n"
        "| API_002 | `test_api_002` | `tests/api/test_b.py` |"
    )
    relation = extract_layer_mapping(layer="api", plan_text=plan, cases=_cases("API_001", "API_002"))
    assert [entry.case_id for entry in relation.entries] == ["API_001", "API_002"]
    assert mapped_case_ids_for_path(relation, "tests/api/test_a.py") == ("API_001",)


def test_performance_uses_task_mapping_headers() -> None:
    plan = """# Perf

## Task Mapping

| Case ID | Task Method | Target File |
|---------|-------------|-------------|
| PERF_001 | `get_accounts` | `tests/perf/locustfile.py` |
"""
    relation = extract_layer_mapping(
        layer="performance",
        plan_text=plan,
        cases=_cases("PERF_001", case_type="Performance"),
    )
    assert relation.entries[0].symbol == "get_accounts"


def test_fuzz_requires_schema_acquisition_alignment() -> None:
    plan = """# Fuzz

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| FUZZ_001 | `test_fuzz_001` | `tests/fuzz/test_a.py` |

## Schema Acquisition

| Case ID | Strategy | Import Path |
|---------|----------|-------------|
| FUZZ_001 | `from_asgi` | `app.main:app` |
"""
    relation = extract_layer_mapping(
        layer="fuzz",
        plan_text=plan,
        cases=_cases("FUZZ_001", case_type="Fuzz"),
    )
    assert relation.schema_case_ids == ("FUZZ_001",)

    bad = plan.replace("FUZZ_001 | `from_asgi`", "FUZZ_999 | `from_asgi`")
    with pytest.raises(MappingExtractionError, match="schema_case_mismatch"):
        extract_layer_mapping(layer="fuzz", plan_text=bad, cases=_cases("FUZZ_001", case_type="Fuzz"))


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        (
            "# API\n\n## Other\n\n| Case ID | X |\n|---|---|\n| A | b |\n",
            "missing_mapping",
        ),
        (
            _api_plan(rows="| API_001 | `t1` | `tests/api/a.py` |\n| API_001 | `t2` | `tests/api/b.py` |"),
            "duplicated_mapping",
        ),
        (
            _api_plan(rows="| API_001 | `t1` |"),
            "malformed_mapping",
        ),
        (
            _api_plan(rows="| API_001 | `t1` | `tests/e2e/a.py` |"),
            "wrong_layer_mapping",
        ),
    ],
)
def test_mapping_rejects_invalid_shapes(plan: str, code: str) -> None:
    with pytest.raises(MappingExtractionError, match=code):
        extract_layer_mapping(layer="api", plan_text=plan, cases=_cases("API_001"))


def test_mapping_rejects_interrupted_table() -> None:
    plan = _api_plan(rows="| API_001 | `t1` | `tests/api/a.py` |") + (
        "\n| Case ID | Test Function | Target File |\n"
        "|---------|---------------|-------------|\n"
        "| API_002 | `t2` | `tests/api/b.py` |\n"
    )
    with pytest.raises(MappingExtractionError, match="interrupted_mapping"):
        extract_layer_mapping(layer="api", plan_text=plan, cases=_cases("API_001", "API_002"))


def test_selected_case_ids_filter_unautomated() -> None:
    plan = _api_plan(rows="| API_001 | `t1` | `tests/api/a.py` |\n| API_002 | `t2` | `tests/api/b.py` |")
    cases = [
        {
            "added": [
                {"case_id": "API_001", "type": "API", "automation": {"required": True}},
                {"case_id": "API_002", "type": "API", "automation": {"required": False}},
            ],
            "modified": [],
        }
    ]
    relation = extract_layer_mapping(layer="api", plan_text=plan, cases=cases)
    assert relation.selected_case_ids == ("API_001",)
    assert mapped_case_ids_for_path(relation, "tests/api/b.py") == ()
