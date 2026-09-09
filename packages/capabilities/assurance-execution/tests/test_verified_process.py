from __future__ import annotations

import sys
import json
import threading
import time
from pathlib import Path

import pytest

from assurance_execution.operations.verified_process import (
    ProcessLimits,
    SubprocessVerificationHost,
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


@pytest.mark.parametrize("attack", ["closure_report", "crash", "signal", "close_ipc"])
def test_pytest_child_cannot_emit_host_terminal_or_exit_without_installed_result(tmp_path, attack):
    source = """from assurance_execution import bridge
import json, os, signal
def test_case():
    bridge.execute_case("TC_USER_CREATE_001")
    cells = [cell.cell_contents for cell in bridge._request.__closure__ or ()]
    writers = [value for value in cells if hasattr(value, "write") and value.writable()]
"""
    if attack == "closure_report":
        source += """    raw = {"exitcode": 0, "summary": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}, "tests": [{"nodeid": "tests/test_case.py::test_case", "when": stage, "outcome": "passed"} for stage in ("setup", "call", "teardown")]}
    for writer in writers:
        writer.write(json.dumps({"type": "runner_report", "raw": raw}).encode() + b"\\n")
        writer.flush()
    os._exit(0)
"""
    elif attack == "signal":
        source += "    os.kill(os.getpid(), signal.SIGKILL)\n"
    elif attack == "close_ipc":
        source += "    for writer in writers:\n        writer.close()\n    os._exit(0)\n"
    else:
        source += "    os._exit(0)\n"
    receipt, calls = child(tmp_path, source)
    assert calls == ["TC_USER_CREATE_001"]
    assert receipt.report is None
    assert receipt.reason is not None


@pytest.mark.parametrize("trigger", ["timeout", "cancelled"])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_timeout_and_cancel_reap_pytest_child(tmp_path, trigger, ignore_term):
    import os

    pid_path = tmp_path / "pytest-child.pid"
    source = "import os,time,signal\ndef test_case():\n"
    if ignore_term:
        source += "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    source += f'    open({str(pid_path)!r}, "w").write(str(os.getpid()))\n    time.sleep(60)\n'
    receipt, _ = child(
        tmp_path, source, timeout=2, cancel=lambda: trigger == "cancelled" and pid_path.exists()
    )
    assert receipt.reason == trigger
    assert pid_path.is_file()
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("attack", ["report_then_return", "oversize", "shutdown_then_return"])
def test_execute_only_child_ipc_cannot_publish_completion(tmp_path, attack):
    source = """from assurance_execution import bridge
import json, socket
def test_case():
    bridge.execute_case("TC_USER_CREATE_001")
    writer = next(cell.cell_contents for cell in bridge._request.__closure__ if hasattr(cell.cell_contents, "write") and cell.cell_contents.writable())
"""
    if attack == "shutdown_then_return":
        source += "    writer._sock.shutdown(socket.SHUT_RDWR)\n"
    elif attack == "oversize":
        source += '    writer.write(b"x" * (256 * 1024 + 1)); writer.flush()\n'
    else:
        source += '    writer.write(b\'{"type":"runner_report","raw":{}}\\n\'); writer.flush()\n'
    receipt, calls = child(tmp_path, source)
    assert calls == ["TC_USER_CREATE_001"]
    assert receipt.report is None
    assert receipt.reason is not None


def test_pytest_worker_has_no_host_protocol_stdio(tmp_path):
    receipt, calls = child(
        tmp_path,
        """from assurance_execution import bridge
import os, stat
def test_case(capfd):
    with capfd.disabled():
        assert os.read(0, 1) == b""
        assert os.fstat(1).st_ino == os.fstat(2).st_ino
        writer = next(cell.cell_contents for cell in bridge._request.__closure__ if hasattr(cell.cell_contents, "write") and cell.cell_contents.writable())
        assert stat.S_ISSOCK(os.fstat(writer.fileno()).st_mode)
        bridge.execute_case("TC_USER_CREATE_001")
""",
    )
    assert calls == ["TC_USER_CREATE_001"]
    assert receipt.reason is None
    assert receipt.exit_code == 0


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
    receipt = SubprocessVerificationHost(limits=ProcessLimits(timeout_seconds=timeout)).run(
        view=tmp_path,
        nodeid="tests/test_case.py::test_case",
        case_id="TC_USER_CREATE_001",
        container_name="unused",
        execute=lambda case, _: calls.append(case),
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


def test_bridge_and_pytest_do_not_inherit_parent_authority(tmp_path, monkeypatch):
    import os
    import subprocess

    sensitive = {
        "AA_API_TOKEN": "api-token-canary",
        "AA_DB_PATH": "/private/db-canary.sqlite3",
        "AA_MANAGED_AUTHORITY": "authority-secret-canary",
        "AA_SECRET_HANDLES": "secret-handles-canary",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector-canary:4318",
        "AA_EVIDENCE_PATH": "/private/evidence-canary",
        "PYTHONPATH": "/private/python-path-canary",
    }
    for key, value in sensitive.items():
        monkeypatch.setenv(key, value)
    launched = []
    original = subprocess.Popen

    def capture(*args, **kwargs):
        launched.append(kwargs.get("env"))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    receipt, calls = child(
        tmp_path,
        """from assurance_execution.bridge import execute_case
import os
def test_case():
    assert not set(%r).intersection(os.environ)
    execute_case("TC_USER_CREATE_001")
"""
        % list(sensitive),
    )
    assert launched[0] is not None, "bridge must receive an explicit minimal environment"
    assert not set(sensitive).intersection(launched[0])
    assert calls == ["TC_USER_CREATE_001"]
    assert receipt.reason is None and receipt.exit_code == 0
    assert all(os.environ[key] == value for key, value in sensitive.items())


def test_parent_process_loss_closes_bridge_and_reaps_pytest(tmp_path):
    import os
    import subprocess

    view = tmp_path / "attempt/view"
    tests = view / "tests"
    tests.mkdir(parents=True)
    pid_path = view / "pytest.pid"
    (tests / "test_case.py").write_text(
        "import os,time,tempfile\nfrom pathlib import Path\ndef test_case():\n"
        '    Path("pytest.pid").write_text(str(os.getpid()))\n'
        '    Path("temporary-root").write_text(tempfile.gettempdir())\n'
        "    time.sleep(60)\n"
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from assurance_execution.operations.verified_process import SubprocessVerificationHost; "
            "SubprocessVerificationHost().run(view=Path.cwd(), nodeid='tests/test_case.py::test_case', "
            "case_id='case', container_name='unused', execute=lambda *args: None, "
            "cancel_requested=lambda: False)",
        ],
        cwd=view,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_path.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists()
        worker_pid = int(pid_path.read_text())
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("pytest survived the loss of its parent host protocol")
        temporary = Path((view / "temporary-root").read_text())
        assert temporary.is_relative_to(tmp_path / "attempt")
        assert temporary.is_dir()  # A killed owner cannot execute normal finally cleanup.
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        assert owner.stderr is not None
        owner.stderr.close()


def test_explicit_oci_client_keeps_only_its_connection_environment(tmp_path, monkeypatch):
    from assurance_execution.operations.verified_process import DockerVerificationHost

    # A real process stands in for the external CLI; this does not qualify OCI.
    client = tmp_path / "docker"
    client.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "sys.stdin.readline()\nsys.stderr.write(json.dumps(dict(os.environ)))\n"
    )
    client.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "client-home"))
    monkeypatch.setenv("DOCKER_CONTEXT", "test-only-colima-context")
    monkeypatch.setenv("AA_API_TOKEN", "must-not-reach-the-cli")
    host = DockerVerificationHost(source_root=tmp_path, qualification_path=tmp_path / "unused")
    host._image = "sha256:" + "a" * 64
    monkeypatch.setattr(host, "preflight", lambda: {})
    receipt = host.run(
        view=tmp_path,
        nodeid="tests/test_case.py::test_case",
        case_id="case",
        container_name="aa-verify-12345678-1234-4123-8123-123456789abc",
        execute=lambda *args: None,
        cancel_requested=lambda: False,
    )
    environment = json.loads(receipt.stderr)
    assert environment["HOME"] == str(tmp_path / "client-home")
    assert environment["DOCKER_CONTEXT"] == "test-only-colima-context"
    assert "AA_API_TOKEN" not in environment


