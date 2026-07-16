import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from assurance_agent.eval.metrics import read_metrics
from assurance_agent.eval.paths import run_dir as run_dir_for
from assurance_agent.eval.types import RunManifest
from assurance_agent.exceptions import AaError


class BaselineSuiteEntry(BaseModel):
    run_id: str
    suite_version: str = "1"
    approved_at: str
    approved_by: str
    metrics: dict[str, float]


BaselineFile = dict[str, BaselineSuiteEntry]


def read_run_manifest(run_dir: Path) -> RunManifest:
    try:
        return RunManifest.model_validate_json((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise AaError(f"invalid eval run manifest: {run_dir}: {err}") from err


def read_baseline(project_root: Path, name: str = "main") -> BaselineFile:
    if re.fullmatch(r"[A-Za-z0-9._-]+", name) is None:
        raise AaError(f"unsafe baseline name: {name!r}")
    path = project_root / "eval/baselines" / f"{name}.json"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("baseline root must be an object")
    except (OSError, ValueError) as err:
        raise AaError(f"invalid baseline {path}: {err}") from err
    try:
        return {key: BaselineSuiteEntry.model_validate(value) for key, value in raw.items()}
    except ValidationError as err:
        raise AaError(f"invalid baseline {path}: {err}") from err


def compare_with_baseline(run_dir: Path, baseline_metrics: dict[str, float]) -> dict[str, float]:
    current = read_metrics(run_dir).metrics
    return {
        name: current[name] - baseline_metrics[name]
        for name in sorted(current.keys() & baseline_metrics.keys())
    }


def update_baseline(
    engine_root: Path,
    *,
    suite_name: str,
    run_id: str,
    approved_by: str,
    sut_root: Path | None = None,
) -> Path:
    """Promote a run's metrics into engine-side ``eval/baselines/main.json``.

    Run artifacts are read from ``sut_root`` (defaults to ``engine_root`` for
    backward-compatible single-root layouts).
    """
    out_root = sut_root or engine_root
    run_dir = run_dir_for(out_root, run_id)
    manifest = read_run_manifest(run_dir)
    if manifest.suite != suite_name:
        raise AaError(f"run suite {manifest.suite!r} does not match {suite_name!r}")
    baseline = read_baseline(engine_root)
    baseline[suite_name] = BaselineSuiteEntry(
        run_id=run_id,
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_by=approved_by,
        metrics=read_metrics(run_dir).metrics,
    )
    path = engine_root / "eval/baselines/main.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({k: v.model_dump(mode="json") for k, v in baseline.items()}, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
    return path
