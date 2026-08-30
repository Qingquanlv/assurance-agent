from __future__ import annotations

import hashlib
import stat
import sys
import tempfile
from pathlib import Path

from agent_runtime_cursor.process import ProcessLaunchRequest

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
BWRAP = Path("/usr/bin/bwrap")


def authenticate_sandbox_executable(path: Path) -> Path:
    resolved = Path(path)
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("sandbox executable identity must be a regular file")
    mode = resolved.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("sandbox executable identity must be a regular file")
    return resolved


def _sbpl_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos_profile(project_root: Path, write_root: Path, temp_root: Path) -> str:
    project = _sbpl_quote(str(project_root))
    write = _sbpl_quote(str(write_root))
    temp = _sbpl_quote(str(temp_root))
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow process-info*)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow mach-priv-task-port)",
            "(allow ipc-posix-shm)",
            "(allow ipc-posix-sem)",
            "(allow system-socket)",
            "(allow network-outbound)",
            "(allow network-inbound)",
            "(allow network-bind)",
            "(allow file-read-metadata)",
            "(allow file-ioctl)",
            "(allow file-read*)",
            f"(allow file-read* (subpath {project}))",
            '(allow file-write-data (literal "/dev/null"))',
            '(allow file-ioctl (literal "/dev/null"))',
            f"(allow file-write* (subpath {write}) (subpath {temp}))",
            "",
        )
    )


def _wrap_environment(request: ProcessLaunchRequest, temp_root: Path) -> dict[str, str]:
    environment = dict(request.environment)
    environment["TMPDIR"] = str(temp_root)
    environment["TEMP"] = str(temp_root)
    environment["TMP"] = str(temp_root)
    environment["HOME"] = str(temp_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class FilesystemSandbox:
    @staticmethod
    def wrap(request: ProcessLaunchRequest) -> ProcessLaunchRequest:
        if sys.platform == "darwin":
            executable = authenticate_sandbox_executable(SANDBOX_EXEC)
            temp_root = Path(tempfile.mkdtemp(prefix="aa-cursor-sandbox-"))
            profile = _macos_profile(request.cwd.resolve(), request.write_root.resolve(), temp_root)
            digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
            argv = (str(executable), "-p", profile, *request.argv)
            return ProcessLaunchRequest(
                argv=argv,
                cwd=request.cwd,
                write_root=request.write_root,
                environment=_wrap_environment(request, temp_root),
                stdin=request.stdin,
                shell=False,
                executable_version_digest=request.executable_version_digest,
                request_digest=request.request_digest,
                argv_policy_digest=request.argv_policy_digest,
                workspace_identity_digest=request.workspace_identity_digest,
                sandbox_profile=profile,
                sandbox_profile_digest=digest,
                temp_root=temp_root,
            )
        if sys.platform.startswith("linux"):
            executable = authenticate_sandbox_executable(BWRAP)
            temp_root = Path(tempfile.mkdtemp(prefix="aa-cursor-sandbox-"))
            write_root = str(request.write_root.resolve())
            temp = str(temp_root)
            argv = (
                str(executable),
                "--die-with-parent",
                "--share-net",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--bind",
                write_root,
                write_root,
                "--bind",
                temp,
                temp,
                "--",
                *request.argv,
            )
            profile = " ".join(argv[: argv.index("--")])
            digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
            return ProcessLaunchRequest(
                argv=argv,
                cwd=request.cwd,
                write_root=request.write_root,
                environment=_wrap_environment(request, temp_root),
                stdin=request.stdin,
                shell=False,
                executable_version_digest=request.executable_version_digest,
                request_digest=request.request_digest,
                argv_policy_digest=request.argv_policy_digest,
                workspace_identity_digest=request.workspace_identity_digest,
                sandbox_profile=profile,
                sandbox_profile_digest=digest,
                temp_root=temp_root,
            )
        raise ValueError("filesystem sandbox is unavailable")


__all__ = ["BWRAP", "SANDBOX_EXEC", "FilesystemSandbox", "authenticate_sandbox_executable"]
