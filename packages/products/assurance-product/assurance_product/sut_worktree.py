"""Create the SUT git worktree before an aa run writes qa/."""

from __future__ import annotations

import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

from assurance_product.opencode_agents import _opencode_config


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _sut_git_root(project_dir: Path) -> Path:
    completed = _run_git(project_dir, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        raise ValueError(f"SUT is not a git repository: {project_dir}")
    root = Path(completed.stdout.strip()).resolve()
    if root != project_dir.resolve():
        raise ValueError(
            f"SUT git root is {root}, not {project_dir.resolve()}; refuse to worktree the parent repo"
        )
    return root


def _primary_checkout(project_dir: Path) -> Path:
    listed = _run_git(project_dir, "worktree", "list", "--porcelain")
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            if line.startswith("worktree "):
                return Path(line[len("worktree ") :]).resolve()
    return Path(project_dir).resolve()


def _worktree_home(source: Path, home: Path | None) -> Path:
    if home is not None:
        return home.resolve()
    env = os.environ.get("AA_SUT_WORKTREE_HOME", "").strip()
    if env:
        return Path(env).resolve()
    return (source.parent / ".worktrees").resolve()


def _seed_worktree_runtime(source: Path, dest: Path) -> None:
    config = dest / "opencode.json"
    config.write_text(_opencode_config(), encoding="utf-8")
    plugin = dest / ".opencode" / "plugins" / "assurance-boundary.mjs"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_bytes(
        files("assurance_product").joinpath("resources", "opencode", "assurance-boundary.mjs").read_bytes()
    )
    source_migrations = source / "migrations"
    dest_migrations = dest / "migrations"
    if source_migrations.is_dir() and not dest_migrations.exists():
        shutil.copytree(source_migrations, dest_migrations)


def ensure_run_worktree(
    project_dir: Path,
    change_id: str,
    *,
    home: Path | None = None,
) -> Path:
    """Checkout a fresh linked worktree for this change, or reuse one already added."""
    root = Path(project_dir)
    completed = _run_git(root, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        return root.resolve()
    checkout = _sut_git_root(root)
    source = _primary_checkout(checkout)
    dest = (_worktree_home(source, home) / source.name / change_id).resolve()
    if checkout == dest or dest == source:
        return dest
    if source in dest.parents:
        raise ValueError(f"worktree destination must be outside the SUT checkout: {dest}")
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    added = _run_git(source, "worktree", "add", "-b", f"bench/{change_id}", str(dest))
    if added.returncode != 0:
        raise ValueError(f"SUT worktree add failed: {added.stderr.strip() or added.stdout.strip()}")
    _seed_worktree_runtime(source, dest)
    return dest
