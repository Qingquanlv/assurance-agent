"""Installed pytest worker. It never receives the host protocol descriptors."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

from assurance_execution import bridge


class _RawReport:
    def __init__(self) -> None:
        self.tests: list[dict[str, Any]] = []
        self.collected = 0

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.tests.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome})


def main() -> int:
    # Only the execute socket is captured by the generated-code bridge callback.
    channel_fd, result_fd = int(sys.argv[1]), int(sys.argv[2])
    nodeid = sys.argv[3]
    sys.argv = ["pytest", nodeid]
    channel = socket.socket(fileno=channel_fd)
    reader, writer = channel.makefile("rb", buffering=0), channel.makefile("wb", buffering=0)

    def request(case_id: str) -> None:
        payload = json.dumps({"type": "execute", "case_id": case_id}).encode() + b"\n"
        if len(payload) > bridge._MAX_FRAME:
            raise RuntimeError("bridge frame exceeds limit")
        writer.write(payload)
        writer.flush()
        response = reader.readline(bridge._MAX_FRAME + 1)
        if len(response) > bridge._MAX_FRAME or not response.endswith(b"\n"):
            raise RuntimeError("parent bridge closed without acknowledgement")
        if json.loads(response) != {"type": "ack"}:
            raise RuntimeError("parent bridge rejected execution request")

    bridge._configure(request)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    import pytest

    report = _RawReport()
    code = pytest.main(
        [
            nodeid,
            "-c",
            "/dev/null",
            "--rootdir=.",
            "--confcutdir=.",
            "-p",
            "no:cacheprovider",
            "--noconftest",
            "--basetemp",
            str(Path(tempfile.gettempdir()) / "pytest"),
            "-q",
        ],
        plugins=[report],
    )
    summary = {"collected": report.collected, "passed": 0, "failed": 0, "skipped": 0}
    if report.tests:
        outcomes = [item["outcome"] for item in report.tests]
        outcome = "failed" if "failed" in outcomes else "skipped" if "skipped" in outcomes else "passed"
        summary[outcome] = 1
    # This private result is raw pytest data, never a host runner_report frame.
    # There is no imported function/global capable of emitting a host terminal report.
    # Arbitrary frame/fd introspection can still forge child data; it is not oracle authority.
    payload = json.dumps({"exitcode": int(code), "summary": summary, "tests": report.tests}).encode() + b"\n"
    if len(payload) > bridge._MAX_FRAME:
        return 2
    try:
        with os.fdopen(result_fd, "wb", buffering=0) as result:
            remaining = memoryview(payload)
            while remaining:
                written = result.write(remaining)
                if written is None or written <= 0:
                    return 2
                remaining = remaining[written:]
        # Keep the execute socket open until the supervisor has consumed the raw result.
        # This makes an earlier socket shutdown detectable across both IPC channels.
        confirmation = reader.readline(bridge._MAX_FRAME + 1)
        if confirmation != b'{"type":"ack"}\n':
            return 2
    finally:
        reader.close()
        writer.close()
        channel.close()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
