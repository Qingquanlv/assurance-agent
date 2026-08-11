"""Snapshot-based changed-line detection for SUTs without git metadata.

``raw/changed-lines.json`` (A1 diff coverage, diff-scoped mutation) needs a diff
base, but the benchmark SUT ships no ``.git``. The runner snapshots the product
sources into ``.aa/cache/diff-base/`` after each passed api batch — and once
unconditionally to bootstrap the very first base; the next batch diffs the current
tree against that base with :mod:`difflib` (unified-diff semantics: added/updated
line numbers only, deletions mark none).

Fail-closed by construction: a missing, partial, or tampered base — and any IO
error while diffing — yields ``None``, and the runner writes no file rather than
fabricating a changed set. Binary files stay integrity-checked but do not enter
the line-oriented diff because they have no coverable source lines.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from assurance_agent.workflow.healing.safety import load_product_code_roots

logger = logging.getLogger(__name__)

DIFF_BASE_REL = Path(".aa") / "cache" / "diff-base"
_FILES_DIR = "files"
_INDEX_NAME = "index.json"
_FALLBACK_ROOTS = ["app"]


def has_diff_base(project_root: Path) -> bool:
    """Whether a diff base is already established (index present).

    Distinguishes *bootstrapping* a base from *advancing* one: the runner may only
    advance past a green batch, but it must bootstrap regardless of status, or a SUT
    whose api suite is never fully green never acquires a base and A1 stays
    permanently uncollectable. Integrity is not checked here — a corrupt base still
    reads as "established", and :func:`compute_changed_lines` fails it closed.
    """
    return (project_root / DIFF_BASE_REL / _INDEX_NAME).is_file()


def snapshot_product_tree(project_root: Path) -> None:
    """Repoint ``.aa/cache/diff-base/`` at the current product tree (best effort).

    Rebuilds into a fresh staging dir and swaps, so a reader never mistakes a
    half-written base for a complete one — a partial swap fails the integrity
    check in :func:`compute_changed_lines` and reads as "no base". Errors are
    logged, never raised: a failed snapshot must not fail a test run.
    """
    try:
        files = _product_files(project_root)
        cache_dir = project_root / DIFF_BASE_REL.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="diff-base-", dir=cache_dir))
        try:
            index: dict[str, str] = {}
            for rel, path in files.items():
                data = path.read_bytes()
                dest = staging / _FILES_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                index[rel] = hashlib.sha256(data).hexdigest()
            (staging / _INDEX_NAME).write_text(
                json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            base = project_root / DIFF_BASE_REL
            if base.exists():
                shutil.rmtree(base, ignore_errors=True)
            os.replace(staging, base)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except OSError as err:
        logger.warning("changed-lines: cannot snapshot product tree: %s", err)


def compute_changed_lines(project_root: Path) -> dict[str, list[int]] | None:
    """Diff current product files against the snapshot; ``None`` when unusable.

    Returns ``{repo-relative path: sorted added/updated 1-based line numbers}``
    covering modified and new files only — unchanged and deleted files contribute
    no entries. ``None`` (never an empty or partial map) when there is no
    integrity-valid base to diff against, so the collector's missing-file gap —
    not a fabricated zero — describes a first run.
    """
    base = project_root / DIFF_BASE_REL
    index_path = base / _INDEX_NAME
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("diff-base index root must be an object")
        snapshot_hashes = {str(rel): str(sha) for rel, sha in payload.items()}
    except (OSError, ValueError) as err:
        logger.warning("changed-lines: unreadable diff-base index: %s", err)
        return None

    snapshot_bytes: dict[str, bytes] = {}
    for rel, sha in sorted(snapshot_hashes.items()):
        data = _read_bytes(base / _FILES_DIR / rel, rel)
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            logger.warning("changed-lines: diff-base copy %r missing or failed integrity", rel)
            return None
        snapshot_bytes[rel] = data

    current = _product_files(project_root)
    changed: dict[str, list[int]] = {}
    for rel in sorted(current):
        try:
            data = current[rel].read_bytes()
        except OSError as err:
            logger.warning("changed-lines: cannot read %r: %s", rel, err)
            return None
        if snapshot_hashes.get(rel) == hashlib.sha256(data).hexdigest():
            continue
        lines = _decode_lines(data, rel)
        if lines is None:
            continue
        base_data = snapshot_bytes.get(rel)
        if base_data is None:
            if lines:
                changed[rel] = list(range(1, len(lines) + 1))
            continue
        base_lines = _decode_lines(base_data, rel)
        if base_lines is None:
            continue
        added = _added_lines(base_lines, lines)
        if added:
            changed[rel] = added
    return changed


def _product_files(project_root: Path) -> dict[str, Path]:
    """Repo-relative product source files, product roots from config (``app/`` fallback)."""
    roots = load_product_code_roots(project_root) or list(_FALLBACK_ROOTS)
    files: dict[str, Path] = {}
    for root in roots:
        root_path = project_root / root
        if not root_path.is_dir():
            continue
        for path in sorted(root_path.rglob("*")):
            if not path.is_file() or _is_junk(path):
                continue
            files[path.relative_to(project_root).as_posix()] = path
    return files


def _is_junk(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _read_bytes(path: Path, rel: str) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as err:
        logger.warning("changed-lines: cannot read snapshot copy %r: %s", rel, err)
        return None


def _decode_lines(data: bytes, rel: str) -> list[str] | None:
    if b"\x00" in data:
        logger.debug("changed-lines: excluding binary file %r (NUL byte)", rel)
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        logger.debug("changed-lines: excluding binary file %r (not UTF-8 text)", rel)
        return None


def _added_lines(base: list[str], current: list[str]) -> list[int]:
    """1-based line numbers the current file adds or replaces (unified-diff ``+`` lines)."""
    matcher = difflib.SequenceMatcher(a=base, b=current, autojunk=False)
    added: list[int] = []
    for tag, _a0, _a1, b0, b1 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(range(b0 + 1, b1 + 1))
    return added


__all__ = [
    "DIFF_BASE_REL",
    "compute_changed_lines",
    "has_diff_base",
    "snapshot_product_tree",
]
