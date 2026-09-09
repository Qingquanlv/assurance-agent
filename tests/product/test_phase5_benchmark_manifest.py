from __future__ import annotations

import hashlib
import json
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from graph_engine.attempts.events import AttemptOpened, AttemptTerminated
from assurance_product.configuration import capability_leafs_from_knowledge

from tests.product.conformance import PREPARE_IDS

REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO / "benchmark" / "assurance-product" / "manifest.json"
RUNNER_PATH = REPO / "benchmark" / "assurance-product" / "run_item.py"
DEPT_REQUIREMENT_PATH = (
    REPO / "benchmark" / "vue-fastapi-admin" / "benchmark" / "requirements" / "dept-management.md"
)
DATA_KNOWLEDGE_PATH = REPO / "benchmark" / "vue-fastapi-admin" / ".aa" / "data-knowledge.yaml"
POLICY_PATH = REPO / "benchmark" / "vue-fastapi-admin" / ".aa" / "policy.yaml"
DEPT_SCHEMA_PATH = REPO / "benchmark" / "vue-fastapi-admin" / "app" / "schemas" / "depts.py"
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
    "execution.execute",
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
    assert item.selected_test_families == ("api",)
    assert item.case_modules == ("system/dept",)
    assert item.adapter_version == "0.1.0"
    assert item.expected_terminal == "completed"
    assert item.required_steps == FULL_WORKFLOW_REQUIRED_STEPS
    assert set(item.routing_assignments) == set(PREPARE_IDS)
    assert item.routing_assignments == item.deployment_binding_routes
    assert all(route.provider_model == "openai/gpt-5.6-terra" for route in item.routing_assignments.values())
    assert all(route.worker_profile == "max" for route in item.routing_assignments.values())


def test_opencode_benchmark_allows_four_review_fix_rounds() -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item_budget", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    assert '"review_rounds": 4' in runner._WRITE_PRODUCT_INPUT


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
                "selected_test_families": ["api"],
            },
        ),
        journal_events=events,
    )

    assert runner._status_steps(status.model_dump(mode="json")) == FULL_WORKFLOW_REQUIRED_STEPS


def test_full_benchmark_requirement_is_api_only() -> None:
    requirement = DEPT_REQUIREMENT_PATH.read_text(encoding="utf-8")
    assert "仅覆盖 API 层" in requirement
    assert "POST /api/v1/dept/create" in requirement
    assert "GET /api/v1/dept/list" in requirement
    assert "并发用户：10" not in requirement
    assert "P95 响应时间不超过 500 ms" not in requirement


def test_dept_create_rejects_names_longer_than_the_database_column() -> None:
    spec = importlib.util.spec_from_file_location("phase5_dept_schema", DEPT_SCHEMA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.DeptCreate(name="x" * 20)
    with pytest.raises(ValidationError):
        module.DeptCreate(name="x" * 21)
    with pytest.raises(ValidationError):
        module.DeptCreate(name="")


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


def test_packaged_performance_dept_adapter_is_not_declared_missing() -> None:
    knowledge = yaml.safe_load(DATA_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    dept = knowledge["capabilities"]["adapters"]["performance"]["dept"]

    assert dept["auth"]["symbol"] == "tests.perf.adapters.admin_auth.acquire_admin_token"
    assert dept["setup"]["symbol"] == "tests.perf.adapters.dept_seed.setup"
    assert dept["cleanup"]["symbol"] == "tests.perf.adapters.dept_seed.cleanup"
    assert dept["auth"]["create-if-missing"] is False
    assert dept["setup"]["create-if-missing"] is False
    assert dept["cleanup"]["create-if-missing"] is False


def test_packaged_performance_dept_adapter_materializes_and_cleans_one_valid_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_path = TEST_RUNTIME_SEED_ROOT / "tests" / "perf" / "adapters" / "dept_seed.py"
    spec = importlib.util.spec_from_file_location("phase5_dept_seed", adapter_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    manifest = tmp_path / "dept_seed.json"
    monkeypatch.setattr(module, "_MANIFEST", manifest)

    class Response:
        def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
            self._body = body
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def json(self):
            return self._body

        def failure(self, message: str) -> None:
            raise AssertionError(message)

        def success(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.nodes: list[dict[str, Any]] = []
            self.created: list[dict[str, Any]] = []
            self.deleted: list[int] = []

        def post(self, _path: str, *, json: dict[str, Any], **_kwargs):
            payload = dict(json)
            identifier = len(self.created) + 1
            node = {**payload, "id": identifier, "children": []}
            parent_id = payload["parent_id"]
            if parent_id == 0:
                self.nodes.append(node)
            else:
                parent = next(item for item in self._walk(self.nodes) if item["id"] == parent_id)
                parent["children"].append(node)
            self.created.append(payload)
            return Response({"code": 200, "msg": "Created Successfully", "data": None})

        def get(self, _path: str, **_kwargs):
            return Response({"code": 200, "msg": None, "data": self.nodes})

        def delete(self, _path: str, *, params: dict[str, int], **_kwargs):
            self.deleted.append(params["dept_id"])
            return Response({"code": 200, "msg": "Deleted Successfully", "data": None})

        @classmethod
        def _walk(cls, nodes: list[dict[str, Any]]):
            for node in nodes:
                yield node
                yield from cls._walk(node["children"])

    client = Client()
    seed = module.setup(client, {"token": "test-token"})

    assert [item["name"] for item in client.created] == [
        f"{seed.prefix}r",
        f"{seed.prefix}c",
        f"{seed.prefix}g",
    ]
    assert seed.prefix.startswith("p") and len(seed.prefix) == 9
    assert [item["order"] for item in client.created] == [0, 1, 2]
    assert [item["parent_id"] for item in client.created] == [0, 1, 2]
    assert json.loads(manifest.read_text(encoding="utf-8"))["grandchild_id"] == 3

    module.cleanup(client, {"token": "test-token"}, seed)

    assert client.deleted == [3, 2, 1]
    assert not manifest.exists()


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


def test_runner_accepts_api_only_full_benchmark_item() -> None:
    spec = importlib.util.spec_from_file_location("phase5_run_item_api_only", RUNNER_PATH)
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
    spec = importlib.util.spec_from_file_location("phase5_run_item_acceptance", RUNNER_PATH)
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
