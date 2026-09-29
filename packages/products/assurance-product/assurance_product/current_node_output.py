"""Read current worktree QA files for legacy runs without preserved node outputs.

These files are display context, never historical attempt evidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict


class CurrentNodeOutputError(ValueError):
    pass


class CurrentOutputItem(TypedDict):
    output_id: str
    kind: str
    logical_path: str


class CurrentOutputs(TypedDict):
    schema_version: str
    source: str
    node_id: str
    outputs: list[CurrentOutputItem]
    truncated: bool


class CurrentPreview(TypedDict):
    output: CurrentOutputItem
    preview: str | None
    truncated: bool


# Presentation-only compatibility paths. Managed runs use preserved attempt bytes instead.
_PATHS: dict[str, tuple[str, ...]] = {
    "intake.intake": ("qa/requirement.md", "qa/proposal.md", "qa/.qa.yaml", "qa/results/intake/**/*"),
    "intake.explore": ("qa/results/explore/**/*",),
    "intake.case-design": ("qa/cases/**/*.yaml", "qa/results/cases/**/*", "qa/results/trace/**/*"),
    "intake.case-review": (
        "qa/results/review/case-review*",
        "qa/cases/reviewed-case.json",
        "qa/cases/reviews/**/*",
    ),
    "quality.fact-baseline": ("qa/results/facts/**/*",),
    "quality.surface-baseline": ("qa/results/facts/**/*",),
    "generation.api.codegen": (
        "qa/results/codegen/api-generated-files.json",
        "qa/results/codegen/api-codegen-summary.md",
        "qa/tests/api/**/*.py",
    ),
    "generation.api.codegen-review": (
        "qa/results/review/api-codegen-review*",
        "qa/results/codegen/api/reviews/**/*",
    ),
    "quality.inspect": ("qa/results/inspect/**/*",),
    "quality.issue-analyze": ("qa/results/inspect/issue-analysis.json", "qa/results/issues/**/*"),
    "quality.report": ("qa/results/report/**/*",),
    "improvement.retro": (
        "qa/results/retro/retro.json",
        "qa/results/retro/status.json",
        "qa/results/retro/reconciliation.json",
        "qa/improvements/ledger.json",
    ),
    "improvement.retro-workflow-analysis": (
        "qa/results/retro/retro-workflow-analysis.json",
        "qa/results/retro/context.json",
    ),
    "improvement.retro-issue-analysis": (
        "qa/results/retro/retro-issue-analysis.json",
        "qa/results/retro/context.json",
    ),
    "improvement.retro-eval-analysis": (
        "qa/results/retro/retro-eval-analysis.json",
        "qa/results/retro/context.json",
    ),
}

_MAX_OUTPUTS = 80
_PREVIEW_BYTES = 64_000


def _semantic_node_id(node_id: str) -> str:
    return node_id.removesuffix("/finalize")


def _available_files(project_dir: Path, node_id: str) -> list[Path]:
    root = project_dir.resolve()
    if (root / "qa").is_symlink():
        return []
    qa_root = (root / "qa").resolve()
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in _PATHS.get(_semantic_node_id(node_id), ()):
        for candidate in sorted(root.glob(pattern)):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if qa_root not in resolved.parents:
                continue
            if resolved not in seen:
                paths.append(resolved)
                seen.add(resolved)
    return paths


def _item(root: Path, path: Path) -> CurrentOutputItem:
    logical_path = path.relative_to(root.resolve()).as_posix()
    return {
        "output_id": hashlib.sha256(logical_path.encode("utf-8")).hexdigest(),
        "kind": "artifact",
        "logical_path": logical_path,
    }


def list_current_node_outputs(project_dir: Path, node_id: str) -> CurrentOutputs:
    files = _available_files(project_dir, node_id)
    return {
        "schema_version": "1",
        "source": "current_worktree",
        "node_id": node_id,
        "outputs": [_item(project_dir, path) for path in files[:_MAX_OUTPUTS]],
        "truncated": len(files) > _MAX_OUTPUTS,
    }


def preview_current_node_output(project_dir: Path, node_id: str, output_id: str) -> CurrentPreview:
    for path in _available_files(project_dir, node_id)[:_MAX_OUTPUTS]:
        item = _item(project_dir, path)
        if item["output_id"] != output_id:
            continue
        with path.open("rb") as stream:
            content = stream.read(_PREVIEW_BYTES + 1)
        sample = content[:_PREVIEW_BYTES]
        return {
            "output": item,
            "preview": None if b"\0" in sample else sample.decode("utf-8", errors="replace"),
            "truncated": len(content) > _PREVIEW_BYTES,
        }
    raise CurrentNodeOutputError("current output is not available for this node")
