"""Read the exact cross-wheel import exceptions enforced by import-linter."""

from __future__ import annotations

from configparser import ConfigParser
from functools import cache
from pathlib import Path


@cache
def _declared_exceptions() -> frozenset[tuple[str, str]]:
    config = ConfigParser()
    path = Path(__file__).resolve().parents[2] / ".importlinter"
    if not config.read(path):
        raise FileNotFoundError(path)
    pairs: set[tuple[str, str]] = set()
    for section in config.sections():
        for line in config.get(section, "ignore_imports", fallback="").splitlines():
            if not line.strip():
                continue
            source, separator, target = line.partition("->")
            if not separator or not source.strip() or not target.strip():
                raise ValueError(f"invalid import-linter exception: {line}")
            pairs.add((source.strip(), target.strip()))
    return frozenset(pairs)


def is_declared_cross_wheel_import(root: Path, path: Path, imported: str) -> bool:
    module = ".".join((root.name, *path.relative_to(root).with_suffix("").parts))
    return (module, imported) in _declared_exceptions()
