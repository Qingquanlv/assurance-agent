from __future__ import annotations

import hashlib
from pathlib import Path


def _unprovable_raw_admission(snapshot: object) -> bool:
    reference = getattr(snapshot, "activity_reference", None)
    if not isinstance(reference, dict) or not reference.get("session_id"):
        return False
    return reference.get("terminal_status") == "running"


def _snapshot_files(root: Path) -> dict[str, tuple[str, int, int, int, int, int, int]]:
    if not root.exists():
        return {}
    files: dict[str, tuple[str, int, int, int, int, int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            metadata = path.stat()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[path.relative_to(root).as_posix()] = (
                digest,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
                metadata.st_dev,
                metadata.st_ino,
            )
    return files


def _resolve_claim_paths(
    templates: tuple[str, ...],
    resolved_writes: tuple[str, ...],
) -> set[str]:
    resolved: set[str] = set()
    for template in templates:
        if "{" not in template:
            resolved.add(template)
            continue
        prefix, _, suffix = template.partition("{change_id}")
        for path in resolved_writes:
            if path.startswith(prefix) and path.endswith(suffix):
                middle = path[len(prefix) : len(path) - len(suffix) if suffix else len(path)]
                if middle and "/" not in middle:
                    resolved.add(path)
    return resolved


def _covered_by_claims(path: str, claims: set[str]) -> bool:
    return any(path == claim or path.startswith(f"{claim}/") for claim in claims)
