"""Sole access point for packaged runtime resources under assurance_agent/_resources/.

Never locate resources via __file__ arithmetic elsewhere in the codebase;
importlib.resources keeps this working from wheels and editable installs alike.
"""
from importlib.resources import files
from importlib.abc import Traversable


def _root() -> Traversable:
    return files("assurance_agent") / "_resources"


def _node(*relpath: str) -> Traversable:
    node = _root()
    for part in relpath:
        node = node / part
    return node


def read_text(*relpath: str) -> str:
    return _node(*relpath).read_text(encoding="utf-8")


def exists(*relpath: str) -> bool:
    node = _node(*relpath)
    return node.is_file() or node.is_dir()


def iter_children(*relpath: str) -> list[str]:
    return sorted(child.name for child in _node(*relpath).iterdir())
