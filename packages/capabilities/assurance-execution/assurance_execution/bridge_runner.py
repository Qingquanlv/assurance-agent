"""Container supervisor: only this process owns the host protocol pipes.

The pytest worker receives an execute-only socket and a private raw-result pipe.
Its stdout/stderr are diagnostics. Same-UID /proc inspection is outside this
boundary. Arbitrary child frame/fd introspection can forge child raw data;
that data remains untrusted and never authenticates oracle facts.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from assurance_execution.operations.verified_process import _valid_report

_MAX_FRAME = 256 * 1024


def main() -> int:
    stopped = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + 60
    host_input, host_output = sys.stdin.buffer, sys.stdout.buffer
    selector = selectors.DefaultSelector()
    selector.register(host_input, selectors.EVENT_READ, "host")
    # The first bounded host frame is read before there is any pytest process.
    start_bytes = bytearray()
    while not stopped and time.monotonic() < deadline:
        if not selector.select(0.02):
            continue
        chunk = os.read(host_input.fileno(), 4096)
        if not chunk:
            return 2
        start_bytes.extend(chunk)
        if len(start_bytes) > _MAX_FRAME:
            return 2
        if b"\n" in start_bytes:
            break
    try:
        start = json.loads(start_bytes)
    except (ValueError, UnicodeError):
        return 2
    if not isinstance(start, dict) or set(start) != {"type", "nodeid"} or start["type"] != "start":
        return 2
    nodeid = start["nodeid"]
    if not isinstance(nodeid, str) or not nodeid.startswith("tests/") or "::" not in nodeid:
        return 2
    parent_channel, child_channel = socket.socketpair()
    result_reader, result_writer = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "assurance_execution.pytest_child",
            str(child_channel.fileno()),
            str(result_writer),
            nodeid,
        ],
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
        pass_fds=(child_channel.fileno(), result_writer),
        close_fds=True,
        shell=False,
    )
    child_channel.close()
    os.close(result_writer)
    selector.register(parent_channel, selectors.EVENT_READ, "execute")
    selector.register(result_reader, selectors.EVENT_READ, "result")
    buffers = {"execute": bytearray(), "host": bytearray(), "result": bytearray()}
    awaiting_ack = False
    result: dict[str, Any] | None = None
    result_eof = False
    execute_eof = False
    failed = False
    try:
        while not stopped and not failed and time.monotonic() < deadline:
            if child.poll() is not None and result_eof and execute_eof:
                break
            # Drain raw result before an execute EOF from the worker's orderly shutdown.
            events = sorted(selector.select(0.02), key=lambda event: event[0].data != "result")
            for key, _ in events:
                kind = key.data
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    if kind == "host":
                        failed = True
                    elif kind == "result":
                        result_eof = True
                    else:
                        execute_eof = True
                        if result is None:
                            failed = True
                    continue
                pending = buffers[kind]
                pending.extend(chunk)
                while b"\n" in pending:
                    raw, _, rest = pending.partition(b"\n")
                    buffers[kind] = pending = bytearray(rest)
                    if len(raw) + 1 > _MAX_FRAME:
                        failed = True
                        break
                    try:
                        document = json.loads(raw)
                    except (ValueError, UnicodeError):
                        failed = True
                        break
                    if kind == "execute":
                        if (
                            awaiting_ack
                            or result is not None
                            or not isinstance(document, dict)
                            or set(document) != {"type", "case_id"}
                            or document["type"] != "execute"
                            or not isinstance(document["case_id"], str)
                            or not 0 < len(document["case_id"]) <= 256
                        ):
                            failed = True
                            break
                        host_output.write(raw + b"\n")
                        host_output.flush()
                        awaiting_ack = True
                    elif kind == "host":
                        if not awaiting_ack or document not in ({"type": "ack"}, {"type": "error"}):
                            failed = True
                            break
                        parent_channel.sendall(raw + b"\n")
                        awaiting_ack = False
                    else:
                        if result is not None or awaiting_ack or not _valid_report(document, nodeid):
                            failed = True
                            break
                        # The installed child waits for this final ack before closing execute IPC.
                        # A readable socket now means premature EOF or an unexpected queued request.
                        try:
                            parent_channel.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                        except BlockingIOError:
                            pass
                        else:
                            failed = True
                            break
                        parent_channel.sendall(b'{"type":"ack"}\n')
                        result = document
                if len(pending) > _MAX_FRAME:
                    failed = True
        valid = (
            not stopped
            and not failed
            and child.poll() is not None
            and result_eof
            and execute_eof
            and not awaiting_ack
            and not any(buffers.values())
            and result is not None
            and _valid_report(result, nodeid, child.returncode)
        )
        if not valid:
            return 2
        host_output.write(json.dumps({"type": "runner_report", "raw": result}).encode() + b"\n")
        host_output.flush()
        return int(child.returncode)
    except (OSError, ValueError):
        return 2
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait(timeout=1)
        parent_channel.close()
        os.close(result_reader)
        selector.close()


if __name__ == "__main__":
    raise SystemExit(main())
