"""Sole access point for packaged runtime resources.

Kernel files live under assurance_kernel/_resources/; product files under the
installed product resource root (assurance_agent/_resources/ for the default
product). Never locate resources via __file__ arithmetic elsewhere in the
codebase; importlib.resources keeps this working from wheels and editable
installs alike.
"""

from pathlib import Path
from importlib.resources import files
from importlib.abc import Traversable

_KERNEL = files("assurance_kernel") / "_resources"
_product_root: Traversable | Path | None = None

_PRODUCT_PREFIXES = (
    ("schemas", "workflow-schema.yaml"),
    ("schemas", "execution-contracts.yaml"),
    ("skills",),
    ("opencode", "agents"),
)


def set_product_resource_root(root: Traversable | Path | None) -> None:
    global _product_root
    _product_root = root


def _is_product_rel(relpath: tuple[str, ...]) -> bool:
    for prefix in _PRODUCT_PREFIXES:
        if relpath[: len(prefix)] == prefix:
            return True
    return False


def _root_for(relpath: tuple[str, ...]) -> Traversable | Path:
    if _is_product_rel(relpath):
        if _product_root is not None:
            return _product_root
        return files("assurance_agent") / "_resources"
    return _KERNEL


def _node(*relpath: str) -> Traversable | Path:
    node: Traversable | Path = _root_for(relpath)
    for part in relpath:
        node = node / part
    return node


def read_text(*relpath: str) -> str:
    return _node(*relpath).read_text(encoding="utf-8")


def read_bytes(*relpath: str) -> bytes:
    return _node(*relpath).read_bytes()


def exists(*relpath: str) -> bool:
    node = _node(*relpath)
    return node.is_file() or node.is_dir()


def iter_children(*relpath: str) -> list[str]:
    return sorted(child.name for child in _node(*relpath).iterdir())
