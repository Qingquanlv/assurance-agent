from __future__ import annotations

import base64
import csv
import hashlib
from importlib import metadata
from pathlib import Path

import pytest

import graph_engine.composition.sources as wheel_sources
from graph_engine.composition import (
    EditableWheelPluginSource,
    SourceKind,
    SourceSnapshotError,
    WheelPluginSource,
    WheelProductSource,
    load_snapshotted_entrypoint,
    snapshot_wheel_source,
)
from graph_engine.plugin_api import PluginContribution, PluginDescriptor, PluginRuntime
from graph_engine.graph.schema import WorkflowDef
from graph_engine.product import PluginRequirement, ProductManifest


class _PluginProvider:
    def __init__(self, plugin_id: str = "toy.runtime", version: str = "1.2.3") -> None:
        self._descriptor = PluginDescriptor(
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

    def bind(self, _ports: object) -> PluginRuntime:
        return PluginRuntime(task_handlers={}, commit_validators={})


class _ProductProvider:
    def manifest(self) -> ProductManifest:
        return ProductManifest(
            product_id="toy.product",
            product_version="1.2.3",
            engine_api="0.2",
            plugins=(PluginRequirement(plugin_id="toy.runtime", version="1.2.3"),),
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


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _write_record(root: Path, dist_info: Path, relative_paths: tuple[str, ...]) -> None:
    record_path = dist_info / "RECORD"
    with record_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        for relative_path in relative_paths:
            content = (root / relative_path).read_bytes()
            writer.writerow((relative_path, _record_hash(content), len(content)))
        writer.writerow((record_path.relative_to(root).as_posix(), "", ""))


def _installed_distribution(
    tmp_path: Path,
    *,
    name: str = "toy-runtime",
    version: str = "1.2.3",
    entrypoints: tuple[tuple[str, str, str], ...] = (
        ("graph_engine.plugins", "toy.runtime", "toy_plugin:provider"),
    ),
) -> metadata.Distribution:
    root = tmp_path / "site"
    package = root / "toy_plugin"
    package.mkdir(parents=True)
    package_init = package / "__init__.py"
    package_init.write_text("provider = object()\n", encoding="utf-8")

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
            metadata_path.relative_to(root).as_posix(),
            entrypoints_path.relative_to(root).as_posix(),
        ),
    )
    return metadata.Distribution.at(dist_info)


def _select_distribution(monkeypatch: pytest.MonkeyPatch, distribution: metadata.Distribution) -> None:
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)


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
    assert snapshot.digest == snapshot_wheel_source(source).digest
    assert {item.path for item in snapshot.files} == {
        "toy_plugin/__init__.py",
        "Toy_Runtime-1.2.3.dist-info/METADATA",
        "Toy_Runtime-1.2.3.dist-info/RECORD",
        "Toy_Runtime-1.2.3.dist-info/entry_points.txt",
    }


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
    assert tuple(item.path for item in first.files) == ("plugin.py",)
    assert first.digest != second.digest

    (root / "undeclared.py").write_bytes(b"unexpected\n")
    with pytest.raises(SourceSnapshotError, match="declared source file set"):
        snapshot_wheel_source(source)


def test_provider_load_occurs_only_after_successful_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    loaded: list[str] = []

    def load(entrypoint: metadata.EntryPoint) -> object:
        loaded.append(entrypoint.name)
        return _PluginProvider()

    monkeypatch.setattr(metadata.EntryPoint, "load", load)
    Path(distribution.locate_file("toy_plugin/__init__.py")).write_bytes(b"corrupt\n")
    with pytest.raises(SourceSnapshotError, match="RECORD hash mismatch"):
        snapshot_wheel_source(source)
    assert loaded == []

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
    ("plugin_id", "version", "message"),
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
    message: str,
) -> None:
    distribution = _installed_distribution(tmp_path)
    _select_distribution(monkeypatch, distribution)
    source = WheelPluginSource(distribution="toy-runtime", entrypoint_name="toy.runtime")
    snapshot = snapshot_wheel_source(source)
    monkeypatch.setattr(
        metadata.EntryPoint,
        "load",
        lambda _entrypoint: _PluginProvider(plugin_id=plugin_id, version=version),
    )

    with pytest.raises(SourceSnapshotError, match=message):
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
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _entrypoint: _ProductProvider())

    provider = load_snapshotted_entrypoint(source, snapshot)

    assert provider.manifest().product_id == "toy.product"  # type: ignore[union-attr]
