from __future__ import annotations

import base64
import csv
import hashlib
import importlib
from importlib.machinery import ModuleSpec, NamespaceLoader, PathFinder
from importlib import metadata
import json
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest

import graph_engine.composition.sources as wheel_sources
from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    EditableWheelProductSource as _EditableWheelProductSource,
    EditableWheelPluginSource as _EditableWheelPluginSource,
    SourceIdentity,
    SourceKind,
    SourceSnapshotError,
    WheelPluginSource as _WheelPluginSource,
    WheelProductSource as _WheelProductSource,
    load_snapshotted_entrypoint,
    snapshot_wheel_source,
)
from graph_engine.plugin_api import PluginContribution, PluginDescriptor, ProviderSource
from graph_engine.graph.schema import WorkflowDef
from graph_engine.product import PluginRequirement, ProductManifest


class _PluginProvider:
    def __init__(self, plugin_id: str = "toy.runtime", version: str = "1.2.3") -> None:
        self._descriptor = PluginDescriptor(
            schema_version="1",
            source=ProviderSource(
                distribution="toy-runtime",
                version=version,
                entrypoint_group="graph_engine.plugins",
                entrypoint_name="toy.runtime",
                entrypoint_value="toy_plugin:provider",
                declaration_path="toy_plugin/plugin-declaration.json",
                import_roots=("",),
            ),
            plugin_id=plugin_id,
            plugin_version=version,
            engine_api="0.2",
            task_handlers=(),
            commit_validators=(),
        )

    def descriptor(self) -> PluginDescriptor:
        return self._descriptor

    def contribute(self, _ports: object) -> PluginContribution:
        return PluginContribution.empty()


class _ProductProvider:
    def manifest(self) -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=ProviderSource(
                distribution="toy-product",
                version="1.2.3",
                entrypoint_group="graph_engine.products",
                entrypoint_name="toy.product",
                entrypoint_value="toy_plugin:provider",
                declaration_path="toy_plugin/product-declaration.json",
                import_roots=("",),
            ),
            product_id="toy.product",
            product_version="1.2.3",
            engine_api="0.2",
            plugins=(PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.2.3"),),
            entrypoints={"main": "root"},
            configuration={},
            workflow=WorkflowDef.model_validate(
                {
                    "name": "toy",
                    "entrypoints": {"main": "root"},
                    "retry": {},
                    "timeout": {},
                    "graphs": {
                        "root": {
                            "max_activations": 1,
                            "start": "done",
                            "nodes": {"done": {"kind": "end"}},
                            "edges": [],
                        }
                    },
                }
            ),
        )


def WheelPluginSource(**values: object) -> _WheelPluginSource:
    values.setdefault("declaration_path", "toy_plugin/plugin-declaration.json")
    return _WheelPluginSource.model_validate(values)


def WheelProductSource(**values: object) -> _WheelProductSource:
    values.setdefault("declaration_path", "toy_plugin/product-declaration.json")
    return _WheelProductSource.model_validate(values)


def EditableWheelPluginSource(**values: object) -> _EditableWheelPluginSource:
    root = Path(str(values["source_root"]))
    declaration_path = str(values.setdefault("declaration_path", "plugin-declaration.json"))
    source_files = tuple(values["source_files"])  # type: ignore[arg-type]
    if declaration_path not in source_files:
        values["source_files"] = (*source_files, declaration_path)
    source = ProviderSource(
        distribution=str(values["distribution"]),
        version="1.2.3",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=str(values["entrypoint_name"]),
        entrypoint_value="toy_plugin:provider",
        declaration_path=declaration_path,
        import_roots=("",),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=source,
        plugin_id=str(values["entrypoint_name"]),
        plugin_version="1.2.3",
        engine_api="0.2",
        task_handlers=(),
        commit_validators=(),
    )
    (root / declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "plugin",
                "source": source.model_dump(mode="json"),
                "descriptor": descriptor.model_dump(mode="json"),
            }
        )
    )
    return _EditableWheelPluginSource.model_validate(values)


def EditableWheelProductSource(**values: object) -> _EditableWheelProductSource:
    root = Path(str(values["source_root"]))
    declaration_path = str(values.setdefault("declaration_path", "product-declaration.json"))
    source_files = tuple(values["source_files"])  # type: ignore[arg-type]
    if declaration_path not in source_files:
        values["source_files"] = (*source_files, declaration_path)
    source = ProviderSource(
        distribution=str(values["distribution"]),
        version="1.2.3",
        entrypoint_group="graph_engine.products",
        entrypoint_name=str(values["entrypoint_name"]),
        entrypoint_value="toy_plugin:provider",
        declaration_path=declaration_path,
        import_roots=("",),
    )
    workflow = WorkflowDef.model_validate(
        {
            "name": "toy",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "done",
                    "nodes": {"done": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    manifest = ProductManifest(
        schema_version="1",
        source=source,
        product_id=str(values["entrypoint_name"]),
        product_version="1.2.3",
        engine_api="0.2",
        plugins=(PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.2.3"),),
        entrypoints={"main": "root"},
        configuration={},
        workflow=workflow,
    )
    manifest_document = manifest.model_dump(mode="json")
    manifest_document["workflow"] = workflow.model_dump(mode="json", exclude_defaults=True)
    (root / declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "product",
                "source": source.model_dump(mode="json"),
                "manifest": manifest_document,
            }
        )
    )
    return _EditableWheelProductSource.model_validate(values)


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _write_record(root: Path, dist_info: Path, relative_paths: tuple[str, ...]) -> None:
    declared = list(relative_paths)
    for declaration_path in (
        "toy_plugin/plugin-declaration.json",
        "toy_plugin/product-declaration.json",
    ):
        if (root / declaration_path).is_file() and declaration_path not in declared:
            declared.append(declaration_path)
    record_path = dist_info / "RECORD"
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        for relative_path in declared:
            content = (root / relative_path).read_bytes()
            writer.writerow((relative_path, _record_hash(content), len(content)))
        writer.writerow((record_path.relative_to(root).as_posix(), "", ""))


def _record_path(distribution: metadata.Distribution) -> Path:
    return Path(distribution.locate_file("toy_runtime-1.2.3.dist-info/RECORD"))


def _record_rows(distribution: metadata.Distribution) -> list[list[str]]:
    with _record_path(distribution).open(encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


def _replace_record_rows(distribution: metadata.Distribution, rows: list[list[str]]) -> None:
    with _record_path(distribution).open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)


def _installed_distribution(
    tmp_path: Path,
    *,
    name: str = "toy-runtime",
    version: str = "1.2.3",
    entrypoints: tuple[tuple[str, str, str], ...] = (
        ("graph_engine.plugins", "toy.runtime", "toy_plugin:provider"),
    ),
) -> metadata.Distribution:
    sys.modules.pop("toy_plugin", None)
    root = tmp_path / "site"
    package = root / "toy_plugin"
    package.mkdir(parents=True)
    package_init = package / "__init__.py"
    package_init.write_text("provider = object()\n", encoding="utf-8")

    plugin_name, plugin_value = next(
        (
            (entrypoint_name, value)
            for group, entrypoint_name, value in entrypoints
            if group == "graph_engine.plugins"
        ),
        ("toy.runtime", "toy_plugin:provider"),
    )
    plugin_source = ProviderSource(
        distribution=name,
        version=version,
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=plugin_name,
        entrypoint_value=plugin_value,
        declaration_path="toy_plugin/plugin-declaration.json",
        import_roots=("",),
    )
    plugin_descriptor = PluginDescriptor(
        schema_version="1",
        source=plugin_source,
        plugin_id=plugin_name,
        plugin_version=version,
        engine_api="0.2",
        task_handlers=(),
        commit_validators=(),
    )
    plugin_declaration = package / "plugin-declaration.json"
    plugin_declaration.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "plugin",
                "source": plugin_source.model_dump(mode="json"),
                "descriptor": plugin_descriptor.model_dump(mode="json"),
            }
        )
    )

    product_name, product_value = next(
        (
            (entrypoint_name, value)
            for group, entrypoint_name, value in entrypoints
            if group == "graph_engine.products"
        ),
        ("toy.product", "toy_plugin:provider"),
    )
    product_source = ProviderSource(
        distribution=name,
        version=version,
        entrypoint_group="graph_engine.products",
        entrypoint_name=product_name,
        entrypoint_value=product_value,
        declaration_path="toy_plugin/product-declaration.json",
        import_roots=("",),
    )
    workflow = WorkflowDef.model_validate(
        {
            "name": "toy",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "done",
                    "nodes": {"done": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    product_manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id=product_name,
        product_version=version,
        engine_api="0.2",
        plugins=(PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.2.3"),),
        entrypoints={"main": "root"},
        configuration={},
        workflow=workflow,
    )
    product_manifest_document = product_manifest.model_dump(mode="json")
    product_manifest_document["workflow"] = workflow.model_dump(mode="json", exclude_defaults=True)
    product_declaration = package / "product-declaration.json"
    product_declaration.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1",
                "kind": "product",
                "source": product_source.model_dump(mode="json"),
                "manifest": product_manifest_document,
            }
        )
    )

    dist_info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    entrypoints_path = dist_info / "entry_points.txt"
    sections: dict[str, list[tuple[str, str]]] = {}
    for group, entrypoint_name, value in entrypoints:
        sections.setdefault(group, []).append((entrypoint_name, value))
    entrypoints_path.write_text(
        "".join(
            f"[{group}]\n" + "".join(f"{entrypoint_name} = {value}\n" for entrypoint_name, value in values)
            for group, values in sections.items()
        ),
        encoding="utf-8",
    )
    _write_record(
        root,
        dist_info,
        (
            package_init.relative_to(root).as_posix(),
            plugin_declaration.relative_to(root).as_posix(),
            product_declaration.relative_to(root).as_posix(),
            metadata_path.relative_to(root).as_posix(),
            entrypoints_path.relative_to(root).as_posix(),
        ),
    )
    return metadata.Distribution.at(dist_info)


