"""Project-local append-only performance baseline history (§5-A5).

Stores recent verified batch observations (p95 / error_rate + source digest)
under ``.aa/baseline/performance/``. Receipts are immutable and are **not**
change authority — do not register them in the artifact registry.

Boundary: this module may read/write under
``<project>/.aa/baseline/performance/`` only. It must not import workflow or
write change-scoped artifacts (``inspect/metrics.json``, ``execution/runs/**``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes

BASELINE_HISTORY_DIR_REL = ".aa/baseline/performance"
DEFAULT_BASELINE_WINDOW = 5
DEFAULT_MIN_BASELINE_SAMPLES = 3
_HISTORY_JSONL = "history.jsonl"
_RECEIPTS_DIR = "receipts"


@dataclass(frozen=True)
class PerformanceBaselineObservation:
    """One verified scenario observation from a completed performance batch."""

    capability: str
    endpoint: str
    change_id: str
    batch_id: str
    p95_ms: float
    error_rate: float
    source_digest: str


def scenario_identity(capability: str, endpoint: str) -> str:
    """Stable scenario key: capability + endpoint."""
    return f"{capability}\0{endpoint}"


def identity_digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def receipt_digest(observation: PerformanceBaselineObservation) -> str:
    """Content digest for the immutable receipt filename."""
    payload = {
        "batch_id": observation.batch_id,
        "capability": observation.capability,
        "change_id": observation.change_id,
        "endpoint": observation.endpoint,
        "error_rate": observation.error_rate,
        "p95_ms": observation.p95_ms,
        "source_digest": observation.source_digest,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def receipt_path(project_root: Path, observation: PerformanceBaselineObservation) -> Path:
    """Immutable receipt path keyed by scenario identity + change/batch.

    Same change/batch/scenario always maps to the same path so a conflicting
    rewrite can fail closed without scanning.
    """
    key = identity_digest(scenario_identity(observation.capability, observation.endpoint))
    name = f"{observation.change_id}__{observation.batch_id}.json"
    return project_root / BASELINE_HISTORY_DIR_REL / _RECEIPTS_DIR / key / name


def append_baseline_observation(
    project_root: Path,
    observation: PerformanceBaselineObservation,
) -> Path:
    """Append an immutable receipt; never overwrite an existing different payload.

    Two-phase write: receipt first, then history.jsonl. Same-payload retries heal
    an orphan receipt (crash between phases) into the history index without
    duplicating either the receipt or the history line.
    """
    path = receipt_path(project_root, observation)
    payload = {
        "schema_version": "1",
        **asdict(observation),
        "scenario_identity": scenario_identity(observation.capability, observation.endpoint),
        "receipt_digest": receipt_digest(observation),
    }
    encoded = canonical_json_bytes(payload)
    if path.is_file():
        existing = path.read_bytes()
        if existing == encoded:
            _ensure_history_line(project_root, payload)
            return path
        raise FileExistsError(f"baseline receipt already exists and differs: {path}")
    try:
        _atomic_write_bytes(path, encoded)
    except FileExistsError:
        if path.is_file() and path.read_bytes() == encoded:
            _ensure_history_line(project_root, payload)
            return path
        raise FileExistsError(f"baseline receipt already exists and differs: {path}") from None
    _ensure_history_line(project_root, payload)
    return path


def recent_baseline_observations(
    project_root: Path,
    *,
    identity: str,
    limit: int = DEFAULT_BASELINE_WINDOW,
) -> tuple[PerformanceBaselineObservation, ...]:
    """Return the newest ``limit`` observations for ``identity`` (newest first)."""
    if limit <= 0:
        return ()
    history = project_root / BASELINE_HISTORY_DIR_REL / _HISTORY_JSONL
    if not history.is_file():
        return ()
    matched: list[PerformanceBaselineObservation] = []
    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        if raw.get("scenario_identity") != identity:
            continue
        try:
            matched.append(
                PerformanceBaselineObservation(
                    capability=str(raw["capability"]),
                    endpoint=str(raw["endpoint"]),
                    change_id=str(raw["change_id"]),
                    batch_id=str(raw["batch_id"]),
                    p95_ms=float(raw["p95_ms"]),
                    error_rate=float(raw["error_rate"]),
                    source_digest=str(raw["source_digest"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
        if len(matched) >= limit:
            break
    return tuple(matched)


def _ensure_history_line(project_root: Path, payload: dict[str, object]) -> None:
    """Append a history index line if this receipt_digest is not already indexed."""
    history = project_root / BASELINE_HISTORY_DIR_REL / _HISTORY_JSONL
    digest = payload.get("receipt_digest")
    if digest is not None and history.is_file():
        try:
            for line in history.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict) and raw.get("receipt_digest") == digest:
                    return
        except OSError:
            pass
    _append_history_line(project_root, payload)


def _append_history_line(project_root: Path, payload: dict[str, object]) -> None:
    history = project_root / BASELINE_HISTORY_DIR_REL / _HISTORY_JSONL
    history.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    # O_APPEND is atomic for small writes on local filesystems; never rewrite.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(history, flags, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Project-local create-only write (no workflow import). Refuses overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


__all__ = [
    "BASELINE_HISTORY_DIR_REL",
    "DEFAULT_BASELINE_WINDOW",
    "DEFAULT_MIN_BASELINE_SAMPLES",
    "PerformanceBaselineObservation",
    "append_baseline_observation",
    "identity_digest",
    "receipt_digest",
    "receipt_path",
    "recent_baseline_observations",
    "scenario_identity",
]
