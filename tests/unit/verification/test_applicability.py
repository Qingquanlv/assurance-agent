from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.contracts import load_execution_contracts


def _case(case_id: str, case_type: str, *, required: bool) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "type": case_type,
        "automation": {"required": required},
    }


def test_added_and_modified_automated_cases_make_layer_applicable() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [_case("TC_API_002", "API", required=True)],
                "modified": [_case("TC_API_001", "API", required=True)],
                "removed": [{"case_id": "TC_API_OLD"}],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.applicable is True
    assert result.case_ids == ("TC_API_001", "TC_API_002")


def test_manual_and_other_layer_cases_are_empty_scope() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [_case("TC_API_MANUAL", "API", required=False)],
                "modified": [_case("TC_E2E_001", "E2E", required=True)],
                "removed": [],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.model_dump(mode="json") == {
        "layer": "api",
        "applicable": False,
        "reason_code": "no_automated_cases",
        "case_ids": [],
    }


def test_missing_automation_is_manual_only() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [{"case_id": "TC_API_MANUAL", "type": "API"}],
                "modified": [],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.applicable is False
    assert result.reason_code == "no_automated_cases"


def test_missing_automation_required_is_manual_only() -> None:
    result = derive_layer_applicability(
        [
            {
                "added": [{"case_id": "TC_API_MANUAL", "type": "API", "automation": {}}],
                "modified": [],
            }
        ],
        get_layer_assurance_profile("api"),
    )
    assert result.applicable is False
    assert result.reason_code == "no_automated_cases"


@pytest.mark.parametrize(
    ("cases", "match"),
    [
        (
            [{"added": "not-a-list", "modified": []}],
            r"cases\[0\]\.added must be a list",
        ),
        (
            [{"added": [], "modified": "not-a-list"}],
            r"cases\[0\]\.modified must be a list",
        ),
        (
            [{"added": ["not-a-mapping"], "modified": []}],
            r"cases\[0\]\.added\[0\] must be a mapping",
        ),
        (
            [
                {
                    "added": [{"case_id": "TC", "type": "Unknown", "automation": {"required": True}}],
                    "modified": [],
                }
            ],
            r"cases\[0\]\.added\[0\]\.type is invalid",
        ),
        (
            [{"added": [{"case_id": "TC", "type": "API", "automation": None}], "modified": []}],
            r"cases\[0\]\.added\[0\]\.automation must be a mapping",
        ),
        (
            [{"added": [{"case_id": "TC", "type": "API", "automation": "true"}], "modified": []}],
            r"cases\[0\]\.added\[0\]\.automation must be a mapping",
        ),
        (
            [
                {
                    "added": [{"case_id": "TC", "type": "API", "automation": {"required": "true"}}],
                    "modified": [],
                }
            ],
            r"cases\[0\]\.added\[0\]\.automation\.required must be a boolean",
        ),
        (
            [{"added": [{"type": "API", "automation": {"required": True}}], "modified": []}],
            r"cases\[0\]\.added\[0\]\.case_id must be a non-empty string",
        ),
        (
            [{"added": [{"case_id": "  ", "type": "API", "automation": {"required": True}}], "modified": []}],
            r"cases\[0\]\.added\[0\]\.case_id must be a non-empty string",
        ),
        (
            [{"modified": []}],
            r"cases\[0\] missing required key 'added'",
        ),
        (
            [{"added": []}],
            r"cases\[0\] missing required key 'modified'",
        ),
        (
            [
                {
                    "added": [{"type": "E2E", "automation": {"required": True}}],
                    "modified": [],
                }
            ],
            r"cases\[0\]\.added\[0\]\.case_id must be a non-empty string",
        ),
    ],
)
def test_malformed_scope_raises_value_error(
    cases: list[Mapping[str, object]],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        derive_layer_applicability(cases, get_layer_assurance_profile("api"))


@pytest.mark.parametrize(
    ("layer", "case_rel", "case_id"),
    [
        ("fuzz", "tests/fixtures/assurance/fuzz-contract/cases/FUZZ-001/case.yaml", "FUZZ-001"),
        (
            "performance",
            "tests/fixtures/assurance/performance-contract/cases/PERF-001/case.yaml",
            "PERF-001",
        ),
    ],
)
def test_contract_fixture_cases_make_fuzz_and_performance_applicable(
    layer: str,
    case_rel: str,
    case_id: str,
) -> None:
    mapping = yaml.safe_load(Path(case_rel).read_text(encoding="utf-8"))
    assert isinstance(mapping, dict)
    result = derive_layer_applicability([mapping], get_layer_assurance_profile(layer))
    assert result.applicable is True
    assert result.case_ids == (case_id,)


def test_applicability_operation_contract_stays_cases_only() -> None:
    """Applicability still reads only cases; it may write not_applicable checks."""
    contract = load_execution_contracts(Path.cwd()).contracts["operation:derive-plan-layer-applicability"]
    checks = (
        "change:review/api-plan-checks.json",
        "change:review/e2e-plan-checks.json",
        "change:review/fuzz-plan-checks.json",
        "change:review/performance-plan-checks.json",
    )
    assert contract.reads == ("change:cases/**/case.yaml",)
    assert contract.writes == checks
    assert contract.authorization_writes == checks
    assert contract.side_effect_free is False