def _namespace_distribution(
    tmp_path: Path,
) -> tuple[metadata.Distribution, _WheelPluginSource, _WheelPluginSource, Path]:
    root = tmp_path / "site"
    package = root / "round3_namespace" / "plugins"
    package.mkdir(parents=True)
    plugin_ids = ("namespace.runtime", "namespace.sibling")
    module_names = ("provider", "sibling")
    sources: list[_WheelPluginSource] = []
    shared_module = package / "shared.py"
    shared_module.write_text("VALUE = 'authenticated sibling dependency'\n", encoding="utf-8")
    files: list[Path] = [shared_module]
    for plugin_id, module_name in zip(plugin_ids, module_names, strict=True):
        declaration_path = f"round3_namespace/plugins/{module_name}-declaration.json"
        entrypoint_value = f"round3_namespace.plugins.{module_name}:provider"
        source_expectation = ProviderSource(
            distribution="namespace-runtime",
            version="1.2.3",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=entrypoint_value,
            declaration_path=declaration_path,
            import_roots=("",),
        )
        descriptor = PluginDescriptor(
            schema_version="1",
            source=source_expectation,
            plugin_id=plugin_id,
            plugin_version="1.2.3",
            engine_api="1.0",
            task_handlers=(),
            commit_validators=(),
        )
        module_path = package / f"{module_name}.py"
        eager_shared_import = (
            "from round3_namespace.plugins import shared\n" if module_name == "provider" else ""
        )
        lazy_shared_import = (
            "        from round3_namespace.plugins import shared\n"
            "        assert shared.VALUE == 'authenticated sibling dependency'\n"
            if module_name == "sibling"
            else ""
        )
        module_path.write_text(
            "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
            f"{eager_shared_import}"
            f"_DESCRIPTOR = PluginDescriptor.model_validate({descriptor.model_dump(mode='python')!r})\n"
            "class Provider:\n"
            "    def descriptor(self):\n"
            f"{lazy_shared_import}"
            "        return _DESCRIPTOR\n"
            "    def contribute(self, _ports):\n"
            "        return PluginContribution.empty()\n"
            "provider = Provider()\n",
            encoding="utf-8",
        )
        declaration = root / declaration_path
        declaration.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1",
                    "kind": "plugin",
                    "source": source_expectation.model_dump(mode="json"),
                    "descriptor": descriptor.model_dump(mode="json"),
                }
            )
        )
        files.extend((module_path, declaration))
        sources.append(
            _WheelPluginSource(
                distribution="namespace-runtime",
                entrypoint_name=plugin_id,
                declaration_path=declaration_path,
            )
        )
    dist_info = root / "namespace_runtime-1.2.3.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_text(
        "Metadata-Version: 2.1\nName: namespace-runtime\nVersion: 1.2.3\n",
        encoding="utf-8",
    )
    entrypoints_path = dist_info / "entry_points.txt"
    entrypoints_path.write_text(
        "[graph_engine.plugins]\n"
        "namespace.runtime = round3_namespace.plugins.provider:provider\n"
        "namespace.sibling = round3_namespace.plugins.sibling:provider\n",
        encoding="utf-8",
    )
    _write_record(
        root,
        dist_info,
        tuple(path.relative_to(root).as_posix() for path in (*files, metadata_path, entrypoints_path)),
    )
    return (
        metadata.Distribution.at(dist_info),
        sources[0],
        sources[1],
        root,
    )


def _split_namespace_distributions(
    tmp_path: Path,
) -> tuple[
    dict[str, metadata.Distribution],
    dict[str, _WheelPluginSource],
    dict[str, Path],
]:
    distributions: dict[str, metadata.Distribution] = {}
    sources: dict[str, _WheelPluginSource] = {}
    roots: dict[str, Path] = {}
    for suffix in ("a", "b"):
        distribution_name = f"split-runtime-{suffix}"
        plugin_id = f"split.runtime.{suffix}"
        root = tmp_path / suffix
        roots[suffix] = root
        package = root / "split_namespace"
        package.mkdir(parents=True)
        declaration_path = f"split_namespace/{suffix}-declaration.json"
        entrypoint_value = f"split_namespace.provider_{suffix}:provider"
        source_expectation = ProviderSource(
            distribution=distribution_name,
            version="1.2.3",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=entrypoint_value,
            declaration_path=declaration_path,
            import_roots=("",),
        )
        descriptor = PluginDescriptor(
            schema_version="1",
            source=source_expectation,
            plugin_id=plugin_id,
            plugin_version="1.2.3",
            engine_api="1.0",
            task_handlers=(),
            commit_validators=(),
        )
        module_path = package / f"provider_{suffix}.py"
        lazy_import = (
            "        from split_namespace.external import VALUE\n        assert VALUE == 'external portion'\n"
            if suffix == "a"
            else ""
        )
        module_path.write_text(
            "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
            f"_DESCRIPTOR = PluginDescriptor.model_validate({descriptor.model_dump(mode='python')!r})\n"
            "class Provider:\n"
            "    def descriptor(self):\n"
            f"{lazy_import}"
            "        return _DESCRIPTOR\n"
            "    def contribute(self, _ports):\n"
            "        return PluginContribution.empty()\n"
            "provider = Provider()\n",
            encoding="utf-8",
        )
        extra_files: tuple[Path, ...] = ()
        if suffix == "b":
            external = package / "external.py"
            external.write_text("VALUE = 'external portion'\n", encoding="utf-8")
            extra_files = (external,)
        declaration = root / declaration_path
        declaration.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1",
                    "kind": "plugin",
                    "source": source_expectation.model_dump(mode="json"),
                    "descriptor": descriptor.model_dump(mode="json"),
                }
            )
        )
        dist_info = root / f"split_runtime_{suffix}-1.2.3.dist-info"
        dist_info.mkdir()
        metadata_path = dist_info / "METADATA"
        metadata_path.write_text(
            f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.2.3\n",
            encoding="utf-8",
        )
        entrypoints_path = dist_info / "entry_points.txt"
        entrypoints_path.write_text(
            f"[graph_engine.plugins]\n{plugin_id} = {entrypoint_value}\n",
            encoding="utf-8",
        )
        _write_record(
            root,
            dist_info,
            tuple(
                path.relative_to(root).as_posix()
                for path in (module_path, *extra_files, declaration, metadata_path, entrypoints_path)
            ),
        )
        distributions[distribution_name] = metadata.Distribution.at(dist_info)
        sources[suffix] = _WheelPluginSource(
            distribution=distribution_name,
            entrypoint_name=plugin_id,
            declaration_path=declaration_path,
        )
    return distributions, sources, roots


