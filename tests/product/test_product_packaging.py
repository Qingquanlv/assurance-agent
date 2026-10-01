from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
import subprocess
import zipfile

import pytest


class _EntryPointConfigParser(ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@dataclass(frozen=True)
class WheelMetadata:
    name: str
    entry_points: dict[str, dict[str, str]]
    requires_dist: tuple[str, ...]


def read_wheel_metadata(wheel: Path) -> WheelMetadata:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        message = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        entry_points: dict[str, dict[str, str]] = {}
        if entry_points_name is not None:
            parser = _EntryPointConfigParser(interpolation=None)
            parser.read_string(archive.read(entry_points_name).decode("utf-8"))
            entry_points = {section: dict(parser.items(section)) for section in parser.sections()}
        return WheelMetadata(
            name=str(message["Name"]),
            entry_points=entry_points,
            requires_dist=tuple(message.get_all("Requires-Dist") or ()),
        )


@pytest.fixture
def built_product_wheel(tmp_path: Path) -> Path:
    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "assurance-product",
            "--out-dir",
            str(tmp_path),
        ],
        check=True,
    )
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_product_metadata_exposes_only_the_opencode_product_entry_point(built_product_wheel):
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.name == "assurance-product"
    assert metadata.entry_points["graph_engine.products"] == {
        "assurance-opencode": "assurance_product.product:AssuranceOpenCodeProductProvider",
    }
    assert metadata.entry_points["console_scripts"] == {"aa": "assurance_product.cli:main"}
    assert "aa-next" not in metadata.entry_points["console_scripts"]
    assert "assurance-agent" not in metadata.requires_dist


@pytest.fixture
def built_engine_wheel(tmp_path: Path) -> Path:
    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "graph-engine",
            "--out-dir",
            str(tmp_path / "engine"),
        ],
        check=True,
    )
    wheels = tuple((tmp_path / "engine").glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheels_omit_whole_tree_modules_and_result_export_schema(
    built_engine_wheel: Path, built_product_wheel: Path
) -> None:
    with zipfile.ZipFile(built_engine_wheel) as archive:
        engine_names = archive.namelist()
    assert all("tree_io" not in Path(name).parts for name in engine_names)
    assert all(name != "graph_engine/workspace.py" for name in engine_names)
    assert all("result-export" not in name for name in engine_names)

    with zipfile.ZipFile(built_product_wheel) as archive:
        product_names = archive.namelist()
    assert all("result-export" not in name for name in product_names)
    assert all("tree_io" not in Path(name).parts for name in product_names)
    assert all(name != "graph_engine/workspace.py" for name in product_names)
    assert all("runtime_selection.py" not in name for name in product_names)
    assert "graph_engine.graphs" not in read_wheel_metadata(built_product_wheel).entry_points


def _capability_wheels() -> tuple[tuple[str, str, str], ...]:
    from assurance_product.feature_set import CAPABILITIES
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

    symbols = {ref.owner_id: ref.symbol for ref in FEATURE_GRAPH_FACTORIES}
    return tuple(
        (pin.distribution, pin.package, symbols[pin.owner_id].split(":", 1)[1]) for pin in CAPABILITIES
    )


def test_capability_wheels_ship_python_graphs_without_topology_yaml(tmp_path: Path) -> None:
    for package, module, factory in _capability_wheels():
        out_dir = tmp_path / package
        subprocess.run(
            ["uv", "build", "--package", package, "--out-dir", str(out_dir)],
            check=True,
        )
        built = tuple(out_dir.glob("*.whl"))
        assert len(built) == 1
        wheel = built[0]
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            source = archive.read(f"{module}/graphs/factory.py").decode("utf-8")
        factory_path = f"{module}/graphs/factory.py"
        yaml_path = f"{module}/resources/workflow/module.yaml"
        assert factory_path in names
        assert yaml_path not in names
        assert all("workflow/module.yaml" not in name for name in names)
        assert all("workflow/main.yaml" not in name for name in names)
        assert all("graph-inventory.yaml" not in name for name in names)
        assert all("runtime_selection.py" not in name for name in names)
        graph_py = [name for name in names if name.startswith(f"{module}/graphs/") and name.endswith(".py")]
        assert factory_path in graph_py
        assert any(name.endswith("/__init__.py") for name in graph_py)
        assert f"def {factory}" in source
        metadata = read_wheel_metadata(wheel)
        plugin_points = metadata.entry_points.get("graph_engine.plugins", {})
        assert plugin_points
        assert all("graphs.factory" not in value for value in plugin_points.values())
