from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from assurance_execution.operations.verified_process import (
    ProcessLimits,
    container_argv,
    run_bridge_process,
)


def child(tmp_path: Path, source: str, *, timeout: float = 10, cancel=lambda: False):
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_case.py").write_text(source)
    calls: list[str] = []
    receipt = run_bridge_process(
        [sys.executable, "-m", "assurance_execution.bridge_runner"],
        cwd=tmp_path,
        nodeid="tests/test_case.py::test_case",
        case_id="TC_USER_CREATE_001",
        execute=calls.append,
        limits=ProcessLimits(timeout_seconds=timeout),
        cancel_requested=cancel,
    )
    return receipt, calls


@pytest.mark.parametrize(
    "source", ["", "def test_case():\n    pass\n", "def test_case():\n    assert True\n"]
)
def test_no_bridge_never_creates_parent_facts(tmp_path, source):
    receipt, calls = child(tmp_path, source)
    assert calls == []
    assert receipt.request_count == 0
    assert receipt.reason == "missing_execute_request"


def test_thin_bridge_executes_parent_once(tmp_path):
    receipt, calls = child(
        tmp_path,
        'from assurance_execution.bridge import execute_case\ndef test_case():\n    execute_case("TC_USER_CREATE_001")\n',
    )
    assert calls == ["TC_USER_CREATE_001"]
    assert receipt.exit_code == 0
    assert receipt.reason is None
    assert receipt.report is not None
    assert receipt.report["summary"]["collected"] == 1


@pytest.mark.parametrize(
    ("body", "reason", "count"),
    [
        (
            'execute_case("TC_USER_CREATE_001"); execute_case("TC_USER_CREATE_001")',
            "duplicate_execute_request",
            1,
        ),
        ('execute_case("unknown")', "unknown_case", 0),
    ],
)
def test_bridge_rejects_duplicate_and_unknown_cases(tmp_path, body, reason, count):
    receipt, calls = child(
        tmp_path, "from assurance_execution.bridge import execute_case\ndef test_case():\n    " + body + "\n"
    )
    assert receipt.reason == reason
    assert len(calls) == count


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        ('{"type":"complete","status":"passed"}\n', "unknown_frame"),
        ('{"type":"execute","case_id":"case","sql":"DROP TABLE user"}\n', "invalid_execute_frame"),
        ("not json\n", "invalid_json"),
        ('{"type":', "truncated_frame"),
        ("x" * (256 * 1024 + 1), "frame_too_large"),
    ],
)
def test_raw_subprocess_frames_fail_closed(tmp_path, frame, reason):
    receipt = run_bridge_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.readline(); sys.stdout.write(" + repr(frame) + "); sys.stdout.flush()",
        ],
        cwd=tmp_path,
        nodeid="tests/t.py::test",
        case_id="case",
        execute=lambda _: pytest.fail("must not execute"),
    )
    assert receipt.reason == reason


def test_fragmented_jsonl_is_reassembled_within_limit(tmp_path):
    calls = []
    code = 'import sys,time; sys.stdin.readline(); sys.stdout.write(\'{"type":"execute",\'); sys.stdout.flush(); time.sleep(.05); sys.stdout.write(\'"case_id":"case"}\\n\'); sys.stdout.flush(); sys.stdin.readline()'
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid="tests/t.py::test",
        case_id="case",
        execute=calls.append,
    )
    assert calls == ["case"]
    assert receipt.reason == "missing_runner_report"


def test_timeout_terminates_actual_process(tmp_path):
    receipt, calls = child(tmp_path, "import time\ndef test_case():\n    time.sleep(60)\n", timeout=0.3)
    assert receipt.reason == "timeout"
    assert receipt.exit_code is not None
    assert calls == []


def test_cancellation_terminates_actual_process(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        receipt, _ = child(
            tmp_path, "import time\ndef test_case():\n    time.sleep(60)\n", cancel=cancel.is_set
        )
    finally:
        timer.cancel()
    assert receipt.reason == "cancelled"
    assert receipt.exit_code is not None


def test_container_command_has_exact_closed_mount_and_limits():
    assert container_argv("sha256:" + "a" * 64, "/view", "aa-test") == [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--name",
        "aa-test",
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
        "type=bind,src=/view,dst=/tests,readonly",
        "--workdir=/tests",
        "sha256:" + "a" * 64,
        "python",
        "-m",
        "assurance_execution.bridge_runner",
    ]


def test_missing_qualification_record_is_not_ready(tmp_path):
    from assurance_execution.operations.verified_process import DockerVerificationHost

    host = DockerVerificationHost(source_root=tmp_path, qualification_path=tmp_path / "missing.json")
    with pytest.raises(ValueError, match="NOT_READY"):
        host.preflight()


@pytest.mark.parametrize("raw", ["[]", '{"summary":null}', '{"summary":42}'])
def test_invalid_report_shape_is_protocol_error(tmp_path, raw):
    receipt = run_bridge_process(
        [
            sys.executable,
            "-c",
            'import sys;sys.stdin.readline();print(\'{"type":"runner_report","raw":' + raw + "}',flush=True)",
        ],
        cwd=tmp_path,
        nodeid="tests/a.py::test",
        case_id="case",
        execute=lambda _: None,
    )
    assert receipt.reason == "invalid_runner_report"


def test_bounded_stderr_terminates_flooding_child(tmp_path):
    receipt = run_bridge_process(
        [
            sys.executable,
            "-c",
            'import sys;sys.stdin.readline();sys.stderr.write("x"*2048);sys.stderr.flush()',
        ],
        cwd=tmp_path,
        nodeid="tests/a.py::test",
        case_id="case",
        execute=lambda _: None,
        limits=ProcessLimits(max_stderr_bytes=1024),
    )
    assert receipt.reason == "stderr_limit"
    assert len(receipt.stderr) == 1024


def test_zero_collection_report_cannot_complete_even_after_action(tmp_path):
    code = 'import sys,json;sys.stdin.readline();print(json.dumps({"type":"execute","case_id":"case"}),flush=True);sys.stdin.readline();print(json.dumps({"type":"runner_report","raw":{"summary":{"collected":0}}}),flush=True)'
    calls = []
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid="tests/a.py::test",
        case_id="case",
        execute=calls.append,
    )
    assert calls == ["case"]
    assert receipt.reason == "zero_collection"