class _DistributionMap:
    def __init__(self, distributions: dict[str, metadata.Distribution]) -> None:
        self._distributions = distributions

    def distribution(self, name: str) -> metadata.Distribution:
        return self._distributions[name]


def _rollback_distribution(
    tmp_path: Path,
) -> tuple[metadata.Distribution, _WheelPluginSource, _WheelPluginSource, Path]:
    root = tmp_path / "site"
    package = root / "round3_rollback_pkg"
    package.mkdir(parents=True)
    package_init = package / "__init__.py"
    package_init.write_bytes(b"PACKAGE = 'authenticated'\n")
    nested_package = package / "nested"
    nested_package.mkdir()
    nested_init = nested_package / "__init__.py"
    nested_init.write_bytes(b"NESTED = 'authenticated'\n")
    nested_leaf = nested_package / "leaf.py"
    nested_leaf.write_bytes(b"VALUE = 'authenticated'\n")
    sources: dict[str, ProviderSource] = {}
    descriptors: dict[str, PluginDescriptor] = {}
    declaration_paths = {
        "rollback.sibling": "round3_rollback_pkg/sibling-declaration.json",
        "rollback.child": "round3_rollback_pkg/child-declaration.json",
    }
    entrypoint_values = {
        "rollback.sibling": "round3_rollback_pkg.sibling:provider",
        "rollback.child": "round3_rollback_pkg.child:provider",
    }
    for plugin_id in ("rollback.sibling", "rollback.child"):
        sources[plugin_id] = ProviderSource(
            distribution="rollback-runtime",
            version="1.2.3",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name=plugin_id,
            entrypoint_value=entrypoint_values[plugin_id],
            declaration_path=declaration_paths[plugin_id],
            import_roots=("",),
        )
        descriptors[plugin_id] = PluginDescriptor(
            schema_version="1",
            source=sources[plugin_id],
            plugin_id=plugin_id,
            plugin_version="1.2.3",
            engine_api="1.0",
            task_handlers=(),
            commit_validators=(),
        )
        declaration = root / declaration_paths[plugin_id]
        declaration.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1",
                    "kind": "plugin",
                    "source": sources[plugin_id].model_dump(mode="json"),
                    "descriptor": descriptors[plugin_id].model_dump(mode="json"),
                }
            )
        )

    sibling_module = package / "sibling.py"
    sibling_module.write_text(
        "import round3_rollback_pkg.nested\n"
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
        f"_DESCRIPTOR = PluginDescriptor.model_validate({descriptors['rollback.sibling'].model_dump(mode='python')!r})\n"
        "class Provider:\n"
        "    def descriptor(self):\n"
        "        return _DESCRIPTOR\n"
        "    def contribute(self, _ports):\n"
        "        return PluginContribution.empty()\n"
        "provider = Provider()\n",
        encoding="utf-8",
    )
    child_module = package / "child.py"
    child_module.write_text(
        "import sys\n"
        "import round3_import_control as _control\n"
        "import round3_rollback_pkg.nested.leaf\n"
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
        f"_CORRECT = PluginDescriptor.model_validate({descriptors['rollback.child'].model_dump(mode='python')!r})\n"
        "_DRIFTED = _CORRECT.model_copy(update={'engine_api': '>=2'})\n"
        "_control.seen.append(sys.modules[__name__])\n"
        "class Provider:\n"
        "    def descriptor(self):\n"
        "        return _CORRECT if _control.correct else _DRIFTED\n"
        "    def contribute(self, _ports):\n"
        "        return PluginContribution.empty()\n"
        "provider = Provider()\n",
        encoding="utf-8",
    )
    dist_info = root / "rollback_runtime-1.2.3.dist-info"
    dist_info.mkdir()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_text(
        "Metadata-Version: 2.1\nName: rollback-runtime\nVersion: 1.2.3\n",
        encoding="utf-8",
    )
    entrypoints_path = dist_info / "entry_points.txt"
    entrypoints_path.write_text(
        "[graph_engine.plugins]\n"
        "rollback.sibling = round3_rollback_pkg.sibling:provider\n"
        "rollback.child = round3_rollback_pkg.child:provider\n",
        encoding="utf-8",
    )
    files = (
        package_init,
        nested_init,
        nested_leaf,
        sibling_module,
        child_module,
        *(root / path for path in declaration_paths.values()),
        metadata_path,
        entrypoints_path,
    )
    _write_record(
        root,
        dist_info,
        tuple(path.relative_to(root).as_posix() for path in files),
    )
    return (
        metadata.Distribution.at(dist_info),
        _WheelPluginSource(
            distribution="rollback-runtime",
            entrypoint_name="rollback.sibling",
            declaration_path=declaration_paths["rollback.sibling"],
        ),
        _WheelPluginSource(
            distribution="rollback-runtime",
            entrypoint_name="rollback.child",
            declaration_path=declaration_paths["rollback.child"],
        ),
        root,
    )


def _select_distribution(monkeypatch: pytest.MonkeyPatch, distribution: metadata.Distribution) -> None:
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)


def _mock_authenticated_load(
    monkeypatch: pytest.MonkeyPatch,
    distribution: metadata.Distribution,
    provider: object,
    loaded: list[str] | None = None,
) -> None:
    def load(entrypoint: metadata.EntryPoint) -> object:
        if loaded is not None:
            loaded.append(entrypoint.name)
        monkeypatch.setattr(provider, "__module__", entrypoint.module, raising=False)
        return provider

    monkeypatch.setattr(metadata.EntryPoint, "load", load)


def test_wheel_snapshot_binds_distribution_and_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path, name="Toy_Runtime", version="1.2.3")
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(
        distribution="toy.runtime",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="toy.runtime",
    )

    snapshot = snapshot_wheel_source(source)

    assert snapshot.identity.kind == SourceKind.WHEEL_PLUGIN
    assert snapshot.identity.distribution == "toy-runtime"
    assert snapshot.identity.version == "1.2.3"
    assert snapshot.identity.entrypoint_group == "graph_engine.plugins"
    assert snapshot.identity.entrypoint_name == "toy.runtime"
    assert snapshot.identity.entrypoint_value == "toy_plugin:provider"
    assert snapshot.digest == snapshot_wheel_source(source).digest
    assert {item.path for item in snapshot.files} == {
        "toy_plugin/__init__.py",
        "toy_plugin/plugin-declaration.json",
        "toy_plugin/product-declaration.json",
        "Toy_Runtime-1.2.3.dist-info/METADATA",
        "Toy_Runtime-1.2.3.dist-info/RECORD",
        "Toy_Runtime-1.2.3.dist-info/entry_points.txt",
    }


