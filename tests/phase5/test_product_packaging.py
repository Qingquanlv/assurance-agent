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


def test_product_metadata_exposes_only_two_product_entry_points(built_product_wheel):
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.name == "assurance-product"
    assert metadata.entry_points["graph_engine.products"] == {
        "assurance-opencode": "assurance_product.product:AssuranceOpenCodeProductProvider",
        "assurance-cursor": "assurance_product.product:AssuranceCursorProductProvider",
    }
    assert "assurance-agent" not in metadata.requires_dist
