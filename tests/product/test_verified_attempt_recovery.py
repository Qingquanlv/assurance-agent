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
            telemetry_completion=EvidenceCompletionV1(state="not_required"),
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
    """Real installed lifecycle, default subprocess bridge and HTTP/SQLite."""

    def __init__(self):
        from assurance_execution.operations.verified_attempt import VerifiedAttemptHandler

        self.handler = VerifiedAttemptHandler()

    async def execute(self, request, context):
        assert "verification_runner" not in request.binding_data
        assert request.binding_data["user_host"]["sut_source_root"]
        marker = context.project_root / ".test-crash-cut"
        cut = marker.read_text() if marker.exists() else None
        if cut in {"manifest_before_publish", "manifest_after_publish"}:
            from assurance_execution.operations import record_publication as publication

            publish = publication._publish_exclusive

            def interrupted(source, destination):
                if destination.name == "manifest.json" and cut == "manifest_before_publish":
                    raise RuntimeError("test-only interrupted manifest publication")
                publish(source, destination)
                if destination.name == "manifest.json":
                    raise RuntimeError("test-only interrupted manifest publication")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(publication, "_publish_exclusive", interrupted)
                return await self.handler.execute(request, context)
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
                assert not (context.project_root / ".aa/managed-user").exists()
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
        from assurance_execution.operations.verified_process import SubprocessVerificationHost

        return SubprocessVerificationHost().run(
            view=view,
            nodeid=nodeid,
            case_id=case_id,
            container_name=container_name,
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
        "manifest_before_publish",
        "manifest_after_publish",
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
    from assurance_product.models import VerificationHostConfigV1
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
    import os
    from tests.product.test_user_oracle_full_workflow import load

    harness = load("user_oracle_harness")
    selected = harness.materialize_project(project_dir=project)
    password = "R4-private-password"
    os.environ["AA_SUT_ADMIN_PASSWORD"] = password
    os.environ["AA_SUT_RESET_PASSWORD"] = password
    os.environ["AA_SUT_SECRET_KEY"] = password
    started = harness.serve(project)
    value = accepted_verified_execution_input(
        project, change_id="c", reviewed_source_path="app/controllers/user.py"
    ).model_copy(update={"verification": None})
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    config = VerificationConfiguration(
        validation_profile="api_db.v1",
        host=VerificationHostConfigV1(
            managed_sut_authority_handle="sut.authority",
            managed_sut_readiness_handle="unused.dynamic.selection",
            credential_handle="sut.credential",
            sut_source_root=str(Path(__file__).resolve().parents[2]),
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
                "sut_base_url": started["base_url"],
                "sqlite_path": started["sqlite_path"],
                "instance_id": started["instance_id"],
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
    monkeypatch.setenv("AA_SUT_TEST_ONLY", password)
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
        if cut in {
            "prepared",
            "dispatch_started",
            "terminal_unreported",
            "manifest_before_publish",
            "manifest_after_publish",
        }:
            from graph_engine.attempts.production_host import ProductionHostError

            (project / ".test-crash-cut").write_text(cut)
            with pytest.raises(ProductionHostError, match="test-only"):
                await executor.execute(value, scope)
            (project / ".test-crash-cut").unlink()
            if cut.startswith("manifest_"):
                manifests = list(workspace.write_root.glob("qa/changes/c/execution/*/manifest.json"))
                if cut == "manifest_before_publish":
                    assert manifests == []
                else:
                    assert len(manifests) == 1
                    assert json.loads(manifests[0].read_bytes())["change_id"] == "c"
            record = json.loads(next(private.glob("*.json")).read_bytes())
            retained_credential = record["credential"]
            result = await executor.reconcile(value, scope, await journal.load(key))
            if cut == "prepared":
                assert not (project / ".aa/managed-user").exists()
                assert list(private.glob("*.json"))
                assert result.output.root.completion_status == "collected"
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
            assert not (project / ".aa/managed-user").exists()
            if cut == "cancel_before_start":
                assert not list(private.glob("*.json"))
            else:
                assert list(private.glob("*.json"))
            return
        assert result.output.root.completion_status == (
            "incomplete" if cut == "dispatch_started" else "collected"
        )
        assert not (project / ".aa/managed-user").exists()
        import sqlite3

        with sqlite3.connect(started["sqlite_path"]) as database:
            assert database.execute("select count(*) from user where username = 'oracle_user'").fetchone()[
                0
            ] == (0 if cut == "dispatch_started" else 1)
        if cut in {"sealed", "promoted"}:
            staged = store.seal(workspace.identity)
            if cut == "promoted":
                store.promote(workspace.identity, staged)
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
        assert not (project / ".aa/managed-user").exists()
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
        harness.stop_served(started["pid"])


@pytest.fixture
def owned_input(tmp_path):
    import json
    import os
    from tests.product.test_user_oracle_full_workflow import load
    from tests.verified_generation_fixture import accepted_verified_execution_input
    from tests.product.test_verified_readiness import Secrets
    from graph_engine.attempts import AttemptKey

    project = tmp_path / "project"
    harness = load("user_oracle_harness")
    selected = harness.materialize_project(project_dir=project)
    password = "R4-private-password"
    os.environ["AA_SUT_ADMIN_PASSWORD"] = password
    os.environ["AA_SUT_RESET_PASSWORD"] = password
    os.environ["AA_SUT_SECRET_KEY"] = password
    started = harness.serve(project)
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
                    "sut_base_url": started["base_url"],
                    "sqlite_path": started["sqlite_path"],
                    "instance_id": started["instance_id"],
                    "frozen_artifact_ref": {
                        "path": ".aa/user-oracle/runtime-lock.json",
                        "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                    },
                }
            ).encode(),
            **{
                handle: password.encode()
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
    try:
        yield root, kwargs, private
    finally:
        harness.stop_served(started["pid"])


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
        assert second.verification.sut_instance_id == first.verification.sut_instance_id
        assert second.verification.managed_sqlite_path == first.verification.managed_sqlite_path
        assert second.verification.user_inputs == first.verification.user_inputs
        assert len(logins) == 2
    finally:
        second.stop()


@pytest.mark.parametrize("raced", [False, True])
@pytest.mark.parametrize("winner", [b'{"state":"sealed"}', b'{"state":"other"}', b'{"state":'])
def test_host_record_publication_authenticates_existing_winner(tmp_path, monkeypatch, raced, winner):
    from assurance_execution.operations import record_publication as publication

    expected = b'{"state":"sealed"}'
    destination = tmp_path / "record.json"
    if raced:
        publish = publication._publish_exclusive

        def competing_writer(source, target):
            target.write_bytes(winner)
            publish(source, target)

        monkeypatch.setattr(publication, "_publish_exclusive", competing_writer)
    else:
        destination.write_bytes(winner)
    if winner == expected:
        publication.publish_record(destination, expected)
    else:
        with pytest.raises(ValueError, match="bytes differ"):
            publication.publish_record(destination, expected)
    assert destination.read_bytes() == winner
    assert list(tmp_path.iterdir()) == [destination]