@pytest.mark.parametrize("drift", ("schema", "source"))
def test_static_declaration_drift_rejects_before_provider_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    declaration_path = Path(distribution.locate_file("toy_plugin/plugin-declaration.json"))
    document = json.loads(declaration_path.read_bytes())
    if drift == "schema":
        document["schema_version"] = "2"
    else:
        document["source"]["entrypoint_name"] = "toy.other"
    declaration_path.write_bytes(canonical_json_bytes(document))
    _write_record(
        Path(distribution.locate_file("")),
        Path(distribution.locate_file("toy_runtime-1.2.3.dist-info")),
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    loaded: list[str] = []
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda entrypoint: loaded.append(entrypoint.name))

    with pytest.raises(SourceSnapshotError, match="declaration"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


@pytest.mark.parametrize("kind", (SourceKind.WHEEL_PRODUCT, SourceKind.WHEEL_PLUGIN))
def test_installed_wheel_identity_requires_complete_coordinates(tmp_path: Path, kind: SourceKind) -> None:
    with pytest.raises(ValueError, match="complete wheel coordinates"):
        SourceIdentity(kind=kind, root=tmp_path.resolve())


def test_editable_identity_allows_only_the_coordinate_free_transition(tmp_path: Path) -> None:
    transitional = SourceIdentity(kind=SourceKind.EDITABLE_PLUGIN, root=tmp_path.resolve())
    assert transitional.distribution is None
    with pytest.raises(ValueError, match="complete wheel coordinates"):
        SourceIdentity(
            kind=SourceKind.EDITABLE_PLUGIN,
            root=tmp_path.resolve(),
            distribution="toy-runtime",
        )
    with pytest.raises(ValueError, match="authenticated import roots"):
        SourceIdentity(
            kind=SourceKind.EDITABLE_PLUGIN,
            root=tmp_path.resolve(),
            distribution="toy-runtime",
            version="1.2.3",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="toy.runtime",
            entrypoint_value="toy_plugin:provider",
            declaration_path="toy_plugin/plugin-declaration.json",
        )
    with pytest.raises(ValueError, match="canonical relative POSIX"):
        SourceIdentity(
            kind=SourceKind.EDITABLE_PLUGIN,
            root=tmp_path.resolve(),
            distribution="toy-runtime",
            version="1.2.3",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="toy.runtime",
            entrypoint_value="toy_plugin:provider",
            declaration_path="toy_plugin/plugin-declaration.json",
            import_roots=("../src",),
        )


def test_installed_snapshot_hashes_actual_bytes_and_record_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    first = snapshot_wheel_source(source)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    record_path = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info/RECORD"))
    package_path.write_bytes(b"provider = object()\n# changed source\n")
    _write_record(
        Path(distribution.locate_file("")),
        Path(distribution.locate_file("toy_runtime-1.2.3.dist-info")),
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    second = snapshot_wheel_source(source)
    record_path.write_bytes(record_path.read_bytes() + b"\n")

    third = snapshot_wheel_source(source)

    assert first.digest != second.digest
    assert next(item for item in second.files if item.path == "toy_plugin/__init__.py").content == (
        package_path.read_bytes()
    )
    assert second.digest != third.digest
    assert next(item for item in third.files if item.path.endswith("/RECORD")).content.endswith(b"\n")


def test_installed_snapshot_rejects_record_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    Path(distribution.locate_file("toy_plugin/__init__.py")).write_bytes(b"mutated\n")

    with pytest.raises(SourceSnapshotError, match="RECORD hash mismatch"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_installed_snapshot_rejects_missing_record_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    Path(distribution.locate_file("toy_plugin/__init__.py")).unlink()

    with pytest.raises(SourceSnapshotError, match="missing RECORD file"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_installed_snapshot_rejects_record_listed_symlink_even_when_bytes_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    outside = tmp_path / "same-bytes.py"
    outside.write_bytes(package_path.read_bytes())
    package_path.unlink()
    package_path.symlink_to(outside)

    with pytest.raises(SourceSnapshotError, match="regular no-follow file"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


@pytest.mark.parametrize("replacement", ("regular", "symlink"))
def test_installed_snapshot_rejects_selected_file_replacement_before_open_without_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    outside = tmp_path / "replacement.py"
    outside.write_bytes(package_path.read_bytes())
    loaded: list[str] = []
    replaced = False

    def replace(phase: str, relative_path: str | None) -> None:
        nonlocal replaced
        if phase != "before_component_open" or relative_path != "toy_plugin/__init__.py" or replaced:
            return
        replaced = True
        package_path.unlink()
        if replacement == "regular":
            package_path.write_bytes(outside.read_bytes())
        else:
            package_path.symlink_to(outside)

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", replace)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="changed while opening|regular no-follow"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_selected_byte_mutation_during_read_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    loaded: list[str] = []
    mutated = False

    def mutate(phase: str, relative_path: str | None) -> None:
        nonlocal mutated
        if phase == "before_final_stat" and relative_path == "toy_plugin/__init__.py" and not mutated:
            mutated = True
            package_path.write_bytes(b"mutated while captured\n")

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", mutate)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="changed while it was read"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


@pytest.mark.parametrize("fault", ("unsafe", "duplicate", "malformed"))
def test_installed_snapshot_rejects_invalid_record_without_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    record_path = _record_path(distribution)
    if fault == "malformed":
        record_path.write_text("only,two-columns\n", encoding="utf-8")
    else:
        rows = _record_rows(distribution)
        rows.append(["../escape.py", "", ""] if fault == "unsafe" else list(rows[0]))
        _replace_record_rows(distribution, rows)
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="unsafe RECORD path|duplicate RECORD path|malformed"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_record_listed_fifo_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    package_path.unlink()
    os.mkfifo(package_path)
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="changed while opening"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_disappearance_before_open_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    loaded: list[str] = []
    removed = False

    def remove(phase: str, relative_path: str | None) -> None:
        nonlocal removed
        if phase == "before_component_open" and relative_path == "toy_plugin/__init__.py" and not removed:
            removed = True
            package_path.unlink()

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", remove)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="regular no-follow"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_directory_replacement_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    root = Path(distribution.locate_file(""))
    package = root / "toy_plugin"
    original_package = root / "toy_plugin-original"
    real_open = os.open
    replaced = False
    loaded: list[str] = []

    def replace_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "toy_plugin" and flags & getattr(os, "O_DIRECTORY", 0) and not replaced:
            replaced = True
            package.rename(original_package)
            package.mkdir()
            (package / "__init__.py").write_bytes((original_package / "__init__.py").read_bytes())
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(wheel_sources.os, "open", replace_then_open)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="directory changed while opening"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_root_replacement_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    root = Path(distribution.locate_file(""))
    original_root = root.with_name("site-original")
    loaded: list[str] = []
    replaced = False

    def replace_root(phase: str, _relative_path: str | None) -> None:
        nonlocal replaced
        if phase != "after_rescan" or replaced:
            return
        replaced = True
        root.rename(original_root)
        root.mkdir()

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", replace_root)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="root changed while it was captured"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


def test_installed_snapshot_rejects_record_mutation_during_rescan_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    record_path = _record_path(distribution)
    loaded: list[str] = []
    mutated = False

    def mutate_record(phase: str, _relative_path: str | None) -> None:
        nonlocal mutated
        if phase != "before_rescan" or mutated:
            return
        mutated = True
        record_path.write_bytes(record_path.read_bytes() + b"\n")

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", mutate_record)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name),
    )

    with pytest.raises(SourceSnapshotError, match="changed while it was captured"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))
    assert loaded == []


@pytest.mark.parametrize(
    "entrypoints",
    (
        (),
        (
            ("graph_engine.plugins", "toy.runtime", "toy_plugin:provider"),
            ("graph_engine.plugins", "toy.runtime", "toy_plugin:other"),
        ),
    ),
)
def test_wheel_snapshot_requires_one_exact_entrypoint_in_selected_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoints: tuple[tuple[str, str, str], ...],
) -> None:
    distribution = _installed_distribution(tmp_path, entrypoints=entrypoints)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **_kwargs: pytest.fail("global or foreign entry points must not be consulted"),
    )

    with pytest.raises(SourceSnapshotError, match="matching entry point"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_wheel_snapshot_rejects_distribution_returned_for_a_different_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path, name="foreign-runtime")
    _select_distribution(monkeypatch, distribution)

    with pytest.raises(SourceSnapshotError, match="distribution name mismatch"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


@pytest.mark.parametrize("omitted_name", ("METADATA", "entry_points.txt"))
def test_installed_snapshot_requires_identity_metadata_in_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted_name: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    rows = [row for row in _record_rows(distribution) if not row[0].endswith(f"/{omitted_name}")]
    _replace_record_rows(distribution, rows)

    with pytest.raises(SourceSnapshotError, match="authenticated metadata"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_installed_snapshot_rejects_frozen_entrypoint_target_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    root = Path(distribution.locate_file(""))
    dist_info = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info"))
    entrypoints_path = dist_info / "entry_points.txt"
    changed = False

    def switch_target(phase: str, relative_path: str | None) -> None:
        nonlocal changed
        if phase != "before_component_open" or not relative_path or not relative_path.endswith("/RECORD"):
            return
        if changed:
            return
        changed = True
        entrypoints_path.write_text(
            "[graph_engine.plugins]\ntoy.runtime = toy_plugin:other\n",
            encoding="utf-8",
        )
        _write_record(
            root,
            dist_info,
            (
                "toy_plugin/__init__.py",
                "toy_runtime-1.2.3.dist-info/METADATA",
                "toy_runtime-1.2.3.dist-info/entry_points.txt",
            ),
        )

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", switch_target)

    with pytest.raises(SourceSnapshotError, match="entry point metadata"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_installed_snapshot_rejects_frozen_metadata_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    root = Path(distribution.locate_file(""))
    dist_info = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info"))
    metadata_path = dist_info / "METADATA"
    changed = False

    def switch_version(phase: str, relative_path: str | None) -> None:
        nonlocal changed
        if phase != "before_component_open" or not relative_path or not relative_path.endswith("/RECORD"):
            return
        if changed:
            return
        changed = True
        metadata_path.write_text(
            "Metadata-Version: 2.1\nName: toy-runtime\nVersion: 9.9.9\n",
            encoding="utf-8",
        )
        _write_record(
            root,
            dist_info,
            (
                "toy_plugin/__init__.py",
                "toy_runtime-1.2.3.dist-info/METADATA",
                "toy_runtime-1.2.3.dist-info/entry_points.txt",
            ),
        )

    monkeypatch.setattr(wheel_sources, "_snapshot_boundary", switch_version)

    with pytest.raises(SourceSnapshotError, match="METADATA disagrees"):
        snapshot_wheel_source(WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime"))


def test_editable_snapshot_captures_exact_closed_tree_and_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path / "installed")
    _select_distribution(monkeypatch, distribution)
    root = tmp_path / "editable"
    root.mkdir()
    source_path = root / "plugin.py"
    source_path.write_bytes(b"VERSION = 'one'\n")
    source = EditableWheelPluginSource(
        distribution="toy-runtime",
        entrypoint_name="toy.runtime",
        source_root=root,
        source_files=("plugin.py",),
    )

    first = snapshot_wheel_source(source)
    source_path.write_bytes(b"VERSION = 'two'\n")
    second = snapshot_wheel_source(source)

    assert first.identity.kind == SourceKind.EDITABLE_PLUGIN
    assert first.identity.root == root.resolve()
    assert tuple(item.path for item in first.files) == ("plugin-declaration.json", "plugin.py")
    assert first.digest != second.digest

    (root / "undeclared.py").write_bytes(b"unexpected\n")
    with pytest.raises(SourceSnapshotError, match="declared source file set"):
        snapshot_wheel_source(source)


def test_editable_product_snapshot_captures_explicit_static_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(
        tmp_path / "installed",
        entrypoints=(("graph_engine.products", "toy.product", "toy_plugin:provider"),),
    )
    _select_distribution(monkeypatch, distribution)
    root = tmp_path / "editable"
    root.mkdir()
    (root / "product.py").write_bytes(b"VERSION = 'one'\n")
    source = EditableWheelProductSource(
        distribution="toy-runtime",
        entrypoint_name="toy.product",
        source_root=root,
        source_files=("product.py",),
    )

    snapshot = snapshot_wheel_source(source)

    assert snapshot.identity.kind == SourceKind.EDITABLE_PRODUCT
    assert snapshot.identity.product_id == "toy.product"
    assert snapshot.identity.root == root.resolve()
    assert tuple(item.path for item in snapshot.files) == (
        "product-declaration.json",
        "product.py",
    )


def test_editable_src_namespace_reexport_loads_and_repeats_through_public_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _installed_distribution(
        tmp_path / "metadata",
        name="editable-runtime",
        entrypoints=(
            (
                "graph_engine.plugins",
                "editable.runtime",
                "editable_namespace.bridge:provider",
            ),
        ),
    )
    _select_distribution(monkeypatch, distribution)
    project = tmp_path / "project"
    files = {
        "src/editable_namespace/bridge.py": (
            b"from editable_namespace.implementation.provider import provider\n"
        ),
        "src/editable_namespace/implementation/provider.py": b"provider = object()\n",
    }
    source_expectation = ProviderSource(
        distribution="editable-runtime",
        version="1.2.3",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="editable.runtime",
        entrypoint_value="editable_namespace.bridge:provider",
        declaration_path="plugin-declaration.json",
        import_roots=("src",),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=source_expectation,
        plugin_id="editable.runtime",
        plugin_version="1.2.3",
        engine_api="0.2",
        task_handlers=(),
        commit_validators=(),
    )
    files["plugin-declaration.json"] = canonical_json_bytes(
        {
            "schema_version": "1",
            "kind": "plugin",
            "source": source_expectation.model_dump(mode="json"),
            "descriptor": descriptor.model_dump(mode="json"),
        }
    )
    for relative_path, content in files.items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    source = _EditableWheelPluginSource(
        distribution="editable-runtime",
        entrypoint_name="editable.runtime",
        declaration_path="plugin-declaration.json",
        source_root=project,
        source_files=tuple(files),
    )
    provider = _PluginProvider("editable.runtime", version="1.2.3")
    provider._descriptor = descriptor
    provider.__module__ = "editable_namespace.implementation.provider"
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _entrypoint: provider)
    cache = wheel_sources._AuthenticatedBindingCache()

    try:
        snapshot = snapshot_wheel_source(source)
        first = wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )
        repeated = wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )

        assert repeated is first
        assert repeated._provider is provider
        assert snapshot.identity.import_roots == ("src",)
        assert "src.editable_namespace" not in sys.modules
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "editable_namespace" or module_name.startswith("editable_namespace."):
                sys.modules.pop(module_name, None)


def test_editable_snapshot_rejects_entrypoint_target_drift_before_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path / "installed")
    _select_distribution(monkeypatch, distribution)
    root = tmp_path / "editable"
    root.mkdir()
    (root / "toy_plugin.py").write_bytes(b"provider = object()\n")
    (root / "other_plugin.py").write_bytes(b"provider = object()\n")
    source = EditableWheelPluginSource(
        distribution="toy-runtime",
        entrypoint_name="toy.runtime",
        source_root=root,
        source_files=("toy_plugin.py", "other_plugin.py"),
    )
    snapshot_wheel_source(source)
    entrypoints_path = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info/entry_points.txt"))
    entrypoints_path.write_text(
        "[graph_engine.plugins]\ntoy.runtime = other_plugin:provider\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceSnapshotError, match="source|entry point"):
        snapshot_wheel_source(source)


def test_editable_load_rejects_a_different_explicit_file_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path / "installed")
    _select_distribution(monkeypatch, distribution)
    root = tmp_path / "editable"
    root.mkdir()
    (root / "a.py").write_bytes(b"A = 1\n")
    (root / "b.py").write_bytes(b"B = 1\n")
    snapshotted_source = EditableWheelPluginSource(
        distribution="toy-runtime",
        entrypoint_name="toy.runtime",
        source_root=root,
        source_files=("a.py", "b.py"),
    )
    requested_source = snapshotted_source.model_copy(update={"source_files": ("a.py",)})
    snapshot = snapshot_wheel_source(snapshotted_source)
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name) or _PluginProvider(),
    )

    with pytest.raises(SourceSnapshotError, match="file tuple"):
        load_snapshotted_entrypoint(requested_source, snapshot)
    assert loaded == []


