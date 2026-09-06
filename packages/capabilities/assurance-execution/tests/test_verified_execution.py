from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_execution.contracts.verification import VerificationEvidenceV1

import httpx
import pytest

from assurance_execution.operations.verified_execution import ActionJournal, execute_frozen_action
from assurance_execution.operations.verification_manifest import build_verification_manifest
from assurance_generation.contracts.execution_plan import CasePlanContextV1
from assurance_generation.operations.execution_plan import compile_case_plan
from assurance_intake.contracts.verification import AssertionSourcesV1
from graph_engine.attempts import AttemptKey, BusinessActivation
from tests.verification_support import read_fixture

REPO = Path(__file__).resolve().parents[4]
_ACTIVITY_OWNERS: list[Any] = []


@pytest.mark.parametrize("record", ["action_terminal", "process_terminal", "outcome"])
def test_journal_partial_write_never_publishes_terminal(tmp_path, managed_sut, monkeypatch, record):
    import os

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "atomic-" + record)
    journal.write("action_started", {"state": "started"})
    write = os.write
    calls = 0

    def partial(fd, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("power cut")
        return write(fd, value[:7])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", partial)
        with pytest.raises(KeyboardInterrupt):
            journal.write(record, {"state": "complete"})
    assert not (journal.root / (record + ".json")).exists()
    assert journal.read("action_started") == {"state": "started"}
    journal.write(record, {"state": "complete"})
    journal.write(record, {"state": "complete"})
    assert journal.read(record) == {"state": "complete"}
    with pytest.raises((ValueError, FileExistsError)):
        journal.write(record, {"state": "different"})


def test_unconfirmed_cleanup_retains_nonterminal_and_retries(managed_sut):
    from graph_engine.attempts.activity import TaskActivityIndeterminate
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    class CleanupHost(RealPipeHost):
        confirmed = False
        stops = 0

        def run(self, **kwargs):
            return super().run(**kwargs).model_copy(update={"cleanup_confirmed": False})

        def stop(self, container_name) -> bool:
            self.stops += 1
            return self.confirmed

    request, context, manifest, _ = handler_case(managed_sut, "cleanup-recovery")
    host = CleanupHost()
    handler = VerifiedExecutionHandler(process_host=host)
    with pytest.raises(TaskActivityIndeterminate, match="cleanup"):
        asyncio.run(handler.execute(request, context))
    root = context.write_root / manifest.evidence_root
    assert (root / "action_terminal.json").is_file()
    assert (root / "process_terminal.json").is_file()
    assert not (root / "outcome.json").exists()
    assert context.activity is not None
    for _ in range(2):
        result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
        assert result.status == "indeterminate"
        assert not (root / "outcome.json").exists()
    host.confirmed = True
    result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert result.status == "terminal"
    assert host.stops == 3
    host.confirmed = False
    assert (
        asyncio.run(handler.reconcile(request, context, context.activity.snapshot)).status == "indeterminate"
    )


@pytest.mark.parametrize("record", ["action_terminal", "process_terminal", "outcome"])
def test_journal_crash_after_publish_leaves_complete_authenticated_record(
    tmp_path, managed_sut, monkeypatch, record
):
    from assurance_execution.operations import verified_execution

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "published-" + record)
    publish = verified_execution._publish_exclusive

    def crash(source, destination):
        publish(source, destination)
        raise KeyboardInterrupt("after atomic publication")

    with monkeypatch.context() as patch:
        patch.setattr(verified_execution, "_publish_exclusive", crash)
        with pytest.raises(KeyboardInterrupt):
            journal.write(record, {"complete": True})
    assert journal.read(record) == {"complete": True}
    assert (journal.root / (record + ".json")).stat().st_nlink == 1


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_journal_rejects_link_attacks_and_exclusive_action_replay(tmp_path, managed_sut, attack):
    import os

    _, _, journal, _ = setup_action(tmp_path, managed_sut, "links-" + attack)
    journal.write("action_started", {"started": True})
    with pytest.raises(FileExistsError):
        journal.write("action_started", {"started": True})
    source = journal.root / "action_started.json"
    if attack == "symlink":
        source.rename(tmp_path / "target")
        source.symlink_to(tmp_path / "target")
    else:
        os.link(source, tmp_path / "target")
    with pytest.raises(ValueError, match="nonregular"):
        journal.read("action_started")


