from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts/build_wheels.py"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Use a real provider and Hatch project, not a mocked build backend."""
    root = tmp_path / "workspace"
    package = root / "example"
    shutil.copytree(REPO_ROOT / "examples/graph-engine-toy-a", package)
    (root / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["example"]\n', encoding="utf-8")
    # A Python-only registration change: the checked-in declaration is now stale.
    plugin = package / "graph_engine_toy_a/plugin.py"
    plugin.write_text(
        plugin.read_text(encoding="utf-8").replace('plugin_id="toy.a"', 'plugin_id="toy.updated"'),
        encoding="utf-8",
    )
    return root


def run_builder(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), *args],
        cwd=root,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(REPO_ROOT / ".venv")},
        text=True,
        capture_output=True,
        check=False,
    )


def declaration_path(root: Path) -> Path:
    return root / "example/graph_engine_toy_a/plugin-declaration.json"


def test_check_detects_python_registration_drift_without_writing(workspace: Path) -> None:
    target = declaration_path(workspace)
    before = target.read_bytes()
    result = run_builder(workspace, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "plugin-declaration.json" in result.stdout
    assert target.read_bytes() == before


def test_generate_updates_declaration_and_is_idempotent(workspace: Path) -> None:
    target = declaration_path(workspace)
    target.unlink()
    result = run_builder(workspace, "--declarations-only")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(target.read_bytes())
    assert payload["kind"] == "plugin"
    assert payload["descriptor"]["plugin_id"] == "toy.updated"
    before = target.stat().st_mtime_ns
    result = run_builder(workspace, "--declarations-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.stat().st_mtime_ns == before
    result = run_builder(workspace, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_wheel_build_refreshes_stale_declaration(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "dist"
    result = run_builder(workspace, "--package", "graph-engine-toy-a", "--offline", "--out-dir", str(output))
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next(output.glob("graph_engine_toy_a-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        payload = archive.read("graph_engine_toy_a/plugin-declaration.json")
        assert payload == declaration_path(workspace).read_bytes()
        assert json.loads(payload)["descriptor"]["plugin_id"] == "toy.updated"
        record = archive.read("graph_engine_toy_a-1.0.0.dist-info/RECORD").decode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        assert f"graph_engine_toy_a/plugin-declaration.json,sha256={digest}," in record


def test_unknown_package_fails_before_writing(workspace: Path) -> None:
    target = declaration_path(workspace)
    before = target.read_bytes()
    result = run_builder(workspace, "--package", "unknown", "--declarations-only")
    assert result.returncode != 0
    assert "unknown" in result.stderr
    assert target.read_bytes() == before


def test_entrypoint_drift_fails_before_writing(workspace: Path) -> None:
    target = declaration_path(workspace)
    before = target.read_bytes()
    config = workspace / "example/pyproject.toml"
    config.write_text(
        config.read_text().replace(
            'toy-a = "graph_engine_toy_a.plugin:', 'wrong = "graph_engine_toy_a.plugin:'
        )
    )
    result = run_builder(workspace, "--declarations-only")
    assert result.returncode != 0
    assert "coordinates" in result.stderr
    assert target.read_bytes() == before


def test_generator_rejects_symlink_destination(workspace: Path, tmp_path: Path) -> None:
    target = declaration_path(workspace)
    outside = tmp_path / "unrelated.json"
    outside.write_text("do not overwrite", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)
    result = run_builder(workspace, "--declarations-only")
    assert result.returncode != 0
    assert "escapes package" in result.stderr
    assert outside.read_text(encoding="utf-8") == "do not overwrite"
