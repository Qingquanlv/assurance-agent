"""Deterministic SHA-256 hashes over test and product file trees."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TreeHash:
    aggregate: str
    files: dict[str, str]


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _should_skip(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _hash_roots(project_root: Path, roots: tuple[str, ...] | list[str]) -> TreeHash:
    files: dict[str, str] = {}
    for root in roots:
        root_path = project_root / root
        if not root_path.is_dir():
            continue
        for file_path in sorted(root_path.rglob("*")):
            if not file_path.is_file() or _should_skip(file_path):
                continue
            rel = file_path.relative_to(project_root).as_posix()
            digest = sha256_file(file_path)
            if digest is not None:
                files[rel] = digest
    aggregate = _sha256_text("\n".join(f"{k}:{v}" for k, v in sorted(files.items())))
    return TreeHash(aggregate=aggregate, files=files)


def hash_test_tree(project_root: Path, roots: tuple[str, ...] = ("tests",)) -> TreeHash:
    return _hash_roots(project_root, roots)


def hash_product_tree(project_root: Path, roots: list[str]) -> TreeHash:
    return _hash_roots(project_root, roots)


def diff_trees(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    changed: list[str] = []
    all_keys = sorted(set(baseline) | set(current))
    for key in all_keys:
        if baseline.get(key) != current.get(key):
            changed.append(key)
    return changed
