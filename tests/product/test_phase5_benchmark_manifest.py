from __future__ import annotations

import hashlib
import json
import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_engine.attempts.events import AttemptOpened, AttemptTerminated

from tests.product.conformance import PREPARE_IDS

REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO / "benchmark" / "assurance-product" / "manifest.json"
RUNNER_PATH = REPO / "benchmark" / "assurance-product" / "run_item.py"
DEPT_REQUIREMENT_PATH = (
    REPO / "benchmark" / "vue-fastapi-admin" / "benchmark" / "requirements" / "dept-management.md"
)
DATA_KNOWLEDGE_PATH = REPO / "benchmark" / "vue-fastapi-admin" / ".aa" / "data-knowledge.yaml"
TEST_RUNTIME_SEED_ROOT = (
    REPO / "benchmark" / "assurance-product" / "fixtures" / "vue-fastapi-admin-tests-runtime-v1"
)

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
    case_modules: tuple[str, ...]
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
    if "auto_archive" in raw:
        raise AssertionError("benchmark items must not declare auto_archive")
    return BenchmarkItem(
        sut_item_id=str(raw["sut_item_id"]),
        product=str(raw["product"]),
        entrypoint=str(raw["entrypoint"]),
        selected_test_families=tuple(raw["selected_test_families"]),
        case_modules=tuple(raw["case_modules"]),
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
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items = document["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    assert items[0]["id"] == "opencode-ret-dept-management"
    assert items[0]["sut_item_id"] == "RET-dept-management"
    assert items[0]["adapter_binding"]["protocol_profile"] == "opencode-http-v1"
    assert all("cursor" not in str(entry.get("id", "")).lower() for entry in items)
    item = phase5_manifest.item("opencode-ret-dept-management")
    assert item.sut_item_id == "RET-dept-management"
    assert item.product == "assurance-opencode"
    assert item.entrypoint == "full"
    assert item.selected_test_families == ("api", "e2e", "fuzz", "performance")
    assert item.case_modules == ("system/dept",)
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert all(route.provider_model == "openai/gpt-5.6-terra" for route in item.routing_assignments.values())
    assert all(route.worker_profile == "max" for route in item.routing_assignments.values())


def test_opencode_benchmark_preserves_three_case_fix_rounds() -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item_budget", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    assert '"review_rounds": 3' in runner._WRITE_PRODUCT_INPUT


def test_status_projection_round_trips_full_benchmark_steps() -> None:
    from types import SimpleNamespace

    from assurance_product.status import render_status_from_langgraph

    spec = importlib.util.spec_from_file_location("phase5_run_item_status", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    events = tuple(
        event
        for semantic_node_id in FULL_WORKFLOW_REQUIRED_STEPS
        for event in (
            AttemptOpened(
                contract_digest="a" * 64,
                input_digest="b" * 64,
                graph_revision="c" * 64,
                invocation_id="inv-status-round-trip",
                public_entrypoint="full",
                semantic_node_id=semantic_node_id,
            ),
            AttemptTerminated(
                resolution_kind="committed",
                output={"status": "completed"},
                receipt_id=f"receipt-{semantic_node_id}",
                receipt_digest="d" * 64,
            ),
        )
    )
    status = render_status_from_langgraph(
        invocation_id="inv-status-round-trip",
        lock_digest="e" * 64,
        root_input_digest="f" * 64,
        entrypoint="full",
        change_id="CH-BENCHMARK-001",
        status="completed",
        snapshot=SimpleNamespace(
            next=(),
            interrupts=(),
            values={
                "terminal": {"status": "completed", "reason": "achieved"},
                "selected_test_families": ["api", "e2e", "fuzz", "performance"],
            },
        ),
        journal_events=events,
    )

    assert runner._status_steps(status.model_dump(mode="json")) == FULL_WORKFLOW_REQUIRED_STEPS


def test_full_benchmark_requirement_preconfirms_noninteractive_fuzz_and_performance() -> None:
    requirement = DEPT_REQUIREMENT_PATH.read_text(encoding="utf-8")
    assert "POST /api/v1/dept/create" in requirement
    assert "GET /api/v1/dept/list" in requirement
    assert "并发用户：10" in requirement
    assert "每秒启动用户：2" in requirement
    assert "持续时间：60 秒" in requirement
    assert "P95 响应时间不超过 500 ms" in requirement
    assert "错误率不超过 1%" in requirement


def test_dept_capability_catalog_does_not_publish_missing_api_adapter() -> None:
    document = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    capabilities = document["capabilities"]

    assert "dept" in capabilities["domain_factories"]
    assert "dept" not in capabilities["adapters"]["api"]


def test_dept_runtime_seed_is_closed_versioned_and_contains_no_case_oracle() -> None:
    manifest_path = TEST_RUNTIME_SEED_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared_files = manifest["files"]
    actual_files = {
        path.relative_to(TEST_RUNTIME_SEED_ROOT).as_posix()
        for path in TEST_RUNTIME_SEED_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))

    def dept_symbols(value: object, *, under_dept: bool = False) -> set[str]:
        if not isinstance(value, dict):
            return set()
        found: set[str] = set()
        for key, child in value.items():
            selected = under_dept or key == "dept"
            if selected and key == "symbol" and isinstance(child, str):
                found.add(child)
            found.update(dept_symbols(child, under_dept=selected))
        return found

    assert manifest["schema_version"] == "vue-fastapi-admin-tests-runtime/v1"
    assert set(declared_files) == actual_files
    assert dept_symbols(knowledge) <= set(manifest["symbols"])
    assert {
        "tests/__init__.py",
        "tests/config.py",
        "tests/conftest.py",
        "tests/schema_validation.py",
    } <= actual_files
    assert not any(
        part.startswith("test_") or part.startswith("locustfile")
        for relative in actual_files
        for part in Path(relative).parts
    )
    for relative, expected_digest in declared_files.items():
        assert hashlib.sha256((TEST_RUNTIME_SEED_ROOT / relative).read_bytes()).hexdigest() == expected_digest


def test_runner_resolves_real_sut_without_copying(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item_overlay", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    worktree = tmp_path / "repo" / ".worktrees" / "feature"
    relative = Path("benchmark/vue-fastapi-admin")
    fallback = worktree.parents[1] / relative
    for required in (fallback / "app", fallback / "web"):
        required.mkdir(parents=True)
    (fallback / "tests" / "api").mkdir(parents=True)
    (fallback / "tests" / "api" / "test_existing.py").write_text("original\n", encoding="utf-8")

    source = runner._resolve_sut(worktree, str(relative))

    assert source == fallback.resolve()
    assert not hasattr(runner, "_copy_sut")
    assert (source / "tests" / "api" / "test_existing.py").read_text(encoding="utf-8") == "original\n"


def test_runner_rejects_ambient_agent_permission_allows() -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    expected = {
        "agent": {
            "assurance-v1-explorer": {
                "prompt": "bounded",
                "tools": {"bash": False, "question": False, "write": True},
                "permission": {
                    "bash": {"*": "deny", "aa risk *": "deny"},
                    "edit": {"**": "deny", "**/explore/**": "allow"},
                    "external_directory": "deny",
                },
            }
        }
    }
    resolved = {
        "agent": {
            "assurance-v1-explorer": {
                "prompt": "bounded",
                "tools": None,
                "permission": {
                    "bash": {"*": "deny", "aa risk *": "allow"},
                    "edit": {
                        "**": "deny",
                        "**/explore/**": "allow",
                        "**/explore/advisory.json": "allow",
                        "**/context.json": "deny",
                    },
                    "external_directory": "deny",
                    "question": "deny",
                    "write": "allow",
                },
            }
        }
    }

    errors = runner._agent_profile_errors(resolved, expected)

    assert errors == [
        "assurance-v1-explorer: permission bash['aa risk *'] resolved to 'allow', expected 'deny'"
    ]


def test_runner_requires_loaded_assurance_boundary_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item_boundary", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    class Response:
        def __init__(self, tools: list[str]) -> None:
            self._payload = json.dumps(tools).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return self._payload

    observed_timeouts: list[float] = []

    def ready_with_timeout(_request, *, timeout: float):
        observed_timeouts.append(timeout)
        return Response(["read", "assurance_boundary_v1"])

    monkeypatch.setattr(runner, "urlopen", ready_with_timeout)
    assert runner._check_opencode_boundary_plugin("http://127.0.0.1:4096", Path("/project")) == []
    assert observed_timeouts == [300]

    monkeypatch.setattr(runner, "urlopen", lambda *_args, **_kwargs: Response(["read"]))
    assert runner._check_opencode_boundary_plugin("http://127.0.0.1:4096", Path("/project")) == [
        "resolved OpenCode tool surface is missing assurance_boundary_v1"
    ]

    attempts = 0
    backoffs: list[float] = []

    def bootstrap_then_ready(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("plugin bootstrap is still running")
        return Response(["read", "assurance_boundary_v1"])

    monkeypatch.setattr(runner, "urlopen", bootstrap_then_ready)
    monkeypatch.setattr(runner.time, "sleep", backoffs.append)
    assert runner._check_opencode_boundary_plugin("http://127.0.0.1:4096", Path("/project")) == []
    assert attempts == 2
    assert backoffs == [10]


def test_live_product_input_authorizes_declared_test_roots() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")
    assert '"allowed_artifact_paths": ["qa/archive", "qa/cases", "qa/changes", "tests"]' in source


def test_live_runner_keeps_polling_while_external_activity_is_recoverable() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")

    assert "parked_recovery" not in source
    assert 'last_run.get("terminal_reason") == "activity_recovery"' in source
