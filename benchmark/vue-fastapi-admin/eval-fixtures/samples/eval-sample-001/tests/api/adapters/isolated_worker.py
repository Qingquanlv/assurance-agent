"""Sync bridge spawning bounded subprocess workers for async domain factories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKER_TIMEOUT_SECONDS = 30
WORKER_MODULE = "tests.api.adapters._worker_main"


def _parse_worker_json_value(stdout: str) -> Any:
    """Return the last stdout line that parses as valid JSON (dict, list, or scalar)."""
    for line in reversed(stdout.strip().splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"isolated_worker: no valid JSON value in stdout: {stdout!r}")


def run_isolated(func_path: str, **kwargs: Any) -> Any:
    """Invoke an async domain function in a short-lived worker process."""
    project_root = Path(__file__).resolve().parents[3]
    cmd = [
        sys.executable,
        "-m",
        WORKER_MODULE,
        "--func",
        func_path,
        "--args",
        json.dumps(kwargs),
    ]
    sqlite_file = os.getenv("QA_SQLITE_FILE")
    if sqlite_file:
        cmd.extend(["--sqlite-file", sqlite_file])
    completed = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"isolated_worker failed for {func_path} (exit {completed.returncode}): {stderr}"
        )
    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(f"isolated_worker returned empty stdout for {func_path}")
    return _parse_worker_json_value(stdout)
