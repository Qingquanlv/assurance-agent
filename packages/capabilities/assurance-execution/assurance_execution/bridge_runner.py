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
    bridge._configure(reader, writer)
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
    bridge._send(
        {
            "type": "runner_report",
            "raw": {
                "exitcode": int(code),
                "summary": {"collected": report.collected},
                "tests": report.tests,
            },
        }
    )
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
