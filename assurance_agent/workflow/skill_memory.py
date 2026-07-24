"""Load repo skill memory for prompt injection (spec C6b)."""

from __future__ import annotations

from pathlib import Path

_MEMORY_LIMIT_BYTES = 8 * 1024
_TRUNCATION_SUFFIX = "... [truncated]"


def load_skill_memory(project_root: Path, skill: str) -> str:
    """Return active memory rules for ``skill`` from ``<project_root>/.aa/memory/<skill>.md``.

    Filters ``- deprecated: `` lines, caps output at 8 KiB (line-aligned), and
    returns an empty string when no memory file exists.
    """
    if not skill.strip():
        return ""
    path = project_root / ".aa" / "memory" / f"{skill}.md"
    if not path.is_file():
        return ""
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    active_lines = [line for line in raw_lines if not line.startswith("- deprecated: ")]
    text = "\n".join(active_lines).strip()
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= _MEMORY_LIMIT_BYTES:
        return text
    kept: list[str] = []
    size = 0
    suffix_bytes = len(_TRUNCATION_SUFFIX.encode("utf-8"))
    budget = _MEMORY_LIMIT_BYTES - suffix_bytes
    for line in active_lines:
        chunk = (line + "\n").encode("utf-8")
        if size + len(chunk) > budget:
            break
        kept.append(line)
        size += len(chunk)
    trimmed = "\n".join(kept).rstrip()
    return f"{trimmed}\n{_TRUNCATION_SUFFIX}" if trimmed else _TRUNCATION_SUFFIX
