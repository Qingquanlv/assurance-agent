from __future__ import annotations

import sys
import json
import threading
import time
from pathlib import Path

import pytest

from assurance_execution.operations.verified_process import (
    ProcessLimits,
    container_argv,
    run_bridge_process,
)


@pytest.mark.parametrize(
    "mutation",
    [
        "negative",
        "missing",
        "wrong_node",
        "wrong_exit",
        "inconsistent",
        "trailing",
        "real_exit",
        "duplicate",
        "missing_stage",
        "bad_outcome",
    ],
)
def test_runner_terminal_requires_consistent_selected_pytest_records(tmp_path, mutation):
    node = "tests/t.py::test"
    raw = {
        "exitcode": 0,
        "summary": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0},
        "tests": [
            {"nodeid": node, "when": stage, "outcome": "passed"} for stage in ("setup", "call", "teardown")
        ],
    }
    if mutation == "negative":
        raw["summary"]["collected"] = -1
    elif mutation == "missing":
        raw["tests"] = []
    elif mutation == "wrong_node":
        raw["tests"][1]["nodeid"] = "tests/other.py::test"
    elif mutation == "wrong_exit":
        raw["exitcode"] = 1
    elif mutation == "inconsistent":
        raw["summary"]["failed"] = 1
    elif mutation == "missing_stage":
        raw["tests"].pop()
    elif mutation == "bad_outcome":
        raw["tests"][0]["outcome"] = []
    frames = json.dumps({"type": "runner_report", "raw": raw}) + "\n"
    if mutation == "duplicate":
        frames += frames
    if mutation == "trailing":
        frames += json.dumps({"type": "execute", "case_id": "case"}) + "\n"
    code = (
        'import sys;sys.stdin.readline();print(\'{"type":"execute","case_id":"case"}\',flush=True);sys.stdin.readline();sys.stdout.write('
        + repr(frames)
        + ");sys.stdout.flush()"
    )
    if mutation == "real_exit":
        code += ";sys.exit(1)"
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid=node,
        case_id="case",
        execute=lambda _, control: None,
    )
    assert receipt.reason == "invalid_runner_report"


def test_generated_code_cannot_use_bridge_to_emit_forged_terminal(tmp_path):
    receipt, _ = child(
        tmp_path,
        'from assurance_execution import bridge\nimport os\ndef test_case():\n    bridge.execute_case("TC_USER_CREATE_001")\n    bridge._send({"type":"runner_report","raw":{"exitcode":0,"summary":{"collected":1},"tests":[]}})\n    os._exit(0)\n',
    )
    assert receipt.exit_code != 0
    assert receipt.report is not None
    assert receipt.report["summary"]["failed"] == 1


@pytest.mark.parametrize("trigger", ["cancelled", "stderr_limit", "timeout"])
def test_parent_action_stays_supervised_and_joined(tmp_path, trigger):
    started = threading.Event()
    finished = threading.Event()
    cleanup_calls = []

    def execute(_, control):
        started.set()
        while not control.stopped:
            time.sleep(0.005)
        finished.set()

    code = 'import sys,time;sys.stdin.readline();print(\'{"type":"execute","case_id":"case"}\',flush=True);time.sleep(.1);'
    code += 'sys.stderr.write("x"*2048);sys.stderr.flush();' if trigger == "stderr_limit" else ""
    code += "sys.stdin.readline()"
    before = time.monotonic()
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid="tests/t.py::test",
        case_id="case",
        execute=execute,
        limits=ProcessLimits(timeout_seconds=0.5, max_stderr_bytes=1024),
        cancel_requested=lambda: trigger == "cancelled" and started.is_set(),
        cleanup=lambda: cleanup_calls.append(True) or True,
    )
    assert receipt.reason == trigger
    assert finished.is_set()
    assert time.monotonic() - before < 2
    assert cleanup_calls == [True]


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
        execute=lambda case, _: calls.append(case),
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
        execute=lambda _, control: pytest.fail("must not execute"),
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
        execute=lambda case, _: calls.append(case),
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
        execute=lambda _, control: None,
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
        execute=lambda _, control: None,
        limits=ProcessLimits(max_stderr_bytes=1024),
    )
    assert receipt.reason == "stderr_limit"
    assert len(receipt.stderr) == 1024


def test_zero_collection_report_cannot_complete_even_after_action(tmp_path):
    code = 'import sys,json;sys.stdin.readline();print(json.dumps({"type":"execute","case_id":"case"}),flush=True);sys.stdin.readline();print(json.dumps({"type":"runner_report","raw":{"exitcode":5,"summary":{"collected":0,"passed":0,"failed":0,"skipped":0},"tests":[]}}),flush=True);sys.exit(5)'
    calls = []
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid="tests/a.py::test",
        case_id="case",
        execute=lambda case, _: calls.append(case),
    )
    assert calls == ["case"]
    assert receipt.reason == "zero_collection"
