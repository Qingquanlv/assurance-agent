#!/usr/bin/env python3
"""Capture a golden eval fixture from an archived change.

Usage:
  python scripts/capture_eval_fixture.py \\
    --archive benchmark/vue-fastapi-admin/qa/archive/RET-api-management-20260716-192358-cursor \\
    --change-dir benchmark/vue-fastapi-admin/qa/changes/RET-api-management-20260716-192358-cursor \\
    --sut benchmark/vue-fastapi-admin \\
    --out benchmark/vue-fastapi-admin/eval-fixtures \\
    --sample-id eval-sample-001
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml

from assurance_agent.eval.fixtures import write_fixture_lock


CHANGE_RELS = (
    "proposal.md",
    "cases",
    "plans",
    "review",
    "facts",
    "explore",
    "codegen",
    "workflow-state.yaml",
    ".qa.yaml",
)

TEST_GLOBS = (
    "tests/api/**/*.py",
    "tests/e2e/**/*.py",
    "tests/conftest.py",
    "tests/__init__.py",
    "tests/config.py",
    "tests/schema_validation.py",
)


def _copy_tree(src: Path, dest: Path) -> None:
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    elif src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _write_tier(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def capture(
    *,
    archive: Path,
    change_dir: Path | None,
    sut: Path,
    out: Path,
    sample_id: str,
) -> None:
    sample = out / "samples" / sample_id
    if sample.exists():
        shutil.rmtree(sample)
    sample.mkdir(parents=True)

    source = change_dir if change_dir and change_dir.is_dir() else archive
    for rel in CHANGE_RELS:
        src = source / rel
        if not src.exists() and archive != source:
            src = archive / rel
        if src.exists():
            _copy_tree(src, sample / rel)

    # Prefer live change workflow-state if present; else archive.
    state_src = (change_dir / "workflow-state.yaml") if change_dir else None
    if state_src and state_src.exists():
        shutil.copy2(state_src, sample / "workflow-state.yaml")
    elif (archive / "workflow-state.yaml").exists():
        shutil.copy2(archive / "workflow-state.yaml", sample / "workflow-state.yaml")

    for pattern in TEST_GLOBS:
        for path in sut.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(sut).as_posix()
            dest = sample / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    tiers = out / "tiers"
    _write_tier(
        tiers / "L0-case-seed.yaml",
        {
            "name": "L0-case-seed",
            "description": "Case design complete; ready for planning",
            "source_prefix": f"samples/{sample_id}",
            "paths": [
                "proposal.md",
                "cases",
                "facts",
                "explore",
                "workflow-state.yaml",
                ".qa.yaml",
            ],
            "resets": {
                "workflow_state": {
                    "phases.case-design.status": "done",
                    "phases.case-review.status": "done",
                    "phases.api-plan.status": "pending",
                }
            },
        },
    )
    _write_tier(
        tiers / "L1-plan-seed.yaml",
        {
            "name": "L1-plan-seed",
            "extends": "L0-case-seed",
            "description": "Plans reviewed; ready for codegen",
            "paths": ["plans", "review"],
            "resets": {
                "workflow_state": {
                    "phases.api-plan.status": "done",
                    "phases.e2e-plan.status": "done",
                    "phases.api-codegen.status": "pending",
                }
            },
        },
    )
    _write_tier(
        tiers / "L2-api-codegen-seed.yaml",
        {
            "name": "L2-api-codegen-seed",
            "extends": "L1-plan-seed",
            "description": "API codegen done; ready for execution",
            "paths": [
                "codegen",
                "tests/config.py",
                "tests/conftest.py",
                "tests/schema_validation.py",
                "tests/api",
            ],
            "resets": {
                "workflow_state": {
                    "phases.api-codegen.status": "done",
                    "phases.execution.status": "pending",
                },
                "qa_yaml": {"test_types": ["api"]},
            },
        },
    )
    # Aliases expected by migrated datasets
    for alias, target in (
        ("L2-e2e-codegen-seed.yaml", "L2-api-codegen-seed"),
        ("L2-fuzz-codegen-seed.yaml", "L2-api-codegen-seed"),
        ("L2-performance-codegen-seed.yaml", "L2-api-codegen-seed"),
    ):
        _write_tier(
            tiers / alias,
            {
                "name": alias.replace(".yaml", ""),
                "extends": target,
                "description": f"Alias of {target} for suite wiring",
                "paths": [],
            },
        )
    _write_tier(
        tiers / "L3-run-seed.yaml",
        {
            "name": "L3-run-seed",
            "extends": "L2-api-codegen-seed",
            "description": "Full codegen ready; execution pending",
            "paths": ["tests/e2e"],
            "resets": {
                "workflow_state": {
                    "phases.e2e-codegen.status": "done",
                    "phases.execution.status": "pending",
                },
                "qa_yaml": {"test_types": ["api", "e2e"]},
            },
        },
    )
    fixture_dirs = sorted(path for path in (out / "samples").iterdir() if path.is_dir())
    write_fixture_lock(out, {path.name: f"samples/{path.name}" for path in fixture_dirs})
    print(f"captured sample={sample}")
    print(f"tiers={tiers}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--change-dir", type=Path, default=None)
    parser.add_argument("--sut", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sample-id", default="eval-sample-001")
    args = parser.parse_args()
    capture(
        archive=args.archive.resolve(),
        change_dir=args.change_dir.resolve() if args.change_dir else None,
        sut=args.sut.resolve(),
        out=args.out.resolve(),
        sample_id=args.sample_id,
    )


if __name__ == "__main__":
    main()
