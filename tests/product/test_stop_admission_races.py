from __future__ import annotations

import os
import signal
import select
import sys
from pathlib import Path

import pytest

from assurance_product import worker_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def current_lifecycle_module(monkeypatch):
    # The session wheel fixture evicts modules imported during collection.
    # Exercise the same canonical lifecycle module as dynamic product imports.
    from assurance_product import worker_lifecycle

    monkeypatch.setitem(globals(), "lifecycle", worker_lifecycle)


def test_linux_stop_binds_handle_before_identity_check(monkeypatch):
    events = []
    identity = {"pid": 123456, "pgid": 1, "created": "old", "boot": "boot", "host": "host"}
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags=0: events.append("open") or 999, raising=False)

    def observe(pid):
        events.append("identity")
        return identity

    monkeypatch.setattr(lifecycle, "process_identity", observe)

    def send(fd, sig, info=None, flags=0):
        events.append("send")
        raise ProcessLookupError()

    monkeypatch.setattr(signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda fd: "send" in events)
    monkeypatch.setattr(os, "close", lambda fd: events.append("close"))
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("numeric signal"))
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("numeric group signal"))
    assert lifecycle._terminate(identity, 0)
    assert events[0] == "open"
    assert events[-1] == "close"


def test_bootstrap_reserves_destination_before_worktree_mutation(tmp_path: Path, monkeypatch):
    from assurance_product import sut_worktree

    destination = tmp_path / "destination"

    @lifecycle.exclusive_bootstrap
    def execute(**kwargs):
        return "done"

    monkeypatch.setattr(sut_worktree, "resolve_run_worktree", lambda *args: destination, raising=False)

    def prepare(*args):
        with pytest.raises(lifecycle.ExecutionConflict):
            # A separate context represents a competing caller.
            import contextvars

            def competitor():
                with lifecycle.acquire_execution(destination, "other"):
                    pytest.fail("admitted during preparation")

            contextvars.Context().run(competitor)
        assert not destination.exists()
        destination.mkdir()
        return destination

    monkeypatch.setattr(sut_worktree, "ensure_run_worktree", prepare)
    assert execute(project_dir=tmp_path, runs_root=tmp_path / "runs", change_id="one") == "done"


