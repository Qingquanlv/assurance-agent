"""Bounded pipe transport and fixed OCI confinement for verified pytest."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
import threading
from concurrent.futures import Future, ThreadPoolExecutor
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


class ActionControl:
    """One process deadline and cooperative stop signal for the installed host action."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self._stop = threading.Event()

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def stopped(self) -> bool:
        return self._stop.is_set() or self.remaining <= 0

    def stop(self) -> None:
        self._stop.set()

    def require(self, seconds: float) -> None:
        if self.stopped or self.remaining < seconds:
            raise TimeoutError("insufficient_action_budget")


def _valid_report(raw: Any, nodeid: str, exit_code: int | None = None) -> bool:
    if not isinstance(raw, dict) or set(raw) != {"exitcode", "summary", "tests"}:
        return False
    code, summary, tests = raw["exitcode"], raw["summary"], raw["tests"]
    if type(code) is not int or code not in range(6) or (exit_code is not None and code != exit_code):
        return False
    if not isinstance(summary, dict) or set(summary) != {"collected", "passed", "failed", "skipped"}:
        return False
    if any(type(value) is not int or value < 0 for value in summary.values()):
        return False
    if not isinstance(tests, list):
        return False
    if summary["collected"] == 0:
        return tests == [] and sum(summary.values()) == 0 and code in {2, 4, 5}
    if summary["collected"] != 1 or not tests:
        return False
    for test in tests:
        if (
            not isinstance(test, dict)
            or set(test) != {"nodeid", "when", "outcome"}
            or test["nodeid"] != nodeid
            or not isinstance(test["outcome"], str)
            or test["outcome"] not in {"passed", "failed", "skipped"}
        ):
            return False
    stages = [test["when"] for test in tests]
    expected_stages = (
        ["setup", "call", "teardown"] if tests[0]["outcome"] == "passed" else ["setup", "teardown"]
    )
    if stages != expected_stages:
        return False
    outcomes = [test["outcome"] for test in tests]
    outcome = "failed" if "failed" in outcomes else "skipped" if "skipped" in outcomes else "passed"
    return all(
        summary[key] == int(key == outcome) for key in ("passed", "failed", "skipped")
    ) and code == int(outcome == "failed")


def run_bridge_process(
    argv: list[str],
    *,
    cwd: Path,
    nodeid: str,
    case_id: str,
    execute: Callable[[str, ActionControl], None],
    limits: ProcessLimits = ProcessLimits(),
    cancel_requested: Callable[[], bool] = lambda: False,
    cleanup: Callable[[], bool] | None = None,
) -> VerifiedProcessReceiptV1:
    """Supervise pipes and the installed, bounded host action under one deadline."""
    reason: str | None = None
    report: dict[str, Any] | None = None
    request_count = 0
    pending = bytearray()
    stderr = bytearray()
    control = ActionControl(time.monotonic() + limits.timeout_seconds)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verified-action")
    action: Future[None] | None = None
    fatal: BaseException | None = None
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
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    cleanup_confirmed = cleanup is None
    try:
        process.stdin.write(json.dumps({"type": "start", "nodeid": nodeid}).encode() + b"\n")
        process.stdin.flush()
        while (selector.get_map() or action is not None or process.poll() is None) and reason is None:
            if cancel_requested():
                reason = "cancelled"
                break
            if control.remaining <= 0:
                reason = "timeout"
                break
            if action is not None and action.done():
                try:
                    action.result()
                except BaseException as error:
                    reason = "parent_execution_error"
                    if not isinstance(error, Exception):
                        fatal = error
                action = None
                process.stdin.write(
                    json.dumps({"type": "ack" if reason is None else "error"}).encode() + b"\n"
                )
                process.stdin.flush()
            for key, _ in selector.select(min(0.02, control.remaining)):
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
                    if report is not None:
                        reason = "invalid_runner_report"
                    elif not isinstance(frame, dict):
                        reason = "unknown_frame"
                    elif frame.get("type") == "execute":
                        request_count += 1
                        if set(frame) != {"type", "case_id"} or not isinstance(frame["case_id"], str):
                            reason = "invalid_execute_frame"
                        elif frame["case_id"] != case_id:
                            reason = "unknown_case"
                        elif request_count != 1:
                            reason = "duplicate_execute_request"
                        else:
                            action = worker.submit(execute, case_id, control)
                    elif frame.get("type") == "runner_report":
                        if (
                            set(frame) != {"type", "raw"}
                            or action is not None
                            or not _valid_report(frame["raw"], nodeid)
                        ):
                            reason = "invalid_runner_report"
                        else:
                            report = frame["raw"]
                    else:
                        reason = "unknown_frame"
                if len(pending) > limits.max_frame_bytes:
                    reason = "frame_too_large"
        if reason is None and pending:
            reason = "invalid_runner_report" if report is not None else "truncated_frame"
        if reason is None:
            try:
                process.wait(timeout=max(0.001, control.remaining))
            except subprocess.TimeoutExpired:
                reason = "timeout"
    except (BrokenPipeError, OSError):
        reason = reason or "pipe_closed"
    finally:
        control.stop()
        # The installed action cancels HTTP and bounds SQLite reads. Join before any terminal return.
        worker.shutdown(wait=True, cancel_futures=True)
        if action is not None and action.done() and not action.cancelled():
            error = action.exception()
            if error is not None and not isinstance(error, Exception):
                fatal = error
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if cleanup is not None:
            cleanup_confirmed = cleanup()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    if fatal is not None:
        raise fatal
    if not cleanup_confirmed:
        reason = reason or "container_cleanup_unconfirmed"
    if reason is None and report is not None and not _valid_report(report, nodeid, process.returncode):
        reason = "invalid_runner_report"
    if reason is None and request_count == 0:
        reason = "missing_execute_request"
    if reason is None and report is None:
        reason = "missing_runner_report"
    if reason is None and report is not None and report["summary"]["collected"] == 0:
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
        execute: Callable[[str, ActionControl], None],
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
