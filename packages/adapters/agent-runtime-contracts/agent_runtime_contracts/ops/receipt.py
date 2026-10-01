"""The default Agent result: a receipt naming the workspace files the run wrote."""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import Field, field_validator

from graph_engine.plugin_api import FrozenModel


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


class ArtifactListResultV1(FrozenModel):
    output_files: tuple[str, ...] = Field(min_length=1)

    @field_validator("output_files")
    @classmethod
    def _output_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("artifact path must be a non-empty string")
        paths = tuple(sorted(set(cleaned)))
        if not all(_canonical_relative(path) for path in paths):
            raise ValueError("artifact path must be canonical and relative")
        return paths


__all__ = ["ArtifactListResultV1"]
