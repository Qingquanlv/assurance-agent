from __future__ import annotations

import json
import importlib.util
import re
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_engine.attempts.orchestration.checkpoint import AttemptResult
from tests.attempt_checkpoints import completed_checkpoint
from assurance_product.configuration import capability_leafs_from_knowledge

from tests.product.conformance import PREPARE_IDS

REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO / "benchmark" / "assurance-product" / "manifest.json"
RUNNER_PATH = REPO / "benchmark" / "assurance-product" / "run_item.py"
DEPT_REQUIREMENT_PATH = REPO / "benchmark" / "assurance-product" / "requirements" / "dept-management.md"
DATA_KNOWLEDGE_PATH = REPO / "benchmark" / "vue-fastapi-admin" / ".aa" / "data-knowledge.yaml"
POLICY_PATH = REPO / "benchmark" / "vue-fastapi-admin" / ".aa" / "policy.yaml"
FULL_WORKFLOW_REQUIRED_STEPS = (
    "intake.intake",
    "intake.explore",
    "intake.case-design",
    "intake.case-review",
    "quality.fact-baseline",
    "generation.api.codegen",
    "generation.api.codegen-review",
    "execution.execute",
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


class BenchmarkManifest:
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
def benchmark_manifest() -> BenchmarkManifest:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError("benchmark manifest must be a JSON object")
    items = document.get("items")
    if not isinstance(items, list):
        raise AssertionError("benchmark manifest must declare items[]")
    loaded: dict[str, BenchmarkItem] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise AssertionError("each benchmark item must be a mapping")
        item_id = raw.get("id")
        if not isinstance(item_id, str):
            raise AssertionError("benchmark item id must be a string")
        loaded[item_id] = _load_item(raw)
    return BenchmarkManifest(loaded)


@pytest.mark.parametrize("module", ["dept", "user", "permission"])
def test_opencode_benchmark_has_full_locked_items(benchmark_manifest, module):
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items = document["items"]
    assert isinstance(items, list)
    assert len(items) == 3
    assert items[0]["id"] == "opencode-ret-dept-management"
    assert items[0]["sut_item_id"] == "RET-dept-management"
    assert items[0]["adapter_binding"]["protocol_profile"] == "opencode-http-v1"
    assert all("cursor" not in str(entry.get("id", "")).lower() for entry in items)
    item = benchmark_manifest.item(f"opencode-ret-{module}-management")
    assert item.sut_item_id == f"RET-{module}-management"
    assert item.product == "assurance-opencode"
    assert item.entrypoint == "full"
    assert item.selected_test_families == ("api",)
    assert item.case_modules == (f"system/{module}",)
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert all(
        route.provider_model == "deepseek/deepseek-flash" for route in item.routing_assignments.values()
    )
    assert all(route.worker_profile == "max" for route in item.routing_assignments.values())


def test_benchmark_runner_accepts_only_deepseek_v4_pro_routes() -> None:
    spec = importlib.util.spec_from_file_location("run_item_routing", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    deepseek_item = deepcopy(manifest["items"][0])
    for route_map in ("routing_assignments", "deployment_binding_routes"):
        for assignment in deepseek_item[route_map].values():
            assignment["provider_model"] = "deepseek/deepseek-flash"

    accepted = runner._manifest_item({"items": [deepseek_item]}, "opencode-ret-dept-management", "opencode")
    assert accepted["routing_assignments"] == deepseek_item["routing_assignments"]

    terra_item = deepcopy(deepseek_item)
    for route_map in ("routing_assignments", "deployment_binding_routes"):
        for assignment in terra_item[route_map].values():
            assignment["provider_model"] = "openai/gpt-5.6-terra"
    with pytest.raises(SystemExit, match="OpenCode model mismatch"):
        runner._manifest_item({"items": [terra_item]}, "opencode-ret-dept-management", "opencode")


def test_opencode_benchmark_allows_four_review_fix_rounds() -> None:
    assert '"review_rounds": 4' in RUNNER_PATH.read_text(encoding="utf-8")


def test_status_projection_round_trips_full_benchmark_steps(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from assurance_product.status import render_status_from_langgraph

    spec = importlib.util.spec_from_file_location("run_item_status", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    events = tuple(
        completed_checkpoint(
            terminal=AttemptResult(
                resolution_kind="committed",
                output={"status": "completed"},
                receipt_id=f"receipt-{node}",
                receipt_digest="d" * 64,
            ),
            invocation_id="inv-status-round-trip",
            public_entrypoint="full",
            semantic_node_id=node,
        )
        for node in FULL_WORKFLOW_REQUIRED_STEPS
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
                "selected_test_families": ["api"],
            },
        ),
        attempt_checkpoints=events,
        project_root=tmp_path,
    )

    assert runner._status_steps(status.model_dump(mode="json")) == FULL_WORKFLOW_REQUIRED_STEPS


def test_full_benchmark_requirement_is_api_only() -> None:
    requirement = DEPT_REQUIREMENT_PATH.read_text(encoding="utf-8")
    assert "仅覆盖 API 层" in requirement
    assert "POST /api/v1/dept/create" in requirement
    assert "GET /api/v1/dept/list" in requirement
    assert "并发用户：10" not in requirement
    assert "P95 响应时间不超过 500 ms" not in requirement


def test_dept_name_boundary_is_an_explicit_benchmark_requirement() -> None:
    requirement = DEPT_REQUIREMENT_PATH.read_text(encoding="utf-8")

    assert "部门名称长度不超过 20 个字符" in requirement
    assert "21 个及以上字符必须被拒绝" in requirement


def test_full_benchmark_declares_its_required_e2e_journey() -> None:
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))

    assert knowledge["journeys"] == ["dept_management_crud"]


