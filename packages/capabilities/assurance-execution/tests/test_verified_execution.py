from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast
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


def formal_plan():
    raw = read_fixture("user-plan.json")
    return compile_case_plan(
        read_fixture("user-case.json"),
        AssertionSourcesV1.model_validate(read_fixture("user-sources.json")),
        cast(dict[str, object], raw["bindings"]),
        "api_db.v1",
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

    def stop(self, container_name):
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
    ["before_dispatch", "dispatch_before_action", "http_before_terminal", "before_seal", "after_promotion"],
)
def test_production_handler_recovery_cuts_never_repeat_post(managed_sut, monkeypatch, cut):
    from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

    store = None
    if cut == "after_promotion":
        from graph_engine.attempts.workspace import TaskWorkspaceStore

        root = managed_sut[0]
        store = TaskWorkspaceStore(root, root / "promotion-attempts", root / "promotion-receipts")
    request, context, manifest, _ = handler_case(managed_sut, "cut_" + cut[:8], store=store)
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
        }[cut]

        def crash_write(self, name, payload):
            if name == record:
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
    recovered = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert recovered.status == "terminal"
    assert recovered.outcome is not None
    assert VerificationEvidenceV1.model_validate(recovered.outcome.output).state == (
        "collected" if cut in {"before_seal", "after_promotion"} else "incomplete"
    )
    again = asyncio.run(handler.reconcile(request, context, context.activity.snapshot))
    assert again.outcome == recovered.outcome
    assert len(posts) == (0 if cut == "dispatch_before_action" else 1)
    if store is not None:
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
