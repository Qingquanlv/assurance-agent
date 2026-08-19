"""Generic subprocess lifecycle + timeout management for the driver.

Used by HeadlessAdapter (spawn an arbitrary agent CLI). Mirrors the TS
ProcessRunner seam (src/workflow/driver/process_runner.ts) so callers can
inject a fake.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class ProcessRunner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: float | None = None,
        stdin_text: str | None = None,
    ) -> ProcessResult: ...


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class SubprocessRunner:
    """Default ProcessRunner backed by subprocess.run."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: float | None = None,
        stdin_text: str | None = None,
    ) -> ProcessResult:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            return ProcessResult(
                exit_code=124,
                stdout=_as_text(err.stdout),
                stderr=_as_text(err.stderr),
                timed_out=True,
            )
        return ProcessResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _looks_like_assurance_aa(path: Path) -> bool:
    """True if ``path`` is the assurance-agent CLI, not macOS Apple Archive ``aa``."""
    try:
        completed = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{completed.stdout}\n{completed.stderr}"
    return "Assurance Agent" in blob or "aa - Assurance" in blob


def resolve_aa_command() -> list[str]:
    """Resolve the assurance-agent ``aa`` CLI (never macOS ``/usr/bin/aa``)."""
    candidates: list[Path] = []
    env_bin = os.environ.get("AA_BIN")
    if env_bin:
        candidates.append(Path(env_bin))
    candidates.append(Path(sys.executable).resolve().parent / "aa")
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            candidates.append(Path(directory) / "aa")
    which = shutil.which("aa")
    if which:
        candidates.append(Path(which))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and _looks_like_assurance_aa(candidate):
            return [str(candidate)]
    return [which] if which else ["aa"]
