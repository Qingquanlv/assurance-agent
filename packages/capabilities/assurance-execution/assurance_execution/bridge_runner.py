"""Fixed pytest entrypoint; stdout is exclusively bounded, untrusted JSONL."""

from __future__ import annotations

import json
import os
import sys
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
    # Save the pipes before pytest captures streams or imports test-controlled code.
    reader = os.fdopen(os.dup(0), "rb", buffering=0)
    writer = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def request(case_id: str) -> None:
        payload = json.dumps({"type": "execute", "case_id": case_id}, separators=(",", ":")).encode() + b"\n"
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
    raw = reader.readline(bridge._MAX_FRAME + 1)
    if len(raw) > bridge._MAX_FRAME or not raw.endswith(b"\n"):
        return 2
    start = json.loads(raw)
    if not isinstance(start, dict) or set(start) != {"type", "nodeid"} or start["type"] != "start":
        return 2
    nodeid = start["nodeid"]
    if not isinstance(nodeid, str) or not nodeid.startswith("tests/") or "::" not in nodeid:
        return 2
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
            "-q",
        ],
        plugins=[report],
    )
    summary = {"collected": report.collected, "passed": 0, "failed": 0, "skipped": 0}
    if report.tests:
        outcomes = [item["outcome"] for item in report.tests]
        outcome = "failed" if "failed" in outcomes else "skipped" if "skipped" in outcomes else "passed"
        summary[outcome] = 1
    payload = (
        json.dumps(
            {
                "type": "runner_report",
                "raw": {
                    "exitcode": int(code),
                    "summary": summary,
                    "tests": report.tests,
                },
            }
        ).encode()
        + b"\n"
    )
    if len(payload) > bridge._MAX_FRAME:
        return 2
    writer.write(payload)
    writer.flush()
    writer.close()
    reader.close()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
