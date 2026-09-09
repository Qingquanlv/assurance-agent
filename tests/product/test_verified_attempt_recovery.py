"""Production task-host protocol tests for the frozen execution facade."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from pathlib import Path

import pytest

from graph_engine.plugin_api import TaskOutcome, TaskActivityReconcileResult


class VerifiedHostProbe:
    """Test installed host endpoint; no Docker qualification or SUT success is claimed."""

    async def execute(self, request, context):
        assert context.activity is not None and context.secrets is not None
        assert request.invocation.lock_digest == "c" * 64
        assert context.secrets.resolve("sut.authority") == b"host-only-authority"
        assert context.secrets.resolve("sut.credential") == b"host-only-credential"
        assert not context.cancel_requested()
        context.activity.mark_dispatch_started({"execution_id": "probe"})
        context.activity.bind({"execution_id": "probe"})
        return self.outcome(request)

    def outcome(self, request):
        from datetime import datetime, timezone

        from assurance_execution.contracts.agent import ExecutionPrepareInputV1
        from assurance_execution.contracts.verification import (
            EvidenceCompletionV1,
            VerificationEvidenceV1,
            VerifiedExecutionResultV1,
        )
        from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
        from graph_engine.attempts import AttemptKey

        payload = ExecutionPrepareInputV1.model_validate(request.input)
        assert payload.verification is not None
        execution_id = "00000000-0000-4000-8000-000000000001"
        receipt_ref = EvidenceArtifactRefV1(path="qa/changes/c/execution/probe.json", digest="e" * 64)
        evidence = VerificationEvidenceV1(
            execution_id=execution_id,
            manifest_digest="d" * 64,
            receipt_ref=receipt_ref,
            observations=(),
            host_completion=EvidenceCompletionV1(state="error", reason="test_probe"),
            collector_completion=EvidenceCompletionV1(state="not_required"),
            state="incomplete",
        )
        reviewed = ReviewedCaseV1(
            change_id=payload.change_id,
            coverage_epoch=payload.coverage_epoch,
            plan_digest=payload.plan_digest,
            plan_ref=payload.plan_ref,
            preparation_refs=(payload.plan_ref,),
            case_refs=(
                EvidenceArtifactRefV1(path="qa/changes/c/cases/system/user/case.yaml", digest="1" * 64),
            ),
            review_ref=EvidenceArtifactRefV1(path="qa/changes/c/review/case-review.json", digest="2" * 64),
        )
        return TaskOutcome.succeeded(
            VerifiedExecutionResultV1(
                validation_profile=payload.verification.validation_profile,
                change_id=payload.change_id,
                case_id="TC_PROBE",
                reviewed_case=reviewed,
                coverage_epoch=payload.coverage_epoch,
                repair_round=payload.repair_round,
                plan_digest=payload.plan_digest,
                plan_ref=payload.plan_ref,
                case_execution_plan_ref=payload.verification.case_execution_plan_ref,
                case_execution_plan_digest=payload.verification.case_execution_plan_ref.digest,
                spec_digest="3" * 64,
                execution_id=execution_id,
                attempt_key=AttemptKey(digest="a" * 64),
                batch_id="probe",
                mapping_digest="4" * 64,
                manifest_ref=EvidenceArtifactRefV1(
                    path="qa/changes/c/execution/manifest.json", digest="d" * 64
                ),
                evidence_ref=EvidenceArtifactRefV1(
                    path="qa/changes/c/execution/outcome.json", digest="5" * 64
                ),
                execution_authority_ref=EvidenceArtifactRefV1(
                    path=(f"qa/changes/c/execution/{execution_id}/execution_terminal.json"),
                    digest="6" * 64,
                ),
                raw_evidence_refs=(receipt_ref,),
                executed_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
                completion_status="incomplete",
                evidence=evidence,
            ).model_dump(mode="json")
        )

    async def reconcile(self, request, context, activity):
        assert context.activity is not None
        assert context.activity.snapshot == activity
        if activity.state == "prepared":
            return TaskActivityReconcileResult(status="not_dispatched")
        return TaskActivityReconcileResult(
            status="terminal", outcome=self.outcome(request), reference=activity.reference
        )


@pytest.mark.parametrize("cut", ["prepared", "dispatch_started", "terminal", "sealed", "promoted"])
def test_frozen_delegate_uses_production_host_activity_and_reconcile(tmp_path: Path, monkeypatch, cut):
    asyncio.run(_run_host(tmp_path, monkeypatch, cut))


async def _run_host(tmp_path: Path, monkeypatch, cut):
    from assurance_product.verification_execution import (
        ProfiledExecutionExecutor,
        VerificationConfiguration,
        HANDLER_ID,
    )
    from assurance_product.models import VerificationHostConfigV1
    from graph_engine.attempts import AttemptExecutionContext, AttemptKey, AuthorizedAttemptScope
    from graph_engine.attempts.activity import journal_backed_activity_factory
    from graph_engine.attempts.events import (
        AttemptOpened,
        ResourcesAuthorized,
        ActivityPrepared,
        ActivityDispatchStarted,
    )
    from graph_engine.attempts.workspace import TaskWorkspaceStore
    from graph_engine.attempts.production_host import create_production_task_execution_host
    from graph_engine.attempts.host_receipts import TerminalReceiptStore
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )
    from graph_engine.canonical import JSONValue, canonical_digest
    from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
    from tests.verified_generation_fixture import accepted_verified_execution_input

    project = tmp_path / "project"
    project.mkdir()
    value = accepted_verified_execution_input(project, change_id="c")
    config = VerificationConfiguration(
        validation_profile="api_db.v1",
        host=VerificationHostConfigV1(
            managed_sut_authority_handle="sut.authority", credential_handle="sut.credential"
        ),
    )
    key = AttemptKey(digest="a" * 64)
    store = TaskWorkspaceStore(project, project / ".attempts", project / ".receipts")
    workspace = store.begin(task_id=key.digest, attempt=1, output_paths=("qa/changes/c/execution",))
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv",
            public_entrypoint="execute",
            semantic_node_id="execution.execute",
            attempt_key=key,
            fencing_token=1,
            authorization_id="d" * 64,
        ),
        workspace=workspace,
    )
    journal = MemoryAttemptJournal()
    await journal.append(
        key,
        (
            AttemptOpened(
                contract_digest="e" * 64,
                input_digest=canonical_digest(value.model_dump(mode="json")),
                graph_revision="f" * 64,
                invocation_id="inv",
                public_entrypoint="execute",
                semantic_node_id="execution.execute",
            ),
            ResourcesAuthorized(authorization_id="d" * 64),
            ActivityPrepared(activity_id=key.digest),
        ),
        expected_revision=0,
        fencing_token=1,
    )
    monkeypatch.setenv("AA_TEST_AUTHORITY", "host-only-authority")
    monkeypatch.setenv("AA_TEST_CREDENTIAL", "host-only-credential")
    sources = (
        SecretSourceBinding("sut.authority", "environment", "AA_TEST_AUTHORITY"),
        SecretSourceBinding("sut.credential", "environment", "AA_TEST_CREDENTIAL"),
    )
    auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )

    async def fence():
        pass

    activity_port_factory = journal_backed_activity_factory(
        journal=journal, attempt_key=key, owner_loop=asyncio.get_running_loop(), assert_live_fence=fence
    )

    def activity_factory(call, *, remaining_deadline):
        return cast(Any, activity_port_factory)(call, remaining_deadline=remaining_deadline)

    host = create_production_task_execution_host(
        authorization=auth,
        handlers={HANDLER_ID: VerifiedHostProbe()},
        store=store,
        receipts=TerminalReceiptStore.create(tmp_path / "host-receipts"),
        activity_factory=activity_factory,
        invocation_root=tmp_path,
        handler_import_roots={HANDLER_ID: (str(Path(__file__).resolve().parents[2]),)},
    )
    executor = ProfiledExecutionExecutor(
        config=config,
        config_digest="c" * 64,
        legacy=None,
        callable_path="tests.product.test_verified_attempt_recovery:VerifiedHostProbe.execute",
    ).with_host(host, graph_revision="f" * 64, product_lock_digest="c" * 64)
    try:
        if cut == "dispatch_started":
            snapshot = await journal.load(key)
            assert snapshot is not None
            fingerprint: JSONValue = {"execution_id": "probe"}
            await journal.append(
                key,
                (
                    ActivityDispatchStarted(
                        activity_id=key.digest,
                        dispatch_fingerprint=fingerprint,
                        dispatch_fingerprint_digest=canonical_digest(fingerprint),
                    ),
                ),
                expected_revision=snapshot.revision,
                fencing_token=1,
            )
        if cut in {"terminal", "sealed", "promoted"}:
            result = await executor.execute(value, scope)
        else:
            result = await executor.reconcile(value, scope, await journal.load(key))
        assert result.output.root.completion_status == "incomplete"
        if cut in {"sealed", "promoted"}:
            staged = store.seal(workspace.identity)
            if cut == "promoted":
                store.promote(workspace.identity, staged)
        replay = await executor.reconcile(value, scope, await journal.load(key))
        assert replay.output == result.output
        for changed in (
            value.model_copy(update={"validation_profile": "api_db_trace.v1"}),
            value.model_copy(update={"verification_config_digest": "0" * 64}),
        ):
            with pytest.raises(ValueError, match="profile|configuration"):
                await executor.execute(changed, scope)
            with pytest.raises(ValueError, match="profile|configuration"):
                await executor.reconcile(changed, scope, await journal.load(key))
    finally:
        store.close()


class OwnedAttemptProbe:
    """Real installed lifecycle and HTTP/SQLite, with test-only pipe transport."""

    def __init__(self):
        from assurance_execution.operations.verified_attempt import VerifiedAttemptHandler
        from assurance_execution.operations.verified_execution import VerifiedExecutionHandler

        self.handler = VerifiedAttemptHandler()
        self.handler._delegate = VerifiedExecutionHandler(process_host=PipeHost())

    async def execute(self, request, context):
        marker = context.project_root / ".test-crash-cut"
        cut = marker.read_text() if marker.exists() else None
        if cut == "wrong_recovery_scope":
            assert context.activity is not None
            self.handler._owned_request(request, context)
            rejected = await self.handler.cancel(
                request.model_copy(update={"node_id": "execution.run"}), context, context.activity.snapshot
            )
            assert rejected.status == "indeterminate"
            return await self.handler.execute(request, context)
        if cut in {"dynamic_db_mismatch", "dynamic_instance_mismatch"}:
            from assurance_execution.operations.verified_execution import VerifiedExecutionInputV1

            delegate = self.handler._delegate.execute

            async def mismatch(prepared, private_context):
                payload = VerifiedExecutionInputV1.model_validate(prepared.input)
                field, value = (
                    ("observer_sqlite_path", "/tmp/wrong.sqlite3")
                    if cut == "dynamic_db_mismatch"
                    else ("sut_instance_id", "wrong-instance")
                )
                changed = payload.model_copy(
                    update={"verification": payload.verification.model_copy(update={field: value})}
                )
                return await delegate(
                    prepared.model_copy(update={"input": changed.model_dump(mode="json")}), private_context
                )

            pytest.MonkeyPatch().setattr(self.handler._delegate, "execute", mismatch)
            with pytest.raises(ValueError):
                await self.handler.execute(request, context)
            return TaskOutcome.failed("configuration", "NOT_READY: test dynamic mismatch", retryable=False)
        if cut in {"cancel_before_start", "cancel_prepared"}:
            assert context.activity is not None
            if cut == "cancel_prepared":
                _, _, owned = self.handler._owned_request(request, context)
                assert owned is not None
                Path(request.binding_data["verification_runner"]["qualification_path"]).unlink()
            cancelled = await self.handler.cancel(request, context, context.activity.snapshot)
            assert cancelled.status == "acknowledged"
            if cut == "cancel_before_start":
                assert not (context.project_root / ".aa/managed-user").exists()
            return TaskOutcome.failed("internal", "test-only cancellation", retryable=False)
        if cut == "live_prepared":
            import httpx

            original_post = httpx.Client.post
            logins = []

            def count_login(client, url, *args, **kwargs):
                if str(url).endswith("/access_token"):
                    logins.append(str(url))
                return original_post(client, url, *args, **kwargs)

            patch = pytest.MonkeyPatch()
            patch.setattr(httpx.Client, "post", count_login)
            try:
                _, _, first = self.handler._owned_request(request, context)
                assert first is not None
                assert context.activity is not None
                checked = await self.handler.reconcile(request, context, context.activity.snapshot)
                assert checked.status == "not_dispatched"
                outcome = await self.handler.execute(request, context)
                assert len(logins) == 1
                assert len(list((context.project_root / ".aa/managed-user").iterdir())) == 1
                return outcome
            finally:
                patch.undo()
        if cut in {"prepared", "dispatch_started", "terminal_unreported"}:
            prepared, private_context, _ = self.handler._owned_request(request, context)
            if cut != "prepared":
                if cut == "dispatch_started":
                    self.handler._delegate._host = CrashPipeHost()
                await self.handler._delegate.execute(prepared, private_context)
            raise RuntimeError("test-only worker crash before terminal receipt")
        return await self.handler.execute(request, context)

    async def reconcile(self, request, context, activity):
        return await self.handler.reconcile(request, context, activity)

    async def cancel(self, request, context, activity):
        return await self.handler.cancel(request, context, activity)


class PipeHost:
    def preflight(self):
        return {"transport": "test-only-real-pipe"}

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


class CrashPipeHost(PipeHost):
    def run(self, **kwargs):
        raise RuntimeError("test-only process lost after authenticated dispatch")


@pytest.mark.parametrize(
    "cut",
    [
        "wrong_recovery_scope",
        "dynamic_db_mismatch",
        "dynamic_instance_mismatch",
        "cancel_before_start",
        "cancel_prepared",
        "live_prepared",
        "prepared",
        "dispatch_started",
        "terminal_unreported",
        "terminal",
        "sealed",
        "promoted",
    ],
)
def test_installed_host_owns_lifecycle_and_replays_stopped_attempt(tmp_path, monkeypatch, cut):
    asyncio.run(_run_owned_host(tmp_path, monkeypatch, cut))


async def _run_owned_host(tmp_path: Path, monkeypatch, cut):
    from assurance_product.verification_execution import (
        ProfiledExecutionExecutor,
        VerificationConfiguration,
        HANDLER_ID,
    )
    from assurance_product.models import VerificationHostConfigV1, VerificationRunnerConfigV1
    from graph_engine.attempts import AttemptExecutionContext, AttemptKey, AuthorizedAttemptScope
    from graph_engine.attempts.activity import journal_backed_activity_factory
    from graph_engine.attempts.events import (
        AttemptOpened,
        ResourcesAuthorized,
        ActivityPrepared,
    )
    from graph_engine.attempts.workspace import TaskWorkspaceStore
    from graph_engine.attempts.production_host import create_production_task_execution_host
    from graph_engine.attempts.host_receipts import TerminalReceiptStore
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )
    from graph_engine.canonical import canonical_digest
    from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
    from tests.verified_generation_fixture import accepted_verified_execution_input

    project = tmp_path / "project"
    import json
    import hashlib
    from tests.product.test_user_oracle_full_workflow import load

    selected = load("user_oracle_harness").materialize_project(project_dir=project)
    value = accepted_verified_execution_input(
        project, change_id="c", reviewed_source_path="app/controllers/user.py"
    ).model_copy(update={"verification": None})
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    qualification = tmp_path / "qualification.json"
    qualification.write_text("test-only pipe transport, no OCI qualification claim")
    config = VerificationConfiguration(
        validation_profile="api_db.v1",
        host=VerificationHostConfigV1(
            managed_sut_authority_handle="sut.authority",
            managed_sut_readiness_handle="unused.dynamic.selection",
            credential_handle="sut.credential",
            runner=VerificationRunnerConfigV1(
                source_root=str(Path(__file__).resolve().parents[2]),
                qualification_path=str(qualification),
                qualification_digest=hashlib.sha256(qualification.read_bytes()).hexdigest(),
            ),
        ),
    )
    key = AttemptKey(digest="a" * 64)
    store = TaskWorkspaceStore(project, project / ".attempts", project / ".receipts")
    workspace = store.begin(
        task_id=key.digest,
        attempt=1,
        output_paths=("qa/changes/c/execution", "qa/changes/c/.staging/execution"),
    )
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv",
            public_entrypoint="execute",
            semantic_node_id="execution.execute",
            attempt_key=key,
            fencing_token=1,
            authorization_id="d" * 64,
        ),
        workspace=workspace,
    )
    journal = MemoryAttemptJournal()
    await journal.append(
        key,
        (
            AttemptOpened(
                contract_digest="e" * 64,
                input_digest=canonical_digest(value.model_dump(mode="json")),
                graph_revision="f" * 64,
                invocation_id="inv",
                public_entrypoint="execute",
                semantic_node_id="execution.execute",
            ),
            ResourcesAuthorized(authorization_id="d" * 64),
            ActivityPrepared(activity_id=key.digest),
        ),
        expected_revision=0,
        fencing_token=1,
    )
    monkeypatch.setenv(
        "AA_TEST_AUTHORITY",
        json.dumps(
            {
                "kind": "user-invocation-host.v1",
                "authority_root": str(private),
                "fault": "none",
                "frozen_artifact_ref": {
                    "path": ".aa/user-oracle/runtime-lock.json",
                    "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                },
            }
        ),
    )
    monkeypatch.setenv("AA_TEST_CREDENTIAL", "host-only-credential")
    sources = (
        SecretSourceBinding("sut.authority", "environment", "AA_TEST_AUTHORITY"),
        SecretSourceBinding("sut.credential", "environment", "AA_TEST_CREDENTIAL"),
    )
    monkeypatch.setenv("AA_SUT_TEST_ONLY", "local-only-password")
    sources += tuple(
        SecretSourceBinding(handle, "environment", "AA_SUT_TEST_ONLY")
        for handle in ("managed-sut.admin-password", "managed-sut.reset-password", "managed-sut.secret-key")
    )
    auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )

    async def fence():
        pass

    activity_port_factory = journal_backed_activity_factory(
        journal=journal, attempt_key=key, owner_loop=asyncio.get_running_loop(), assert_live_fence=fence
    )

    def activity_factory(call, *, remaining_deadline):
        return cast(Any, activity_port_factory)(call, remaining_deadline=remaining_deadline)

    host = create_production_task_execution_host(
        authorization=auth,
        handlers={HANDLER_ID: OwnedAttemptProbe()},
        store=store,
        receipts=TerminalReceiptStore.create(tmp_path / "host-receipts"),
        activity_factory=activity_factory,
        invocation_root=tmp_path,
        handler_import_roots={HANDLER_ID: (str(Path(__file__).resolve().parents[2]),)},
    )
    executor = ProfiledExecutionExecutor(
        config=config,
        config_digest="c" * 64,
        legacy=None,
        callable_path="tests.product.test_verified_attempt_recovery:OwnedAttemptProbe.execute",
    ).with_host(host, graph_revision="f" * 64, product_lock_digest="c" * 64)
    try:
        assert not (project / ".aa/managed-user").exists()
        retained_credential = None
        if cut in {
            "wrong_recovery_scope",
            "live_prepared",
            "cancel_before_start",
            "cancel_prepared",
            "dynamic_db_mismatch",
            "dynamic_instance_mismatch",
        }:
            (project / ".test-crash-cut").write_text(cut)
        if cut in {"prepared", "dispatch_started", "terminal_unreported"}:
            from graph_engine.attempts.production_host import ProductionHostError

            (project / ".test-crash-cut").write_text(cut)
            with pytest.raises(ProductionHostError, match="test-only"):
                await executor.execute(value, scope)
            (project / ".test-crash-cut").unlink()
            record = json.loads(next(private.glob("*.json")).read_bytes())
            retained_credential = record["credential"]
            result = await executor.reconcile(value, scope, await journal.load(key))
            if cut == "prepared":
                from graph_engine.attempts import IndeterminateTaskResult

                assert isinstance(result, IndeterminateTaskResult)
                runs = list((project / ".aa/managed-user").iterdir())
                assert len(runs) == 1
                assert (runs[0] / "runtime/stopped-process.json").exists()
                assert not list(workspace.write_root.rglob("action_started.json"))
                return
        else:
            result = await executor.execute(value, scope)
        if cut in {
            "cancel_before_start",
            "cancel_prepared",
            "dynamic_db_mismatch",
            "dynamic_instance_mismatch",
        }:
            from graph_engine.attempts import PermanentTaskFailure

            assert isinstance(result, PermanentTaskFailure)
            assert not list(workspace.write_root.rglob("action_started.json"))
            runs = list(project.glob(".aa/managed-user/*/runtime"))
            if cut == "cancel_before_start":
                assert runs == []
            else:
                assert len(runs) == 1 and (runs[0] / "stopped-process.json").exists()
            return
        assert result.output.root.completion_status == (
            "incomplete" if cut == "dispatch_started" else "collected"
        )
        runs = list((project / ".aa/managed-user").iterdir())
        assert len(runs) == 1
        assert (runs[0] / "runtime/stopped-process.json").exists()
        import sqlite3

        with sqlite3.connect(runs[0] / "runtime/sut/db.sqlite3") as database:
            assert database.execute(
                "select count(*) from user where username like 'u%' and username != 'admin'"
            ).fetchone()[0] == (0 if cut == "dispatch_started" else 1)
        if cut in {"sealed", "promoted"}:
            staged = store.seal(workspace.identity)
            if cut == "promoted":
                store.promote(workspace.identity, staged)
        qualification.unlink()
        replay = await executor.reconcile(value, scope, await journal.load(key))
        assert replay.output == result.output
        assert len(list(private.glob("*.json"))) == 1
        private_credentials = json.loads(json.loads(next(private.glob("*.json")).read_bytes())["credential"])
        for path in workspace.write_root.rglob("*"):
            if path.is_file():
                for secret in private_credentials.values():
                    assert secret.encode() not in path.read_bytes()
        if retained_credential is not None:
            assert json.loads(next(private.glob("*.json")).read_bytes())["credential"] == retained_credential
        assert list((project / ".aa/managed-user").iterdir()) == runs
        for changed in (
            value.model_copy(update={"validation_profile": "api_db_trace.v1"}),
            value.model_copy(update={"verification_config_digest": "0" * 64}),
        ):
            with pytest.raises(ValueError, match="profile|configuration"):
                await executor.execute(changed, scope)
            with pytest.raises(ValueError, match="profile|configuration"):
                await executor.reconcile(changed, scope, await journal.load(key))
    finally:
        store.close()


@pytest.fixture
def owned_input(tmp_path):
    import json
    from tests.product.test_user_oracle_full_workflow import load
    from tests.verified_generation_fixture import accepted_verified_execution_input
    from tests.product.test_verified_readiness import Secrets
    from graph_engine.attempts import AttemptKey

    project = tmp_path / "project"
    selected = load("user_oracle_harness").materialize_project(project_dir=project)
    root = accepted_verified_execution_input(
        project, reviewed_source_path="app/controllers/user.py"
    ).model_copy(update={"verification": None})
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secrets = Secrets(
        {
            "sut.authority": json.dumps(
                {
                    "kind": "user-invocation-host.v1",
                    "authority_root": str(private),
                    "fault": "none",
                    "frozen_artifact_ref": {
                        "path": ".aa/user-oracle/runtime-lock.json",
                        "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                    },
                }
            ).encode(),
            **{
                handle: b"R4-private-password"
                for handle in (
                    "sut.credential",
                    "managed-sut.admin-password",
                    "managed-sut.reset-password",
                    "managed-sut.secret-key",
                )
            },
        }
    )
    kwargs = dict(
        source_root=Path(__file__).resolve().parents[2],
        workspace_root=project,
        attempt_key=AttemptKey(digest="a" * 64),
        invocation_id="inv",
        task_id="a" * 64,
        graph_instance_id="graph",
        node_id="execution.execute",
        authorization_scope_digest="b" * 64,
        secrets=secrets,
        authority_handle="sut.authority",
        credential_handle="sut.credential",
    )
    yield root, kwargs, private
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    for receipt in project.glob(".aa/managed-user/*/runtime/owned-process.json"):
        if not receipt.with_name("stopped-process.json").exists():
            document = json.loads(receipt.read_bytes())
            try:
                ManagedUserSutHost(source_root=Path(__file__).resolve().parents[2], secret_port=secrets).stop(
                    workspace_root=project, receipt_path=receipt, instance_id=document["instance_id"]
                )
            except ValueError:
                pass


def test_startup_failure_cleans_process_after_start_receipt_is_written(owned_input, monkeypatch):
    from assurance_execution.operations.user_attempt import start_user_attempt
    from assurance_execution.operations.managed_sut import ManagedUserSutHost

    root, kwargs, _ = owned_input
    start = ManagedUserSutHost.start

    def lose_start_reply(self, **arguments):
        start(self, **arguments)
        raise ValueError("test lost start reply")

    monkeypatch.setattr(ManagedUserSutHost, "start", lose_start_reply)
    with pytest.raises(ValueError, match="test lost start reply"):
        start_user_attempt(root, **kwargs)
    runs = list(kwargs["workspace_root"].glob(".aa/managed-user/*/runtime"))
    assert len(runs) == 1
    assert (runs[0] / "stopped-process.json").exists()


def test_owned_stop_is_idempotent_even_if_runtime_source_drifted(owned_input):
    from assurance_execution.operations.user_attempt import start_user_attempt

    root, kwargs, _ = owned_input
    owned = start_user_attempt(root, **kwargs)
    source = Path(owned.authority.run_root) / "sut/app/controllers/user.py"
    original = source.read_bytes()
    source.chmod(0o600)
    try:
        source.write_bytes(original + b"\n# changed after preparation\n")
        owned.stop()
        owned.stop()
        assert (Path(owned.authority.run_root) / "stopped-process.json").exists()
    finally:
        source.write_bytes(original)


def test_retained_attempt_recovers_credentials_without_login_and_rejects_links(owned_input, monkeypatch):
    import json
    import os
    import traceback
    import httpx
    from assurance_execution.operations.user_attempt import start_user_attempt, recover_user_attempt
    from graph_engine.attempts import AttemptKey

    root, kwargs, private = owned_input
    original_post = httpx.Client.post
    logins = []

    def count_login(client, url, *args, **arguments):
        if str(url).endswith("/access_token"):
            logins.append(str(url))
        return original_post(client, url, *args, **arguments)

    monkeypatch.setattr(httpx.Client, "post", count_login)
    first = start_user_attempt(root, **kwargs)
    recover_kwargs = {
        key: kwargs[key]
        for key in ("workspace_root", "source_root", "attempt_key", "secrets", "authority_handle")
    }
    record = private / f"{first.execution_id}.json"
    retained = record.read_bytes()
    recovered = recover_user_attempt(root, **recover_kwargs)
    assert recovered is not None
    assert recovered.verification == first.verification
    assert recovered.secrets.resolve("sut.credential") == first.secrets.resolve("sut.credential")
    assert len(logins) == 1
    extra = private / "link"
    os.link(record, extra)
    with pytest.raises(ValueError, match="invalid"):
        recover_user_attempt(root, **recover_kwargs)
    extra.unlink()
    record.rename(extra)
    record.symlink_to(extra)
    with pytest.raises((ValueError, OSError)):
        recover_user_attempt(root, **recover_kwargs)
    record.unlink()
    extra.rename(record)
    record.chmod(0o600)
    record.write_text(json.dumps({"credential": "R4_SECRET_CANARY"}))
    with pytest.raises(ValueError) as caught:
        recover_user_attempt(root, **recover_kwargs)
    assert "R4_SECRET_CANARY" not in "".join(traceback.format_exception(caught.value))
    record.write_bytes(retained)
    record.chmod(0o400)
    first.stop()
    project_private = kwargs["workspace_root"] / ".aa/private"
    project_private.mkdir(mode=0o700)
    project_record = project_private / record.name
    project_record.write_bytes(retained)
    project_record.chmod(0o400)
    secrets = kwargs["secrets"]
    seed_bytes = secrets.values["sut.authority"]
    seed = json.loads(seed_bytes)
    seed["authority_root"] = str(project_private)
    secrets.values["sut.authority"] = json.dumps(seed).encode()
    with pytest.raises(ValueError, match="outside"):
        recover_user_attempt(root, **recover_kwargs)
    secrets.values["sut.authority"] = seed_bytes
    second = start_user_attempt(
        root, **{**kwargs, "attempt_key": AttemptKey(digest="b" * 64), "task_id": "b" * 64}
    )
    try:
        assert second.execution_id != first.execution_id
        assert second.verification.sut_instance_id != first.verification.sut_instance_id
        assert second.verification.managed_sqlite_path != first.verification.managed_sqlite_path
        assert second.verification.user_inputs != first.verification.user_inputs
        assert len(logins) == 2
    finally:
        second.stop()
