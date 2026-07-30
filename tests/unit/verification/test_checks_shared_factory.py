"""Declared L1 shared factories must be reused in every layer's codegen plan."""

import pytest

from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.shared_factory import check_shared_factory

DK = {
    "capabilities": {
        "domain_factories": {
            "dept": {
                "make_dept": {"kind": "async_factory", "symbol": "tests.testdata.domain.dept.make_dept"},
                "cleanup_dept": {
                    "kind": "async_factory",
                    "symbol": "tests.testdata.domain.dept.cleanup_dept",
                },
            }
        }
    }
}

HEADER = (
    "## Factory Mapping\n\n"
    "| Entity | Shared Module | Function | Ownership | Required By |\n"
    "|---|---|---|---|---|\n"
)


def _ctx(rows: str) -> CheckContext:
    return CheckContext(
        plan_texts={"plans/api-codegen-plan.md": HEADER + rows},
        cases=(),
        data_knowledge=DK,
    )


def test_reuse_of_declared_factory_passes() -> None:
    rows = "| Dept | `tests/testdata/domain/dept.py` | `make_dept` | reuse | 003-006 |\n"
    assert check_shared_factory(_ctx(rows)).status == "pass"


def test_rewriting_a_declared_factory_fails_with_symbol_locator() -> None:
    rows = "| Dept | `tests/testdata/domain/dept.py` | `make_dept` | rewrite | 003 |\n"
    result = check_shared_factory(_ctx(rows))
    assert result.status == "fail"
    assert result.findings[0].locator == "plans/api-codegen-plan.md:5"
    assert result.findings[0].actual == "rewrite"
    assert "tests.testdata.domain.dept.make_dept" in result.findings[0].expected


def test_new_factory_absent_from_l1_is_not_constrained() -> None:
    rows = "| Menu | `tests/testdata/domain/menu.py` | `make_menu` | create | 020 |\n"
    assert check_shared_factory(_ctx(rows)).status == "pass"


def test_create_if_missing_wording_counts_as_reuse() -> None:
    rows = "| Dept | `tests/testdata/domain/dept.py` | `make_dept` | reuse (create-if-missing) | 003 |\n"
    assert check_shared_factory(_ctx(rows)).status == "pass"


def test_plan_without_factory_mapping_table_passes() -> None:
    ctx = CheckContext(plan_texts={"plans/api-plan.md": "# API Plan\n"}, cases=(), data_knowledge=DK)
    assert check_shared_factory(ctx).status == "pass"


def test_matching_columns_outside_factory_mapping_section_do_not_count_as_the_contract_table() -> None:
    plan = (
        "## Migration Notes\n\n"
        "| Shared Module | Function | Ownership |\n"
        "|---|---|---|\n"
        "| `tests/testdata/domain/dept.py` | `make_dept` | rewrite |\n"
    )
    ctx = CheckContext(
        plan_texts={"plans/api-codegen-plan.md": plan},
        cases=(),
        data_knowledge=DK,
    )
    result = check_shared_factory(ctx)
    assert result.status == "fail"
    assert result.findings[0].actual == "Factory Mapping table not parsed"


def test_factory_mapping_heading_variant_is_still_checked() -> None:
    plan = (HEADER + "| Dept | `tests/testdata/domain/dept.py` | `make_dept` | rewrite | 003 |\n").replace(
        "## Factory Mapping", "## Domain Factory Mappings"
    )
    result = check_shared_factory(
        CheckContext(plan_texts={"plans/api-codegen-plan.md": plan}, cases=(), data_knowledge=DK)
    )
    assert result.status == "fail"
    assert result.findings[0].actual == "rewrite"


def test_codegen_plan_without_factory_mapping_fails_closed() -> None:
    result = check_shared_factory(
        CheckContext(
            plan_texts={"plans/api-codegen-plan.md": "# API Codegen Plan\n\nNo factory table.\n"},
            cases=(),
            data_knowledge=DK,
        )
    )
    assert result.status == "fail"
    assert result.findings[0].actual == "Factory Mapping table not parsed"


@pytest.mark.parametrize("layer", ["api", "e2e", "fuzz", "performance"])
def test_every_codegen_layer_fails_closed_without_factory_mapping(layer: str) -> None:
    rel = f"plans/{layer}-codegen-plan.md"
    result = check_shared_factory(
        CheckContext(plan_texts={rel: f"# {layer} Codegen Plan\n"}, cases=(), data_knowledge=DK)
    )

    assert result.status == "fail"
    assert result.findings[0].locator == rel