def test_load_rejects_same_name_version_distribution_at_a_different_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshotted_distribution = _installed_distribution(tmp_path / "snapshotted")
    replacement_distribution = _installed_distribution(tmp_path / "replacement")
    selected = iter((snapshotted_distribution, replacement_distribution))
    monkeypatch.setattr(metadata, "distribution", lambda _name: next(selected))
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name) or _PluginProvider(),
    )

    with pytest.raises(SourceSnapshotError, match="changed after snapshot|declaration source"):
        load_snapshotted_entrypoint(source, snapshot)
    assert loaded == []


def test_load_rejects_post_snapshot_installed_byte_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    root = Path(distribution.locate_file(""))
    dist_info = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info"))
    Path(distribution.locate_file("toy_plugin/__init__.py")).write_bytes(b"changed after snapshot\n")
    _write_record(
        root,
        dist_info,
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name) or _PluginProvider(),
    )

    with pytest.raises(SourceSnapshotError, match="changed after snapshot|declaration source"):
        load_snapshotted_entrypoint(source, snapshot)
    assert loaded == []


def test_load_rejects_post_snapshot_entrypoint_target_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    root = Path(distribution.locate_file(""))
    dist_info = Path(distribution.locate_file("toy_runtime-1.2.3.dist-info"))
    (dist_info / "entry_points.txt").write_text(
        "[graph_engine.plugins]\ntoy.runtime = toy_plugin:other\n",
        encoding="utf-8",
    )
    _write_record(
        root,
        dist_info,
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.value) or _PluginProvider(),
    )

    with pytest.raises(SourceSnapshotError, match="changed after snapshot|declaration source"):
        load_snapshotted_entrypoint(source, snapshot)
    assert loaded == []


