"""Bounded pipe transport and fixed OCI confinement for verified pytest."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


from assurance_execution.contracts.verification import VerifiedProcessLimitsV1, VerifiedProcessReceiptV1

ProcessLimits = VerifiedProcessLimitsV1


def container_argv(image: str, view: str, container_name: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--name",
        container_name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=64",
        "--memory=512m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={view},dst=/tests,readonly",
        "--workdir=/tests",
        image,
        "python",
        "-m",
        "assurance_execution.bridge_runner",
    ]


def stop_container(name: str) -> bool:
    """Stop only our named container, then positively confirm its absence."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10, check=False)
        result = subprocess.run(
            ["docker", "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and not result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_bridge_process(
    argv: list[str],
    *,
    cwd: Path,
    nodeid: str,
    case_id: str,
    execute: Callable[[str], None],
    limits: ProcessLimits = ProcessLimits(),
    cancel_requested: Callable[[], bool] = lambda: False,
    cleanup: Callable[[], bool] | None = None,
) -> VerifiedProcessReceiptV1:
    """Transport primitive; production callers supply only container_argv."""
    reason: str | None = None
    report: dict[str, Any] | None = None
    request_count = 0
    pending = bytearray()
    stderr = bytearray()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    deadline = time.monotonic() + limits.timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    cleanup_confirmed = cleanup is None
    try:
        process.stdin.write(json.dumps({"type": "start", "nodeid": nodeid}).encode() + b"\n")
        process.stdin.flush()
        while selector.get_map() and reason is None:
            if cancel_requested():
                reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    remaining = limits.max_stderr_bytes - len(stderr)
                    stderr.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        reason = "stderr_limit"
                    continue
                pending.extend(chunk)
                while b"\n" in pending and reason is None:
                    raw, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if len(raw) + 1 > limits.max_frame_bytes:
                        reason = "frame_too_large"
                        break
                    try:
                        frame = json.loads(raw)
                    except (UnicodeError, ValueError):
                        reason = "invalid_json"
                        break
                    if not isinstance(frame, dict):
                        reason = "unknown_frame"
                    elif frame.get("type") == "execute":
                        request_count += 1
                        if set(frame) != {"type", "case_id"} or not isinstance(frame["case_id"], str):
                            reason = "invalid_execute_frame"
                        elif frame["case_id"] != case_id:
                            reason = "unknown_case"
                        elif request_count != 1 or report is not None:
                            reason = "duplicate_execute_request"
                        else:
                            try:
                                execute(case_id)
                            except Exception:
                                reason = "parent_execution_error"
                        process.stdin.write(
                            json.dumps({"type": "ack" if reason is None else "error"}).encode() + b"\n"
                        )
                        process.stdin.flush()
                    elif frame.get("type") == "runner_report":
                        if (
                            set(frame) != {"type", "raw"}
                            or not isinstance(frame["raw"], dict)
                            or not isinstance(frame["raw"].get("summary"), dict)
                            or type(frame["raw"].get("summary", {}).get("collected")) is not int
                            or report is not None
                        ):
                            reason = "invalid_runner_report"
                        else:
                            report = frame["raw"]
                    else:
                        reason = "unknown_frame"
                if len(pending) > limits.max_frame_bytes:
                    reason = "frame_too_large"
        if reason is None and pending:
            reason = "truncated_frame"
        if reason is None:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
    except (BrokenPipeError, OSError):
        reason = reason or "pipe_closed"
    finally:
        selector.close()
        # Reap the CLI first so it cannot issue another create while cleanup runs.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if cleanup is not None:
            cleanup_confirmed = cleanup()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    if not cleanup_confirmed:
        reason = "container_cleanup_unconfirmed"
    if reason is None and request_count == 0:
        reason = "missing_execute_request"
    if reason is None and report is None:
        reason = "missing_runner_report"
    if reason is None and report is not None and report.get("summary", {}).get("collected", 0) == 0:
        reason = "zero_collection"
    return VerifiedProcessReceiptV1(
        command=tuple(argv),
        limits=limits,
        exit_code=process.returncode,
        report=report,
        reason=reason,
        request_count=request_count,
        stderr=stderr.decode("utf-8", errors="replace"),
        cleanup_confirmed=cleanup_confirmed,
    )


def runner_source_inputs(root: Path) -> dict[str, str]:
    """Bytes that invalidate a qualification, including every shipped wheel source."""
    import hashlib

    selected = [
        root / "pyproject.toml",
        root / "uv.lock",
        root / "scripts/build_verification_runner.py",
        root / "scripts/assurance_product_wheel_smoke_test.sh",
    ]
    fixture = root / "benchmark/assurance-product/fixtures/user-oracle"
    selected += [
        fixture / "runner.Dockerfile",
        fixture / "runner-lock.json",
        fixture / "runtime-lock.json",
        fixture / "requirements.lock",
        root / "benchmark/assurance-product/user_oracle_harness.py",
    ]
    selected += [
        path
        for path in (root / "packages").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and not any(part.endswith(".egg-info") for part in path.parts)
        and path.suffix != ".pyc"
    ]
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(selected)
    }