def test_execute_near_process_deadline_never_posts(tmp_path, managed_sut):
    import sys
    from assurance_execution.operations.verified_process import run_bridge_process, ProcessLimits
    from assurance_execution.operations.sqlite_oracle import observe_user

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "near-deadline")
    code = 'import sys,time;sys.stdin.readline();time.sleep(.2);print(\'{"type":"execute","case_id":"case"}\',flush=True);sys.stdin.readline()'
    receipt = run_bridge_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        nodeid=manifest.nodeid,
        case_id="case",
        limits=ProcessLimits(timeout_seconds=1),
        execute=lambda _, control: execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "parent-password"}).encode(),
            control,
        ),
    )
    assert receipt.reason == "parent_execution_error"
    assert receipt.cleanup_confirmed
    assert journal.read("action_started") is None
    assert (
        observe_user(Path(manifest.sqlite.path), manifest.inputs.username, manifest.inputs.email)["rows"]
        == []
    )


def test_cancellation_during_real_http_stops_worker_and_never_replays(tmp_path, managed_sut):
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1
    from assurance_execution.operations.verified_process import run_bridge_process

    plan, manifest, _, token = setup_action(tmp_path, managed_sut, "cancel-http")
    requested = threading.Event()
    release = threading.Event()
    posts = []

    class SlowPeer(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            requested.set()
            release.wait(3)

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "cancel-evidence", manifest, b"host-key" * 4)
    credential = json.dumps({"token": token, "user_password": "parent-password"}).encode()
    code = 'import sys;sys.stdin.readline();print(\'{"type":"execute","case_id":"case"}\',flush=True);sys.stdin.readline()'
    before = time.monotonic()
    cleanup = []
    try:
        receipt = run_bridge_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            nodeid=manifest.nodeid,
            case_id="case",
            execute=lambda _, control: execute_frozen_action(plan, manifest, journal, credential, control),
            cancel_requested=requested.is_set,
            cleanup=lambda: cleanup.append(True) or True,
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert time.monotonic() - before < 2
    assert receipt.reason == "cancelled"
    assert receipt.cleanup_confirmed and cleanup == [True]
    terminal = journal.read("action_terminal")
    assert terminal is not None and terminal["http"]["state"] == "timeout"
    assert terminal["oracle"]["state"] == "skipped"
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(plan, manifest, journal, credential)
    assert posts == ["/api/v1/user/create"]
    assert not any(item.name.startswith("verified-action") for item in threading.enumerate())


def formal_plan(profile: Literal["api_db.v1", "api_db_trace.v1"] = "api_db.v1"):
    raw = read_fixture("user-plan.json")
    return compile_case_plan(
        read_fixture("user-case.json"),
        AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
        cast(dict[str, object], raw["bindings"]),
        profile,
        context=CasePlanContextV1.model_validate(raw["context"]),
    )


@pytest.fixture(scope="module")
def managed_sut(tmp_path_factory):
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    workspace = tmp_path_factory.mktemp("task4-managed")
    harness = ManagedUserSutHost(
        source_root=REPO,
        secret_port=Secrets(
            {
                "managed-sut.admin-password": b"task4-host-only-canary",
                "managed-sut.reset-password": b"task4-host-only-canary",
                "managed-sut.secret-key": b"task4-host-only-canary",
            }
        ),
    )
    prepared = harness.prepare(
        workspace_root=workspace, project_dir=workspace / "project", run_root=workspace / "run"
    )
    started = harness.start(workspace_root=workspace, prepare_receipt=workspace / "run/harness-prepare.json")
    try:
        response = httpx.post(
            started["base_url"] + "/api/v1/base/access_token",
            json={"username": "admin", "password": "task4-host-only-canary"},
            trust_env=False,
        )
        response.raise_for_status()
        token = response.json()["data"]["access_token"]
        yield workspace, prepared, started, token
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=workspace / "run/owned-process.json",
            instance_id=started["instance_id"],
        )
        for owner in _ACTIVITY_OWNERS:
            owner.loop.call_soon_threadsafe(owner.loop.stop)
            owner.thread.join(timeout=2)
            owner.loop.close()
        _ACTIVITY_OWNERS.clear()