def test_full_benchmark_declares_department_tree_integrity_as_a_closed_key() -> None:
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))

    assert knowledge["entities"]["dept"]["constraints"]["parent_child_tree_consistency"] is True
    assert "entities.dept.constraints.parent_child_tree_consistency" in capability_leafs_from_knowledge(
        knowledge
    )


def test_dept_capability_catalog_does_not_publish_missing_api_adapter() -> None:
    document = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    capabilities = document["capabilities"]

    assert "dept" in capabilities["domain_factories"]
    assert "dept" not in capabilities["adapters"]["api"]


def test_limited_identity_contract_names_the_managed_runtime_handoff() -> None:
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))

    assert knowledge["auth"]["api_limited_role_user_token"]["acquire"] == {
        "source": "pytest_fixture",
        "provisioner": "tests.api.conftest",
    }
    assert knowledge["auth"]["e2e_limited_user_login"]["acquire"] == {
        "source": "environment",
        "username_env": "QA_LIMITED_USERNAME",
        "password_env": "QA_LIMITED_PASSWORD",
        "provisioner": "benchmark-managed-sut",
    }
    assert knowledge["auth"]["api_no_role_superuser_token"]["acquire"] == {
        "source": "pytest_fixture",
        "provisioner": "tests.api.conftest",
    }
    assert knowledge["auth"]["api_no_role_user_token"]["acquire"] == {
        "source": "pytest_fixture",
        "provisioner": "tests.api.conftest",
    }


def test_benchmark_policy_declares_quality_goal_inputs() -> None:
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))

    assert policy["test_family_policy"] == {
        "required": ["api"],
        "allowed": ["api", "e2e", "fuzz", "performance"],
    }
    assert policy["coverage_floor_by_tier"] == {
        "low": 0.7,
        "medium": 0.8,
        "high": 0.9,
        "critical": 1.0,
    }
    assert policy["evidence_sufficiency"] == {
        "recency_hours": 24,
        "require_current_batch": True,
    }


def test_packaged_performance_dept_adapter_is_not_declared_missing() -> None:
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    dept = knowledge["capabilities"]["adapters"]["performance"]["dept"]

    assert dept["auth"]["symbol"] == "tests.perf.adapters.admin_auth.acquire_admin_token"
    assert dept["setup"]["symbol"] == "tests.perf.adapters.dept_seed.setup"
    assert dept["cleanup"]["symbol"] == "tests.perf.adapters.dept_seed.cleanup"
    assert dept["auth"]["create-if-missing"] is False
    assert dept["setup"]["create-if-missing"] is False
    assert dept["cleanup"]["create-if-missing"] is False


def test_runner_resolves_real_sut_without_copying(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("run_item_overlay", RUNNER_PATH)
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


def test_runner_accepts_api_only_full_benchmark_item() -> None:
    spec = importlib.util.spec_from_file_location("run_item_api_only", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["items"][0]["selected_test_families"] = ["api"]

    item = runner._manifest_item(document, "opencode-ret-dept-management", "opencode")

    assert item["selected_test_families"] == ["api"]


@pytest.mark.parametrize(
    ("terminal", "omitted_step", "expected_error"),
    [
        ("completed", None, None),
        ("completed", "execution.execute", "execution.execute"),
        ("completed", "quality.inspect", "quality.inspect"),
        ("completed", "quality.report", "quality.report"),
        ("failed", None, "terminal status"),
    ],
)
def test_full_benchmark_acceptance_does_not_require_a_repair_rerun(
    terminal: str, omitted_step: str | None, expected_error: str | None
) -> None:
    spec = importlib.util.spec_from_file_location("run_item_acceptance", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    item = runner._manifest_item(
        json.loads(MANIFEST_PATH.read_text(encoding="utf-8")),
        "opencode-ret-dept-management",
        "opencode",
    )
    # A healthy first execution goes straight through Inspect to Report. The
    # execution.run branch is only entered after an applied test repair.
    steps = [step for step in FULL_WORKFLOW_REQUIRED_STEPS if step != omitted_step]
    status = {
        "status": terminal,
        "change": {"state": "achieved" if terminal == "completed" else "open"},
        "selected_test_families": ["api"],
        "graph_hierarchy": [{"graph_instance_id": step, "graph_id": step} for step in steps],
        "node_states": [
            {"graph_instance_id": step, "node_id": "finalize", "state": "succeeded"} for step in steps
        ],
    }

    errors = runner._validate_live_result(item=item, status=status)

    if expected_error is None:
        assert errors == []
    else:
        assert any(expected_error in error for error in errors)


def test_runner_rejects_ambient_agent_permission_allows() -> None:
    spec = importlib.util.spec_from_file_location("run_item", RUNNER_PATH)
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
    spec = importlib.util.spec_from_file_location("run_item_boundary", RUNNER_PATH)
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
    from assurance_product.models import LOCKED_ALLOWED_ARTIFACT_PATHS

    assert LOCKED_ALLOWED_ARTIFACT_PATHS == (
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    )


def test_live_runner_keeps_polling_while_external_activity_is_recoverable() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")
    driver = (
        REPO / "packages" / "products" / "assurance-product" / "assurance_product" / "bootstrap" / "driver.py"
    ).read_text(encoding="utf-8")

    assert "parked_recovery" not in source
    assert "activity_recovery" not in source
    assert re.search(r'"bootstrap",\s*"run"', source)
    assert "timeout" in driver
    assert "run_invocation" in driver
