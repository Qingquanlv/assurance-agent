from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_generation.contracts.codegen import (
    FAMILY_DIRS,
    FAMILY_TARGET_ROOTS,
    CodegenScopeV1,
    case_module_from_path,
    locked_testdata_file,
    locked_test_file,
)
from assurance_generation.operations.codegen_scope import build_codegen_scope
from assurance_intake.contracts import CaseYamlAuthoring
from planning_fixtures import VALID_LEAFS, reviewed_cases


def _cases(family: str) -> CaseYamlAuthoring:
    return CaseYamlAuthoring.model_validate(
        reviewed_cases(family),
        context={"capability_leafs": frozenset(VALID_LEAFS)},
    )


def _case_ids(cases: CaseYamlAuthoring) -> tuple[str, ...]:
    return tuple(sorted(entry.case_id for entry in (*cases.added, *cases.modified)))


def test_family_dirs_and_target_roots_are_per_family() -> None:
    assert FAMILY_DIRS == {
        "api": "api",
        "e2e": "e2e",
        "fuzz": "fuzz",
        "performance": "perf",
    }
    assert FAMILY_TARGET_ROOTS["api"] == ("qa/tests/api/", "qa/tests/testdata/api/")
    assert FAMILY_TARGET_ROOTS["e2e"] == ("qa/tests/e2e/", "qa/tests/testdata/e2e/")
    assert FAMILY_TARGET_ROOTS["fuzz"] == ("qa/tests/fuzz/", "qa/tests/testdata/fuzz/")
    assert FAMILY_TARGET_ROOTS["performance"] == ("qa/tests/perf/", "qa/tests/testdata/perf/")


def test_locked_paths_use_module_and_family_dir() -> None:
    assert case_module_from_path("qa/cases/dept/case.yaml") == "dept"
    assert locked_test_file("api", "dept") == "qa/tests/api/dept/test_dept.py"
    assert locked_testdata_file("api", "dept") == "qa/tests/testdata/api/dept.py"
    assert case_module_from_path("qa/cases/foo/bar/case.yaml") == "foo/bar"
    assert locked_test_file("e2e", "foo/bar") == "qa/tests/e2e/foo/bar/test_bar.py"
    assert locked_testdata_file("e2e", "foo/bar") == "qa/tests/testdata/e2e/foo/bar.py"


def test_case_module_from_path_rejects_non_case_yaml() -> None:
    with pytest.raises(ValueError, match="qa/cases/<module>/case.yaml"):
        case_module_from_path("qa/cases/dept.yaml")


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_host_builds_codegen_scope_from_reviewed_cases(family: str) -> None:
    cases = _cases(family)
    scope = build_codegen_scope(
        family=family,
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={"qa/cases/items/case.yaml": _case_ids(cases)},
    )
    assert isinstance(scope, CodegenScopeV1)
    assert scope.family == family
    assert scope.change_id == "CH-DEMO-001"
    assert scope.case_ids == (f"TC_{family.upper()}_001",)
    assert scope.required_capabilities == ("entities.item.create",)
    assert scope.write_roots == FAMILY_TARGET_ROOTS[family]
    assert len(scope.coverage) == 1
    row = scope.coverage[0]
    assert row.case_id == scope.case_ids[0]
    assert row.operation == "COND-1"
    assert row.risk == "high"
    assert row.required_capabilities == ("entities.item.create",)
    if family == "fuzz":
        assert scope.fuzz_strategy is not None
        assert scope.fuzz_strategy.endpoint == "POST /items"
        assert scope.fuzz_strategy.property_name == "item.create.payload"
    else:
        assert scope.fuzz_strategy is None
    if family == "performance":
        assert len(scope.performance_scenarios) == 1
        scenario = scope.performance_scenarios[0]
        assert scenario.scenario_id == "tc_performance_001-load"
        assert scenario.capability == "entities.item.create"
        assert scenario.endpoint == "POST /items"
        assert scenario.p95_ms == 200
        assert scenario.error_rate_max == 0.01
    else:
        assert scope.performance_scenarios == ()


def test_host_scope_rejects_unknown_capability_leaf() -> None:
    cases = _cases("api")
    with pytest.raises((ValueError, ValidationError), match="unknown capability leaf"):
        build_codegen_scope(
            family="api",
            change_id="CH-DEMO-001",
            cases=cases,
            capability_leafs=frozenset({"auth.session.create"}),
            case_ids_by_path={"qa/cases/items/case.yaml": _case_ids(cases)},
        )


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_host_scope_locks_module_test_and_testdata(family: str) -> None:
    cases = _cases(family)
    ids = _case_ids(cases)
    scope = build_codegen_scope(
        family=family,
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={"qa/cases/dept/case.yaml": ids},
    )
    directory = {"api": "api", "e2e": "e2e", "fuzz": "fuzz", "performance": "perf"}[family]
    assert scope.locked_modules[0].module == "dept"
    assert scope.locked_modules[0].case_ids == ids
    assert scope.locked_modules[0].test_file == f"qa/tests/{directory}/dept/test_dept.py"
    assert scope.locked_modules[0].testdata_file == f"qa/tests/testdata/{directory}/dept.py"
    assert scope.locked_outputs == tuple(
        sorted(
            (
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
                scope.locked_modules[0].test_file,
                scope.locked_modules[0].testdata_file,
            )
        )
    )


def test_host_scope_locks_one_pair_per_module() -> None:
    cases = _cases("api")
    first = cases.added[0].case_id
    scope = build_codegen_scope(
        family="api",
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={
            "qa/cases/dept/case.yaml": (first,),
            "qa/cases/menus/case.yaml": (),
        },
    )
    modules = {row.module: row for row in scope.locked_modules}
    assert set(modules) == {"dept"}
    assert modules["dept"].test_file == "qa/tests/api/dept/test_dept.py"


def test_host_scope_nested_module_keeps_full_testdata_path() -> None:
    cases = _cases("api")
    scope = build_codegen_scope(
        family="api",
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={"qa/cases/foo/bar/case.yaml": _case_ids(cases)},
    )
    row = scope.locked_modules[0]
    assert row.module == "foo/bar"
    assert row.test_file == "qa/tests/api/foo/bar/test_bar.py"
    assert row.testdata_file == "qa/tests/testdata/api/foo/bar.py"


def test_host_scope_rejects_invalid_case_path() -> None:
    cases = _cases("api")
    with pytest.raises((ValueError, ValidationError), match="qa/cases/<module>/case.yaml"):
        build_codegen_scope(
            family="api",
            change_id="CH-DEMO-001",
            cases=cases,
            capability_leafs=frozenset(VALID_LEAFS),
            case_ids_by_path={"qa/cases/dept.yaml": _case_ids(cases)},
        )