def setup_action(tmp_path: Path, managed_sut, suffix: str):
    _, prepared, started, token = managed_sut
    plan = formal_plan()
    from assurance_execution.operations.verification_manifest import allocate_user_inputs
    from assurance_execution.operations.sqlite_oracle import observe_user

    inputs = allocate_user_inputs(
        lambda username, email: bool(observe_user(Path(prepared["sqlite_path"]), username, email)["rows"]),
        token_factory=lambda: hashlib.sha256(suffix.encode()).hexdigest()[:12],
    )
    manifest = build_verification_manifest(
        change_id=plan.change_id,
        case_id=plan.case_id,
        nodeid="tests/test_case.py::test_case",
        invocation_id="invocation",
        task_id="task",
        graph_instance_id="graph",
        attempt_key=AttemptKey(digest=hashlib.sha256(suffix.encode()).hexdigest()),
        business_activation=BusinessActivation.for_trigger("execution"),
        coverage_epoch=0,
        repair_round=0,
        authorization_scope_digest="a" * 64,
        activity_receipt_digest="b" * 64,
        plan_ref=plan.plan_ref.path,
        plan_digest=plan.plan_digest,
        case_execution_plan_ref="plans/case.json",
        case_execution_plan_digest="c" * 64,
        spec_digest=plan.spec_digest,
        mapping_digest="d" * 64,
        sut_digest=plan.sut_digest,
        technical_config_digest=plan.technical_config_digest,
        validation_profile="api_db.v1",
        sut_base_url=started["base_url"],
        sut_instance_id=started["instance_id"],
        sut_sqlite_path=Path(prepared["sqlite_path"]),
        sqlite_path=Path(prepared["sqlite_path"]),
        username=inputs.username,
        email=inputs.email,
        evidence_root=f"qa/changes/{plan.change_id}/execution",
    )
    journal = ActionJournal(tmp_path / "evidence", manifest, b"host-key" * 4)
    return plan, manifest, journal, token


def test_real_parent_http_then_new_sqlite_observer(tmp_path, managed_sut):
    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "created")
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
    )
    started = journal.read("action_started")
    terminal = journal.read("action_terminal")
    assert started is not None and terminal is not None
    assert started["execution_id"] == manifest.execution_id
    assert terminal["http"]["status"] == 200
    assert terminal["http"]["code"] == 200
    assert terminal["initial"]["rows"] == []
    assert terminal["oracle"]["rows"] == [manifest.inputs.model_dump(mode="json")]
    assert token not in json.dumps(terminal)
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )


def test_journal_drift_is_rejected(tmp_path, managed_sut):
    _, _, journal, _ = setup_action(tmp_path, managed_sut, "drift")
    journal.write("action_started", {"state": "started"})
    path = tmp_path / "evidence/action_started.json"
    path.chmod(0o600)
    value = json.loads(path.read_text())
    value["payload"]["state"] = "finished"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="authentication"):
        journal.read("action_started")


