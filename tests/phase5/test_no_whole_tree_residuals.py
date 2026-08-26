from __future__ import annotations

import importlib
import json
import subprocess
import zipfile
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import get_args

import pytest

from graph_engine.plugin_api import InvocationWorkspaceBinding, TaskContext, TaskHandler
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.events import RuntimeEvent
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
)
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.task_workspace import TaskWorkspaceStore

from tests.phase5.cli_support import start_lifecycle_invocation
from tests.phase5.composition_harness import InstalledSources

pytestmark = pytest.mark.usefixtures("installed_sources")


_DELETED_PUBLIC_IMPORTS = (
    ("graph_engine", "SnapshotStore"),
    ("graph_engine.runtime", "SnapshotStore"),
    ("graph_engine.runtime", "WorkspaceSeed"),
    ("graph_engine.runtime", "capture_workspace_seed"),
    ("graph_engine.runtime", "materialize_snapshot"),
    ("graph_engine.runtime", "HeadAdvanced"),
    ("graph_engine", "AttemptWorkspaceIdentity"),
    ("graph_engine.plugin_api", "AttemptWorkspaceIdentity"),
    ("assurance_product.models", "ResultExportV1"),
    ("assurance_product.models", "ExportedArtifactV1"),
)
_DELETED_MODULES = (
    "graph_engine.runtime.tree_io",
    "graph_engine.runtime.workspace",
)
_TREE_ID_FIELD_NAMES = frozenset(
    {
        "tree_id",
        "initial_tree_id",
        "current_head_tree_id",
        "final_tree_id",
        "previous_tree_id",
        "seed_tree_id",
        "baseline_tree_id",
    }
)
_FORBIDDEN_LAYOUT_NAMES = frozenset({"trees", "HEAD.json", "result-tree", "result-export"})


def _import_name(module_name: str, attribute: str) -> object:
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def _schema_field_names(schema: object) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(str(key) for key in properties)
        required = schema.get("required")
        if isinstance(required, list):
            names.update(str(item) for item in required)
        definitions = schema.get("$defs")
        if isinstance(definitions, dict):
            for nested in definitions.values():
                names.update(_schema_field_names(nested))
        for nested in schema.values():
            if isinstance(nested, dict | list):
                names.update(_schema_field_names(nested))
    elif isinstance(schema, list):
        for item in schema:
            names.update(_schema_field_names(item))
    return names


def _event_types() -> tuple[type[object], ...]:
    event_union = get_args(RuntimeEvent)[0]
    return get_args(event_union)


def _forbidden_layout_hits(root: Path) -> set[str]:
    if not root.exists():
        return set()
    hits: set[str] = set()
    for path in root.rglob("*"):
        if path.name in _FORBIDDEN_LAYOUT_NAMES:
            hits.add(str(path.relative_to(root)))
        if path.is_dir() and path.name == "workspace" and (path / "trees").exists():
            hits.add(str((path / "trees").relative_to(root)))
    return hits


class _InProcessTestHost:
    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: dict[str, TaskHandler],
        store: TaskWorkspaceStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        identity = call.attempt_root.workspace_identity
        binding = self._store.begin(
            task_id=identity.task_id,
            attempt=identity.attempt,
            output_paths=identity.output_paths,
        )
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=binding.project_root,
                write_root=binding.write_root,
                workspace_identity=binding.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: object) -> TaskHostCallResult:
        del call
        raise AssertionError("reconcile must stay unwired")

    async def cancel(self, call: object) -> TaskHostCallResult:
        del call
        raise AssertionError("cancel must stay unwired")

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[()]:
        del identity
        return ()


def _workspace_binding(root: Path) -> InvocationWorkspaceBinding:
    project_root = root.parent / f".{root.name}-project"
    attempts_root = root.parent / f".{root.name}-attempts"
    receipts_root = root.parent / f".{root.name}-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


