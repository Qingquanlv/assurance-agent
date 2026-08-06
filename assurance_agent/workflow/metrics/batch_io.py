"""Thin helpers shared by PR-cadence metric collectors (batch-scoped I/O)."""

from __future__ import annotations

from pathlib import Path

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.execution import ExecutionManifest
from assurance_agent.workflow.execution.evidence import atomic_write_bytes

EXECUTION_MANIFEST_REL = "execution/execution-manifest.yaml"
BATCH_RAW_REL = "raw"
FORBIDDEN_COVERAGE_BASENAMES = frozenset({".coverage"})


def batch_runs_dir(change_dir: Path, batch_id: str) -> Path:
    return change_dir / "execution" / "runs" / batch_id


def batch_raw_dir(change_dir: Path, batch_id: str) -> Path:
    return batch_runs_dir(change_dir, batch_id) / BATCH_RAW_REL


def resolve_batch_id(change_dir: Path, *, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    path = change_dir / EXECUTION_MANIFEST_REL
    if not path.is_file():
        raise FileNotFoundError(EXECUTION_MANIFEST_REL)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest = ExecutionManifest.model_validate(raw)
    return manifest.batch_id


def is_forbidden_coverage_sidecar(path: Path, *, allowed_raw_dir: Path) -> bool:
    """True when ``path`` is a freeze-excluded ``.coverage*`` outside batch raw/.

    Collectors may read coverage JSON already materialized under
    ``execution/runs/<batch>/raw/``; they must not open project-root
    ``.coverage`` / ``.coverage.*`` sidecars that the workspace freeze excludes.
    """
    name = path.name
    if name != ".coverage" and not name.startswith(".coverage."):
        return False
    try:
        path.resolve().relative_to(allowed_raw_dir.resolve())
    except ValueError:
        return True
    return False


def write_batch_evidence(change_dir: Path, batch_id: str, filename: str, model: object) -> Path:
    out = batch_runs_dir(change_dir, batch_id) / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(model))
    return out


__all__ = [
    "BATCH_RAW_REL",
    "EXECUTION_MANIFEST_REL",
    "FORBIDDEN_COVERAGE_BASENAMES",
    "batch_raw_dir",
    "batch_runs_dir",
    "is_forbidden_coverage_sidecar",
    "resolve_batch_id",
    "write_batch_evidence",
]
