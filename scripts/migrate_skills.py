#!/usr/bin/env python3
"""One-off dev tool: migrate aws-* skills and OpenCode agents from the TS repo
into assurance_agent/_resources/ with deterministic aa-* rewrites (spec 8/9).

NOT shipped in the wheel (lives in scripts/, excluded by pyproject). Run once to
seed _resources/; Task 2 (dashboard server.py) and the manual review pass are
applied on top and are the source of truth afterward. The default mode refuses
to overwrite a non-empty skills destination; `--force` is an explicit destructive
regeneration and requires the manual review pass to be repeated.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_SRC_ROOT = Path(os.environ.get("AA_TS_SOURCE", "../assurance-workflow-skills"))
DST_ROOT = Path(__file__).resolve().parent.parent / "assurance_agent" / "_resources"

# Files whose content we rewrite as text (everything else is copied verbatim).
TEXT_SUFFIXES = {".md", ".sh", ".dot", ".js", ".cjs", ".html", ".txt", ".yaml", ".yml", ".json"}
# Never copy these (junk or replaced-by-hand).
DROP_NAMES = {".DS_Store"}
DROP_RELPATHS = {"aws-dashboard/scripts/server.cjs"}  # replaced by server.py in Task 2


def rewrite_text(text: str) -> str:
    text = text.replace("Assurance Workflow Skills", "Assurance Agent")  # R1
    text = text.replace("AWS-HEALING-SKILLS-UNAVAILABLE", "AA-HEALING-SKILLS-UNAVAILABLE")  # R2
    text = text.replace("aws_cli_missing", "aa_cli_missing")
    text = text.replace("aws_cli_invalid", "aa_cli_invalid")
    text = text.replace("run_aws_run", "run_aa_run")
    text = re.sub(r"\baws-", "aa-", text)  # R3
    text = re.sub(r"\.aws/", ".aa/", text)  # R4
    text = re.sub(r"\.aws\b", ".aa", text)  # R5
    text = re.sub(r"\bAWS_", "AA_", text)  # R6
    text = re.sub(r"(?<![\w-])aws (?=[a-z])", "aa ", text)  # R7
    text = text.replace("`aws`", "`aa`")  # R7b
    text = re.sub(r"\baws\b", "aa", text)  # R7c
    text = re.sub(r"\bAWS\b", "AA", text)  # R8
    text = text.replace("npm run build && npm link", "uv sync")  # R9
    text = text.replace("npm install -g assurance-agent", "uv tool install assurance-agent")  # R10
    return text


def target_name(name: str) -> str:
    return "aa-" + name[len("aws-") :] if name.startswith("aws-") else name


def _migrate_tree(src_dir: Path, dst_dir: Path, drop_prefix: str) -> int:
    """Copy+rewrite one source tree into dst_dir; returns number of top-level entries."""
    count = 0
    for entry in sorted(src_dir.iterdir()):
        if not entry.is_dir():
            continue
        out_dir = dst_dir / target_name(entry.name)
        for src_file in sorted(entry.rglob("*")):
            if src_file.is_dir():
                continue
            if src_file.name in DROP_NAMES:
                continue
            rel_from_src_root = src_file.relative_to(src_dir).as_posix()
            if f"{drop_prefix}{rel_from_src_root}" in DROP_RELPATHS or rel_from_src_root in DROP_RELPATHS:
                continue
            rel = Path(*[target_name(p) for p in src_file.relative_to(entry).parts])
            dst_file = out_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if src_file.suffix in TEXT_SUFFIXES:
                dst_file.write_text(rewrite_text(src_file.read_text(encoding="utf-8")), encoding="utf-8")
            else:
                shutil.copyfile(src_file, dst_file)
        count += 1
    return count


def _migrate_agents(src_dir: Path, dst_dir: Path) -> int:
    count = 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(src_dir.iterdir()):
        if src_file.suffix != ".md" or not src_file.name.startswith("aws-"):
            continue
        dst_file = dst_dir / target_name(src_file.name)
        dst_file.write_text(rewrite_text(src_file.read_text(encoding="utf-8")), encoding="utf-8")
        count += 1
    return count


def migrate(src_root: Path = DEFAULT_SRC_ROOT, dst_root: Path = DST_ROOT, *, force: bool = False) -> int:
    skills_dst = dst_root / "skills"
    if skills_dst.is_dir() and any(skills_dst.iterdir()) and not force:
        raise RuntimeError(f"destination already contains reviewed skills: {skills_dst}; pass --force to replace")
    if not src_root.is_dir():
        raise FileNotFoundError(f"reference source repo not found: {src_root}")
    skills = _migrate_tree(src_root / "skills", skills_dst, drop_prefix="")
    agents = _migrate_agents(src_root / ".opencode" / "agents", dst_root / "opencode" / "agents")
    print(f"migrated {skills} skills, {agents} agents into {dst_root}")
    return skills


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SRC_ROOT)
    parser.add_argument("--destination", type=Path, default=DST_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        migrate(args.source, args.destination, force=args.force)
    except (FileNotFoundError, RuntimeError) as err:
        sys.exit(str(err))