def test_load_rejects_preloaded_target_module_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    shadow = ModuleType("toy_plugin")
    shadow.__file__ = str(tmp_path / "shadow" / "toy_plugin.py")
    monkeypatch.setitem(sys.modules, "toy_plugin", shadow)
    loaded: list[str] = []
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda entrypoint: loaded.append(entrypoint.name) or _PluginProvider(),
    )

    with pytest.raises(SourceSnapshotError, match="platform-authenticated"):
        load_snapshotted_entrypoint(source, snapshot)
    assert loaded == []


def test_load_rejects_arbitrary_preloaded_module_at_authenticated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    provider = _PluginProvider()
    provider.__module__ = "toy_plugin"
    shadow = ModuleType("toy_plugin")
    shadow.__file__ = str(distribution.locate_file("toy_plugin/__init__.py"))
    shadow.provider = provider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "toy_plugin", shadow)

    with pytest.raises(SourceSnapshotError, match="platform-authenticated"):
        load_snapshotted_entrypoint(source, snapshot)


def test_load_rejects_preload_executed_from_changed_then_restored_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    module_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    original = module_path.read_bytes()
    provider = _PluginProvider()
    provider.__module__ = "toy_plugin"
    stale = ModuleType("toy_plugin")
    stale.__file__ = str(module_path)
    stale.injected = provider  # type: ignore[attr-defined]
    changed = b"provider = injected\n"
    module_path.write_bytes(changed)
    exec(compile(changed, str(module_path), "exec"), stale.__dict__)
    module_path.write_bytes(original)
    monkeypatch.setitem(sys.modules, "toy_plugin", stale)

    with pytest.raises(SourceSnapshotError, match="platform-authenticated"):
        load_snapshotted_entrypoint(source, snapshot)


def test_binding_quarantines_same_source_helper_before_entrypoint_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    root = Path(distribution.locate_file(""))
    package_init = Path(distribution.locate_file("toy_plugin/__init__.py"))
    helper_path = Path(distribution.locate_file("toy_plugin/helper.py"))
    package_init.write_bytes(b"from . import helper\nprovider = object()\n")
    helper_path.write_bytes(b"VALUE = 'authenticated'\n")
    _write_record(
        root,
        Path(distribution.locate_file("toy_runtime-1.2.3.dist-info")),
        (
            "toy_plugin/__init__.py",
            "toy_plugin/helper.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    provider = _PluginProvider()
    _mock_authenticated_load(monkeypatch, distribution, provider)
    fake_helper = ModuleType("toy_plugin.helper")
    fake_helper.__file__ = str(helper_path)
    monkeypatch.setitem(sys.modules, fake_helper.__name__, fake_helper)
    cache = wheel_sources._AuthenticatedBindingCache()

    try:
        binding = wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )

        authenticated_helper = sys.modules["toy_plugin.helper"]
        assert authenticated_helper is not fake_helper
        assert authenticated_helper.VALUE == "authenticated"  # type: ignore[attr-defined]
        assert binding._provider is provider
        assert cache.modules["toy_plugin.helper"].module is authenticated_helper
        assert {
            provenance.source_digest for provenance in cache.modules["toy_plugin.helper"].provenances
        } == {snapshot.digest}
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "toy_plugin" or module_name.startswith("toy_plugin."):
                sys.modules.pop(module_name, None)


def test_platform_loads_and_repeats_a_real_pep420_namespace_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution, source, sibling_source, root = _namespace_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    snapshot = snapshot_wheel_source(source)
    sibling_snapshot = snapshot_wheel_source(sibling_source)
    cache = wheel_sources._AuthenticatedBindingCache()

    try:
        first = wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )
        sibling = wheel_sources._load_snapshotted_entrypoint_binding(
            sibling_source,
            sibling_snapshot,
            binding_cache=cache,
        )
        repeated = wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )

        assert repeated is first
        assert sibling.descriptor().plugin_id == "namespace.sibling"
        shared_item = sibling.import_plan.module("round3_namespace.plugins.shared")
        assert {role.value for role in shared_item.roles} == {"authorized"}
        assert (
            cache.modules["round3_namespace.plugins.shared"].module
            is sys.modules["round3_namespace.plugins.shared"]
        )
        for module_name in ("round3_namespace", "round3_namespace.plugins"):
            namespace = sys.modules[module_name]
            assert getattr(namespace, "__file__", None) is None
            assert namespace.__spec__ is not None
            assert isinstance(namespace.__spec__.loader, NamespaceLoader)
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "round3_namespace" or module_name.startswith("round3_namespace."):
                sys.modules.pop(module_name, None)


