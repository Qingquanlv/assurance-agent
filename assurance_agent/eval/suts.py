"""SUT registry for eval (`eval/suts.yaml`)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from assurance_agent.eval.paths import eval_root
from assurance_agent.exceptions import AaError


class SutEntry(BaseModel):
    local_dir: str
    pinned_rev: str | None = None
    repo: str | None = None


class SutRegistry(BaseModel):
    suts: dict[str, SutEntry] = Field(default_factory=dict)


def load_sut_registry(engine_root: Path) -> SutRegistry:
    path = eval_root(engine_root) / "suts.yaml"
    if not path.is_file():
        return SutRegistry()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise AaError(f"invalid suts.yaml: {path}")
    return SutRegistry.model_validate(raw)


def resolve_sut_dir(engine_root: Path, sut_name: str | None = None) -> Path:
    registry = load_sut_registry(engine_root)
    if not registry.suts:
        raise AaError(f"no SUTs registered in {eval_root(engine_root) / 'suts.yaml'}")
    if sut_name is None:
        if len(registry.suts) == 1:
            sut_name = next(iter(registry.suts))
        else:
            raise AaError("multiple SUTs registered; pass an explicit sut name")
    entry = registry.suts.get(sut_name)
    if entry is None:
        raise AaError(f"unknown SUT: {sut_name}")
    path = Path(entry.local_dir)
    if not path.is_absolute():
        path = (engine_root / path).resolve()
    return path
