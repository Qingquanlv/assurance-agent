"""Structural mapping extraction and selected-test AST classifiers."""

from __future__ import annotations

from dataclasses import replace

import pytest

from assurance_agent.verification.generated_entries import (
    MappedTestEntry,
    MappingExtractionError,
    behavioral_policy_for_layer,
    classify_api_entry,
    classify_e2e_entry,
    classify_fuzz_entry,
    classify_generated_entry,
    classify_performance_entry,
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


def test_fuzz_mapping_accepts_parenthetical_section_qualifiers() -> None:
    plan = """# Fuzz

## Test Function Mapping (codegen binding)

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| FUZZ_001 | `test_fuzz_001` | `tests/fuzz/test_a.py` |

## Schema Acquisition Procedure (codegen must follow)

| Case ID | Strategy | Import Path |
|---------|----------|-------------|
| FUZZ_001 | `from_asgi` | `app:app` |
"""

    relation = extract_layer_mapping(
        layer="fuzz",
        plan_text=plan,
        cases=_cases("FUZZ_001", case_type="Fuzz"),
    )

    assert relation.entries == (
        MappedTestEntry(
            case_id="FUZZ_001",
            symbol="test_fuzz_001",
            target_file="tests/fuzz/test_a.py",
        ),
    )
    assert relation.schema_case_ids == ("FUZZ_001",)


def test_fuzz_mapping_accepts_inline_schema_acquisition_column() -> None:
    plan = """# Fuzz

## Test Function Mapping (codegen binding)

| Case ID | Test Function | Target File | Schema Acquisition | Endpoint |
|---------|---------------|-------------|--------------------|----------|
| FUZZ_001 | `test_fuzz_001` | `tests/fuzz/test_a.py` | Load app OpenAPI or use `app:app` | `POST /x` |

## Schema Acquisition Procedure (codegen must follow)

1. Prefer runtime OpenAPI from the FastAPI app.
2. Fall back to the documented OpenAPI URL.
"""

    relation = extract_layer_mapping(
        layer="fuzz",
        plan_text=plan,
        cases=_cases("FUZZ_001", case_type="Fuzz"),
    )

    assert relation.schema_case_ids == ("FUZZ_001",)


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
    assert relation.behavioral_policy is not None
    assert relation.policy().layer == "api"


def _entry(symbol: str = "test_api_001", path: str = "tests/api/test_a.py") -> MappedTestEntry:
    return MappedTestEntry(case_id="API_001", symbol=symbol, target_file=path)


def test_api_positive_requires_client_request_and_response_assertion() -> None:
    source = """
def test_api_001(client):
    response = client.get("/x")
    assert response.status_code == 200
    assert "id" in response.json()
"""
    decision = classify_api_entry(source, entry=_entry())
    assert decision.accepted
    assert decision.reason_code == "accepted"


def test_e2e_positive_requires_navigation_interaction_and_expect() -> None:
    source = """
from playwright.sync_api import expect

def test_e2e_001(page):
    page.goto("/")
    page.click("button")
    expect(page.locator("h1")).to_be_visible()
"""
    decision = classify_e2e_entry(
        source,
        entry=MappedTestEntry(case_id="E2E_001", symbol="test_e2e_001", target_file="tests/e2e/t.py"),
    )
    assert decision.accepted


def test_fuzz_positive_requires_schema_binding_and_call_and_validate() -> None:
    source = """
@schema.parametrize()
def test_fuzz_001(case):
    case.call_and_validate()
"""
    decision = classify_fuzz_entry(
        source,
        entry=MappedTestEntry(case_id="FUZZ_001", symbol="test_fuzz_001", target_file="tests/fuzz/t.py"),
    )
    assert decision.accepted


def test_performance_positive_requires_user_task_and_self_client() -> None:
    source = """
from locust import HttpUser, task

class ApiUser(HttpUser):
    @task
    def list_apis(self):
        self.client.get("/api/v1/api/list")
"""
    decision = classify_performance_entry(
        source,
        entry=MappedTestEntry(case_id="PERF_001", symbol="list_apis", target_file="tests/perf/l.py"),
    )
    assert decision.accepted


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("def test_api_001():\n    x = 1\n", "behaviorless_assignment"),
        ("def test_api_001():\n    return 1\n", "behaviorless_return"),
        ("def test_api_001():\n    assert True\n", "behaviorless_assert_true"),
        ("def test_api_001():\n    assert 1 == 1\n", "behaviorless_constant_compare"),
        ("@pytest.mark.skip\ndef test_api_001():\n    pass\n", "decorator_only"),
        ("def helper():\n    return 1\n", "helper_only"),
        (
            "def test_other(client):\n    response = client.get('/x')\n    assert response.status_code == 200\n",
            "unmapped_symbol",
        ),
        (
            "def not_a_test(client):\n    response = client.get('/x')\n    assert response.status_code == 200\n",
            "wrong_case_symbol",
        ),
        (
            "def test_api_001(client):\n    client.get('/x')\n",
            "missing_client_request",
        ),
        (
            "def test_api_001(client):\n    response = client.get('/x')\n    assert True\n",
            "missing_response_assertion",
        ),
        (
            "def test_api_001():\n    x = 1\n    assert True\n",
            "missing_client_request",
        ),
    ],
)
def test_api_rejects_behaviorless_and_incomplete_shapes(source: str, reason: str) -> None:
    symbol = "not_a_test" if "not_a_test" in source else "test_api_001"
    decision = classify_api_entry(source, entry=_entry(symbol=symbol))
    assert not decision.accepted
    assert decision.reason_code == reason