@pytest.mark.parametrize("binding_order", (("a", "b"), ("b", "a")))
def test_split_distribution_namespace_keeps_external_standard_portion_without_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_order: tuple[str, str],
) -> None:
    distributions, sources, roots = _split_namespace_distributions(tmp_path)
    metadata_provider = _DistributionMap(distributions)
    for suffix in reversed(binding_order):
        monkeypatch.syspath_prepend(str(roots[suffix]))
    snapshots = {
        suffix: snapshot_wheel_source(source, metadata_provider) for suffix, source in sources.items()
    }
    cache = wheel_sources._AuthenticatedBindingCache()

    try:
        bindings = {
            suffix: wheel_sources._load_snapshotted_entrypoint_binding(
                sources[suffix],
                snapshots[suffix],
                metadata_provider,
                cache,
            )
            for suffix in binding_order
        }
        repeated = wheel_sources._load_snapshotted_entrypoint_binding(
            sources[binding_order[0]],
            snapshots[binding_order[0]],
            metadata_provider,
            cache,
        )

        assert repeated is bindings[binding_order[0]]
        namespace = sys.modules["split_namespace"]
        assert set(Path(path) for path in namespace.__path__) == {
            roots["a"] / "split_namespace",
            roots["b"] / "split_namespace",
        }
        assert "split_namespace.external" in sys.modules
        assert "split_namespace.external" not in cache.modules
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "split_namespace" or module_name.startswith("split_namespace."):
                sys.modules.pop(module_name, None)


def test_unowned_real_pep420_namespace_parent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution, source, _sibling_source, root = _namespace_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    snapshot = snapshot_wheel_source(source)

    try:
        namespace = importlib.import_module("round3_namespace")
        assert namespace.__file__ is None
        with pytest.raises(SourceSnapshotError, match="platform-authenticated"):
            load_snapshotted_entrypoint(source, snapshot)
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "round3_namespace" or module_name.startswith("round3_namespace."):
                sys.modules.pop(module_name, None)


def test_load_created_fake_namespace_parent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution, source, _sibling_source, root = _namespace_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    snapshot = snapshot_wheel_source(source)
    declaration = json.loads(
        next(
            source_file.content
            for source_file in snapshot.files
            if source_file.path == source.declaration_path
        )
    )
    descriptor = PluginDescriptor.model_validate(declaration["descriptor"])

    class Provider:
        __module__ = "round3_namespace.plugins.provider"

        def descriptor(self) -> PluginDescriptor:
            return descriptor

    provider = Provider()
    loaded: list[object] = []

    def synthetic_namespace(name: str, directory: Path) -> ModuleType:
        loader = NamespaceLoader(name, [str(directory)], PathFinder)
        spec = ModuleSpec(name, loader, origin=None, is_package=True)
        locations = [str(directory)]
        spec.submodule_search_locations = locations
        module = ModuleType(name)
        module.__spec__ = spec
        module.__loader__ = loader
        module.__path__ = locations
        module.__package__ = name
        return module

    def load(_entrypoint: metadata.EntryPoint) -> object:
        loaded.append(provider)
        top = synthetic_namespace("round3_namespace", distribution.locate_file("round3_namespace"))
        sys.modules[top.__name__] = top
        nested = synthetic_namespace(
            "round3_namespace.plugins",
            distribution.locate_file("round3_namespace/plugins"),
        )
        leaf = ModuleType("round3_namespace.plugins.provider")
        leaf.__file__ = str(distribution.locate_file("round3_namespace/plugins/provider.py"))
        sys.modules[nested.__name__] = nested
        sys.modules[leaf.__name__] = leaf
        return provider

    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    cache = wheel_sources._AuthenticatedBindingCache()

    with pytest.raises(SourceSnapshotError, match="planned standard"):
        wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )
    assert cache.modules == {}
    assert loaded == [provider]
    assert "round3_namespace" not in sys.modules


@pytest.mark.parametrize("prior_child", ("missing", "present"))
def test_failed_import_restores_existing_parent_child_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_child: str,
) -> None:
    distribution, sibling_source, child_source, root = _rollback_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    sibling_snapshot = snapshot_wheel_source(sibling_source)
    child_snapshot = snapshot_wheel_source(child_source)
    cache = wheel_sources._AuthenticatedBindingCache()
    control = ModuleType("round3_import_control")
    control.correct = False  # type: ignore[attr-defined]
    control.seen = []  # type: ignore[attr-defined]
    sys.modules[control.__name__] = control

    try:
        wheel_sources._load_snapshotted_entrypoint_binding(
            sibling_source,
            sibling_snapshot,
            binding_cache=cache,
        )
        parent = sys.modules["round3_rollback_pkg"]
        nested_parent = sys.modules["round3_rollback_pkg.nested"]
        sentinel = object()
        if prior_child == "present":
            parent.child = sentinel  # type: ignore[attr-defined]
            nested_parent.leaf = sentinel  # type: ignore[attr-defined]
        else:
            parent.__dict__.pop("child", None)
            nested_parent.__dict__.pop("leaf", None)

        with pytest.raises(SourceSnapshotError, match="static declaration"):
            wheel_sources._load_snapshotted_entrypoint_binding(
                child_source,
                child_snapshot,
                binding_cache=cache,
            )
        stale = control.seen[-1]  # type: ignore[attr-defined]
        assert "round3_rollback_pkg.child" not in sys.modules
        assert "round3_rollback_pkg.nested.leaf" not in sys.modules
        if prior_child == "present":
            assert parent.child is sentinel  # type: ignore[attr-defined]
            assert nested_parent.leaf is sentinel  # type: ignore[attr-defined]
        else:
            assert "child" not in parent.__dict__
            assert "leaf" not in nested_parent.__dict__

        control.correct = True  # type: ignore[attr-defined]
        corrected = wheel_sources._load_snapshotted_entrypoint_binding(
            child_source,
            child_snapshot,
            binding_cache=cache,
        )
        assert parent.child is sys.modules["round3_rollback_pkg.child"]  # type: ignore[attr-defined]
        assert parent.child is not stale  # type: ignore[attr-defined]
        assert corrected._provider is parent.child.provider  # type: ignore[attr-defined]
        assert nested_parent.leaf is sys.modules["round3_rollback_pkg.nested.leaf"]  # type: ignore[attr-defined]
    finally:
        sys.modules.pop(control.__name__, None)
        for module_name in tuple(sys.modules):
            if module_name == "round3_rollback_pkg" or module_name.startswith("round3_rollback_pkg."):
                sys.modules.pop(module_name, None)


def test_import_rollback_failure_is_typed_and_preserves_the_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution, sibling_source, child_source, root = _rollback_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    monkeypatch.syspath_prepend(str(root))
    cache = wheel_sources._AuthenticatedBindingCache()
    control = ModuleType("round3_import_control")
    control.correct = False  # type: ignore[attr-defined]
    control.seen = []  # type: ignore[attr-defined]
    sys.modules[control.__name__] = control

    try:
        wheel_sources._load_snapshotted_entrypoint_binding(
            sibling_source,
            snapshot_wheel_source(sibling_source),
            binding_cache=cache,
        )

        def fail_module_restore(_before: dict[str, ModuleType]) -> None:
            raise RuntimeError("simulated sys.modules rollback failure")

        monkeypatch.setattr(wheel_sources, "_restore_modules", fail_module_restore)
        with pytest.raises(SourceSnapshotError, match="rollback is indeterminate") as caught:
            wheel_sources._load_snapshotted_entrypoint_binding(
                child_source,
                snapshot_wheel_source(child_source),
                binding_cache=cache,
            )

        assert isinstance(caught.value.__cause__, SourceSnapshotError)
        assert "static declaration" in str(caught.value.__cause__)
        assert any(
            "simulated sys.modules rollback failure" in note
            for note in getattr(caught.value, "__notes__", ())
        )
    finally:
        sys.modules.pop(control.__name__, None)
        for module_name in tuple(sys.modules):
            if module_name == "round3_rollback_pkg" or module_name.startswith("round3_rollback_pkg."):
                sys.modules.pop(module_name, None)


