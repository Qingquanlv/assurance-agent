"""assert_ideal must not be narrowed or dropped (spec C2)."""

import pytest

from assurance_agent.verification.checks.assert_ideal import check_assert_ideal
from assurance_agent.verification.checks.base import CheckContext


def _case(case_id: str, title: str, assertion: str, *, type_: str = "API", required: bool = True) -> dict:
    return {
        "case_id": case_id,
        "title": title,
        "type": type_,
        "automation": {"required": required},
        "assertions": [assertion],
    }


CASES = (
    {
        "added": [
            _case("TC_DEPT_API_010", "duplicate name rejected", "returns 4xx failure (not unhandled 5xx)"),
            _case("TC_DEPT_API_002", "list tree", "HTTP 200 and data is array"),
        ]
    },
)

HEADER = "| Case ID | Scenario | Expected |\n|---|---|---|\n"
BOTH_PRESENT = (
    "| TC_DEPT_API_002 | List tree | HTTP 200; data array |\n"
    "| TC_DEPT_API_010 | Duplicate name | **assert_ideal HTTP 4xx** on dup create |\n"
)


def _ctx(rows: str, cases: tuple = CASES) -> CheckContext:
    return CheckContext(
        plan_texts={"plans/api-plan.md": HEADER + rows}, cases=cases, data_knowledge={}, layer="api"
    )


def test_complete_plan_with_ideal_4xx_passes() -> None:
    assert check_assert_ideal(_ctx(BOTH_PRESENT)).status == "pass"


def test_negated_500_on_an_assert_ideal_row_passes() -> None:
    rows = BOTH_PRESENT.replace("on dup create", "(not 500); rejected")
    assert check_assert_ideal(_ctx(rows)).status == "pass"


def test_500_denial_after_the_token_on_an_assert_ideal_row_passes() -> None:
    rows = BOTH_PRESENT.replace("on dup create", "; 500 is not acceptable")
    assert check_assert_ideal(_ctx(rows)).status == "pass"


def test_assert_ideal_row_expecting_500_fails() -> None:
    rows = BOTH_PRESENT.replace("**assert_ideal HTTP 4xx** on dup create", "assert_ideal HTTP 500")
    result = check_assert_ideal(_ctx(rows))
    assert result.status == "fail"
    finding = next(f for f in result.findings if f.actual == "500")
    assert finding.locator == "plans/api-plan.md:4"


def test_latency_500_on_an_assert_ideal_row_is_not_an_http_500() -> None:
    rows = BOTH_PRESENT.replace("on dup create", "; p95 < 500 ms")
    assert check_assert_ideal(_ctx(rows)).status == "pass"


@pytest.mark.parametrize(
    "wording",
    [
        "assert_ideal HTTP 4xx; 500 禁止出现",
        "assert_ideal HTTP 4xx; 禁止返回 500",
        "assert_ideal HTTP 4xx; 避免 HTTP 500",
    ],
)
def test_chinese_server_error_negation_is_not_treated_as_an_expectation(wording: str) -> None:
    rows = BOTH_PRESENT.replace("**assert_ideal HTTP 4xx** on dup create", wording)
    assert check_assert_ideal(_ctx(rows)).status == "pass"


def test_prose_line_mentioning_500_is_not_scanned() -> None:
    rows = BOTH_PRESENT + "\nDo not narrow assert_ideal 4xx expectations to bypass/500 morphologies.\n"
    assert check_assert_ideal(_ctx(rows)).status == "pass"


def test_deleting_a_case_from_the_plan_fails() -> None:
    result = check_assert_ideal(_ctx("| TC_DEPT_API_002 | List tree | HTTP 200; data array |\n"))
    assert result.status == "fail"
    finding = next(f for f in result.findings if f.locator == "TC_DEPT_API_010")
    assert finding.actual == "no plan row"


def test_empty_plan_fails_for_every_in_scope_case() -> None:
    result = check_assert_ideal(_ctx(""))
    assert result.status == "fail"
    assert {f.locator for f in result.findings} == {"TC_DEPT_API_002", "TC_DEPT_API_010"}


def test_narrowed_rejection_expectation_fails_with_case_locator() -> None:
    rows = BOTH_PRESENT.replace("**assert_ideal HTTP 4xx** on dup create", "HTTP 200; name appears once")
    result = check_assert_ideal(_ctx(rows))
    assert result.status == "fail"
    finding = next(f for f in result.findings if f.locator == "TC_DEPT_API_010")
    assert "4xx" in finding.expected


def test_negated_case_rejection_token_does_not_require_a_plan_rejection_token() -> None:
    cases = ({"added": [_case("TC_DEPT_API_011", "create succeeds", "must not return 400; expect 200")]},)
    result = check_assert_ideal(_ctx("| TC_DEPT_API_011 | Create | HTTP 200 |\n", cases=cases))
    assert result.status == "pass"


