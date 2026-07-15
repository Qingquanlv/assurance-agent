"""Sync packaged skills / OpenCode assets into a target project.

Reads packaged resources only via assurance_agent.resources (no __file__ paths),
writes into <project>/skills/ and <project>/.opencode/. Content-hash based:
reports created / updated / unchanged so callers can print idempotent summaries.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from assurance_agent import resources
from assurance_agent.config import CONFIG_RELPATH

PLUGIN_ENTRY = "./.opencode/plugins/aa.mjs"


class SyncResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while True:
        if (cur / CONFIG_RELPATH).is_file() or (cur / "qa").is_dir():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _walk_resource_files(*rel: str) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)
        except (NotADirectoryError, ValueError):
            out.append(child)
        else:
            out.extend(_walk_resource_files(*child))
    return out


def _sync(
    resource_rel: tuple[str, ...],
    dest_root: Path,
    report_prefix: str,
    result: SyncResult,
    dry_run: bool,
) -> None:
    for parts in _walk_resource_files(*resource_rel):
        rel_under = Path(*parts[len(resource_rel):])
        report = (Path(report_prefix) / rel_under).as_posix()
        content = resources.read_text(*parts)
        dest = dest_root / rel_under
        if dest.is_file() and dest.read_text(encoding="utf-8") == content:
            result.unchanged.append(report)
            continue
        status_updated = dest.is_file()
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        (result.updated if status_updated else result.created).append(report)


def sync_skills(project_root: Path, dry_run: bool = False) -> SyncResult:
    result = SyncResult()
    _sync(("skills",), project_root / "skills", "skills", result, dry_run)
    return result


def sync_opencode(project_root: Path, dry_run: bool = False) -> SyncResult:
    result = SyncResult()
    for sub in ("agents", "tools", "plugins"):
        if resources.exists("opencode", sub):
            _sync(("opencode", sub), project_root / ".opencode" / sub, f".opencode/{sub}", result, dry_run)
    return result