def test_module_rollback_restores_a_stored_none_sentinel() -> None:
    module_name = "round5_none_sentinel"
    sys.modules[module_name] = None
    before = dict(sys.modules)
    del sys.modules[module_name]
    try:
        wheel_sources._restore_modules(before)  # type: ignore[arg-type]
        assert module_name in sys.modules
        assert sys.modules[module_name] is None
    finally:
        sys.modules.pop(module_name, None)


def test_import_transaction_none_sentinel_restores_parent_child_attribute() -> None:
    parent_name = "round5_none_parent"
    child_name = f"{parent_name}.child"
    parent = ModuleType(parent_name)
    parent.__path__ = []  # type: ignore[attr-defined]
    prior_child_attribute = object()
    parent.child = prior_child_attribute  # type: ignore[attr-defined]
    sys.modules[parent_name] = parent
    sys.modules[child_name] = None  # type: ignore[assignment]
    before = dict(sys.modules)
    attributes = wheel_sources._capture_parent_attributes(before)
    sys.modules.pop(child_name)
    parent.child = ModuleType(child_name)  # type: ignore[attr-defined]
    plan = wheel_sources.ImportProvenancePlan(
        source_digest="0" * 64,
        entrypoint_value="round5_none_parent:provider",
        modules=(),
    )

    try:
        wheel_sources._restore_import_transaction(
            before,
            attributes,
            plan,
            RuntimeError("primary"),
        )
        assert child_name in sys.modules
        assert sys.modules[child_name] is None
        assert parent.child is prior_child_attribute  # type: ignore[attr-defined]
    finally:
        sys.modules.pop(child_name, None)
        sys.modules.pop(parent_name, None)


def test_platform_cache_rejects_owned_module_from_a_different_source_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    first_snapshot = snapshot_wheel_source(source)
    provider = _PluginProvider()
    loaded: list[str] = []
    _mock_authenticated_load(monkeypatch, distribution, provider, loaded)
    cache = wheel_sources._AuthenticatedBindingCache()
    wheel_sources._load_snapshotted_entrypoint_binding(
        source,
        first_snapshot,
        binding_cache=cache,
    )

    root = Path(distribution.locate_file(""))
    package_path = Path(distribution.locate_file("toy_plugin/__init__.py"))
    package_path.write_bytes(package_path.read_bytes() + b"# authenticated drift\n")
    _write_record(
        root,
        Path(distribution.locate_file("toy_runtime-1.2.3.dist-info")),
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    second_snapshot = snapshot_wheel_source(source)
    assert second_snapshot.digest != first_snapshot.digest

    with pytest.raises(SourceSnapshotError, match="different authenticated source"):
        wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            second_snapshot,
            binding_cache=cache,
        )
    assert loaded == ["toy.runtime"]


def test_load_rejects_provider_module_from_foreign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    foreign = tmp_path / "foreign" / "toy_plugin.py"
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign provider\n")
    loaded: list[str] = []
    provider = _PluginProvider()
    provider.__module__ = "toy_plugin"

    def load(entrypoint: metadata.EntryPoint) -> object:
        loaded.append(entrypoint.name)
        module = ModuleType("toy_plugin")
        module.__file__ = str(foreign)
        monkeypatch.setitem(sys.modules, "toy_plugin", module)
        return provider

    monkeypatch.setattr(metadata.EntryPoint, "load", load)

    with pytest.raises(SourceSnapshotError, match="planned standard import"):
        load_snapshotted_entrypoint(source, snapshot)
    assert loaded == ["toy.runtime"]


def test_live_descriptor_module_replacement_is_rejected_before_cache_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    descriptor = _PluginProvider().descriptor()

    class ReplacingProvider:
        __module__ = "toy_plugin"

        def descriptor(self) -> PluginDescriptor:
            replacement = ModuleType("toy_plugin")
            replacement.__file__ = str(distribution.locate_file("toy_plugin/__init__.py"))
            sys.modules[replacement.__name__] = replacement
            return descriptor

    provider = ReplacingProvider()
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _entrypoint: provider)
    cache = wheel_sources._AuthenticatedBindingCache()

    with pytest.raises(SourceSnapshotError, match="planned standard"):
        wheel_sources._load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            binding_cache=cache,
        )

    assert cache.modules == {}
    assert cache.bindings == {}
    assert "toy_plugin" not in sys.modules


def test_provider_load_occurs_only_after_successful_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    loaded: list[str] = []

    provider_value = _PluginProvider()
    _mock_authenticated_load(monkeypatch, distribution, provider_value, loaded)
    Path(distribution.locate_file("toy_plugin/__init__.py")).write_bytes(b"corrupt\n")
    with pytest.raises(SourceSnapshotError, match="RECORD hash mismatch"):
        snapshot_wheel_source(source)
    assert loaded == []

    Path(distribution.locate_file("toy_plugin/__init__.py")).write_bytes(b"provider = object()\n")
    _write_record(
        Path(distribution.locate_file("")),
        Path(distribution.locate_file("toy_runtime-1.2.3.dist-info")),
        (
            "toy_plugin/__init__.py",
            "toy_runtime-1.2.3.dist-info/METADATA",
            "toy_runtime-1.2.3.dist-info/entry_points.txt",
        ),
    )
    snapshot = snapshot_wheel_source(source)
    provider = load_snapshotted_entrypoint(source, snapshot)

    assert provider.descriptor().plugin_id == "toy.runtime"  # type: ignore[union-attr]
    assert loaded == ["toy.runtime"]


@pytest.mark.parametrize(
    ("plugin_id", "version", "_message"),
    (
        ("toy.other", "1.2.3", "plugin id"),
        ("toy.runtime", "9.9.9", "plugin version"),
    ),
)
def test_loaded_plugin_descriptor_must_match_snapshotted_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_id: str,
    version: str,
    _message: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    _mock_authenticated_load(
        monkeypatch,
        distribution,
        _PluginProvider(plugin_id=plugin_id, version=version),
    )

    with pytest.raises(SourceSnapshotError, match="static declaration"):
        load_snapshotted_entrypoint(source, snapshot)


def test_source_kind_fixes_the_entrypoint_group() -> None:
    with pytest.raises(ValueError):
        WheelPluginSource(
            distribution="toy-runtime",
            entrypoint_group="graph_engine.products",  # type: ignore[arg-type]
            entrypoint_name="toy.runtime",
        )
    product = WheelProductSource(distribution="toy-product", entrypoint_name="toy.product")
    assert product.entrypoint_group == "graph_engine.products"


def test_loads_product_provider_from_its_exact_snapshotted_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(
        tmp_path,
        name="toy-product",
        entrypoints=(("graph_engine.products", "toy.product", "toy_plugin:provider"),),
    )
    _select_distribution(monkeypatch, distribution)
    source = WheelProductSource(distribution="toy-product", entrypoint_name="toy.product")
    snapshot = snapshot_wheel_source(source)
    _mock_authenticated_load(monkeypatch, distribution, _ProductProvider())

    provider = load_snapshotted_entrypoint(source, snapshot)

    assert provider.manifest().product_id == "toy.product"  # type: ignore[union-attr]
