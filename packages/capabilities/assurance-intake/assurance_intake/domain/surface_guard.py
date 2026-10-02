"""Reject case steps that invent operations or pages outside the live surface."""

from __future__ import annotations

from collections.abc import Mapping

from assurance_intake.contracts.cases import CaseEntryAuthoring, CaseYamlAuthoring
from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument


class SurfaceMismatch(ValueError):
    """Authored case cites an operation or page absent from the live surface."""


def assert_cases_match_surface(
    document: CaseYamlAuthoring,
    api: ApiDiscoveryDocument,
    ui: UiExplorationDocument,
    families: set[str],
) -> None:
    operations = api.operation_keys()
    pages = ui.allowed_paths()
    for entry in (*document.added, *document.modified):
        if entry.type == "API" and "api" in families:
            _require_api_steps(entry, operations)
        elif entry.type == "E2E" and "e2e" in families:
            _require_e2e_steps(entry, pages)
        elif entry.type == "Fuzz" and "fuzz" in families:
            _require_fuzz_endpoints(entry, operations)
        elif entry.type == "Performance" and "performance" in families:
            _require_performance_endpoint(entry, operations)


def _require_api_steps(entry: CaseEntryAuthoring, operations: set[tuple[str, str]]) -> None:
    matched = 0
    for step in entry.steps:
        pair = _method_path_from_mapping(step)
        if pair is None:
            continue
        matched += 1
        _require_operation(pair, operations)
    if matched == 0:
        label = getattr(entry, "case_id", "API")
        raise SurfaceMismatch(
            f"{label}: API case requires at least one structured step "
            "{method: <METHOD>, path: /absolute/path} copied from api_discovery"
        )


def _require_e2e_steps(entry: CaseEntryAuthoring, pages: set[str]) -> None:
    matched = 0
    for step in entry.steps:
        path = _path_from_mapping(step)
        if path is None:
            continue
        matched += 1
        if path not in pages:
            raise SurfaceMismatch(path)
    if matched == 0:
        label = getattr(entry, "case_id", "E2E")
        raise SurfaceMismatch(f"{label}: E2E case requires at least one path step")


def _require_fuzz_endpoints(entry: CaseEntryAuthoring, operations: set[tuple[str, str]]) -> None:
    automation = getattr(entry, "automation", None)
    fuzz = getattr(automation, "fuzz", None) if automation is not None else None
    if fuzz is None:
        label = getattr(entry, "case_id", "Fuzz")
        raise SurfaceMismatch(f"{label}: Fuzz case requires automation.fuzz.endpoints")
    for endpoint in fuzz.endpoints:
        _require_operation((endpoint.method, endpoint.path), operations)


def _require_performance_endpoint(
    entry: CaseEntryAuthoring,
    operations: set[tuple[str, str]],
) -> None:
    automation = getattr(entry, "automation", None)
    performance = getattr(automation, "performance", None) if automation is not None else None
    if performance is None:
        label = getattr(entry, "case_id", "Performance")
        raise SurfaceMismatch(f"{label}: Performance case requires automation.performance")
    method, separator, path = performance.scenario.endpoint.partition(" ")
    if not separator or not method or not path:
        raise SurfaceMismatch(performance.scenario.endpoint)
    _require_operation((method, path), operations)


def _require_operation(pair: tuple[str, str], operations: set[tuple[str, str]]) -> None:
    if pair not in operations:
        raise SurfaceMismatch(f"{pair[0]} {pair[1]}")


def _method_path_from_mapping(step: object) -> tuple[str, str] | None:
    if not isinstance(step, Mapping):
        return None
    method = step.get("method")
    path = step.get("path")
    if isinstance(method, str) and isinstance(path, str):
        return (method, path)
    return None


def _path_from_mapping(step: object) -> str | None:
    if not isinstance(step, Mapping):
        return None
    path = step.get("path")
    return path if isinstance(path, str) else None
