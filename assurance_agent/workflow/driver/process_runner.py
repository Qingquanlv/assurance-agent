"""Generic subprocess lifecycle + timeout management for the driver.

Used both by HeadlessAdapter (spawn an arbitrary agent CLI) and by the default
CliPhaseExecutor (invoke pinned `aa` subcommands). Mirrors the TS ProcessRunner
seam (src/workflow/driver/process_runner.ts) so callers can inject a fake.
"""
import shutil
import subprocess
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
    ) -> ProcessResult:
        ...


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


def resolve_aa_command() -> list[str]:
    """Resolve the `aa` CLI entrypoint (prefer PATH). Callers may override in tests."""
    found = shutil.which("aa")
    return [found] if found else ["aa"]