def test_out_of_layer_and_non_automated_cases_are_out_of_scope() -> None:
    cases = (
        {
            "added": [
                _case("TC_DEPT_E2E_001", "E2E", "returns 4xx", type_="E2E"),
                _case("TC_DEPT_API_900", "manual", "returns 4xx", required=False),
            ]
        },
    )
    assert check_assert_ideal(_ctx("", cases=cases)).status == "pass"


def test_similar_case_ids_do_not_satisfy_each_other() -> None:
    cases = (
        {"added": [_case("TC_DEPT_API_1", "a", "returns 4xx"), _case("TC_DEPT_API_10", "b", "returns 4xx")]},
    )
    rows = "| TC_DEPT_API_10 | dup | **assert_ideal HTTP 4xx** |\n"
    result = check_assert_ideal(_ctx(rows, cases=cases))
    assert result.status == "fail"
    assert [f.locator for f in result.findings] == ["TC_DEPT_API_1"]


def test_same_case_across_tables_passes() -> None:
    across = CheckContext(
        plan_texts={
            "plans/api-plan.md": HEADER + BOTH_PRESENT,
            "plans/api-codegen-plan.md": "| Case ID | Test Function |\n|---|---|\n| TC_DEPT_API_010 | test_dup |\n| TC_DEPT_API_002 | test_list |\n",
        },
        cases=CASES,
        data_knowledge={},
        layer="api",
    )
    assert check_assert_ideal(across).status == "pass"


def test_same_case_twice_in_one_table_fails() -> None:
    rows = (
        "| TC_DEPT_API_002 | List tree | HTTP 200; data array |\n"
        "| TC_DEPT_API_010 | Duplicate name | assert_ideal HTTP 4xx |\n"
        "| TC_DEPT_API_010 | Duplicate name | response is not HTTP 500 |\n"
    )
    result = check_assert_ideal(_ctx(rows))
    assert result.status == "fail"
    assert any("duplicate" in finding.actual for finding in result.findings)


def test_interrupted_case_table_reports_a_structural_parse_failure() -> None:
    text = (
        HEADER
        + "| TC_DEPT_API_002 | List tree | HTTP 200; data array |\n"
        + "wrapped prose interrupts the table\n"
        + "| TC_DEPT_API_010 | Duplicate name | assert_ideal HTTP 4xx |\n"
    )
    result = check_assert_ideal(
        CheckContext(
            plan_texts={"plans/api-plan.md": text},
            cases=CASES,
            data_knowledge={},
            layer="api",
        )
    )
    assert result.status == "fail"
    finding = next(f for f in result.findings if f.locator == "plans/api-plan.md:5")
    assert finding.actual == "Case ID row outside a parseable Case ID table"
    missing = next(f for f in result.findings if f.locator == "TC_DEPT_API_010")
    assert missing.actual == "no plan row"


def test_success_alternative_does_not_exempt_a_rejection_case() -> None:
    cases = (
        {
            "added": [
                _case(
                    "TC_DEPT_API_012",
                    "create or reject",
                    "expect HTTP 201 or HTTP 409 when the name already exists",
                )
            ]
        },
    )
    result = check_assert_ideal(_ctx("| TC_DEPT_API_012 | Create | HTTP 201 |\n", cases=cases))

    assert result.status == "fail"
    assert any(f.locator == "TC_DEPT_API_012" for f in result.findings)


def test_registry_runs_every_check_and_folds_status() -> None:
    from assurance_agent.verification.checks.registry import PLAN_CHECKS, run_plan_checks

    assert [check.__name__ for check in PLAN_CHECKS] == [
        "check_l1_path",
        "check_shared_factory",
        "check_assert_ideal",
        "check_capability_keys",
    ]
    ctx = CheckContext(
        plan_texts={
            "plans/api-plan.md": "read `qa/.knowledge/data-knowledge.yaml`\n",
            "plans/api-test-data-plan.md": "# Plan\n",
            "plans/api-codegen-plan.md": "# Plan\n",
        },
        cases=(
            {
                "added": [
                    {
                        "case_id": "TC_DEPT_API_001",
                        "title": "smoke",
                        "type": "API",
                        "automation": {"required": True},
                        "assertions": ["HTTP 200"],
                    }
                ],
                "modified": [],
            },
        ),
        data_knowledge={},
    )
    doc = run_plan_checks(ctx)
    assert doc.status == "fail"
    assert {check.check_id for check in doc.checks} == {
        "l1_path",
        "shared_factory",
        "assert_ideal",
        "capability_keys",
    }
