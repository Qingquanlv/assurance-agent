"""Project-local mutation sample cache (§5-B1).

Cache key = module content digest + test tree digest + sampler version.
Replayable results live at ``.aa/cache/mutation/<digest>.json`` and are **not**
change authority — do not register them in the artifact registry.

Boundary: this module may read/write under ``<project>/.aa/cache/mutation/``
only. It must not import workflow or write change-scoped artifacts
(``inspect/metrics.json``, ``execution/runs/**``, etc.). Digests are computed
here so ``verification/`` stays free of evidence/workflow tree scanners.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from assurance_kernel.artifacts.canonical import canonical_json_bytes
from assurance_kernel.verification.mutation_sampling import (
    SAMPLER_VERSION,
    SelectedMutant,
)

MUTATION_CACHE_DIR_REL = ".aa/cache/mutation"
_TESTS_ROOT = "tests"


@dataclass(frozen=True)
class MutationCacheKey:
    module_digest: str
    test_tree_digest: str
    sampler_version: str = SAMPLER_VERSION


@dataclass(frozen=True)
class MutationCacheRecord:
    """Replayable sampling result for one cache key."""

    key: MutationCacheKey
    seed: int
    selected: tuple[SelectedMutant, ...]


def digest_module_contents(modules: Mapping[str, bytes]) -> str:
    """SHA-256 of the canonical ``{posix_path: content_sha256}`` map."""
    file_map = {path: hashlib.sha256(content).hexdigest() for path, content in sorted(modules.items())}
    return hashlib.sha256(canonical_json_bytes(file_map)).hexdigest()


def digest_test_tree(project_root: Path) -> str:
    """SHA-256 of the sorted ``tests/`` file map (``__pycache__`` / ``.pyc`` skipped)."""
    tests_root = project_root / _TESTS_ROOT
    file_map: dict[str, str] = {}
    if tests_root.is_dir():
        for path in sorted(tests_root.rglob("*")):
            if not path.is_file() or _is_ignored(path):
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            rel = path.relative_to(project_root).as_posix()
            file_map[rel] = hashlib.sha256(content).hexdigest()
    return hashlib.sha256(canonical_json_bytes(file_map)).hexdigest()


def cache_digest(key: MutationCacheKey) -> str:
    """Filename stem for ``.aa/cache/mutation/<digest>.json``."""
    payload = {
        "module_digest": key.module_digest,
        "sampler_version": key.sampler_version,
        "test_tree_digest": key.test_tree_digest,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def cache_path(project_root: Path, key: MutationCacheKey) -> Path:
    return project_root / MUTATION_CACHE_DIR_REL / f"{cache_digest(key)}.json"


def read_cache(project_root: Path, key: MutationCacheKey) -> MutationCacheRecord | None:
    """Return the stored record on hit; ``None`` on miss or corrupt payload."""
    path = cache_path(project_root, key)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        stored_key = MutationCacheKey(
            module_digest=str(raw["module_digest"]),
            test_tree_digest=str(raw["test_tree_digest"]),
            sampler_version=str(raw["sampler_version"]),
        )
        if stored_key != key:
            return None
        selected_raw = raw["selected"]
        if not isinstance(selected_raw, list):
            return None
        selected = tuple(
            SelectedMutant(
                module=str(item["module"]),
                line=int(item["line"]),
                operator=str(item["operator"]),
                mutant_id=str(item["mutant_id"]),
            )
            for item in selected_raw
        )
        return MutationCacheRecord(key=stored_key, seed=int(raw["seed"]), selected=selected)
    except (KeyError, TypeError, ValueError):
        return None


def write_cache(project_root: Path, record: MutationCacheRecord) -> Path:
    """Persist a replayable sampling record; returns the cache file path."""
    path = cache_path(project_root, record.key)
    payload = {
        "module_digest": record.key.module_digest,
        "test_tree_digest": record.key.test_tree_digest,
        "sampler_version": record.key.sampler_version,
        "seed": record.seed,
        "selected": [asdict(m) for m in record.selected],
    }
    _atomic_write_bytes(path, canonical_json_bytes(payload))
    return path


def _is_ignored(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Project-local atomic write (no workflow import)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


__all__ = [
    "MUTATION_CACHE_DIR_REL",
    "MutationCacheKey",
    "MutationCacheRecord",
    "cache_digest",
    "cache_path",
    "digest_module_contents",
    "digest_test_tree",
    "read_cache",
    "write_cache",
]
