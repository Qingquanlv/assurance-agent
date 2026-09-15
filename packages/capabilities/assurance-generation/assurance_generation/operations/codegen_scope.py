"""Host-built codegen scope from reviewed cases. No model-authored plan package."""

from __future__ import annotations

from collections.abc import Mapping

from assurance_generation.contracts.codegen import (
    FAMILY_TARGET_ROOTS,
    CodegenScopeV1,
    case_module_from_path,
    locked_testdata_file,
    locked_test_file,
)
from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.plans import FuzzStrategyV1, PerformanceScenarioV1, PlanCoverageRow
from assurance_intake.contracts import CaseYamlAuthoring


def build_codegen_scope(
    *,
    family: LayerName,
    change_id: str,
    cases: CaseYamlAuthoring,
    capability_leafs: frozenset[str],
    case_ids_by_path: Mapping[str, tuple[str, ...]],
) -> CodegenScopeV1:
    entries = tuple((*cases.added, *cases.modified))
    coverage = tuple(
        PlanCoverageRow(
            case_id=entry.case_id,
            operation=entry.test_condition_id,
            risk=entry.risk.level,
            required_capabilities=tuple(sorted(entry.trace)),
        )
        for entry in entries
    )
    coverage = tuple(sorted(coverage, key=lambda row: row.case_id))
    case_ids = tuple(row.case_id for row in coverage)
    required = tuple(sorted({key for row in coverage for key in row.required_capabilities}))
    fuzz_strategy = None
    performance_scenarios: tuple[PerformanceScenarioV1, ...] = ()
    if family == "fuzz":
        fuzz = next(entry.automation.fuzz for entry in entries if entry.automation.fuzz is not None)
        endpoint = fuzz.endpoints[0]
        fuzz_strategy = FuzzStrategyV1(
            endpoint=f"{endpoint.method} {endpoint.path}",
            property_name=fuzz.property,
        )
    if family == "performance":
        performance_scenarios = tuple(
            PerformanceScenarioV1(
                scenario_id=f"{entry.case_id.lower()}-load",
                capability=entry.automation.performance.scenario.capability,
                endpoint=entry.automation.performance.scenario.endpoint,
                p95_ms=entry.automation.performance.scenario.thresholds.p95_ms,
                error_rate_max=entry.automation.performance.scenario.thresholds.error_rate_max,
            )
            for entry in entries
            if entry.automation.performance is not None
        )
    locked_modules = []
    for path, ids in sorted(case_ids_by_path.items()):
        if not ids:
            continue
        module = case_module_from_path(path)
        locked_modules.append(
            {
                "module": module,
                "case_ids": list(ids),
                "test_file": locked_test_file(family, module),
                "testdata_file": locked_testdata_file(family, module),
            }
        )
    generated = [path for row in locked_modules for path in (row["test_file"], row["testdata_file"])]
    locked_outputs = sorted(
        (
            *generated,
            f"qa/results/codegen/{family}-codegen-summary.md",
            f"qa/results/codegen/{family}-generated-files.json",
        )
    )
    return CodegenScopeV1.model_validate(
        {
            "schema_version": "1",
            "family": family,
            "change_id": change_id,
            "case_ids": case_ids,
            "required_capabilities": required,
            "coverage": [row.model_dump(mode="json") for row in coverage],
            "write_roots": list(FAMILY_TARGET_ROOTS[family]),
            "fuzz_strategy": None if fuzz_strategy is None else fuzz_strategy.model_dump(mode="json"),
            "performance_scenarios": [item.model_dump(mode="json") for item in performance_scenarios],
            "locked_modules": locked_modules,
            "locked_outputs": locked_outputs,
        },
        context={"capability_leafs": capability_leafs},
    )


__all__ = ["build_codegen_scope"]
