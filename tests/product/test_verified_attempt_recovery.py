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
