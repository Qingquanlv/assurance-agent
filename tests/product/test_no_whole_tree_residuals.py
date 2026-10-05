from __future__ import annotations

import asyncio
import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import get_args

import pytest

from graph_engine.attempts.activity import RuntimeEvent

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


def test_status_and_event_schemas_expose_no_tree_ids() -> None:
    from assurance_product.models import StatusV1

    status_fields = set(StatusV1.model_fields)
    assert status_fields.isdisjoint(_TREE_ID_FIELD_NAMES)

    schema = json.loads(files("assurance_product").joinpath("resources/schemas/status-v1.json").read_bytes())
    assert _schema_field_names(schema).isdisjoint(_TREE_ID_FIELD_NAMES)

    for event_type in _event_types():
        fields = set(getattr(event_type, "model_fields"))
        assert fields.isdisjoint(_TREE_ID_FIELD_NAMES), event_type


def test_toy_invocation_creates_no_whole_tree_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graph_engine.boot.generic import (
        boot_factory_product,
        contract_resolver_from_plugins,
        run_factory_product,
        workspace_provider_for,
    )
    from graph_engine.composition.lock import ProductLock

    composition = _toy_a_composition(tmp_path / "toy-composition", monkeypatch)
    assert not hasattr(composition, "workflow")
    assert composition.manifest.graph_factory_symbol
    assert isinstance(composition.lock, ProductLock)
    workspace, project_root = workspace_provider_for(tmp_path / "toy-engine")
    resolver = contract_resolver_from_plugins(composition.descriptors, workspace)
    artifact, kernel = boot_factory_product(
        composition,
        workspace=workspace,
        contract_resolver=resolver,
    )
    result = asyncio.run(
        run_factory_product(
            artifact,
            kernel=kernel,
            workspace=workspace,
            entrypoint="hello",
            invocation_id="toy-residual-1",
            graph_input={"name": "Ada"},
            lease_root=tmp_path / "toy-leases",
        )
    )
    assert getattr(result, "status") == "completed", result
    dumped = getattr(result, "model_dump")(mode="json") if hasattr(result, "model_dump") else vars(result)
    assert "final_tree_id" not in dumped
    toy_hits = _forbidden_layout_hits(tmp_path / "toy-engine")
    toy_hits.update(_forbidden_layout_hits(project_root))
    assert not toy_hits, toy_hits
