from __future__ import annotations

import ast
import importlib
from pathlib import Path

SIX_WHEEL_WORKFLOW = Path(
    "tests/capabilities/fixtures/six-wheel-product/test_six_wheel_product/workflow.yaml"
)
CONVERTED_PRODUCTS = (
    Path("examples/graph-engine-toy-a/graph_engine_toy_a/product.py"),
    Path("examples/graph-engine-toy-b/graph_engine_toy_b/product.py"),
    Path("examples/agent-runtime-fixture/agent_runtime_fixture/product.py"),
    Path("tests/capabilities/fixtures/six-wheel-product/test_six_wheel_product/product.py"),
)
FORBIDDEN_CONVERSION_NAMES = frozenset(
    {
        "WorkflowDef",
        "parse_workflow",
        "graph_engine.graph.schema",
    }
)
DELETED_AUTHORITY_PATHS = (
    Path("packages/framework/graph-engine/graph_engine/graph"),
    Path("packages/framework/graph-engine/graph_engine/runtime"),
    Path("packages/framework/graph-engine/graph_engine/composition/workflow_assembler.py"),
)
PRODUCTION_ROOTS = (
    Path("packages/framework/graph-engine/graph_engine"),
    Path("packages/products/assurance-product/assurance_product"),
    Path("packages/capabilities"),
    Path("packages/adapters"),
)
EVIDENCE_ALLOWLIST = frozenset(
    {
        "packages/framework/graph-engine/graph_engine/evidence/events.py",
        "packages/framework/graph-engine/graph_engine/evidence/ledger.py",
        "packages/framework/graph-engine/graph_engine/evidence/models.py",
        "packages/framework/graph-engine/graph_engine/evidence/checkpoint.py",
        "packages/framework/graph-engine/graph_engine/evidence/seed.py",
    }
)
TOKEN_EVENT_NAMES = frozenset({"TokenOffered", "TokenConsumed"})
TOKEN_KIND_LITERALS = frozenset({"token_offered", "token_consumed", "node_activated"})
CLI_FORBIDDEN_NAMES = frozenset(
    {
        "Engine",
        "EngineError",
        "StartSpec",
        "acquire_invocation",
        "plan_next",
        "Scheduler",
        "select_wave",
        "fold_events",
    }
)
PERMANENT_MODULES = (
    "graph_engine.attempts.resources.activity",
    "graph_engine.attempts.execution_host.host_receipts",
    "graph_engine.attempts.orchestration.kernel",
    "graph_engine.attempts.execution_host.production_host",
    "graph_engine.attempts.resources.secret_sources",
    "graph_engine.attempts.resources.workspace",
    "graph_engine.persistence.attempt_checkpoint",
    "assurance_product.application",
    "assurance_product.cli",
    "assurance_product.runtime_ports",
    "assurance_product.status",
    "agent_runtime_opencode.handler",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_six_wheel_workflow_yaml_is_deleted() -> None:
    assert not (_repo_root() / SIX_WHEEL_WORKFLOW).exists()


def _product_uses_workflow_def(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "graph_engine.graph.schema" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "graph_engine.graph.schema":
                return True
            if any(alias.name in FORBIDDEN_CONVERSION_NAMES for alias in node.names):
                return True
    return False


def test_converted_examples_and_six_wheel_fixture_do_not_embed_workflow_def() -> None:
    still_embedded = tuple(
        path.as_posix() for path in CONVERTED_PRODUCTS if _product_uses_workflow_def(_repo_root() / path)
    )
    assert still_embedded == ()


def test_compiler_and_custom_runtime_packages_are_physically_absent() -> None:
    root = _repo_root()
    remaining = tuple(path.as_posix() for path in DELETED_AUTHORITY_PATHS if (root / path).exists())
    assert remaining == ()


def test_live_event_models_have_no_workflow_token_authority() -> None:
    root = _repo_root()
    offenders: list[str] = []
    for base in PRODUCTION_ROOTS:
        for path in (root / base).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if relative in EVIDENCE_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in TOKEN_EVENT_NAMES:
                    offenders.append(f"{relative}:{node.lineno}:{node.name}")
                if isinstance(node, ast.Constant) and node.value in TOKEN_KIND_LITERALS:
                    offenders.append(f"{relative}:{node.lineno}:{node.value}")
        assert offenders == []


def test_cli_imports_no_engine_planner_scheduler_or_leftover_fold() -> None:
    path = _repo_root() / "packages/products/assurance-product/assurance_product/cli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add(alias.name)
                if module.startswith("graph_engine.runtime") or module.startswith("graph_engine.graph"):
                    imported.add(f"{module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        leftover = sorted(
            name
            for name in imported
            if name in CLI_FORBIDDEN_NAMES or name.startswith("graph_engine.runtime")
        )
        assert leftover == []


def test_permanent_modules_resolve_without_runtime_package() -> None:
    assert not (_repo_root() / "packages/framework/graph-engine/graph_engine/runtime").exists()
    failed: list[str] = []
    for name in PERMANENT_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 — deletion gate reports the exact import
            failed.append(f"{name}:{type(error).__name__}:{error}")
            continue
        source = (
            Path(getattr(module, "__file__", "")).read_text(encoding="utf-8")
            if getattr(module, "__file__", None)
            else ""
        )
        if "graph_engine.runtime" in source:
            failed.append(f"{name}:source-imports-graph_engine.runtime")
        assert failed == []
