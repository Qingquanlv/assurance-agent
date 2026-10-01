from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import PurePosixPath

_package = __package__
if _package is None:
    raise RuntimeError("resource loader must be imported as a package module")
_PACKAGE_FILES = files(sys.modules[_package])


def resource_bytes(relative_path: str) -> bytes:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be canonical and relative")
    base = _PACKAGE_FILES if path.parts[:1] == ("ops",) else _PACKAGE_FILES.joinpath("resources")
    content = base.joinpath(*path.parts).read_bytes()
    return content.rstrip(b"\n") if path.suffix == ".json" else content


def resource_text(relative_path: str) -> str:
    return resource_bytes(relative_path).decode("utf-8")