def test_e2e_rejects_navigation_without_interaction_or_assertion() -> None:
    source = """
def test_e2e_001(page):
    page.goto("/")
"""
    decision = classify_e2e_entry(
        source,
        entry=MappedTestEntry(case_id="E2E_001", symbol="test_e2e_001", target_file="tests/e2e/t.py"),
    )
    assert decision.reason_code == "missing_interaction"


def test_fuzz_rejects_missing_bound_schema_call() -> None:
    source = """
@schema.parametrize()
def test_fuzz_001(case):
    assert True
"""
    decision = classify_fuzz_entry(
        source,
        entry=MappedTestEntry(case_id="FUZZ_001", symbol="test_fuzz_001", target_file="tests/fuzz/t.py"),
    )
    assert decision.reason_code in {"behaviorless_assert_true", "missing_schema_call"}


def test_performance_rejects_task_without_request_or_outside_user() -> None:
    outside = """
@task
def list_apis(self):
    self.client.get("/x")
"""
    decision = classify_performance_entry(
        outside,
        entry=MappedTestEntry(case_id="PERF_001", symbol="list_apis", target_file="tests/perf/l.py"),
    )
    assert decision.reason_code == "missing_user_ownership"

    no_request = """
from locust import HttpUser, task

class ApiUser(HttpUser):
    @task
    def list_apis(self):
        return None
"""
    decision = classify_performance_entry(
        no_request,
        entry=MappedTestEntry(case_id="PERF_001", symbol="list_apis", target_file="tests/perf/l.py"),
    )
    assert decision.reason_code in {"behaviorless_return", "missing_client_request_in_task"}


def test_registered_client_forms_are_closed_policy_with_direct_mutation_coverage() -> None:
    policy = behavioral_policy_for_layer("api")
    mutated = replace(policy, client_names=frozenset({"custom_client"}))
    source = """
def test_api_001(custom_client):
    response = custom_client.get("/x")
    assert response.status_code == 200
"""
    assert classify_api_entry(source, entry=_entry(), policy=mutated).accepted
    assert not classify_api_entry(source, entry=_entry(), policy=policy).accepted


def test_classify_generated_entry_dispatches_by_layer() -> None:
    source = """
def test_api_001(client):
    response = client.post("/x", json={})
    assert response.status_code == 201
"""
    decision = classify_generated_entry(layer="api", source=source, entry=_entry())
    assert decision.accepted
