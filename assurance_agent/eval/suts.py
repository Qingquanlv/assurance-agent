"""SUT registry for eval (`eval/suts.yaml`)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.eval.paths import eval_root
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.tree_hash import hash_product_tree, hash_test_tree


class SutEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_dir: str
    source_archive_id: str = Field(min_length=1)
    tests_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_roots: list[str] = Field(min_length=1)
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
    try:
        return SutRegistry.model_validate(raw)
    except ValidationError as err:
        raise AaError(f"invalid suts.yaml {path}: {err}") from err


def _validate_sut_pin(path: Path, entry: SutEntry) -> None:
    if not path.is_dir():
        raise AaError(f"SUT directory not found: {path}")
    tests_hash = hash_test_tree(path).aggregate
    if tests_hash != entry.tests_tree_sha256:
        raise AaError(
            f"SUT tests tree hash mismatch for {entry.source_archive_id}: "
            f"expected {entry.tests_tree_sha256}, got {tests_hash}"
        )
    product_hash = hash_product_tree(path, entry.product_roots).aggregate
    if product_hash != entry.product_tree_sha256:
        raise AaError(
            f"SUT product tree hash mismatch for {entry.source_archive_id}: "
            f"expected {entry.product_tree_sha256}, got {product_hash}"
        )


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
    _validate_sut_pin(path, entry)
    return path