@pytest.mark.parametrize("ignore_term", [False, True])
def test_killed_supervisor_cannot_leave_its_pytest_process_group(tmp_path, ignore_term):
    import os
    import signal

    pids = tmp_path / "owned-pids.json"
    source = f"""import json, os, signal, time
from pathlib import Path
def test_case():
    signal.signal(signal.SIGTERM, signal.SIG_IGN if {ignore_term!r} else signal.SIG_DFL)
    Path({str(pids)!r}).write_text(json.dumps([os.getpid(), os.getpgrp()]))
    os.kill(os.getppid(), signal.SIGKILL)
    time.sleep(60)
"""
    try:
        receipt, calls = child(tmp_path, source, timeout=2)
        worker_pid, pgid = json.loads(pids.read_text())
        assert receipt.exit_code == -signal.SIGKILL
        assert calls == []
        # The orphan reaper may briefly retain an already-dead worker PID after
        # the process group has vanished. Confirm its final removal as well.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
        assert receipt.cleanup_confirmed
    finally:
        if pids.exists():
            _, pgid = json.loads(pids.read_text())
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("timeout", [False, True])
def test_default_host_keeps_all_temporary_files_in_attempt(tmp_path, monkeypatch, timeout):
    from assurance_execution.operations.verified_process import SubprocessVerificationHost

    attempt = tmp_path / "attempt"
    view = attempt / "view"
    (view / "tests").mkdir(parents=True)
    observed = attempt / "observed.json"
    monkeypatch.setenv("TMPDIR", "/parent-secret-temp-dir")
    monkeypatch.setenv("TMP", "/parent-secret-temp")
    monkeypatch.setenv("TEMP", "/parent-secret-temp")
    (view / "tests/test_case.py").write_text(f"""import json, os, tempfile, time
from pathlib import Path
from assurance_execution.bridge import execute_case
def test_case(tmp_path):
    Path({str(observed)!r}).write_text(json.dumps({{
        "temp": tempfile.gettempdir(), "pytest": str(tmp_path),
        "environment": {{key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP", "PYTHONUNBUFFERED")}}
    }}))
    {"time.sleep(60)" if timeout else 'execute_case("case")'}
""")
    receipt = SubprocessVerificationHost(limits=ProcessLimits(timeout_seconds=2)).run(
        view=view,
        nodeid="tests/test_case.py::test_case",
        case_id="case",
        container_name="unused",
        execute=lambda *args: None,
        cancel_requested=lambda: False,
    )
    snapshot = json.loads(observed.read_text())
    temporary = Path(snapshot["temp"])
    assert temporary.is_relative_to(attempt)
    assert Path(snapshot["pytest"]).is_relative_to(temporary)
    assert snapshot["environment"] == {
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "PYTHONUNBUFFERED": "1",
    }
    assert not temporary.exists()
    assert receipt.reason == ("timeout" if timeout else None)
    assert receipt.cleanup_confirmed
