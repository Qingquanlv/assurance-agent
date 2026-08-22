from __future__ import annotations

from importlib.resources import files
from pathlib import PurePosixPath


def resource_bytes(relative_path: str) -> bytes:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be canonical and relative")
    return files("assurance_improvement").joinpath("resources", *path.parts).read_bytes()


def resource_text(relative_path: str) -> str:
    return resource_bytes(relative_path).decode("utf-8")