def test_http_return_before_terminal_cut_never_resends(tmp_path, managed_sut, monkeypatch):
    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "cut")
    original_write = journal.write

    def interrupted(name: str, payload: dict[str, Any]):
        if name == "action_terminal":
            raise KeyboardInterrupt("crash after HTTP")
        return original_write(name, payload)

    monkeypatch.setattr(journal, "write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )
    assert journal.read("action_started") is not None
    assert journal.read("action_terminal") is None
    with pytest.raises(ValueError, match="already started"):
        execute_frozen_action(
            plan,
            manifest,
            journal,
            json.dumps({"token": token, "user_password": "host-only-created-password"}).encode(),
        )


def test_recoverable_handler_uses_production_activity_protocol():
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler
    from graph_engine.plugin_api import RecoverableTaskHandler

    assert isinstance(VerifiedExecutionHandler(), RecoverableTaskHandler)


class Activity:
    """Use the production journal-backed activity port with a test fence owner."""

    def __init__(self, identity, request):
        import threading
        from graph_engine.attempts.activity import JournalBackedTaskActivityPort
        from graph_engine.attempts.events import AttemptOpened, ResourcesAuthorized, ActivityPrepared
        from graph_engine.attempts.host_protocol import TaskActivityRpcIdentity, current_bound_identity
        from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
        from graph_engine.canonical import canonical_digest

        loop = asyncio.new_event_loop()
        self.loop = loop
        self.thread = threading.Thread(target=loop.run_forever, daemon=True)
        self.thread.start()
        _ACTIVITY_OWNERS.append(self)
        journal = MemoryAttemptJournal()
        key = AttemptKey(digest=identity.task_id)

        async def seed():
            await journal.append(
                key,
                (
                    AttemptOpened(
                        contract_digest="d" * 64,
                        input_digest="e" * 64,
                        graph_revision="c" * 64,
                        invocation_id=request.invocation_id,
                        public_entrypoint="full",
                        semantic_node_id=request.node_id,
                    ),
                    ResourcesAuthorized(authorization_id="b" * 64),
                    ActivityPrepared(activity_id="activity"),
                ),
                expected_revision=0,
                fencing_token=1,
            )

        asyncio.run_coroutine_threadsafe(seed(), loop).result(timeout=5)
        bound = current_bound_identity(
            attempt_key_digest=key.digest,
            authorization_id="b" * 64,
            workspace_identity_digest=identity.identity_digest,
            request_digest=canonical_digest(request.model_dump(mode="json")),
            graph_revision="c" * 64,
            product_lock_digest=request.invocation.lock_digest,
            handler_id=request.capability_id,
        )
        rpc = TaskActivityRpcIdentity.model_validate(
            dict(
                bound,
                invocation_id=request.invocation_id,
                task_id=request.task_id,
                activation_id="activation",
                attempt=1,
                activity_id="activity",
            )
        )

        async def fence():
            return None

        self.port = JournalBackedTaskActivityPort(
            journal=journal,
            attempt_key=key,
            identity=rpc,
            workspace_identity=identity,
            assert_live_fence=fence,
            owner_loop=loop,
            remaining_deadline=5,
        )

    @property
    def snapshot(self):
        return self.port.snapshot

    def mark_dispatch_started(self, fingerprint):
        return self.port.mark_dispatch_started(fingerprint)

    def bind(self, reference):
        return self.port.bind(reference)


class Secrets:
    def __init__(self, values):
        self.values = values

    def resolve(self, handle):
        return self.values[handle]


class RealPipeHost:
    """Exercises real installed pytest transport; it does not qualify OCI isolation."""

    def preflight(self):
        return {"transport": "real-pipe-contract-test"}

    def run(self, *, view, nodeid, case_id, container_name, execute, cancel_requested):
        import sys
        from assurance_execution.operations.verified_process import run_bridge_process

        return run_bridge_process(
            [sys.executable, "-m", "assurance_execution.bridge_runner"],
            cwd=view,
            nodeid=nodeid,
            case_id=case_id,
            execute=execute,
            cancel_requested=cancel_requested,
        )

    def stop(self, container_name) -> bool:
        return True


def handler_case(managed_sut, suffix, source=None, store=None):
    from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
    from assurance_execution.operations.verified_execution import VerifiedExecutionInputV1
    from assurance_execution.operations.verification_manifest import build_managed_sut_authority
    from assurance_execution.execution_view import build_execution_view
    from assurance_execution.generated_merge import GeneratedFileV2, MergedGeneratedSet, staged_generated_path
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
    from graph_engine.canonical import canonical_digest
    from graph_engine.plugin_api import InvocationMetadata, TaskWorkspaceIdentity, TaskContext, TaskRequest

    workspace, prepared, started, token = managed_sut
    project = workspace
    write_root = project / suffix
    plan, manifest, _, _ = setup_action(write_root, managed_sut, suffix)
    raw = {
        "task_id": manifest.attempt_key.digest,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(**raw, identity_digest=canonical_digest(raw))
    if store is not None:
        from assurance_execution.execution_view import execution_view_relative

        binding = store.begin(
            task_id=manifest.attempt_key.digest,
            attempt=1,
            output_paths=(
                manifest.evidence_root,
                execution_view_relative(manifest.change_id, suffix, manifest.execution_id),
                "case-plan.json",
                "manifest.json",
            ),
        )
        identity, write_root = binding.identity, binding.write_root
    activity_digest = canonical_digest(
        {
            "attempt_key": identity.task_id,
            "invocation_id": "invocation",
            "task_id": "task",
            "graph_instance_id": "graph",
            "node_id": "execution.execute",
            "workspace_identity_digest": identity.identity_digest,
        }
    )
    plan_ref_path = write_root / "case-plan.json"
    plan_bytes = CaseExecutionPlanSetV1(change_id=plan.change_id, cases=(plan,)).model_dump_json().encode()
    plan_ref_path.write_bytes(plan_bytes)
    manifest = manifest.model_copy(
        update={
            "authorization_scope_digest": identity.identity_digest,
            "activity_receipt_digest": activity_digest,
            "coverage_epoch": plan.coverage_epoch,
            "case_execution_plan_ref": plan_ref_path.relative_to(project).as_posix(),
            "case_execution_plan_digest": hashlib.sha256(plan_bytes).hexdigest(),
        }
    )
    manifest_path = write_root / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    authority_token = project / "run/.ownership-token"
    token_stat = authority_token.stat()

    def ref(path):
        return {
            "path": path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    authority = build_managed_sut_authority(
        run_root=project / "run",
        ownership_token_path=authority_token,
        ownership_token_device=token_stat.st_dev,
        ownership_token_inode=token_stat.st_ino,
        ownership_token_digest="sha256:" + hashlib.sha256(authority_token.read_bytes()).hexdigest(),
        prepare_receipt_digest=ref(project / "run/harness-prepare.json")["digest"],
        start_receipt_digest=ref(project / "run/owned-process.json")["digest"],
        authorization_scope_digest=identity.identity_digest,
        activity_receipt_digest=activity_digest,
    )
    profile = VerifiedExecutionPrepareV1(
        validation_profile="api_db.v1",
        case_execution_plan_ref=EvidenceArtifactRefV1.model_validate(ref(plan_ref_path)),
        nodeid=manifest.nodeid,
        business_activation=manifest.business_activation,
        sut_instance_id=started["instance_id"],
        sut_base_url=started["base_url"],
        managed_sqlite_path=prepared["sqlite_path"],
        observer_sqlite_path=prepared["sqlite_path"],
        user_inputs=manifest.inputs,
        managed_sut_prepare_receipt_ref=EvidenceArtifactRefV1.model_validate(
            ref(project / "run/harness-prepare.json")
        ),
        managed_sut_start_receipt_ref=EvidenceArtifactRefV1.model_validate(
            ref(project / "run/owned-process.json")
        ),
        managed_sut_authority_handle="sut-authority",
    )
    test_source = (
        source
        or 'from assurance_execution.bridge import execute_case\ndef test_case():\n    execute_case("TC_USER_CREATE_001")\n'
    )
    target = "tests/test_case.py"
    staged = staged_generated_path(manifest.change_id, "api", target)
    staged_path = project / staged
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text(test_source)
    item = GeneratedFileV2(
        target_path=target,
        staged_path=staged,
        sha256="sha256:" + hashlib.sha256(test_source.encode()).hexdigest(),
        mode=0o644,
        operation="generated",
        family="api",
    )
    merged = MergedGeneratedSet(files=(item,), digest=canonical_digest([item.model_dump(mode="json")]))
    view = build_execution_view(
        project,
        write_root=write_root,
        request=merged.execution_view_input(
            change_id=manifest.change_id,
            batch_id=suffix,
            execution_id=manifest.execution_id,
            selected=(manifest.nodeid,),
        ),
    )
    payload = VerifiedExecutionInputV1(
        manifest_ref=EvidenceArtifactRefV1.model_validate(ref(manifest_path)), verification=profile, view=view
    )
    invocation = InvocationMetadata(
        invocation_id="invocation", lock_digest="a" * 64, composition_digest="b" * 64, entrypoint="full"
    )
    request = TaskRequest(
        invocation_id="invocation",
        task_id="task",
        graph_instance_id="graph",
        node_id="execution.execute",
        capability_id="assurance.execution.run-tests",
        invocation=invocation,
        attempt=1,
        input=payload.model_dump(mode="json"),
    )
    context = TaskContext(
        project_root=project,
        write_root=write_root,
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
        activity=Activity(identity, request),
        secrets=Secrets(
            {
                "sut-authority": authority.model_dump_json().encode(),
                plan.action.credential_ref: json.dumps(
                    {"token": token, "user_password": "host-password"}
                ).encode(),
            }
        ),
    )
    return request, context, manifest, plan


def test_full_parent_handler_real_pipe_http_sqlite_and_recovery(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, manifest, _ = handler_case(managed_sut, "handler")
    assert context.activity is not None
    handler = VerifiedExecutionHandler(process_host=RealPipeHost())
    first = asyncio.run(handler.execute(request, context))
    evidence = VerificationEvidenceV1.model_validate(first.output)
    assert evidence.state == "collected"
    observations = {item.obligation_id: item.actual for item in evidence.observations}
    assert observations["user.dept_id"] is None
    assert observations["user.username"] == manifest.inputs.username
    recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert recovered.status == "terminal"
    assert recovered.outcome == first
    for path in (context.write_root / manifest.evidence_root).rglob("*.json"):
        assert "host-password" not in path.read_text()
        assert managed_sut[3] not in path.read_text()


@pytest.mark.parametrize(
    "cut",
    [
        "before_dispatch",
        "dispatch_before_action",
        "http_before_terminal",
        "before_seal",
        "after_promotion",
        "partial_action_terminal",
        "partial_process_terminal",
        "partial_outcome",
    ],
)
def test_production_handler_recovery_cuts_never_repeat_post(managed_sut, monkeypatch, cut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    store = None
    if cut == "after_promotion":
        from graph_engine.attempts.workspace import TaskWorkspaceStore

        root = managed_sut[0]
        store = TaskWorkspaceStore(root, root / "promotion-attempts", root / "promotion-receipts")
    request, context, manifest, _ = handler_case(managed_sut, "cut_" + cut, store=store)
    assert context.activity is not None
    handler = VerifiedExecutionHandler(process_host=RealPipeHost())
    original = ActionJournal.write
    posts = []
    from assurance_execution.operations import verified_execution

    original_post = verified_execution._post

    async def count_post(*args):
        posts.append(1)
        return await original_post(*args)

    monkeypatch.setattr(verified_execution, "_post", count_post)
    if cut == "before_dispatch":
        result = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
        assert result.status == "not_dispatched"
        assert posts == []
        return
    original_bind = context.activity.bind
    if cut == "dispatch_before_action":

        def crash_bind(reference):
            original_bind(reference)
            raise KeyboardInterrupt("cut after dispatch")

        monkeypatch.setattr(context.activity, "bind", crash_bind)
    else:
        record = {
            "http_before_terminal": "action_terminal",
            "before_seal": "outcome",
            "after_promotion": "never",
            "partial_action_terminal": "action_terminal",
            "partial_process_terminal": "process_terminal",
            "partial_outcome": "outcome",
        }[cut]

        def crash_write(self, name, payload):
            if name == record:
                if cut.startswith("partial_"):
                    import os

                    write = os.write
                    calls = 0

                    def partial(fd, value):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise KeyboardInterrupt("cut after partial temporary write")
                        return write(fd, value[:7])

                    with monkeypatch.context() as patch:
                        patch.setattr(os, "write", partial)
                        return original(self, name, payload)
                raise KeyboardInterrupt("cut")
            return original(self, name, payload)

        monkeypatch.setattr(ActionJournal, "write", crash_write)
    if cut != "after_promotion":
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(handler.execute(request, context))
    else:
        asyncio.run(handler.execute(request, context))
    monkeypatch.setattr(ActionJournal, "write", original)
    if cut == "dispatch_before_action":
        monkeypatch.setattr(context.activity, "bind", original_bind)
    if store is not None:
        staged = store.seal(context.workspace_identity)
        promoted = store.promote(context.workspace_identity, staged)
        assert (context.project_root / manifest.evidence_root / "outcome.json").is_file()
        assert store.promote(context.workspace_identity, staged) == promoted
    promoted_files = (
        {
            str(path): path.read_bytes()
            for path in (context.project_root / manifest.evidence_root).rglob("*")
            if path.is_file()
        }
        if store is not None
        else None
    )
    recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert recovered.status == "terminal"
    assert recovered.outcome is not None
    assert VerificationEvidenceV1.model_validate(recovered.outcome.output).state == (
        "collected" if cut in {"before_seal", "after_promotion", "partial_outcome"} else "incomplete"
    )
    again = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert again.outcome == recovered.outcome
    assert len(posts) == (0 if cut == "dispatch_before_action" else 1)
    if store is not None:
        assert {
            str(path): path.read_bytes()
            for path in (context.project_root / manifest.evidence_root).rglob("*")
            if path.is_file()
        } == promoted_files
        store.close()


def test_managed_lifecycle_rejects_harness_source_drift(tmp_path):
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    host = ManagedUserSutHost(source_root=tmp_path, secret_port=Secrets({}))
    with pytest.raises(ValueError, match="NOT_READY"):
        host.prepare(workspace_root=tmp_path, project_dir=tmp_path / "project", run_root=tmp_path / "run")


def test_unbridged_pytest_pass_is_incomplete_and_secrets_never_leak(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, manifest, _ = handler_case(
        managed_sut, "no_bridge", "def test_case():\n    assert True\n"
    )
    outcome = asyncio.run(VerifiedExecutionHandler(process_host=RealPipeHost()).execute(request, context))
    assert VerificationEvidenceV1.model_validate(outcome.output).state == "incomplete"
    root = context.write_root / manifest.evidence_root
    assert not (root / "action_started.json").exists()
    for path in root.rglob("*.json"):
        contents = path.read_text()
        assert "task4-host-only-canary" not in contents
        assert "host-password" not in contents
        assert managed_sut[3] not in contents


def test_recovery_rejects_unauthenticated_activity(managed_sut):
    from dataclasses import replace
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, _, _ = handler_case(managed_sut, "no_history")
    assert context.activity is not None
    activity = context.activity.snapshot
    result = asyncio.run(
        VerifiedExecutionHandler(process_host=RealPipeHost()).reconcile(
            request, replace(context, activity=None), activity
        )
    )
    assert result.status == "indeterminate"


def test_http_redirect_is_never_followed_and_started_is_durable(tmp_path, managed_sut):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1

    plan, manifest, journal, token = setup_action(tmp_path, managed_sut, "redirect")
    calls = []

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            assert journal.read("action_started") is not None
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["username"] == manifest.inputs.username
            assert body["password"] == "parent-password"
            self.send_response(302)
            self.send_header("Location", "/unexpected-second-request")
            self.end_headers()

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "redirect-evidence", manifest, b"host-key" * 4)
    try:
        execute_frozen_action(
            plan, manifest, journal, json.dumps({"token": token, "user_password": "parent-password"}).encode()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["status"] == 302
    assert calls == ["/api/v1/user/create"]


def test_http_total_deadline_bounds_a_continuously_streaming_peer(tmp_path, managed_sut):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assurance_execution.contracts.verification import ManagedSutV1

    plan, manifest, _, token = setup_action(tmp_path, managed_sut, "stream")
    calls = []

    class SlowStream(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(140):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = manifest.model_copy(
        update={
            "sut": ManagedSutV1(
                instance_id=manifest.sut.instance_id,
                base_url=f"http://127.0.0.1:{server.server_port}",
                sqlite_path=manifest.sqlite.path,
            )
        }
    )
    journal = ActionJournal(tmp_path / "stream-evidence", manifest, b"host-key" * 4)
    before = time.monotonic()
    try:
        execute_frozen_action(
            plan, manifest, journal, json.dumps({"token": token, "user_password": "parent-password"}).encode()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert time.monotonic() - before < 12
    terminal = journal.read("action_terminal")
    assert terminal is not None
    assert terminal["http"]["state"] == "timeout"
    assert calls == ["/api/v1/user/create"]


def test_production_handler_without_qualified_runner_is_not_ready(managed_sut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    request, context, manifest, _ = handler_case(managed_sut, "not_ready")
    outcome = asyncio.run(VerifiedExecutionHandler().execute(request, context))
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.retryable is False
    assert "NOT_READY" in outcome.failure.message
    assert not (context.write_root / manifest.evidence_root / "action_started.json").exists()


@pytest.mark.parametrize("degradation", ["expired", "stopped"])
@pytest.mark.parametrize("method", ["reconcile", "cancel"])
def test_actual_semantic_handler_recovers_without_live_collector(
    managed_sut, tmp_path, monkeypatch, degradation, method
):
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.verified_attempt import VerifiedAttemptHandler
    from assurance_execution.operations.verified_execution import (
        VerifiedExecutionHandler,
        VerifiedExecutionInputV1,
    )
    from assurance_execution.operations import verified_attempt, verified_execution
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
    from tests.product.test_verified_readiness import collector_listener

    # Use the actual test listener fixture generator with this test's retained identity.
    listener = collector_listener(tmp_path)
    collector, process = next(listener)
    try:
        base, context, manifest, plan = handler_case(managed_sut, "semantic_" + degradation + method)
        assert isinstance(context.secrets, Secrets)
        secrets = context.secrets
        payload = VerifiedExecutionInputV1.model_validate(base.input)
        plan = formal_plan("api_db_trace.v1")
        plan_bytes = (
            CaseExecutionPlanSetV1(change_id=plan.change_id, cases=(plan,)).model_dump_json().encode()
        )
        (context.project_root / payload.verification.case_execution_plan_ref.path).write_bytes(plan_bytes)
        plan_ref = payload.verification.case_execution_plan_ref.model_copy(
            update={"digest": hashlib.sha256(plan_bytes).hexdigest()}
        )
        profile = payload.verification.model_copy(
            update={"validation_profile": "api_db_trace.v1", "case_execution_plan_ref": plan_ref}
        )
        manifest = manifest.model_copy(
            update={"validation_profile": "api_db_trace.v1", "case_execution_plan_digest": plan_ref.digest}
        )
        manifest_bytes = manifest.model_dump_json().encode()
        (context.project_root / payload.manifest_ref.path).write_bytes(manifest_bytes)
        payload = payload.model_copy(
            update={
                "verification": profile,
                "manifest_ref": payload.manifest_ref.model_copy(
                    update={"digest": hashlib.sha256(manifest_bytes).hexdigest()}
                ),
            }
        )
        selected = ManagedSutReadinessSelectionV1(
            workspace_root=str(context.project_root),
            verification=profile,
            configuration_digest="c" * 64,
            execution_id=manifest.execution_id,
            authorization_scope_digest=manifest.authorization_scope_digest,
            activity_receipt_digest=manifest.activity_receipt_digest,
        )
        collector.update(
            {
                "sut_instance_id": manifest.sut.instance_id,
                "execution_id": manifest.execution_id,
                "authorization_scope_digest": manifest.authorization_scope_digest,
                "activity_receipt_digest": manifest.activity_receipt_digest,
            }
        )
        response = json.dumps(
            {"probe_nonce": collector["probe_nonce"], "execution_id": manifest.execution_id}
        ).encode()
        (tmp_path / "response.json").write_bytes(response)
        collector["endpoint_response_digest"] = hashlib.sha256(response).hexdigest()
        secrets.values.update(
            {
                "sut.selection": selected.model_dump_json().encode(),
                "sut.collector": json.dumps(collector).encode(),
            }
        )
        root = ExecutionPrepareInputV1(
            change_id=plan.change_id,
            plan_ref=plan.plan_ref,
            plan_digest=plan.plan_digest,
            selected_test_families=("api",),
            capability_leafs=(),
            validation_profile="api_db_trace.v1",
            verification_config_digest="c" * 64,
            verification=profile,
        )
        qualification = tmp_path / "qualification.json"
        qualification.write_text("test-only real pipe qualification")
        request = base.model_copy(
            update={
                "input": root.model_dump(mode="json"),
                "binding_data": {
                    "verification_runner": {
                        "source_root": str(REPO),
                        "qualification_path": str(qualification),
                        "qualification_digest": hashlib.sha256(qualification.read_bytes()).hexdigest(),
                    },
                    "readiness": {
                        "selection_handle": "sut.selection",
                        "authority_handle": "sut-authority",
                        "collector_handle": "sut.collector",
                        "configuration_digest": "c" * 64,
                        "validation_profile": "api_db_trace.v1",
                    },
                },
            }
        )
        # T3 prepare contracts are covered independently; this seam supplies its already authenticated output.
        prepared = SimpleNamespace(
            change_id=plan.change_id,
            batch_id=payload.view.batch_id,
            execution_id=manifest.execution_id,
            verification_manifest_ref=payload.manifest_ref,
            mapping=SimpleNamespace(selected=(manifest.nodeid,)),
            execution_view_digest=payload.view.digest,
            executed_at=payload.view.executed_at,
        )
        monkeypatch.setattr(verified_attempt, "assemble_execution_input", lambda *args, **kwargs: prepared)
        posts, cleanups = [], []
        original_post = verified_execution._post

        async def count_post(*args):
            posts.append(1)
            return await original_post(*args)

        monkeypatch.setattr(verified_execution, "_post", count_post)

        class Host(RealPipeHost):
            def run(self, **kwargs):
                return (
                    super()
                    .run(**kwargs)
                    .model_copy(
                        update={"cleanup_confirmed": False, "reason": "container_cleanup_unconfirmed"}
                    )
                )

            def stop(self, container_name):
                cleanups.append(container_name)
                return True

        handler = VerifiedAttemptHandler()
        handler._delegate = VerifiedExecutionHandler(process_host=Host())
        original_context = context
        # Make the receipt invalid before a fresh dispatch: it must never start.
        stale = {
            **collector,
            "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
            "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        secrets.values["sut.collector"] = json.dumps(stale).encode()
        with pytest.raises(ValueError):
            asyncio.run(handler.execute(request, context))
        assert posts == []
        secrets.values["sut.collector"] = json.dumps(collector).encode()
        from graph_engine.attempts.activity import TaskActivityIndeterminate

        with pytest.raises(TaskActivityIndeterminate):
            asyncio.run(handler.execute(request, context))
        assert len(posts) == 1
        if degradation == "expired":
            secrets.values["sut.collector"] = json.dumps(stale).encode()
        else:
            process.terminate()
            process.wait(timeout=5)
        qualification.unlink()
        assert context.activity is not None
        forged = context.activity.snapshot.model_copy(update={"state": "prepared"})
        rejected = asyncio.run(getattr(handler, method)(request, context, forged))
        assert rejected.status == "indeterminate"
        recovered = asyncio.run(getattr(handler, method)(request, context, context.activity.snapshot))
        assert recovered.status == "terminal"
        assert recovered.outcome is not None
        assert (context.write_root / manifest.evidence_root / "cleanup_terminal.json").is_file()
        assert cleanups and len(posts) == 1
        bindings = cast(dict[str, Any], request.binding_data)
        altered = request.model_copy(
            update={
                "binding_data": {
                    **bindings,
                    "readiness": {
                        **dict(bindings["readiness"]),
                        "configuration_digest": "f" * 64,
                    },
                }
            }
        )
        try:
            result = asyncio.run(
                getattr(handler, method)(altered, original_context, context.activity.snapshot)
            )
            assert result.status == "indeterminate"
        except ValueError:
            pass
        assert len(posts) == 1
    finally:
        listener.close()
