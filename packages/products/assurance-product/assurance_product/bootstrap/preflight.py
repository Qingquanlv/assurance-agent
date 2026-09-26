from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import yaml

from assurance_product.bootstrap.contracts import RunSpecV1
from assurance_product.bootstrap.status import find_active_run
from assurance_product.change_workspace import require_real_directory

_AMBIENT_OVERRIDES = frozenset(
    {
        "OPENCODE_ENDPOINT",
        "OPENCODE_MODEL",
        "AA_MODEL",
        "PROVIDER_MODEL",
    }
)


class BootstrapPreflightError(Exception):
    pass


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BootstrapPreflightError(f"live SUT must provide a regular {label}")


def _qa_change_id(project_dir: Path) -> str | None:
    status_path = project_dir / "qa" / "status.json"
    if status_path.is_file() and not status_path.is_symlink():
        try:
            document = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict):
            change = document.get("change")
            if isinstance(change, dict):
                change_id = change.get("change_id")
                if isinstance(change_id, str) and change_id.strip():
                    return change_id.strip()
    qa_yaml = project_dir / "qa" / ".qa.yaml"
    if qa_yaml.is_file() and not qa_yaml.is_symlink():
        try:
            document = yaml.safe_load(qa_yaml.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            document = None
        if isinstance(document, dict):
            change = document.get("change")
            if isinstance(change, dict):
                change_id = change.get("change_id")
                if isinstance(change_id, str) and change_id.strip():
                    return change_id.strip()
    return None


def preflight_bootstrap(
    *,
    project_dir: Path,
    spec: RunSpecV1,
    runs_root: Path,
    change_id: str,
    environ: Mapping[str, str],
    reuse_directory: bool = False,
) -> None:
    try:
        root = require_real_directory(project_dir if project_dir.is_absolute() else project_dir.resolve())
    except ValueError as error:
        raise BootstrapPreflightError(str(error)) from error
    _require_regular_file(root / ".aa" / "policy.yaml", ".aa/policy.yaml")
    _require_regular_file(root / ".aa" / "data-knowledge.yaml", ".aa/data-knowledge.yaml")
    qa_root = root / "qa"
    if qa_root.exists() and not reuse_directory:
        existing = _qa_change_id(root)
        if existing is not None and existing != change_id:
            raise BootstrapPreflightError("qa/ already belongs to a different change")
    active = find_active_run(runs_root, root)
    expected = (runs_root / change_id).resolve()
    if active is not None and active != expected:
        raise BootstrapPreflightError(f"project already has an active bootstrap run: {active}")
    present = [name for name in _AMBIENT_OVERRIDES if environ.get(name)]
    if present:
        raise BootstrapPreflightError("ambient override is forbidden: " + ", ".join(sorted(present)))
    if spec.opencode_token_env not in environ:
        raise BootstrapPreflightError(f"missing token env: {spec.opencode_token_env}")
    missing = [name for name in spec.sut.env_from_node if name not in environ]
    if missing:
        raise BootstrapPreflightError("missing sut env_from_node: " + ", ".join(missing))
