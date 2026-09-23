from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from assurance_product.bootstrap.contracts import BootstrapStatusV1, RunSpecV1

_UNSAFE_CHANGE_ID = frozenset({"/", "\\", " ", "\x00"})


def derive_bootstrap_change_id(*, stamp: str, nonce: str) -> str:
    for label, value in (("stamp", stamp), ("nonce", nonce)):
        if not value or any(character in value for character in _UNSAFE_CHANGE_ID):
            raise ValueError(f"{label} is not a safe change-id component")
    return f"BOOT-{stamp}-{nonce}"


def run_dir_for(runs_root: Path, change_id: str) -> Path:
    destination = runs_root / change_id
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_bootstrap_status(run_dir: Path, status: BootstrapStatusV1) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "bootstrap-status.json", status.model_dump(mode="json"))


def read_bootstrap_status(run_dir: Path) -> BootstrapStatusV1:
    raw = json.loads((run_dir / "bootstrap-status.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("bootstrap-status.json must be an object")
    return BootstrapStatusV1.model_validate(raw)


def effective_spec_bytes(spec: RunSpecV1) -> bytes:
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True).encode("utf-8")


def write_effective_spec(run_dir: Path, spec: RunSpecV1) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run-spec.effective.yaml"
    data = effective_spec_bytes(spec)
    path.write_bytes(data)
    return path


def effective_spec_digest(spec: RunSpecV1) -> str:
    return hashlib.sha256(effective_spec_bytes(spec)).hexdigest()


def write_stop_request(run_dir: Path, *, change_id: str) -> None:
    _write_json(
        run_dir / "stop-request.json",
        {"schema_version": "1", "change_id": change_id},
    )


def stop_requested(run_dir: Path) -> bool:
    return (run_dir / "stop-request.json").is_file()


def write_run_manifest(run_dir: Path, document: Mapping[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-manifest.json", document)


def read_run_manifest(run_dir: Path) -> dict[str, object]:
    raw = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("run-manifest.json must be an object")
    return raw


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_active_run(runs_root: Path, project_dir: Path) -> Path | None:
    if not runs_root.is_dir():
        return None
    expected = project_dir.resolve()
    for candidate in sorted(runs_root.iterdir()):
        if not candidate.is_dir():
            continue
        status_path = candidate / "bootstrap-status.json"
        manifest_path = candidate / "run-manifest.json"
        if not status_path.is_file() or not manifest_path.is_file():
            continue
        try:
            status = read_bootstrap_status(candidate)
            manifest = read_run_manifest(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        project = manifest.get("project_dir")
        if not isinstance(project, str):
            continue
        if Path(project).resolve() != expected:
            continue
        if status.phase == "terminal":
            continue
        pid = status.opencode.pid if status.opencode is not None else None
        if pid is None or not _pid_is_alive(pid):
            continue
        return candidate.resolve()
    return None