@pytest.mark.parametrize("stage", ["creation", "seeding"])
@pytest.mark.parametrize("entry", ["bootstrap", "cli", "reuse", "direct"])
def test_real_preparation_excludes_competing_entrypoints(tmp_path: Path, stage: str, entry: str):
    import subprocess
    import sys

    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "tests@example.com"],
        ["config", "user.name", "Tests"],
        ["commit", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    home = tmp_path / "worktrees"
    home.mkdir()
    home_alias = tmp_path / "worktrees-alias"
    home_alias.symlink_to(home, target_is_directory=True)
    destination = home / source.name / "change"
    destination_alias = home_alias / source.name / "change"
    env = dict(os.environ, AA_SUT_WORKTREE_HOME=str(home))
    preparing_code = """import sys
from pathlib import Path
from assurance_product import sut_worktree
from assurance_product.worker_lifecycle import exclusive_bootstrap
source, stage = Path(sys.argv[1]), sys.argv[2]
def barrier():
 print("paused", flush=True)
 assert sys.stdin.readline().strip() == "continue"
if stage == "seeding":
 original = sut_worktree._seed_worktree_runtime
 def seed(*args):
  barrier()
  original(*args)
 sut_worktree._seed_worktree_runtime = seed
else:
 original = sut_worktree._run_git
 def git(root, *args):
  if args[:2] == ("worktree", "add"):
   barrier()
  return original(root, *args)
 sut_worktree._run_git = git
@exclusive_bootstrap
def execute(**kwargs):
 print("business", flush=True)
execute(project_dir=source, runs_root=source.parent / "runs", change_id="change")
"""
    competitor_code = """import sys
from pathlib import Path
from assurance_product.worker_lifecycle import exclusive_bootstrap, exclusive_cli, acquire_execution
from assurance_product.cli import CommandError
source, destination, entry = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
change = sys.argv[4] if len(sys.argv) > 4 else "change"
def business(**kwargs):
 print("business", flush=True)
try:
 if entry == "direct":
  with acquire_execution(destination, "other"):
   business()
 elif entry == "bootstrap":
  exclusive_bootstrap(business)(project_dir=source, runs_root=source.parent / "runs2", change_id=change)
 else:
  exclusive_cli(business)(project_dir=destination if entry == "reuse" else source, change_id="change", invocation_id="other", reuse_directory=entry == "reuse")
except (RuntimeError, CommandError) as error:
 print("rejected", flush=True)
else:
 print("admitted", flush=True)
"""
    preparing = subprocess.Popen(
        [sys.executable, "-c", preparing_code, str(source), stage],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert preparing.stdout is not None
        assert select.select([preparing.stdout], [], [], 15)[0], "preparation barrier timed out"
        assert preparing.stdout.readline().strip() == "paused"
        exists_before = destination.exists()
        assert exists_before == (stage == "seeding")
        competed = subprocess.run(
            [sys.executable, "-c", competitor_code, str(source), str(destination_alias), entry],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert competed.returncode == 0, competed.stderr
        assert competed.stdout.strip() == "rejected"
        assert destination.exists() == exists_before
        assert not (destination / ".aa").exists()
        independent = subprocess.run(
            [sys.executable, "-c", competitor_code, str(source), str(tmp_path / "independent"), "direct"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert independent.returncode == 0, independent.stderr
        assert independent.stdout.strip() == "business\nadmitted"
        distinct = subprocess.run(
            [
                sys.executable,
                "-c",
                competitor_code,
                str(source),
                str(destination),
                "bootstrap",
                "another-change",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert distinct.returncode == 0, distinct.stderr
        assert distinct.stdout.strip() == "business\nadmitted"
        assert (home / source.name / "another-change" / "opencode.json").is_file()
        out, err = preparing.communicate("continue\n", timeout=15)
        assert preparing.returncode == 0, err
        assert out.strip() == "business"
        assert (destination / "opencode.json").is_file()
    finally:
        if preparing.poll() is None:
            preparing.kill()
        preparing.wait()


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_linux_pidfd_open_errors_never_signal_numeric_pid(monkeypatch, failure):
    identity = {"pid": 123456, "pgid": 1, "created": "old", "boot": "boot", "host": "host"}
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def denied(*args):
        raise failure("denied")

    monkeypatch.setattr(os, "pidfd_open", denied, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: pytest.fail("signal"), raising=False)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("numeric signal"))
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("numeric group signal"))
    with pytest.raises(lifecycle.ExecutionConflict, match="handle unavailable"):
        lifecycle._terminate(identity, 0)


def test_linux_pidfd_identity_replacement_closes_handle_without_signaling(monkeypatch):
    identity = {"pid": 123456, "pgid": 1, "created": "old", "boot": "boot", "host": "host"}
    closed = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda *args: 999, raising=False)
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: dict(identity, created="replacement"))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: pytest.fail("signal"), raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(lifecycle.ExecutionConflict, match="creation identity differs"):
        lifecycle._terminate(identity, 0)
    assert closed == [999]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux pidfds")
def test_native_linux_pidfd_stops_owned_session_with_child(tmp_path: Path):
    import subprocess
    import sys

    code = """import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    child_fd = None
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 15)[0], "child startup timed out"
        child_pid = int(process.stdout.readline())
        child_fd = getattr(os, "pidfd_open")(child_pid, 0)
        identity = lifecycle.process_identity(process.pid)
        assert identity is not None
        assert lifecycle._terminate(identity, 0.1)
        process.wait(timeout=5)
        assert lifecycle.process_identity(child_pid) is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if child_fd is not None:
            try:
                getattr(signal, "pidfd_send_signal")(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(child_fd)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_linux_group_binds_and_freezes_new_children_or_fails_closed(monkeypatch, ambiguous):
    leader = {"pid": 123456, "pgid": 123456, "created": "leader", "boot": "boot", "host": "host"}
    child = dict(leader, pid=123457, created="child")
    newborn = dict(leader, pid=123458, created="newborn")
    identities = {row["pid"]: row for row in (leader, child, newborn)}
    opened = []
    closed = []
    sent = []
    stopped = set()
    exited = set()
    scans = iter(({leader["pid"]: leader, child["pid"]: child}, identities, identities, {}))
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def open_handle(pid, flags):
        opened.append(pid)
        return pid + 100

    monkeypatch.setattr(os, "pidfd_open", open_handle, raising=False)

    def observe(pid):
        assert pid in opened, "identity validated before handle binding"
        return identities[pid]

    monkeypatch.setattr(lifecycle, "process_identity", observe)
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda fd: fd - 100 in exited)
    monkeypatch.setattr(lifecycle, "_linux_stopped", lambda pid: pid in stopped)

    def members(group, **kwargs):
        if ambiguous and child["pid"] in stopped:
            raise lifecycle.ExecutionConflict("enumeration ambiguity")
        return next(scans)

    monkeypatch.setattr(lifecycle, "_linux_group_members", members)

    def send(fd, sig, *args):
        pid = fd - 100
        sent.append((pid, sig))
        if sig == signal.SIGSTOP:
            stopped.add(pid)
        elif sig == signal.SIGCONT:
            stopped.discard(pid)
        elif sig == signal.SIGKILL:
            exited.add(pid)

    monkeypatch.setattr(signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("numeric signal"))
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("numeric group signal"))
    if ambiguous:
        with pytest.raises(lifecycle.ExecutionConflict, match="enumeration ambiguity"):
            lifecycle._terminate(leader, 0)
        assert not stopped
        assert not exited
    else:
        assert lifecycle._terminate(leader, 0)
        assert exited == set(identities)
        for pid in identities:
            assert (pid, signal.SIGSTOP) in sent
            assert (pid, signal.SIGKILL) in sent
    assert set(closed) == {pid + 100 for pid in opened}


def test_linux_missing_group_leader_fails_closed(monkeypatch):
    identity = {"pid": 123456, "pgid": 123456, "created": "old", "boot": "boot", "host": "host"}
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def missing(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "pidfd_open", missing, raising=False)
    monkeypatch.setattr(lifecycle, "_verified_exit", lambda identity: False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: pytest.fail("signal"), raising=False)
    with pytest.raises(lifecycle.ExecutionConflict, match="exited group leader"):
        lifecycle._terminate(identity, 0)


@pytest.mark.parametrize("signal_error", [PermissionError, OSError])
def test_linux_pidfd_signal_failure_closes_handle(monkeypatch, signal_error):
    identity = {"pid": 123456, "pgid": 1, "created": "old", "boot": "boot", "host": "host"}
    closed = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda *args: 999, raising=False)
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: identity)

    def denied(*args):
        raise signal_error("denied")

    monkeypatch.setattr(signal, "pidfd_send_signal", denied, raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(lifecycle.ExecutionConflict, match="signal unavailable"):
        lifecycle._terminate(identity, 0)
    assert closed == [999]


def test_linux_pidfd_unavailable_fails_closed(monkeypatch):
    identity = {"pid": 123456, "pgid": 1}
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("numeric signal"))
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("numeric group signal"))
    with pytest.raises(lifecycle.ExecutionConflict, match="requires native pidfd"):
        lifecycle._terminate(identity, 0)


def test_linux_unconfirmed_freeze_resumes_bound_handle_and_closes(monkeypatch):
    identity = {"pid": 123456, "pgid": 123456, "created": "old", "boot": "boot", "host": "host"}
    sent = []
    closed = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda *args: 999, raising=False)
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: identity)
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda fd: False)
    monkeypatch.setattr(lifecycle, "_linux_stopped", lambda pid: False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)), raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(lifecycle.ExecutionConflict, match="freeze is unconfirmed"):
        lifecycle._terminate(identity, 0)
    assert sent == [(999, signal.SIGSTOP), (999, signal.SIGCONT)]
    assert closed == [999]


def test_linux_missing_handle_uses_natural_exit_proof_without_signal(monkeypatch):
    identity = {"pid": 123456, "pgid": 123456}
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def missing(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "pidfd_open", missing, raising=False)
    monkeypatch.setattr(lifecycle, "_verified_exit", lambda identity: True)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: pytest.fail("signal"), raising=False)
    assert lifecycle._terminate(identity, 0)


def test_fork_cannot_borrow_parent_preparation_reservation(tmp_path: Path):
    import json

    with lifecycle.reserve_preparation(tmp_path / "target"):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                with lifecycle.reserve_preparation(tmp_path / "target"):
                    result = "borrowed"
            except lifecycle.ExecutionConflict:
                result = "rejected"
            os.write(write_fd, json.dumps(result).encode())
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        try:
            assert select.select([read_fd], [], [], 15)[0], "fork reservation test timed out"
            assert json.loads(os.read(read_fd, 100)) == "rejected"
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)


def test_linux_rescan_reused_member_pid_cannot_reuse_exited_handle(monkeypatch):
    leader = {"pid": 123456, "pgid": 123456, "created": "leader", "boot": "boot", "host": "host"}
    child = dict(leader, pid=123457, created="child")
    identities = {leader["pid"]: leader, child["pid"]: child}
    stopped = set()
    exited = set()
    sent = []
    scans = 0
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags: pid + 100, raising=False)
    monkeypatch.setattr(lifecycle, "process_identity", identities.__getitem__)
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda fd: fd - 100 in exited)
    monkeypatch.setattr(lifecycle, "_linux_stopped", lambda pid: pid in stopped)

    def members(group, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            exited.add(child["pid"])
            return {leader["pid"]: leader, child["pid"]: dict(child, created="replacement")}
        return identities

    monkeypatch.setattr(lifecycle, "_linux_group_members", members)

    def send(fd, sig):
        sent.append((fd, sig))
        if sig == signal.SIGSTOP:
            stopped.add(fd - 100)

    monkeypatch.setattr(signal, "pidfd_send_signal", send, raising=False)
    closed = []
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(lifecycle.ExecutionConflict, match="member identity changed"):
        lifecycle._terminate(leader, 0)
    assert all(sig not in {signal.SIGTERM, signal.SIGKILL} for _, sig in sent)
    assert (leader["pid"] + 100, signal.SIGCONT) in sent
    assert set(closed) == {pid + 100 for pid in identities}


def test_linux_group_cannot_freeze_stop_controller_as_member(monkeypatch):
    leader = {"pid": 123456, "pgid": 123456, "created": "leader", "boot": "boot", "host": "host"}
    controller = dict(leader, pid=os.getpid(), created="controller")
    opened = []
    stopped = set()
    sent = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def open_handle(pid, flags):
        opened.append(pid)
        return pid + 100

    monkeypatch.setattr(os, "pidfd_open", open_handle, raising=False)
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: leader)
    monkeypatch.setattr(lifecycle, "_pidfd_exited", lambda fd: False)
    monkeypatch.setattr(lifecycle, "_linux_stopped", lambda pid: pid in stopped)
    monkeypatch.setattr(
        lifecycle, "_linux_group_members", lambda group, **kwargs: {controller["pid"]: controller}
    )

    def send(fd, sig):
        sent.append((fd, sig))
        if sig == signal.SIGSTOP:
            stopped.add(fd - 100)

    monkeypatch.setattr(signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: None)
    with pytest.raises(lifecycle.ExecutionConflict, match="cannot terminate itself"):
        lifecycle._terminate(leader, 0)
    assert opened == [leader["pid"]]
    assert sent == [(leader["pid"] + 100, signal.SIGSTOP), (leader["pid"] + 100, signal.SIGCONT)]


@pytest.mark.parametrize("state,group,terminating", [("Z", 1, False), ("Z", 123456, True)])
def test_linux_group_scan_ignores_unrelated_or_confirmed_dead_members(
    tmp_path: Path, monkeypatch, state, group, terminating
):
    entry = tmp_path / "123457"
    entry.mkdir()
    (entry / "stat").write_text(f"123457 (test) {state} 1 {group} 0 0")
    monkeypatch.setattr(lifecycle, "Path", lambda value: tmp_path if value == "/proc" else Path(value))
    assert lifecycle._linux_group_members(123456, terminating=terminating) == {}


@pytest.mark.parametrize("metadata", ["missing", "owned-zombie"])
def test_linux_group_scan_rejects_incomplete_or_unknown_dead_members(tmp_path: Path, monkeypatch, metadata):
    entry = tmp_path / "123457"
    entry.mkdir()
    if metadata == "owned-zombie":
        (entry / "stat").write_text("123457 (test) Z 1 123456 0 0")
    monkeypatch.setattr(lifecycle, "Path", lambda value: tmp_path if value == "/proc" else Path(value))
    with pytest.raises(lifecycle.ExecutionConflict):
        lifecycle._linux_group_members(123456)


@pytest.mark.parametrize("states,expected", [(("T", "T"), True), (("T", "R"), False)])
def test_linux_freeze_requires_every_thread_stopped(tmp_path: Path, monkeypatch, states, expected):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for tid, state in enumerate(states, start=123456):
        entry = tasks / str(tid)
        entry.mkdir()
        (entry / "stat").write_text(f"{tid} (test) {state} 1 123456 0 0")
    monkeypatch.setattr(
        lifecycle, "Path", lambda value: tasks if value == "/proc/123456/task" else Path(value)
    )
    assert lifecycle._linux_stopped(123456) is expected


@pytest.mark.parametrize("name,alias", [("target", "TARGET"), ("caf\u00e9", "cafe\u0301")])
@pytest.mark.parametrize("visible", [False, True])
def test_preparation_excludes_filesystem_case_and_unicode_aliases(
    tmp_path: Path, name: str, alias: str, visible: bool
):
    import contextvars

    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / name).touch()
    if not (probe / alias).exists() or not (probe / alias).samefile(probe / name):
        pytest.skip("filesystem treats these names as distinct")
    target = tmp_path / name
    alternate = tmp_path / alias
    with lifecycle.reserve_preparation(target):
        if visible:
            target.mkdir()

        def competitor():
            with lifecycle.acquire_execution(alternate, "other"):
                pytest.fail("filesystem alias admitted during preparation")

        with pytest.raises(lifecycle.ExecutionConflict):
            contextvars.Context().run(competitor)
    assert target.exists() is visible
    assert not (target / ".aa").exists()


@pytest.mark.parametrize(
    "name", [".aa-preparation-locks", ".AA-PREPARATION-LOCKS", ".aa-preparation-lock\u017f"]
)
def test_preparation_rejects_reserved_namespace_before_target_creation(tmp_path: Path, name: str):
    target = tmp_path / name
    with pytest.raises(lifecycle.ExecutionConflict, match="reserved preparation infrastructure"):
        with lifecycle.acquire_execution(target, "run"):
            pytest.fail("reserved namespace admitted")
    assert not target.exists()
    assert not (tmp_path / ".aa-preparation-locks").exists()


@pytest.mark.parametrize("shape", ["symlink", "file"])
def test_preparation_namespace_unsafe_shape_fails_closed(tmp_path: Path, shape: str):
    root = tmp_path / ".aa-preparation-locks"
    if shape == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        root.symlink_to(elsewhere, target_is_directory=True)
    else:
        root.touch()
    target = tmp_path / "target"
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(target, "run"):
            pytest.fail("unsafe namespace admitted")
    assert not target.exists()


@pytest.mark.parametrize("name,other", [("target", "TARGET"), ("caf\u00e9", "cafe\u0301")])
def test_preparation_distinct_filesystem_names_admit_independently(tmp_path: Path, name: str, other: str):
    import contextvars

    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / name).touch()
    if (probe / other).exists() and (probe / other).samefile(probe / name):
        pytest.skip("filesystem aliases these names")
    target = tmp_path / name
    independent = tmp_path / other
    with lifecycle.reserve_preparation(target):

        def competitor():
            with lifecycle.acquire_execution(independent, "independent"):
                assert independent.is_dir()

        contextvars.Context().run(competitor)
        assert not target.exists()


def test_preparation_supports_filesystem_maximum_basename(tmp_path: Path):
    target = tmp_path / ("t" * os.pathconf(tmp_path, "PC_NAME_MAX"))
    with lifecycle.reserve_preparation(target):
        with lifecycle.acquire_execution(target, "run"):
            assert target.is_dir()


@pytest.mark.parametrize("name,alias", [("parent", "PARENT"), ("caf\u00e9", "cafe\u0301")])
@pytest.mark.parametrize("visible", [False, True])
def test_preparation_excludes_filesystem_parent_component_aliases(
    tmp_path: Path, name: str, alias: str, visible: bool
):
    import contextvars

    parent = tmp_path / name
    parent.mkdir()
    alternate_parent = tmp_path / alias
    if not alternate_parent.exists() or not alternate_parent.samefile(parent):
        pytest.skip("filesystem treats these parent names as distinct")
    target = parent / "target"
    alternate = alternate_parent / "target"
    with lifecycle.reserve_preparation(target):
        if visible:
            target.mkdir()

        def competitor():
            with lifecycle.acquire_execution(alternate, "other"):
                pytest.fail("parent component alias admitted during preparation")

        with pytest.raises(lifecycle.ExecutionConflict):
            contextvars.Context().run(competitor)
    assert target.exists() is visible
    assert not (target / ".aa").exists()


@pytest.mark.parametrize("shape", ["symlink", "directory", "fifo"])
def test_preparation_lock_unsafe_shape_fails_closed(tmp_path: Path, shape: str):
    root = tmp_path / ".aa-preparation-locks"
    root.mkdir()
    lock = root / "target"
    if shape == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.touch()
        lock.symlink_to(elsewhere)
    elif shape == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock)
    with pytest.raises(lifecycle.ExecutionConflict):
        with lifecycle.acquire_execution(tmp_path / "target", "run"):
            pytest.fail("unsafe lock admitted")
    assert not (tmp_path / "target").exists()
