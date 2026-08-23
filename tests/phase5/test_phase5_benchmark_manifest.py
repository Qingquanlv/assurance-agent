from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.phase5.conformance import PREPARE_IDS

REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO / "benchmark" / "assurance-product-phase5" / "manifest.json"

FULL_WORKFLOW_REQUIRED_STEPS = (
    "intake.intake",
    "intake.explore",
    "intake.case-design",
    "intake.case-review",
    "generation.api.plan",
    "generation.api.plan-review",
    "generation.api.codegen",
    "generation.e2e.plan",
    "generation.e2e.plan-review",
    "generation.e2e.codegen",
    "generation.fuzz.plan",
    "generation.fuzz.plan-review",
    "generation.fuzz.codegen",
    "generation.performance.plan",
    "generation.performance.plan-review",
    "generation.performance.codegen",
    "execution.execute",
    "execution.run",
    "quality.fact-baseline",
    "quality.inspect",
    "quality.report",
)


@dataclass(frozen=True)
class RouteAssignment:
    provider_model: str
    worker_profile: str


@dataclass(frozen=True)
class BenchmarkItem:
    sut_item_id: str
    product: str
    entrypoint: str
    selected_test_families: tuple[str, ...]
    auto_archive: bool
    adapter_version: str
    expected_terminal: str
    required_steps: tuple[str, ...]
    routing_assignments: Mapping[str, RouteAssignment]
    deployment_binding_routes: Mapping[str, RouteAssignment]


class Phase5Manifest:
    def __init__(self, items: Mapping[str, BenchmarkItem]) -> None:
        self._items = dict(items)

    def item(self, item_id: str) -> BenchmarkItem:
        try:
            return self._items[item_id]
        except KeyError as error:
            raise AssertionError(f"unknown benchmark item: {item_id}") from error


def _route(raw: Mapping[str, Any]) -> RouteAssignment:
    return RouteAssignment(
        provider_model=str(raw["provider_model"]),
        worker_profile=str(raw["worker_profile"]),
    )


def _load_item(raw: Mapping[str, Any]) -> BenchmarkItem:
    routes = {key: _route(value) for key, value in dict(raw["routing_assignments"]).items()}
    binding_routes = {key: _route(value) for key, value in dict(raw["deployment_binding_routes"]).items()}
    return BenchmarkItem(
        sut_item_id=str(raw["sut_item_id"]),
        product=str(raw["product"]),
        entrypoint=str(raw["entrypoint"]),
        selected_test_families=tuple(raw["selected_test_families"]),
        auto_archive=bool(raw["auto_archive"]),
        adapter_version=str(raw["adapter_version"]),
        expected_terminal=str(raw["expected_terminal"]),
        required_steps=tuple(raw["required_steps"]),
        routing_assignments=routes,
        deployment_binding_routes=binding_routes,
    )


@pytest.fixture
def phase5_manifest() -> Phase5Manifest:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError("phase5 manifest must be a JSON object")
    items = document.get("items")
    if not isinstance(items, list):
        raise AssertionError("phase5 manifest must declare items[]")
    loaded: dict[str, BenchmarkItem] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise AssertionError("each benchmark item must be a mapping")
        item_id = raw.get("id")
        if not isinstance(item_id, str):
            raise AssertionError("benchmark item id must be a string")
        loaded[item_id] = _load_item(raw)
    return Phase5Manifest(loaded)


def test_opencode_benchmark_is_one_full_locked_item(phase5_manifest):
    item = phase5_manifest.item("opencode-ret-dept-management")
    assert item.sut_item_id == "RET-dept-management"
    assert item.product == "assurance-opencode"
    assert item.entrypoint == "full"
    assert item.selected_test_families == ("api", "e2e", "fuzz", "performance")
    assert item.auto_archive is True
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert all(route.provider_model == "openai/gpt-5.6-terra" for route in item.routing_assignments.values())
    assert all(route.worker_profile == "max" for route in item.routing_assignments.values())
