"""Improvement-scoped memory marker helpers (apply / deprecate / extract).

Sandbox resolution for ``.aa/memory/`` targets lives here so Improvement delivery
owns its marker protocol without depending on deleted Retro lifecycle modules.
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.exceptions import AaError


def resolve_memory_target(sut_root: Path, target: str | None) -> Path:
    """Resolve a memory target, enforcing the ``.aa/memory/`` sandbox.

    Rejects empty targets, absolute paths, escapes outside ``.aa/memory/``
    (including ``..``), symlinks and non-regular files.
    """
    if not target or not target.strip():
        raise AaError("proposal target is empty (expected a path under .aa/memory/)")
    raw = sut_root / target
    if Path(target).is_absolute():
        raise AaError(f"unsafe memory target (absolute path): {target}")
    memory_root = (sut_root / ".aa" / "memory").resolve()
    resolved = raw.resolve()
    if resolved != memory_root and memory_root not in resolved.parents:
        raise AaError(f"unsafe memory target (outside .aa/memory/): {target}")
    if raw.is_symlink():
        raise AaError(f"unsafe memory target (symlink): {target}")
    if resolved.exists() and not resolved.is_file():
        raise AaError(f"unsafe memory target (not a regular file): {target}")
    return resolved


def improvement_marker(improvement_id: str) -> str:
    """Canonical Improvement memory marker token (without HTML comment wrappers)."""
    return f"improvement:{improvement_id}"


def apply_improvement_memory_patch(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
    proposed_change: str,
    evidence_ids: list[str] | tuple[str, ...],
    stage_dir: Path | None = None,
) -> bool:
    """Append an Improvement-scoped memory block, marker-idempotent.

    Marker form: ``<!-- improvement:<id> evidence:<sorted-source-ids> -->``.
    Live mode writes the real target; stage mode writes basename under ``stage_dir``
    without mutating the live file. Returns True when newly appended.
    """
    marker = improvement_marker(improvement_id)
    live_target = resolve_memory_target(sut_root, target)
    live_content = live_target.read_text(encoding="utf-8") if live_target.is_file() else ""

    if stage_dir is not None:
        out_path = stage_dir / live_target.name
        base = out_path.read_text(encoding="utf-8") if out_path.is_file() else live_content
    else:
        out_path = live_target
        base = live_content
    if marker in base:
        if stage_dir is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(base, encoding="utf-8")
        return False

    evidence = ",".join(sorted(evidence_ids))
    block = f"\n<!-- {marker} evidence:{evidence} -->\n- {proposed_change}\n<!-- /improvement -->\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(base + block, encoding="utf-8")
    return True


def deprecate_improvement_memory_block(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
) -> bool:
    """Deprecate the block written by :func:`apply_improvement_memory_patch`."""
    marker = improvement_marker(improvement_id)
    path = resolve_memory_target(sut_root, target)
    if not path.is_file():
        raise AaError(f"memory file not found: {target}")
    lines = path.read_text(encoding="utf-8").split("\n")
    start = next((i for i, line in enumerate(lines) if f"<!-- {marker}" in line), None)
    if start is None:
        raise AaError(f"applied block not found for {marker}")

    changed = False
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "<!-- /improvement -->":
            break
        if lines[i].startswith("- ") and not lines[i].startswith("- deprecated: "):
            lines[i] = f"- deprecated: {lines[i][2:]}"
            changed = True
    if changed:
        path.write_text("\n".join(lines), encoding="utf-8")
    return changed


def extract_improvement_block_bytes(
    sut_root: Path,
    *,
    improvement_id: str,
    target: str,
) -> bytes:
    """Return the exact applied Improvement block bytes (including markers)."""
    marker = improvement_marker(improvement_id)
    path = resolve_memory_target(sut_root, target)
    if not path.is_file():
        raise AaError(f"memory file not found: {target}")
    text = path.read_text(encoding="utf-8")
    start = text.find(f"<!-- {marker}")
    if start < 0:
        raise AaError(f"applied block not found for {marker}")
    end_token = "<!-- /improvement -->"
    end = text.find(end_token, start)
    if end < 0:
        raise AaError(f"applied block close marker missing for {marker}")
    return text[start : end + len(end_token)].encode("utf-8")