class DockerVerificationHost:
    def __init__(
        self, *, source_root: Path, qualification_path: Path, limits: ProcessLimits = ProcessLimits()
    ) -> None:
        self.source_root = source_root
        self.qualification_path = qualification_path
        self.limits = limits
        self._image: str | None = None

    def preflight(self) -> dict[str, Any]:
        import hashlib
        import re
        import zipfile
        from importlib import metadata

        try:
            record = json.loads(self.qualification_path.read_bytes())
            if record["schema_version"] != "1" or record["source_inputs"] != runner_source_inputs(
                self.source_root
            ):
                raise ValueError("source or lock drift")
            for name, digest in record["wheels"].items():
                if Path(name).name != name or not name.endswith(".whl"):
                    raise ValueError("invalid wheel name")
                wheel = self.qualification_path.parent / "wheels" / name
                if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
                    raise ValueError("wheel drift")
            execution = record["execution_wheel"]
            if not execution.startswith("assurance_execution-") or execution not in record["wheels"]:
                raise ValueError("missing execution wheel")
            # Bind the wheel in the runner to the host's actual installed execution package.
            distribution = metadata.distribution("assurance-execution")
            with zipfile.ZipFile(self.qualification_path.parent / "wheels" / execution) as archive:
                for name in archive.namelist():
                    if name.startswith("assurance_execution/") and not name.endswith("/"):
                        installed = Path(str(distribution.locate_file(name)))
                        if not installed.is_file():
                            # Editable installs expose the source package outside site-packages.
                            import assurance_execution

                            installed = Path(assurance_execution.__file__).parent.parent / name
                        if archive.read(name) != installed.read_bytes():
                            raise ValueError("installed execution wheel drift")
            requirements = self.qualification_path.parent / "runner-requirements.txt"
            if hashlib.sha256(requirements.read_bytes()).hexdigest() != record["dependency_lock_digest"]:
                raise ValueError("dependency lock drift")
            bound = {
                key: value for key, value in record.items() if key not in {"image_id", "build_input_digest"}
            }
            calculated = hashlib.sha256(
                json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if record["build_input_digest"] != calculated:
                raise ValueError("qualification build inputs drift")
            image = record["image_id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ValueError("invalid image identity")
            inspected = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=10, check=True
            )
            actual = json.loads(inspected.stdout)[0]
            if actual["Id"] != image or actual["Os"] + "/" + actual["Architecture"] != record["platform"]:
                raise ValueError("image identity/platform drift")
            labels = actual["Config"].get("Labels") or {}
            if labels.get("org.assurance.runner.inputs") != record["build_input_digest"]:
                raise ValueError("image build input mismatch")
            self._image = image
            return {
                "image_id": image,
                "qualification_digest": hashlib.sha256(self.qualification_path.read_bytes()).hexdigest(),
                "limits": self.limits.model_dump(mode="json"),
            }
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            raise ValueError(f"NOT_READY: verification runner qualification failed: {error}") from error

    def run(
        self,
        *,
        view: Path,
        nodeid: str,
        case_id: str,
        container_name: str,
        execute: Callable[[str], None],
        cancel_requested: Callable[[], bool],
    ) -> VerifiedProcessReceiptV1:
        self.preflight()
        assert self._image is not None
        resolved = view.resolve(strict=True)
        if view.is_symlink() or "," in str(resolved) or "\n" in str(resolved):
            raise ValueError("invalid test-only mount path")
        import re

        if re.fullmatch(r"aa-verify-[0-9a-f-]{36}", container_name) is None:
            raise ValueError("invalid owned container name")
        return run_bridge_process(
            container_argv(self._image, str(resolved), container_name),
            cwd=resolved,
            nodeid=nodeid,
            case_id=case_id,
            execute=execute,
            limits=self.limits,
            cancel_requested=cancel_requested,
            cleanup=lambda: self.stop(container_name),
        )

    def stop(self, container_name: str) -> bool:
        return stop_container(container_name)