def _toy_a_composition(root: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib as importlib_module
    import shutil
    import sys

    from graph_engine.composition import (
        EditableWheelPluginSource,
        EditableWheelProductSource,
        RegistryPlatform,
        ResolutionRequest,
    )

    source = root / "source"
    repository = Path(__file__).resolve().parents[2]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_a" or module_name.startswith("graph_engine_toy_a."):
            sys.modules.pop(module_name, None)
    importlib_module.invalidate_caches()
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                distribution="graph-engine-toy-a",
                entrypoint_name="toy-a",
                declaration_path="graph_engine_toy_a/product-declaration.json",
                source_root=source,
                source_files=source_files,
            ),
            plugins=(
                EditableWheelPluginSource(
                    distribution="graph-engine-toy-a",
                    entrypoint_name="toy-a",
                    declaration_path="graph_engine_toy_a/plugin-declaration.json",
                    source_root=source,
                    source_files=source_files,
                ),
            ),
        )
    )


@pytest.mark.parametrize(("module_name", "attribute"), _DELETED_PUBLIC_IMPORTS)
def test_deleted_public_imports_fail(module_name: str, attribute: str) -> None:
    with pytest.raises((ImportError, AttributeError)):
        _import_name(module_name, attribute)


@pytest.mark.parametrize("module_name", _DELETED_MODULES)
def test_deleted_modules_are_not_importable(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_built_wheels_omit_tree_workspace_and_result_export(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--package", "graph-engine", "--out-dir", str(tmp_path / "engine")],
        check=True,
    )
    subprocess.run(
        ["uv", "build", "--package", "assurance-product", "--out-dir", str(tmp_path / "product")],
        check=True,
    )
    engine_wheels = tuple((tmp_path / "engine").glob("*.whl"))
    product_wheels = tuple((tmp_path / "product").glob("*.whl"))
    assert len(engine_wheels) == 1
    assert len(product_wheels) == 1

    with zipfile.ZipFile(engine_wheels[0]) as archive:
        names = archive.namelist()
    assert all("tree_io" not in Path(name).parts for name in names)
    assert all(Path(name).name != "workspace.py" for name in names)
    assert all("result-export" not in name for name in names)

    with zipfile.ZipFile(product_wheels[0]) as archive:
        names = archive.namelist()
    assert all("result-export" not in name for name in names)
    assert all(Path(name).name != "workspace.py" for name in names)
    assert all("tree_io" not in Path(name).parts for name in names)


def test_status_and_event_schemas_expose_no_tree_ids() -> None:
    from assurance_product.models import StatusV1

    status_fields = set(StatusV1.model_fields)
    assert status_fields.isdisjoint(_TREE_ID_FIELD_NAMES)

    schema = json.loads(files("assurance_product").joinpath("resources/schemas/status-v1.json").read_bytes())
    assert _schema_field_names(schema).isdisjoint(_TREE_ID_FIELD_NAMES)

    for event_type in _event_types():
        fields = set(getattr(event_type, "model_fields"))
        assert fields.isdisjoint(_TREE_ID_FIELD_NAMES), event_type


def test_toy_and_assurance_invocation_creates_no_whole_tree_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_sources: InstalledSources,
) -> None:
    invocation = start_lifecycle_invocation(
        tmp_path / "assurance",
        installed_sources,
        invocation_id="inv-residual-001",
        drive=True,
    )
    try:
        roots: Iterable[Path] = (
            invocation.project_dir,
            invocation.engine_root,
            invocation.project_dir / "qa" / "changes" / invocation.change_id,
        )
        hits = set()
        for root in roots:
            hits.update(_forbidden_layout_hits(root))
        assert not hits, hits
        status_path = invocation.project_dir / "qa" / "changes" / invocation.change_id / "status.json"
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert set(status).isdisjoint(_TREE_ID_FIELD_NAMES)
    finally:
        invocation.engine.close()

    composition = _toy_a_composition(tmp_path / "toy-composition", monkeypatch)
    engine_root = tmp_path / "toy-engine"
    binding = _workspace_binding(engine_root)
    with Engine(engine_root, host=_InProcessTestHost()) as engine:
        with engine.start(
            composition,
            entrypoint="hello",
            invocation_id="toy-residual-1",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=binding,
        ) as handle:
            result = engine.run_until_blocked(handle)
            assert result.status == "succeeded", result
            assert "final_tree_id" not in result.model_dump(mode="json")
            toy_hits = _forbidden_layout_hits(tmp_path / "toy-engine")
            toy_hits.update(_forbidden_layout_hits(binding.project_root))
            toy_hits.update(_forbidden_layout_hits(binding.attempts_root))
            toy_hits.update(_forbidden_layout_hits(binding.receipts_root))
            assert not toy_hits, toy_hits
