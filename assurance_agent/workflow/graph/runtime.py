"""Ledger-driven GraphRuntime：Plan → Execute → Update 直到稳定返回边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from assurance_agent.artifacts.policy import PolicyError
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.core.graph_events import (
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    GraphInterruptedEvent,
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    ManualPlanRevisionEvent,
    ResumeAnchor,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskImportedEvent,
)
from assurance_agent.workflow.graph.supersede import (
    SupersedeAction,
    SupersedeError,
    SupersedeResult,
    authorization_consumed,
    build_staged_replacement_plan,
    build_supersede_event,
    evaluate_supersede_eligibility,
    fence_blocks_invocation,
    find_replacement_root,
    find_supersede_event,
    load_staged_definition_request,
    recover_prepared_fence,
    stage_definition_request_record,
)
from assurance_agent.workflow.core.progression import ProgressionError, transaction
from assurance_agent.workflow.graph.manual_revision import (
    ManualRevisionError,
    RevisionPathBaseline,
    RevisionViewBinding,
    build_manual_revision_transition,
    capture_revision_candidate,
    derive_revision_recovery_state,
    find_open_revision_transition,
    stage_missing_resume_suffix,
    transition_from_committed_revision,
)
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.checkpoint import (
    CheckpointImportError,
    CheckpointStore,
    fold_invocation_events,
    render_workflow_state_yaml,
    validate_import,
)
from assurance_agent.workflow.graph.status import (
    graph_status_from_projection,
    pending_write_sets as _pending_write_sets_fn,
)
from assurance_agent.workflow.graph.definition_pinning import (
    InvocationDefinitionBinding,
    bind_root_definitions,
    inherit_child_definitions,
    stage_pinned_definitions,
    verify_pinned_definitions,
)
from assurance_agent.workflow.graph.compiler import (
    PinnedDefinitionRequest,
    canonical_digest,
    resolve_params,
)
from assurance_agent.workflow.graph.selected_wave import (
    SelectedWaveDriftError,
    derive_child_invocation_id,
    preview_selected_wave,
)
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    SystemLivenessProbe,
    recover_running_tasks,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    GraphStatus,
    ImportManifest,
    ImportResult,
    InterruptProjection,
    PlanResult,
    ResumeCommand,
    RunResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.project_locks import ProjectPublicationStore
from assurance_agent.workflow.graph.scheduler import Scheduler, SchedulerError
from assurance_agent.workflow.graph.durable_effects import (
    DurableEffectContext,
    DurableEffectIntegrityError,
    DurableEffectRetryableError,
    DurableEffectRuntime,
    production_effect_registry,
    reconcile_effect,
    record_integrity_failure,
    scan_unacknowledged_intents,
)
from assurance_agent.workflow.graph.effect_retry import (
    EffectRetryStore,
    RootEffectFenceStore,
    RootTerminalFenceError,
    parse_rfc3339_z,
)
from assurance_agent.workflow.graph.resume_compatibility import (
    ResumeCompatibilityDecision,
    event_to_receipt,
)
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TargetedWorkspaceDrift,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
)
from assurance_agent.workflow.orchestration.dsl import Scope, is_satisfied


def _ingest_catalog_digest(compiled: CompiledWorkflow) -> str:
    digest = compiled.ingest_catalog_digest
    if digest:
        return digest
    return validate_catalog_runtime().digest


def _build_invocation_started(
    *,
    invocation_id: str,
    entrypoint: str,
    graph_id: str,
    compiled: CompiledWorkflow,
    params: dict[str, object],
    root_tree_id: str,
    max_parallel_tasks: int,
    checkpoint_ns: str,
    structural_path: str,
    binding: InvocationDefinitionBinding,
    parent_invocation_id: str | None = None,
    parent_task_id: str | None = None,
    supersedes_invocation_id: str | None = None,
    replacement_authorization_id: str | None = None,
) -> GraphInvocationStartedEvent:
    catalog_digest = _ingest_catalog_digest(compiled)
    if not catalog_digest:
        raise GraphRuntimeError("ingest catalog digest is empty; cannot start graph invocation")
    return GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        graph_id=graph_id,
        graph_digest=compiled.digest,
        event_schema_version=binding.event_schema_version,
        ir_digest=compiled.digest,
        ingest_catalog_digest=catalog_digest,
        contract_digests=dict(compiled.contract_digests),
        policy_digest=binding.policy_digest,
        policy_origin=binding.policy_origin,
        gate_semantics_digest=binding.gate_semantics_digest,
        assurance_profile_digest=binding.assurance_profile_digest,
        gate_semantics_object_id=binding.gate_semantics_object_id,
        topology_safety_semantics_object_id=binding.topology_safety_semantics_object_id,
        topology_safety_semantics_digest=binding.topology_safety_semantics_digest,
        commit_safety_semantics_object_id=binding.commit_safety_semantics_object_id,
        commit_safety_semantics_digest=binding.commit_safety_semantics_digest,
        params=params,
        params_sha256=canonical_digest(params),
        root_tree_id=root_tree_id,
        max_parallel_tasks=max_parallel_tasks,
        checkpoint_ns=checkpoint_ns,
        parent_invocation_id=parent_invocation_id,
        parent_task_id=parent_task_id,
        structural_path=structural_path,
        supersedes_invocation_id=supersedes_invocation_id,
        replacement_authorization_id=replacement_authorization_id,
    )


class GraphRuntimeError(AaError):
    """GraphRuntime 基础设施或契约失败；CLI 映射为 exit 40。"""


class GraphDefinitionChanged(GraphRuntimeError):
    """pinned graph/contract digest 与当前定义漂移；拒绝普通 resume。"""


class ResumeCompatibilityBarrier(GraphRuntimeError):
    """Typed v4/v5 resume barrier; read ``decision.reason``, never parse message text."""

    def __init__(self, decision: ResumeCompatibilityDecision) -> None:
        self.decision = decision
        self.reason_code = decision.reason or "resume_compatibility_blocked"
        super().__init__(self.reason_code)


def assert_live_semantic_compatibility(request: PinnedDefinitionRequest) -> None:
    from assurance_agent.verification.profile_manifest import assurance_profile_digest
    from assurance_agent.workflow.graph.definition_pinning import current_v6_semantic_identity
    from assurance_agent.workflow.orchestration.gate_semantics import gate_semantics_digest

    if request.gate_semantics_digest != gate_semantics_digest():
        raise GraphDefinitionChanged(
            "graph_definition_changed: gate semantics digest does not match current executable"
        )
    if request.assurance_profile_digest != assurance_profile_digest():
        raise GraphDefinitionChanged(
            "graph_definition_changed: assurance profile digest does not match current executable"
        )
    if request.event_schema_version >= 6:
        v6 = current_v6_semantic_identity()
        expected = {
            "gate_semantics_object_id": v6.gate_object_id,
            "topology_safety_semantics_object_id": v6.topology_object_id,
            "topology_safety_semantics_digest": v6.topology_digest,
            "commit_safety_semantics_object_id": v6.commit_object_id,
            "commit_safety_semantics_digest": v6.commit_digest,
        }
        actual = {
            "gate_semantics_object_id": request.gate_semantics_object_id,
            "topology_safety_semantics_object_id": request.topology_safety_semantics_object_id,
            "topology_safety_semantics_digest": request.topology_safety_semantics_digest,
            "commit_safety_semantics_object_id": request.commit_safety_semantics_object_id,
            "commit_safety_semantics_digest": request.commit_safety_semantics_digest,
        }
        for name, value in expected.items():
            if actual[name] != value:
                raise GraphDefinitionChanged(
                    f"graph_definition_changed: {name} does not match current executable"
                )


class GraphIntegrityError(GraphRuntimeError):
    """checkpoint/ledger 损坏或因果完整性失败。"""


class GraphRuntime:
    """对外同步的 Plan → Execute → Update 驱动器；并发仅在 Scheduler 内部。"""

    def __init__(
        self,
        *,
        checkpoint_store: CheckpointStore,
        object_store: TreeStore,
        workspace_backend: WorkspaceBackend,
        definition_resolver: Callable[[PinnedDefinitionRequest], Any],
        clock: Clock,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._objects = object_store
        self._workspaces = workspace_backend
        self._definition_resolver = definition_resolver
        self._clock = clock
        self._bundle_cache: dict[PinnedDefinitionRequest, Any] = {}

    def run(
        self,
        schema: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> RunResult:
        invocation_id = self.start_invocation(schema, entrypoint, context)
        return self.drive_started(invocation_id)

    def start_invocation(
        self,
        compiled: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
    ) -> str:
        """Validate params and atomically commit graph_invocation_started (+ pin defs)."""
        return self._start_invocation(compiled, entrypoint, context)

    def drive_started(self, invocation_id: str) -> RunResult:
        """Drive an already-started root invocation to completion or interrupt."""
        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc
        context = self._context_for(projection)
        return self._drive(invocation_id, context)

    def resume(
        self,
        invocation_id: str,
        command: ResumeCommand | None = None,
    ) -> RunResult:
        return self._recover_and_drive(invocation_id, command)

    def status(self, invocation_id: str) -> GraphStatus:
        projection = self._checkpoints.project(invocation_id)
        events = read_events_strict(self._checkpoints._change_dir)  # noqa: SLF001
        from assurance_agent.workflow.graph.status import unacknowledged_durable_effects

        return graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(invocation_id),
            recovery_state=derive_revision_recovery_state(events),
            unacknowledged_durable_effects=unacknowledged_durable_effects(events, invocation_id),
        )

    def supersede(
        self,
        compiled: CompiledWorkflow,
        context: RuntimeContext,
        *,
        invocation_id: str,
        action: SupersedeAction,
        who: str,
        reason: str,
        params: dict[str, object] | None = None,
    ) -> SupersedeResult:
        """Audited legacy-root exit: terminal fence + optional single-use v6 replacement."""
        if not who.strip() or not reason.strip():
            raise SupersedeError("missing_who_or_reason", "supersede requires nonblank who and reason")
        if action == "stop" and params is not None:
            raise SupersedeError("stop_with_params", "stop rejects --params")

        change_dir = context.change_dir
        fence_store = RootEffectFenceStore(context.project_root)
        events = read_events_strict(change_dir)
        existing = recover_prepared_fence(fence_store, root_invocation_id=invocation_id, events=events)
        if existing is not None:
            return self._complete_supersede_after_event(
                compiled=compiled,
                context=context,
                event=existing,
                who=who,
                reason=reason,
                params=params,
            )

        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc

        # Typed legacy-block decision (never parse exception text).
        decision = self._enforce_resume_compatibility(projection, context)
        # Compatibility may have appended a receipt; reload.
        events = read_events_strict(change_dir)
        projection = self._checkpoints.project(invocation_id)

        staged_plan = None
        staged_request = None
        if action == "rerun-v6":
            from assurance_agent.workflow.graph.definition_pinning import request_for_compiled

            staged_request = request_for_compiled(compiled, event_schema_version=6)
            assert_live_semantic_compatibility(staged_request)
            entry = compiled.entrypoints.get(projection.entrypoint)
            if entry is None:
                raise SupersedeError("wrong_entrypoint", f"unknown entrypoint {projection.entrypoint}")
            overrides = dict(projection.params) if params is None else dict(params)
            try:
                resolved = resolve_params(compiled.schema, {**entry.param_overrides, **overrides})
            except Exception as exc:
                raise SupersedeError("invalid_params", f"invalid params: {exc}") from exc
            if projection.entrypoint == "retro":
                try:
                    resolved = ensure_retro_params(resolved)
                except Exception as exc:
                    raise SupersedeError("invalid_params", f"invalid retro params: {exc}") from exc
            staged_plan = build_staged_replacement_plan(request=staged_request, params=resolved)
            stage_definition_request_record(
                change_dir, digest=staged_plan.definition_request_digest, request=staged_request
            )

        latest = self.latest_root_invocation()
        eligibility = evaluate_supersede_eligibility(
            projection=projection,
            latest_root_id=latest,
            expected_entrypoint=projection.entrypoint,
            decision=decision,
            action=action,
            who=who,
            reason=reason,
            params_provided=params is not None,
            staged_request=staged_request,
            project_root=context.project_root,
            change_dir=change_dir,
            events=events,
        )
        if not eligibility.eligible:
            raise SupersedeError(
                eligibility.reason or "not_eligible",
                eligibility.detail or eligibility.reason or "not eligible",
            )

        event = build_supersede_event(
            eligibility=eligibility,
            action=action,
            who=who,
            reason=reason,
            event_schema_version=projection.event_schema_version,
            checkpoint_ns=projection.checkpoint_ns,
            staged=staged_plan,
        )

        # Txn 1: prepare fence → progression append → commit fence.
        # prepare/commit each acquire the frozen guard; hold guard across append.
        fence_store.prepare_terminal(invocation_id, supersede_id=event.supersede_id)
        appended = False
        try:
            with fence_store.guard(invocation_id):
                with transaction(change_dir) as txn:
                    events_locked = txn.read_events_strict()
                    prior = find_supersede_event(events_locked, invocation_id)
                    if prior is not None:
                        if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                            raise SupersedeError(
                                "conflicting_supersede",
                                "conflicting supersede payload under progression lock",
                            )
                        event = prior
                    else:
                        # Rescan eligibility under the lock.
                        projection = fold_invocation_events(invocation_id, events_locked)
                        decision = self._compatibility_decision_readonly(projection, context)
                        eligibility = evaluate_supersede_eligibility(
                            projection=projection,
                            latest_root_id=self.latest_root_invocation(),
                            expected_entrypoint=projection.entrypoint,
                            decision=decision,
                            action=action,
                            who=who,
                            reason=reason,
                            params_provided=params is not None,
                            staged_request=staged_request,
                            project_root=context.project_root,
                            change_dir=change_dir,
                            events=events_locked,
                        )
                        if not eligibility.eligible:
                            raise SupersedeError(
                                eligibility.reason or "not_eligible",
                                eligibility.detail or eligibility.reason or "not eligible",
                            )
                        event = build_supersede_event(
                            eligibility=eligibility,
                            action=action,
                            who=who,
                            reason=reason,
                            event_schema_version=projection.event_schema_version,
                            checkpoint_ns=projection.checkpoint_ns,
                            staged=staged_plan,
                        )
                        txn.append_strict(event)
                        appended = True
            fence_store.commit_terminal(invocation_id)
        except Exception:
            events_now = read_events_strict(change_dir)
            if find_supersede_event(events_now, invocation_id) is None:
                try:
                    fence_store.abort_prepared(invocation_id)
                except RootTerminalFenceError:
                    pass
            else:
                try:
                    fence_store.commit_terminal(invocation_id)
                except RootTerminalFenceError:
                    pass
            raise

        _ = appended
        return self._complete_supersede_after_event(
            compiled=compiled,
            context=context,
            event=event,
            who=who,
            reason=reason,
            params=params,
        )

    def _complete_supersede_after_event(
        self,
        *,
        compiled: CompiledWorkflow,
        context: RuntimeContext,
        event: object,
        who: str,
        reason: str,
        params: dict[str, object] | None,
    ) -> SupersedeResult:
        from assurance_agent.workflow.core.graph_events import GraphInvocationSupersededEvent as _Evt

        assert isinstance(event, _Evt)
        if event.action == "stop":
            return SupersedeResult(
                superseded_invocation_id=event.invocation_id,
                supersede_id=event.supersede_id,
                action="stop",
                replacement_invocation_id=None,
                replacement_authorization_id=None,
                exit_code=0,
                reason="superseded",
            )
        assert event.replacement_authorization_id is not None
        assert event.definition_request_digest is not None
        assert event.params_sha256 is not None

        # Txn 2: consume authorization / recover the one replacement root.
        events = read_events_strict(context.change_dir)
        existing_root = find_replacement_root(
            events,
            supersedes_invocation_id=event.invocation_id,
            replacement_authorization_id=event.replacement_authorization_id,
        )
        if existing_root is not None:
            return SupersedeResult(
                superseded_invocation_id=event.invocation_id,
                supersede_id=event.supersede_id,
                action="rerun-v6",
                replacement_invocation_id=existing_root,
                replacement_authorization_id=event.replacement_authorization_id,
                exit_code=0,
                reason="replacement_resumed",
            )

        staged_request = load_staged_definition_request(context.change_dir, event.definition_request_digest)
        if authorization_consumed(events, event.replacement_authorization_id):
            raise SupersedeError("conflicting_supersede", "replacement authorization already consumed")

        # Resolve params from staged digest binding: reuse event params_sha256.
        entry = compiled.entrypoints[event.entrypoint]
        overrides = dict(params) if params is not None else {}
        if not overrides:
            # Reload superseded root params as overrides.
            old = self._checkpoints.project(event.invocation_id)
            overrides = dict(old.params)
        try:
            resolved = resolve_params(compiled.schema, {**entry.param_overrides, **overrides})
        except Exception as exc:
            raise SupersedeError("invalid_params", f"invalid params: {exc}") from exc
        if canonical_digest(resolved) != event.params_sha256:
            # Prefer exact authorized digest: when CLI retries with same logical
            # params the resolver must match; conflicting params refuse.
            if params is not None:
                raise SupersedeError("conflicting_supersede", "params digest conflicts with authorization")
            # Fall back: use old root projection params already authorized.
            old = self._checkpoints.project(event.invocation_id)
            resolved = dict(old.params)

        replacement_context = context.model_copy(update={"params": resolved})
        new_id = self._start_invocation(
            compiled,
            event.entrypoint,
            replacement_context,
            supersedes_invocation_id=event.invocation_id,
            replacement_authorization_id=event.replacement_authorization_id,
            expected_definition_request=staged_request,
        )
        return SupersedeResult(
            superseded_invocation_id=event.invocation_id,
            supersede_id=event.supersede_id,
            action="rerun-v6",
            replacement_invocation_id=new_id,
            replacement_authorization_id=event.replacement_authorization_id,
            exit_code=0,
            reason="replacement_started",
        )

    def _compatibility_decision_readonly(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> ResumeCompatibilityDecision:
        """Evaluate resume compatibility without appending a new receipt."""
        return ResumeCompatibilityDecision(
            schema_version="1",
            allowed=True,
            event_schema_version=projection.event_schema_version,
            root_invocation_id=projection.parent_invocation_id or projection.invocation_id,
        )

    def import_checkpoint(
        self,
        schema: CompiledWorkflow,
        manifest: ImportManifest,
        context: RuntimeContext,
    ) -> ImportResult:
        """校验并原子导入显式 manifest；不伪造物理 attempt，随后 resume 继续。"""
        # checkpoint_ns must be known before validate_import so imported task
        # ids / structural paths bind to the invocation that will own them.
        invocation_id = str(uuid4())
        validated = validate_import(
            schema,
            manifest,
            context,
            checkpoint_ns=invocation_id,
        )
        latest = self.latest_root_invocation()
        if latest is not None:
            try:
                existing = self._checkpoints.project(latest)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
            if existing.terminal is None:
                raise GraphRuntimeError(
                    f"change already has active invocation {latest}; resume instead of import"
                )

        if manifest.entrypoint not in schema.entrypoints:
            raise CheckpointImportError(f"unknown entrypoint '{manifest.entrypoint}'")
        entry = schema.entrypoints[manifest.entrypoint]
        try:
            params = resolve_params(schema.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if manifest.entrypoint == "retro":
            try:
                params = ensure_retro_params(params)
            except Exception as exc:
                raise GraphRuntimeError(f"invalid retro params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
            raise GraphRuntimeError(f"entrypoint '{manifest.entrypoint}' allow expression rejected params")

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        checkpoint_ns = invocation_id
        bound = context.model_copy(update={"params": params})
        try:
            binding = bind_root_definitions(
                store=self._objects, root_tree_id=root_tree_id, event_schema_version=6
            )
        except PolicyError:
            raise
        started = _build_invocation_started(
            invocation_id=invocation_id,
            entrypoint=manifest.entrypoint,
            graph_id=entry.graph_id,
            compiled=schema,
            params=params,
            root_tree_id=root_tree_id,
            max_parallel_tasks=schema.schema.policies.scheduler.max_parallel_tasks,
            checkpoint_ns=checkpoint_ns,
            structural_path=entry.graph_id,
            binding=binding,
        )

        imported_task_ids: list[str] = []
        try:
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, schema, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(context.project_root),
                            "repo_root": str(context.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                for item in validated.resolved:
                    txn.append_strict(
                        TaskImportedEvent(
                            type="task_imported",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=item.task.graph,
                            node_id=item.task.node,
                            structural_path=item.structural_path,
                            task_key=item.task.task_key,
                            outputs_sha256=dict(item.task.outputs),
                            gate_report=item.gate_report,
                        )
                    )
                    imported_task_ids.append(item.task_id)
                for budget in validated.budgets:
                    txn.append_strict(
                        BudgetConsumedEvent(
                            type="budget_consumed",
                            invocation_id=invocation_id,
                            checkpoint_ns=checkpoint_ns,
                            graph_id=entry.graph_id,
                            budget_id=budget.budget_id,
                            consumption_id=budget.consumption_id,
                            task_id=budget.task_path,
                        )
                    )
                txn.append_strict(
                    CheckpointImportedEvent(
                        type="checkpoint_imported",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        fixture_id=manifest.fixture_id,
                        fixture_digest=_strip_sha_prefix(manifest.fixture_digest),
                        manifest_sha256=validated.manifest_sha256,
                        input_sha256=dict(validated.input_sha256),
                    )
                )
                bootstrap_checkpoint_id = f"bootstrap-{invocation_id}"
                superstep_id = f"import-{invocation_id}"
                txn.append_strict(
                    SuperstepPlannedEvent(
                        type="superstep_planned",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        superstep_id=superstep_id,
                        checkpoint_id=bootstrap_checkpoint_id,
                        task_ids=sorted(imported_task_ids),
                    )
                )
                txn.append_strict(
                    SuperstepCommittedEvent(
                        type="superstep_committed",
                        invocation_id=invocation_id,
                        checkpoint_ns=checkpoint_ns,
                        superstep_id=superstep_id,
                        checkpoint_id=canonical_digest(
                            {
                                "superstep_id": superstep_id,
                                "parent_checkpoint_id": bootstrap_checkpoint_id,
                                "write_set_ids": [],
                                "target_tree_id": root_tree_id,
                                "committed_task_ids": sorted(imported_task_ids),
                            }
                        ),
                        parent_checkpoint_id=bootstrap_checkpoint_id,
                        write_set_ids=[],
                        target_tree_id=root_tree_id,
                        state_values={},
                        committed_task_ids=sorted(imported_task_ids),
                    )
                )
        except CheckpointImportError:
            raise
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise

        projection = self._checkpoints.project(invocation_id)
        checkpoint_id = f"bootstrap-{invocation_id}"
        # 首个 checkpoint 来自严格 ledger 投影（无 superstep_committed 时用 bootstrap 名）。
        self._checkpoints.write(projection)

        # 导入后从未完成 task 继续；失败不回滚已提交的 import 事件。
        self._drive(invocation_id, bound)
        return ImportResult(
            invocation_id=invocation_id,
            checkpoint_id=checkpoint_id,
            imported_tasks=tuple(imported_task_ids),
        )

    def latest_root_invocation(self, entrypoint: str | None = None) -> str | None:
        return self._checkpoints.latest_root_invocation(entrypoint)

    def invocation_terminal(self, invocation_id: str) -> str | None:
        """Return the terminal status of an invocation, or None if still active."""
        proj = self._try_project(invocation_id)
        return proj.terminal if proj is not None else None

    def run_child(
        self,
        parent_task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        """在父 task workspace 上启动/恢复 named subgraph；完成时冻结 write-set。"""
        compiled = self._schema_resolver_for_parent(parent_task)
        if graph_id not in compiled.graphs:
            return TaskResult(
                status="failed",
                error_kind="contract",
                error=f"unknown subgraph '{graph_id}'",
            )
        child_invocation_id = derive_child_invocation_id(parent_task, graph_id)
        checkpoint_ns = f"{parent_task.checkpoint_ns}/{parent_task.node_id}/{child_invocation_id}"
        structural_path = f"{parent_task.structural_path}/{parent_task.node_id}/{graph_id}"
        child_params = dict(context.params)
        if isinstance(parent_task.input, Mapping):
            bound = parent_task.input.get("with")
            if isinstance(bound, Mapping):
                child_params.update({str(key): value for key, value in bound.items()})
        child_context = context.model_copy(
            update={
                "project_root": workspace.project_root,
                "repo_root": workspace.repo_root,
                "params": child_params,
            }
        )
        existing = self._try_project(child_invocation_id)
        parent_projection = self._checkpoints.project(parent_task.invocation_id)
        if existing is None:
            # 父 task workspace 已物化；child 继承同一 base tree，避免以 workspace
            # project_root 调用 TreeStore.capture（change_dir 在 workspace 外）。
            root_tree_id = workspace.base_tree_id
            try:
                binding = inherit_child_definitions(
                    parent=parent_projection,
                    change_dir=context.change_dir,
                )
            except PolicyError as exc:
                raise GraphRuntimeError(f"policy snapshot: {exc}") from exc
            started = _build_invocation_started(
                invocation_id=child_invocation_id,
                entrypoint=graph_id,
                graph_id=graph_id,
                compiled=compiled,
                params=dict(child_context.params),
                root_tree_id=root_tree_id,
                max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
                checkpoint_ns=checkpoint_ns,
                structural_path=structural_path,
                binding=binding,
                parent_invocation_id=parent_task.invocation_id,
                parent_task_id=parent_task.task_id,
            )
            with transaction(context.change_dir) as txn:
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{child_invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(workspace.project_root),
                            "repo_root": str(workspace.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                            "parent_invocation_id": parent_task.invocation_id,
                            "parent_task_id": parent_task.task_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        else:
            try:
                binding = inherit_child_definitions(
                    parent=parent_projection,
                    change_dir=context.change_dir,
                )
                verify_pinned_definitions(binding, context.change_dir)
            except PolicyError as exc:
                raise GraphRuntimeError(f"policy snapshot: {exc}") from exc
        result = self._drive(child_invocation_id, child_context)
        return self._child_result_to_task_result(result, parent_task=parent_task, workspace=workspace)

    def _schema_resolver_for_parent(self, parent_task: ExecutableTask) -> CompiledWorkflow:
        parent = self._checkpoints.project(parent_task.invocation_id)
        return self._resolve_bundle(parent).compiled

    def _try_project(self, invocation_id: str) -> GraphProjection | None:
        try:
            return self._checkpoints.project(invocation_id)
        except LedgerIntegrityError:
            return None

    def _child_result_to_task_result(
        self,
        result: RunResult,
        *,
        parent_task: ExecutableTask,
        workspace: TaskWorkspace,
    ) -> TaskResult:
        if result.status.status == "interrupted":
            pending = result.status.pending_interrupts
            interrupt = pending[0] if pending else None
            if interrupt is not None:
                # 父 ledger 上的 bubbled interrupt 锚定父 subgraph node，保留 child ns。
                interrupt = interrupt.model_copy(update={"node_id": parent_task.node_id})
            return TaskResult(status="interrupted", interrupt=interrupt)
        if result.status.status == "stopped":
            return TaskResult(status="stopped", error=result.reason)
        if result.status.status == "failed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=result.reason,
            )
        if result.status.status != "completed":
            return TaskResult(
                status="failed",
                error_kind="internal",
                error=f"child subgraph ended in unexpected status {result.status.status}",
            )
        try:
            write_set = self._objects.freeze_write_set(
                workspace,
                claims=parent_task.resources,
                outputs=_task_outputs(parent_task),
            )
        except WorkspaceError as exc:
            return TaskResult(status="failed", error_kind="invalid_output", error=str(exc))
        return TaskResult(
            status="succeeded",
            write_set_id=write_set.write_set_id,
            outputs_sha256=dict(write_set.outputs_sha256),
        )

    # ------------------------------------------------------------------ start

    def _start_invocation(
        self,
        compiled: CompiledWorkflow,
        entrypoint: str,
        context: RuntimeContext,
        *,
        supersedes_invocation_id: str | None = None,
        replacement_authorization_id: str | None = None,
        expected_definition_request: PinnedDefinitionRequest | None = None,
    ) -> str:
        replacement = supersedes_invocation_id is not None and replacement_authorization_id is not None
        if (supersedes_invocation_id is None) ^ (replacement_authorization_id is None):
            raise GraphRuntimeError(
                "supersedes_invocation_id and replacement_authorization_id must be all-or-none"
            )

        # --- Entrypoint restart policy safety net ---
        # For "once" entrypoints, refuse if this entrypoint has a completed
        # invocation.  Exact unused D18 replacement authorization is the only bypass.
        ep = compiled.entrypoints.get(entrypoint)
        if not replacement and ep is not None and ep.restart == "once":
            scoped_latest = self.latest_root_invocation(entrypoint)
            if scoped_latest is not None:
                try:
                    scoped_proj = self._checkpoints.project(scoped_latest)
                except LedgerIntegrityError as exc:
                    raise GraphIntegrityError(str(exc)) from exc
                if scoped_proj.terminal is not None:
                    raise GraphRuntimeError(
                        f"entrypoint '{entrypoint}' already completed "
                        f"(invocation {scoped_latest}); refuse restart"
                    )

        # --- Active invocation guard (any entrypoint) ---
        if not replacement:
            latest = self.latest_root_invocation()
            if latest is not None:
                try:
                    existing = self._checkpoints.project(latest)
                except LedgerIntegrityError as exc:
                    raise GraphIntegrityError(str(exc)) from exc
                if existing.terminal is None:
                    raise GraphRuntimeError(
                        f"change already has active invocation {latest}; resume instead of run"
                    )

        if entrypoint not in compiled.entrypoints:
            raise GraphRuntimeError(f"unknown entrypoint '{entrypoint}'")
        entry = compiled.entrypoints[entrypoint]
        try:
            params = resolve_params(compiled.schema, {**entry.param_overrides, **context.params})
        except Exception as exc:  # CompileError
            raise GraphRuntimeError(f"invalid params: {exc}") from exc
        if entry.allow_expr is not None and not is_satisfied(entry.allow_expr, Scope({"params": params})):
            raise GraphRuntimeError(f"entrypoint '{entrypoint}' allow expression rejected params")

        if entrypoint == "retro":
            try:
                params = ensure_retro_params(params)
            except Exception as exc:
                raise GraphRuntimeError(f"invalid retro params: {exc}") from exc

        root_tree_id = self._objects.capture(context.project_root, repo_root=context.repo_root)
        invocation_id = str(uuid4())
        checkpoint_ns = invocation_id
        canonical_digest(params)
        max_parallel = compiled.schema.policies.scheduler.max_parallel_tasks
        graph_id = entry.graph_id

        try:
            binding = bind_root_definitions(
                store=self._objects, root_tree_id=root_tree_id, event_schema_version=6
            )
        except PolicyError:
            raise
        if expected_definition_request is not None:
            from assurance_agent.workflow.graph.definition_pinning import request_for_compiled
            from assurance_agent.workflow.graph.supersede import definition_request_digest

            live = request_for_compiled(compiled, event_schema_version=6)
            if definition_request_digest(live) != definition_request_digest(expected_definition_request):
                raise SupersedeError(
                    "conflicting_supersede",
                    "staged definition request digest drift on replacement start",
                )
        started = _build_invocation_started(
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            graph_id=graph_id,
            compiled=compiled,
            params=params,
            root_tree_id=root_tree_id,
            max_parallel_tasks=max_parallel,
            checkpoint_ns=checkpoint_ns,
            structural_path=graph_id,
            binding=binding,
            supersedes_invocation_id=supersedes_invocation_id,
            replacement_authorization_id=replacement_authorization_id,
        )
        try:
            with transaction(context.change_dir) as txn:
                if replacement:
                    assert supersedes_invocation_id is not None
                    assert replacement_authorization_id is not None
                    events_locked = txn.read_events_strict()
                    supersede_event = find_supersede_event(events_locked, supersedes_invocation_id)
                    if supersede_event is None:
                        raise SupersedeError(
                            "not_eligible",
                            "replacement authorization missing supersede event",
                        )
                    if supersede_event.action != "rerun-v6":
                        raise SupersedeError("not_eligible", "stop supersede has no replacement authority")
                    if supersede_event.replacement_authorization_id != replacement_authorization_id:
                        raise SupersedeError(
                            "conflicting_supersede",
                            "replacement_authorization_id mismatch",
                        )
                    if supersede_event.entrypoint != entrypoint:
                        raise SupersedeError(
                            "wrong_entrypoint",
                            "replacement entrypoint must match superseded root",
                        )
                    if authorization_consumed(events_locked, replacement_authorization_id):
                        existing_root = find_replacement_root(
                            events_locked,
                            supersedes_invocation_id=supersedes_invocation_id,
                            replacement_authorization_id=replacement_authorization_id,
                        )
                        if existing_root is not None:
                            return existing_root
                        raise SupersedeError(
                            "conflicting_supersede",
                            "replacement authorization already consumed",
                        )
                    if (
                        supersede_event.params_sha256 is not None
                        and canonical_digest(params) != supersede_event.params_sha256
                    ):
                        raise SupersedeError(
                            "conflicting_supersede",
                            "replacement params digest mismatch",
                        )
                txn.append_strict(started)
                self._stage_pinned_definitions(txn, compiled, binding)
                txn.write_runtime_file(
                    f".graph-runtime/invocations/{invocation_id}.json",
                    json.dumps(
                        {
                            "project_root": str(context.project_root),
                            "repo_root": str(context.repo_root),
                            "change_id": context.change_id,
                            "parent_session_id": context.parent_session_id,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        except SupersedeError:
            raise
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise GraphRuntimeError(f"duplicate invocation start: {invocation_id}") from exc
            raise
        return invocation_id

    def _bundle_for_compiled(self, compiled: CompiledWorkflow, *, event_schema_version: int) -> Any:
        from assurance_agent.workflow.graph.definition_pinning import request_for_compiled

        request = request_for_compiled(compiled, event_schema_version=event_schema_version)
        assert_live_semantic_compatibility(request)
        try:
            return self._definition_resolver(request)
        except GraphDefinitionChanged:
            raise
        except Exception as exc:
            raise GraphDefinitionChanged(
                f"definition_resolver failed for digest {compiled.digest}: {exc}"
            ) from exc

    def _stage_pinned_definitions(
        self,
        txn: object,
        compiled: CompiledWorkflow,
        binding: InvocationDefinitionBinding,
    ) -> None:
        bundle = self._bundle_for_compiled(compiled, event_schema_version=binding.event_schema_version)
        stage_pinned_definitions(
            txn,  # type: ignore[arg-type]
            compiled,
            binding,
            contracts=bundle.contracts,
            ingest_catalog=bundle.ingest_catalog,
        )

    # ------------------------------------------------------------------ resume

    def _recover_and_drive(
        self,
        invocation_id: str,
        command: ResumeCommand | None,
    ) -> RunResult:
        try:
            projection = self._checkpoints.project(invocation_id)
        except LedgerIntegrityError as exc:
            raise GraphIntegrityError(str(exc)) from exc
        context = self._context_for(projection)
        events = read_events_strict(context.change_dir)
        fenced = fence_blocks_invocation(events, invocation_id)
        if fenced is not None or projection.supersede_id is not None:
            status = self.status(invocation_id)
            return RunResult(
                invocation_id=invocation_id,
                status=status,
                exit_code=EXIT_STOPPED,
                reason="superseded",
            )
        # Manual-revision fix_and_proceed must reach _commit_manual_revision_resume
        # before open-prefix recovery can resolve the interrupt. Otherwise an
        # identical CLI retry after a repaired open prefix hits "not pending",
        # and a non-identical retry never reaches the integrity-conflict path.
        # resume(None) / ordinary actions still recover first.
        uses_manual_revision_command = self._is_manual_revision_resume_command(projection, command)
        if not uses_manual_revision_command:
            self._recover_open_revision_transitions(context)
            try:
                projection = self._checkpoints.project(invocation_id)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc
        if projection.terminal is not None:
            status = self.status(invocation_id)
            return RunResult(
                invocation_id=invocation_id,
                status=status,
                exit_code=_exit_for_status(status.status),
                reason=projection.terminal_reason or status.status,
            )
        if command is not None:
            self._commit_resume_command(projection, context, command)
            if command.action == "stop":
                status = self.status(invocation_id)
                return RunResult(
                    invocation_id=invocation_id,
                    status=status,
                    exit_code=EXIT_STOPPED,
                    reason=command.reason,
                )
            if uses_manual_revision_command:
                # Commit stages the missing suffix; recover is a no-op afterward
                # unless a concurrent open prefix remains.
                self._recover_open_revision_transitions(context)
        return self._drive(invocation_id, context)

    @staticmethod
    def _is_manual_revision_resume_command(
        projection: GraphProjection,
        command: ResumeCommand | None,
    ) -> bool:
        if command is None or command.action != "fix_and_proceed":
            return False
        if projection.event_schema_version < 5:
            return False
        pending = projection.interrupts.get(command.interrupt_id)
        return pending is not None and pending.revision_view is not None

    def _recover_open_revision_transitions(self, context: RuntimeContext) -> None:
        """Repair any open manual-revision resume prefix before ordinary recovery."""
        try:
            with transaction(context.change_dir) as txn:
                events = txn.read_events_strict()
                open_transition = find_open_revision_transition(events)
                if open_transition is None:
                    return
                stage_missing_resume_suffix(txn=txn, transition=open_transition)
        except ManualRevisionError as exc:
            raise GraphIntegrityError(str(exc)) from exc

    def _commit_resume_command(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
        command: ResumeCommand,
    ) -> None:
        if not command.reason.strip() or not command.who.strip():
            raise GraphRuntimeError("resume requires nonblank reason and who")
        pending = projection.interrupts.get(command.interrupt_id)
        if pending is None:
            raise GraphRuntimeError(f"interrupt {command.interrupt_id} is not pending")
        uses_manual_revision = (
            command.action == "fix_and_proceed"
            and projection.event_schema_version >= 5
            and pending.revision_view is not None
        )
        # After a crash that wrote root resume ordinal(s), the root interrupt is
        # already resolved while the revision resume suffix may still be open.
        # Identical fix_and_proceed retries must still reach the revision commit
        # path for suffix repair / no-op ack (and non-identical conflict).
        if pending.resolved_action is not None:
            if uses_manual_revision:
                self._commit_manual_revision_resume(projection, context, command, pending)
                return
            raise GraphRuntimeError(f"interrupt {command.interrupt_id} is not pending")
        if command.action not in pending.actions:
            raise GraphRuntimeError(
                f"action {command.action!r} not allowed for interrupt {command.interrupt_id}"
            )
        if uses_manual_revision:
            self._commit_manual_revision_resume(projection, context, command, pending)
            return

        audited = self._rehash_artifact_view(context.change_dir, pending)
        # Emit graph_resumed for every invocation along the interrupt ns
        # (root → mid → leaf). Writing only root+leaf leaves intermediate
        # subgraphs with a still-pending interrupt, so they re-bubble the
        # same gate instead of honouring resume.action.
        resume_invocation_ids = _invocation_ids_along_ns(pending.checkpoint_ns)
        if projection.invocation_id not in resume_invocation_ids:
            resume_invocation_ids.insert(0, projection.invocation_id)
        leaf_interrupt = _committed_leaf_interrupt(
            read_events_strict(context.change_dir),
            interrupt_id=pending.interrupt_id,
            owner_invocation_id=resume_invocation_ids[-1],
        )
        source_attempt = pending.source_gate_attempt_id if projection.event_schema_version >= 5 else None
        source_tree = pending.source_gate_tree_id if projection.event_schema_version >= 5 else None
        with transaction(context.change_dir) as txn:
            if projection.event_schema_version >= 3:
                parent_anchor_ref: str | None = None
                for index, invocation_id in enumerate(resume_invocation_ids):
                    layer_ns = _checkpoint_ns_for_invocation(pending.checkpoint_ns, invocation_id)
                    layer_node = _node_id_for_invocation(
                        pending.checkpoint_ns, invocation_id, leaf_interrupt.node_id
                    )
                    anchor = ResumeAnchor(
                        invocation_id=invocation_id,
                        checkpoint_ns=layer_ns,
                        node_id=layer_node,
                        interrupt_id=command.interrupt_id,
                    )
                    txn.append_strict(
                        GraphResumedEvent(
                            type="graph_resumed",
                            invocation_id=invocation_id,
                            checkpoint_ns=layer_ns,
                            interrupt_id=command.interrupt_id,
                            action=command.action,
                            reason=command.reason,
                            who=command.who,
                            audited_reads_sha256=audited if index == 0 else {},
                            anchor=anchor,
                            parent_anchor_ref=parent_anchor_ref,
                            payload=command.payload if index == 0 else {},
                            source_gate_attempt_id=source_attempt,
                            source_gate_tree_id=source_tree,
                        )
                    )
                    parent_anchor_ref = canonical_digest(anchor.model_dump(mode="json"))
            else:
                resumed = GraphResumedEvent(
                    type="graph_resumed",
                    invocation_id=resume_invocation_ids[0],
                    checkpoint_ns=pending.checkpoint_ns,
                    interrupt_id=command.interrupt_id,
                    action=command.action,
                    reason=command.reason,
                    who=command.who,
                    audited_reads_sha256=audited,
                    payload=command.payload,
                )
                for index, invocation_id in enumerate(resume_invocation_ids):
                    if index == 0:
                        txn.append_strict(resumed)
                    else:
                        txn.append_strict(
                            resumed.model_copy(update={"invocation_id": invocation_id, "payload": {}})
                        )
            if command.action == "stop":
                txn.append_strict(
                    GraphTerminalEvent(
                        type="graph_stopped",
                        invocation_id=projection.invocation_id,
                        checkpoint_ns=projection.checkpoint_ns,
                        reason=command.reason,
                    )
                )
                live = fold_after_append(
                    context.change_dir,
                    projection.invocation_id,
                    "graph_stopped",
                    command.reason,
                    projection,
                )
                txn.set_workflow_state_projection(render_workflow_state_yaml(live))

    def _commit_manual_revision_resume(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
        command: ResumeCommand,
        pending: InterruptProjection,
    ) -> None:
        """Ingest the revision view, then append revision + ordered resume prefix."""
        if (
            pending.revision_owner_invocation_id is None
            or pending.revision_base_tree_id is None
            or pending.revision_view is None
            or pending.revision_paths is None
            or pending.revision_before_sha256 is None
        ):
            raise GraphRuntimeError(f"interrupt {pending.interrupt_id} lacks committed revision metadata")
        binding = RevisionViewBinding(
            interrupt_id=pending.interrupt_id,
            owner_invocation_id=pending.revision_owner_invocation_id,
            base_tree_id=pending.revision_base_tree_id,
            view_relpath=pending.revision_view,
            logical_paths=tuple(pending.revision_paths),
            baseline=tuple(
                RevisionPathBaseline(logical_path=path, sha256=pending.revision_before_sha256[path])
                for path in pending.revision_paths
            ),
        )
        try:
            tree_revision = capture_revision_candidate(
                change_dir=context.change_dir,
                store=self._objects,
                binding=binding,
            )
        except (WorkspaceError, ManualRevisionError) as exc:
            message = str(exc)
            if "manual_plan_revision_noop" in message:
                raise GraphRuntimeError("manual_plan_revision_noop") from exc
            raise GraphRuntimeError(message) from exc

        events = read_events_strict(context.change_dir)
        interrupted = _committed_leaf_interrupt(
            events,
            interrupt_id=pending.interrupt_id,
            owner_invocation_id=pending.revision_owner_invocation_id,
        )
        owner = self._checkpoints.project(pending.revision_owner_invocation_id)
        pinned = {
            "policy_digest": owner.policy_digest,
            "gate_semantics_digest": owner.gate_semantics_digest,
            "assurance_profile_digest": owner.assurance_profile_digest,
            "graph_digest": owner.graph_digest,
            "ir_digest": owner.ir_digest,
        }
        anchors = _resume_anchors_for(pending, leaf_node_id=interrupted.node_id)
        try:
            transition = build_manual_revision_transition(
                interrupted=interrupted,
                command=command,
                revision=tree_revision,
                pinned_definition_digests=pinned,
                resume_anchors=anchors,
            )
        except ManualRevisionError as exc:
            raise GraphRuntimeError(str(exc)) from exc

        try:
            with transaction(context.change_dir) as txn:
                live_events = txn.read_events_strict()
                existing = next(
                    (
                        event
                        for event in live_events
                        if event.get("type") == "manual_plan_revision"
                        and event.get("interrupt_id") == pending.interrupt_id
                    ),
                    None,
                )
                if existing is not None:
                    committed = transition_from_committed_revision(
                        ManualPlanRevisionEvent.model_validate(
                            {k: v for k, v in existing.items() if k not in {"seq", "ts"}}
                        )
                    )
                    if (
                        committed.revision.revision_transition_id
                        != transition.revision.revision_transition_id
                    ):
                        raise GraphIntegrityError(
                            "manual revision integrity conflict: non-identical retry after commit"
                        )
                    stage_missing_resume_suffix(txn=txn, transition=committed)
                    return
                txn.append_strict(transition.revision)
                stage_missing_resume_suffix(txn=txn, transition=transition)
        except ManualRevisionError as exc:
            raise GraphIntegrityError(str(exc)) from exc

    def _rehash_artifact_view(
        self,
        change_dir: Path,
        pending: InterruptProjection,
    ) -> dict[str, str]:
        if not pending.audited_reads_sha256:
            return {}
        if not pending.artifact_view:
            raise GraphRuntimeError(
                f"interrupt {pending.interrupt_id} lacks artifact_view for audited resume"
            )
        view_root = change_dir / pending.artifact_view
        audited: dict[str, str] = {}
        for rel, expected in sorted(pending.audited_reads_sha256.items()):
            path = view_root / rel
            actual = sha256_file(path)
            if actual is None or actual != expected:
                raise GraphRuntimeError(f"audited read drift for interrupt {pending.interrupt_id}: {rel}")
            audited[rel] = actual
        return audited

    def _context_for(self, projection: GraphProjection) -> RuntimeContext:
        change_dir = self._checkpoints._change_dir  # noqa: SLF001
        meta_path = change_dir / ".graph-runtime" / "invocations" / f"{projection.invocation_id}.json"
        project_root = change_dir.parent.parent.parent
        repo_root = project_root
        change_id = change_dir.name
        parent_session_id: str | None = None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            project_root = Path(meta["project_root"])
            repo_root = Path(meta.get("repo_root", project_root))
            change_id = str(meta.get("change_id", change_id))
            parent_session_id = meta.get("parent_session_id")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return RuntimeContext(
            project_root=project_root,
            repo_root=repo_root,
            change_dir=change_dir,
            change_id=change_id,
            params=dict(projection.params),
            parent_session_id=parent_session_id,
        )

    def _child_projections(self, invocation_id: str) -> dict[str, GraphProjection]:
        children_by_parent: dict[str, list[str]] = {}
        projections: dict[str, GraphProjection] = {}
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
                continue
            parent_id = raw.get("parent_invocation_id")
            child_id = raw.get("invocation_id")
            if not isinstance(parent_id, str) or not isinstance(child_id, str):
                continue
            children_by_parent.setdefault(parent_id, []).append(child_id)

        pending = list(children_by_parent.get(invocation_id, ()))
        seen = {invocation_id}
        while pending:
            child_id = pending.pop(0)
            if child_id in seen:
                continue
            seen.add(child_id)
            try:
                projections[child_id] = self._checkpoints.project(child_id)
            except LedgerIntegrityError:
                pass
            pending.extend(children_by_parent.get(child_id, ()))
        return projections

    # ------------------------------------------------------------------ drive

    def _drive(self, invocation_id: str, context: RuntimeContext) -> RunResult:
        artifacts = self._objects
        while True:
            try:
                projection = self._reach_recovery_barrier(invocation_id, context)
            except LedgerIntegrityError as exc:
                raise GraphIntegrityError(str(exc)) from exc

            if projection.terminal is not None:
                return self._result_from_projection(projection)

            bundle = self._resolve_bundle(projection)
            compiled = bundle.compiled
            scheduler = bundle.scheduler

            wait_until = self._earliest_retry_at(projection)
            if wait_until is not None and wait_until > self._clock.now():
                delay = (wait_until - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            try:
                plan = plan_superstep(compiled, projection, context, artifacts)  # type: ignore[arg-type]
            except PlanError as exc:
                message = str(exc)
                if exc.error_kind == "graph_definition_changed":
                    raise GraphDefinitionChanged(message) from exc
                raise GraphRuntimeError(message) from exc

            child_projections = self._child_projections(invocation_id)
            inherited_lease = context.inherited_prepared_wave_lease(
                scheduler._prepared_wave_lease_owner  # noqa: SLF001
            )
            from assurance_agent.workflow.graph.selected_wave import PreparedWaveLease

            typed_lease = inherited_lease if isinstance(inherited_lease, PreparedWaveLease) else None
            prepared_entry = typed_lease.for_invocation(invocation_id) if typed_lease is not None else None
            selected = preview_selected_wave(
                compiled,
                projection,
                context,
                artifacts,  # type: ignore[arg-type]
                max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
                child_projections=child_projections,
            )
            uses_prepared = prepared_entry is not None or (
                selected is not None and (selected.synchronized_paths or selected.lock_tokens)
            )
            if plan.strict_events and not uses_prepared:
                with transaction(context.change_dir) as txn:
                    for event in plan.strict_events:
                        txn.append_strict(event)

            if plan.terminal is not None:
                return self._finish_terminal(invocation_id, context, plan)

            if not plan.tasks:
                if plan.strict_events:
                    if uses_prepared:
                        with transaction(context.change_dir) as txn:
                            for event in plan.strict_events:
                                txn.append_strict(event)
                    continue
                raise GraphRuntimeError("planner returned no tasks and no terminal")

            try:
                if prepared_entry is not None:
                    assert typed_lease is not None
                    wave = scheduler.execute_selected_wave(
                        typed_lease,
                        context,
                        invocation_id=invocation_id,
                        compiled=compiled,
                        artifacts=artifacts,  # type: ignore[arg-type]
                        child_projections=child_projections,
                    )
                    # Inherited prepared entries authorize one reserved wave only.
                    # Later supersteps must replan against the live projection.
                    context = context.without_prepared_wave_lease()
                else:
                    wave = scheduler.execute(
                        plan,
                        projection,
                        context,
                        selected_wave=selected if uses_prepared else None,
                        compiled=compiled if uses_prepared else None,
                        artifacts=artifacts if uses_prepared else None,  # type: ignore[arg-type]
                        child_projections=child_projections if uses_prepared else None,
                        inherited_lease=typed_lease,
                    )
            except SelectedWaveDriftError as exc:
                raise GraphRuntimeError(str(exc)) from exc
            except (SchedulerError, ProgressionError, WorkspaceError) as exc:
                raise GraphRuntimeError(str(exc)) from exc

            projection = self._checkpoints.project(invocation_id)
            if projection.terminal is not None:
                return self._result_from_projection(projection)

            pending_interrupts = [i for i in projection.interrupts.values() if i.resolved_action is None]
            if pending_interrupts and not plan.tasks:
                return self._result_from_projection(projection)

            if wave.interrupted:
                projection = self._checkpoints.project(invocation_id)
                return self._result_from_projection(projection)

            if wave.stopped:
                reason = "task stopped"
                for task_id in wave.stopped:
                    task_proj = projection.tasks.get(task_id)
                    if task_proj is not None and isinstance(task_proj.value, dict):
                        raw = task_proj.value.get("reason")
                        if isinstance(raw, str) and raw.strip():
                            reason = raw
                            break
                    elif (
                        task_proj is not None and isinstance(task_proj.value, str) and task_proj.value.strip()
                    ):
                        reason = task_proj.value
                        break
                stop_plan = PlanResult(
                    superstep_id=plan.superstep_id,
                    checkpoint_id=plan.checkpoint_id,
                    tasks=(),
                    terminal="stop",
                    reason=reason,
                )
                return self._finish_terminal(invocation_id, context, stop_plan)

            if wave.retry_at is not None:
                retry_at = _parse_ts(wave.retry_at)
                delay = (retry_at - self._clock.now()).total_seconds()
                if delay > 0:
                    self._clock.sleep(delay)
                continue

            continue

    def _finish_terminal(
        self,
        invocation_id: str,
        context: RuntimeContext,
        plan: PlanResult,
    ) -> RunResult:
        assert plan.terminal is not None
        if plan.terminal == "interrupt":
            projection = self._checkpoints.project(invocation_id)
            return self._result_from_projection(projection)

        projection = self._checkpoints.project(invocation_id)
        if self._recovery_work_remains(projection, invocation_id, context):
            self._reach_recovery_barrier(invocation_id, context)
            projection = self._checkpoints.project(invocation_id)
            if self._recovery_work_remains(projection, invocation_id, context):
                raise GraphRuntimeError("cannot terminal while durable recovery work remains")

        event_type: Literal["graph_completed", "graph_stopped", "graph_failed"]
        if plan.terminal == "end":
            event_type = "graph_completed"
        elif plan.terminal == "stop":
            event_type = "graph_stopped"
        else:
            event_type = "graph_failed"
        reason = plan.reason or plan.terminal
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                GraphTerminalEvent(
                    type=event_type,
                    invocation_id=invocation_id,
                    checkpoint_ns=projection.checkpoint_ns,
                    reason=reason,
                )
            )
            live = fold_after_append(context.change_dir, invocation_id, event_type, reason, projection)
            txn.set_workflow_state_projection(render_workflow_state_yaml(live))
        return self._result_from_projection(self._checkpoints.project(invocation_id))

    def _result_from_projection(self, projection: GraphProjection) -> RunResult:
        status = graph_status_from_projection(
            projection,
            pending_write_sets=self._pending_write_sets(projection.invocation_id),
        )
        return RunResult(
            invocation_id=projection.invocation_id,
            status=status,
            exit_code=_exit_for_status(status.status),
            reason=status.terminal_reason or status.status,
        )

    def _request_from_projection(self, projection: GraphProjection) -> PinnedDefinitionRequest:
        return PinnedDefinitionRequest(
            graph_digest=projection.graph_digest,
            ingest_catalog_digest=projection.ingest_catalog_digest,
            contract_digests=tuple(sorted(projection.contract_digests.items())),
            event_schema_version=projection.event_schema_version,
            gate_semantics_digest=projection.gate_semantics_digest,
            assurance_profile_digest=projection.assurance_profile_digest,
            gate_semantics_object_id=projection.gate_semantics_object_id,
            topology_safety_semantics_object_id=projection.topology_safety_semantics_object_id,
            topology_safety_semantics_digest=projection.topology_safety_semantics_digest,
            commit_safety_semantics_object_id=projection.commit_safety_semantics_object_id,
            commit_safety_semantics_digest=projection.commit_safety_semantics_digest,
        )

    def _resolve_bundle(self, projection: GraphProjection) -> Any:
        request = self._request_from_projection(projection)
        assert_live_semantic_compatibility(request)
        try:
            bundle = self._definition_resolver(request)
        except GraphDefinitionChanged:
            raise
        except Exception as exc:
            raise GraphDefinitionChanged(
                f"definition_resolver failed for request {request.graph_digest}: {exc}"
            ) from exc
        if bundle.compiled.digest != projection.graph_digest:
            raise GraphDefinitionChanged(
                f"graph_definition_changed: resolver digest {bundle.compiled.digest} != "
                f"pinned {projection.graph_digest}"
            )
        if bundle.compiled.contract_digests != projection.contract_digests:
            raise GraphDefinitionChanged("graph_definition_changed: contract digests drifted")
        if (
            projection.ingest_catalog_digest
            and bundle.compiled.ingest_catalog_digest != projection.ingest_catalog_digest
        ):
            raise GraphDefinitionChanged("graph_definition_changed: ingest catalog digest drifted")
        self._bundle_cache[request] = bundle
        return bundle

    def _resolve_compiled(self, projection: GraphProjection) -> CompiledWorkflow:
        return self._resolve_bundle(projection).compiled

    def _scheduler_for(self, projection: GraphProjection) -> Scheduler:
        return self._resolve_bundle(projection).scheduler

    def _contracts_for(self, projection: GraphProjection) -> ExecutionContractCatalog:
        return self._resolve_bundle(projection).contracts

    def _reconcile_running(self, projection: GraphProjection, context: RuntimeContext) -> None:
        leases = LeaseRegistry(context.change_dir)
        recover_running_tasks(
            context.change_dir,
            projection=projection,
            leases=leases.read_all(),
            reconnect_for=lambda _task_id: False,
            probe=SystemLivenessProbe(),
            now=self._clock.now(),
        )

    def _reach_recovery_barrier(self, invocation_id: str, context: RuntimeContext) -> GraphProjection:
        """Reconcile and replay durable updates until no recovery seam reports progress."""
        self._recover_open_revision_transitions(context)
        projection = self._checkpoints.project(invocation_id)
        # Compatibility evaluation runs before definition-dependent recovery/dispatch.
        decision = self._enforce_resume_compatibility(projection, context)
        if not decision.allowed:
            raise ResumeCompatibilityBarrier(decision)

        # Definition-dependent recovery requires a live-compatible resolved bundle.
        projection = self._checkpoints.project(invocation_id)
        self._resolve_bundle(projection)
        while True:
            projection = self._checkpoints.project(invocation_id)
            self._reconcile_running(projection, context)
            projection = self._checkpoints.project(invocation_id)

            progress = False
            if self._commit_pending_write_sets(projection, context):
                progress = True
                projection = self._checkpoints.project(invocation_id)
            if self._replay_committed_publications(projection, context):
                progress = True
                projection = self._checkpoints.project(invocation_id)
            if self._reconcile_durable_effects(projection, context):
                progress = True
                projection = self._checkpoints.project(invocation_id)

            if not progress and not self._recovery_work_remains(projection, invocation_id, context):
                break
            if not progress:
                raise GraphRuntimeError("recovery barrier stalled with durable work remaining")

        projection = self._checkpoints.project(invocation_id)
        self._repair_ordinary_materialization(projection, context)
        return self._checkpoints.project(invocation_id)

    def _enforce_resume_compatibility(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> ResumeCompatibilityDecision:
        """Skip topology audit; same-definition resume is allowed for all schema versions."""
        return ResumeCompatibilityDecision(
            schema_version="1",
            allowed=True,
            event_schema_version=projection.event_schema_version,
            root_invocation_id=projection.parent_invocation_id or projection.invocation_id,
        )

    def _load_topology_compatibility_receipt(
        self,
        root_invocation_id: str,
        change_dir: Path,
    ):
        from assurance_agent.workflow.core.events import read_events_strict
        from assurance_agent.workflow.core.graph_events import (
            TopologySafetyCompatibilityRecordedEvent,
        )

        for raw in read_events_strict(change_dir):
            if raw.get("source") != "graph" or raw.get("invocation_id") != root_invocation_id:
                continue
            if raw.get("type") != "topology_safety_compatibility_recorded":
                continue
            payload = {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
            event = TopologySafetyCompatibilityRecordedEvent.model_validate(payload)
            return event_to_receipt(event)
        return None

    def _legacy_profile_reconstructable(
        self,
        projection: GraphProjection,
        change_dir: Path,
    ) -> bool:
        from assurance_agent.verification.profile_manifest import (
            assurance_profile_digest,
            assurance_profile_snapshot_relpath,
        )

        if not projection.assurance_profile_digest:
            return False
        if projection.event_schema_version >= 5:
            path = change_dir / assurance_profile_snapshot_relpath(projection.assurance_profile_digest)
            return path.is_file()
        # v4: uniquely reconstruct only when recorded digest equals current runtime bytes.
        return projection.assurance_profile_digest == assurance_profile_digest()

    def _recovery_work_remains(
        self,
        projection: GraphProjection,
        invocation_id: str,
        context: RuntimeContext,
    ) -> bool:
        if any(task.status == "running" for task in projection.tasks.values()):
            return True
        if self._pending_write_sets(invocation_id):
            return True
        planned = self._last_uncommitted_plan(invocation_id)
        if planned is not None and any(
            task.status == "succeeded" and not task.outputs_committed for task in projection.tasks.values()
        ):
            return True
        publication_store = ProjectPublicationStore(context.project_root)
        for publication, status in publication_store.list_publications(invocation_id=invocation_id):
            if status != "applied":
                return True
        if self._due_unacknowledged_effects_remain(projection, context):
            return True
        return False

    def _reconcile_durable_effects(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        """Reconcile committed-but-unacked inline effects; no handler reinvoke."""
        pending = scan_unacknowledged_intents(context.change_dir, projection.invocation_id)
        if not pending:
            return False
        fence_store = RootEffectFenceStore(context.project_root)
        retry_store = EffectRetryStore(context.project_root)
        effect_runtime = DurableEffectRuntime(
            change_dir=context.change_dir,
            project_root=context.project_root,
            fence_store=fence_store,
            retry_store=retry_store,
        )
        scheduler = self._scheduler_for(projection)
        registry = getattr(scheduler, "_effect_registry", None) or production_effect_registry()
        root_id = projection.parent_invocation_id or projection.invocation_id
        now = self._clock.now()
        progress = False
        for success, intent in pending:
            sidecar = retry_store.load(intent.effect_id)
            if sidecar is not None and not retry_store.is_due(sidecar, now=now):
                continue
            task = projection.tasks.get(success.task_id)
            outputs = dict(sorted((success.outputs_sha256 or {}).items()))
            target = (
                task.target
                if task is not None and task.target is not None and task.target.strip()
                else (task.node_id if task is not None else success.task_id)
            )
            effect_context = DurableEffectContext(
                root_invocation_id=root_id,
                invocation_id=success.invocation_id,
                task_id=success.task_id,
                attempt_id=success.attempt_id,
                target=target,
                output_digests=outputs,
                write_set_id=success.write_set_id,
            )
            try:
                reconcile_effect(intent, effect_context, effect_runtime, registry=registry)
                progress = True
            except RootTerminalFenceError:
                # Prepared/committed fence suppresses or permanently rejects retry.
                continue
            except DurableEffectRetryableError as exc:
                try:
                    retry_store.schedule_next(
                        fence_store=fence_store,
                        root_invocation_id=root_id,
                        invocation_id=success.invocation_id,
                        task_id=success.task_id,
                        attempt_id=success.attempt_id,
                        effect_id=intent.effect_id,
                        kind=intent.kind,
                        lock_key=f"effect:{intent.effect_id}",
                        error_code=exc.error_code,
                        now=now,
                        expected=sidecar,
                    )
                except RootTerminalFenceError:
                    # Fence prepared/committed during the retryable window: suppress.
                    continue
            except DurableEffectIntegrityError as exc:
                record_integrity_failure(
                    context.change_dir,
                    invocation_id=success.invocation_id,
                    checkpoint_ns=success.checkpoint_ns,
                    task_id=success.task_id,
                    attempt_id=success.attempt_id,
                    effect_id=intent.effect_id,
                    reason=str(exc),
                )
                progress = True
        return progress

    def _due_unacknowledged_effects_remain(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        pending = scan_unacknowledged_intents(context.change_dir, projection.invocation_id)
        if not pending:
            return False
        retry_store = EffectRetryStore(context.project_root)
        now = self._clock.now()
        for _success, intent in pending:
            sidecar = retry_store.load(intent.effect_id)
            if sidecar is None or retry_store.is_due(sidecar, now=now):
                return True
        return False

    def _commit_pending_write_sets(self, projection: GraphProjection, context: RuntimeContext) -> bool:
        events = read_events_strict(context.change_dir)
        if fence_blocks_invocation(events, projection.invocation_id) is not None:
            return False
        fence_store = RootEffectFenceStore(context.project_root)
        root_id = projection.parent_invocation_id or projection.invocation_id
        try:
            fence_store.reject_if_terminal(root_id)
        except RootTerminalFenceError as exc:
            raise GraphRuntimeError(f"supersede fence rejects write-set commit: {exc}") from exc
        planned = self._last_uncommitted_plan(projection.invocation_id)
        if planned is None:
            return False
        succeeded = [
            task_id
            for task_id, task in projection.tasks.items()
            if task.status == "succeeded" and not task.outputs_committed
        ]
        if not succeeded:
            return False
        plan = PlanResult(
            superstep_id=planned["superstep_id"],
            checkpoint_id=planned["checkpoint_id"],
            tasks=(),
        )
        try:
            return self._scheduler_for(projection).commit_pending_write_sets(
                plan=plan,
                projection=projection,
                context=context,
                succeeded_ids=succeeded,
            )
        except (WorkspaceError, ProgressionError, SchedulerError, ValueError) as exc:
            raise GraphRuntimeError(f"failed to commit pending write sets: {exc}") from exc

    def _replay_committed_publications(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        events = read_events_strict(self._checkpoints._change_dir)  # noqa: SLF001
        if fence_blocks_invocation(events, projection.invocation_id) is not None:
            return False
        fence_store = RootEffectFenceStore(context.project_root)
        root_id = projection.parent_invocation_id or projection.invocation_id
        try:
            fence_store.reject_if_terminal(root_id)
        except RootTerminalFenceError as exc:
            raise GraphRuntimeError(f"supersede fence rejects publication replay: {exc}") from exc
        progress = False
        committed_tree_ids: list[str] = []
        last_target: str | None = None
        for raw in events:
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            target_tree = raw.get("target_tree_id")
            if isinstance(target_tree, str):
                committed_tree_ids.append(target_tree)
                last_target = target_tree
            raw_checkpoint_id = raw.get("checkpoint_id")
            publication_id = raw_checkpoint_id if isinstance(raw_checkpoint_id, str) else None
            raw_ids = raw.get("write_set_ids")
            write_set_ids = tuple(
                value for value in (raw_ids if isinstance(raw_ids, list) else []) if isinstance(value, str)
            )
            if publication_id is None or not write_set_ids:
                continue
            try:
                if self._scheduler_for(projection).repair_committed_write_sets(
                    context=context,
                    invocation_id=projection.invocation_id,
                    publication_id=publication_id,
                    write_set_ids=write_set_ids,
                ):
                    progress = True
            except TargetedWorkspaceDrift as exc:
                # Nested graph resume in a freshly materialized parent-task workspace
                # can false-positive synchronized-path drift; rehydrate cumulative delta.
                try:
                    if projection.parent_task_id is None or last_target is None:
                        raise exc
                    self._objects.apply_tree_delta(
                        context.project_root,
                        last_target,
                        source_base_tree_id=projection.root_tree_id,
                        destination_base_tree_id=projection.root_tree_id,
                        acceptable_live_tree_ids=tuple(committed_tree_ids[:-1]),
                    )
                    if self._scheduler_for(projection).repair_committed_write_sets(
                        context=context,
                        invocation_id=projection.invocation_id,
                        publication_id=publication_id,
                        write_set_ids=write_set_ids,
                    ):
                        progress = True
                except (SchedulerError, WorkspaceError) as recovery_exc:
                    raise GraphRuntimeError(
                        f"failed to replay committed publication: {recovery_exc}"
                    ) from recovery_exc
            except (SchedulerError, WorkspaceError) as exc:
                raise GraphRuntimeError(f"failed to replay committed publication: {exc}") from exc
        return progress

    def _repair_ordinary_materialization(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> None:
        if not self._ordinary_materialization_drift(projection, context):
            return
        # A nested interrupt can bubble out before the parent superstep commits.
        # In that state the leaf gate tree is newer than the parent's
        # ``current_tree_id``, but it is still ledger-pinned and already
        # materialized in the canonical workspace.  After the resume command is
        # committed, validate/rehydrate that exact tree instead of treating the
        # child's ordinary outputs as external drift against the stale parent
        # edge.  ``apply_tree`` with an identical base/target remains fail-closed
        # for arbitrary repo bytes while allowing runtime-owned change artifacts
        # to be restored.
        prev, target, _, _write_set_ids = self._last_committed_tree_edge(projection)
        resumed_gate_tree = self._latest_resolved_interrupt_tree(projection)
        if resumed_gate_tree is not None and target == resumed_gate_tree:
            try:
                self._objects.apply_tree(
                    context.project_root,
                    resumed_gate_tree,
                    base_tree_id=resumed_gate_tree,
                    restore_change_drift=True,
                )
            except WorkspaceError as exc:
                raise GraphRuntimeError(f"failed to repair materialization: {exc}") from exc
            return
        assert target is not None
        try:
            if projection.parent_task_id is not None:
                # A resumed nested invocation gets a freshly materialized
                # parent-task workspace. It may therefore be at the child root,
                # or at any exact committed prefix, rather than at the base of
                # only the last edge. Replay the cumulative child delta and
                # whitelist only ledger-pinned intermediate trees; arbitrary
                # external bytes still fail closed in apply_tree_delta.
                committed_targets = self._committed_tree_targets(projection)
                self._objects.apply_tree_delta(
                    context.project_root,
                    target,
                    source_base_tree_id=projection.root_tree_id,
                    destination_base_tree_id=projection.root_tree_id,
                    acceptable_live_tree_ids=committed_targets[:-1],
                )
            else:
                base = prev if prev is not None else projection.root_tree_id
                self._objects.apply_tree(
                    context.project_root,
                    target,
                    base_tree_id=base,
                    restore_change_drift=True,
                )
        except WorkspaceError as exc:
            raise GraphRuntimeError(f"failed to repair materialization: {exc}") from exc

    @staticmethod
    def _latest_resolved_interrupt_tree(projection: GraphProjection) -> str | None:
        for interrupt in reversed(tuple(projection.interrupts.values())):
            if (
                interrupt.resolved_action is not None
                and interrupt.source_gate_tree_id is not None
                and interrupt.checkpoint_ns.rsplit("/", 1)[-1] == projection.invocation_id
            ):
                return interrupt.source_gate_tree_id
        return None

    def _committed_tree_targets(self, projection: GraphProjection) -> tuple[str, ...]:
        targets: list[str] = []
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            target = raw.get("target_tree_id")
            if isinstance(target, str):
                targets.append(target)
        return tuple(targets)

    def _ordinary_materialization_drift(
        self,
        projection: GraphProjection,
        context: RuntimeContext,
    ) -> bool:
        prev, target, publication_id, write_set_ids = self._last_committed_tree_edge(projection)
        if target is None or publication_id is None:
            return False
        if write_set_ids:
            write_sets = [self._objects.load_write_set(write_set_id) for write_set_id in write_set_ids]
            if any(write_set.synchronized_paths for write_set in write_sets):
                return False
        try:
            current = self._objects.capture(context.project_root, repo_root=context.repo_root)
        except WorkspaceError:
            return False
        return current != target

    def _last_committed_tree_edge(
        self,
        projection: GraphProjection,
    ) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        cursor = projection.root_tree_id
        last_prev: str | None = None
        last_target: str | None = None
        last_publication_id: str | None = None
        last_write_set_ids: tuple[str, ...] = ()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
                continue
            if raw.get("type") != "superstep_committed":
                continue
            target_tree = raw.get("target_tree_id")
            if isinstance(target_tree, str):
                last_prev = cursor
                last_target = target_tree
                raw_checkpoint_id = raw.get("checkpoint_id")
                last_publication_id = raw_checkpoint_id if isinstance(raw_checkpoint_id, str) else None
                raw_ids = raw.get("write_set_ids")
                last_write_set_ids = tuple(
                    value
                    for value in (raw_ids if isinstance(raw_ids, list) else [])
                    if isinstance(value, str)
                )
                cursor = target_tree
        return last_prev, last_target, last_publication_id, last_write_set_ids

    def _last_uncommitted_plan(self, invocation_id: str) -> dict[str, str] | None:
        last_plan: dict[str, str] | None = None
        committed: set[str] = set()
        for raw in read_events_strict(self._checkpoints._change_dir):  # noqa: SLF001
            if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
                continue
            if raw.get("type") == "superstep_planned":
                sid = raw.get("superstep_id")
                cid = raw.get("checkpoint_id")
                if isinstance(sid, str) and isinstance(cid, str):
                    last_plan = {"superstep_id": sid, "checkpoint_id": cid}
            elif raw.get("type") == "superstep_committed":
                sid = raw.get("superstep_id")
                if isinstance(sid, str):
                    committed.add(sid)
        if last_plan is None or last_plan["superstep_id"] in committed:
            return None
        return last_plan

    def _pending_write_sets(self, invocation_id: str) -> tuple[str, ...]:
        return _pending_write_sets_fn(read_events_strict(self._checkpoints.change_dir), invocation_id)

    def _earliest_retry_at(self, projection: GraphProjection) -> datetime | None:
        times = [
            _parse_ts(task.next_retry_at)
            for task in projection.tasks.values()
            if task.next_retry_at is not None and task.status in ("failed", "pending")
        ]
        context = self._context_for(projection)
        retry_store = EffectRetryStore(context.project_root)
        for _success, intent in scan_unacknowledged_intents(context.change_dir, projection.invocation_id):
            sidecar = retry_store.load(intent.effect_id)
            if sidecar is not None:
                times.append(parse_rfc3339_z(sidecar.next_retry_at))
        return min(times) if times else None


def fold_after_append(
    change_dir: Path,
    invocation_id: str,
    event_type: str,
    reason: str,
    projection: GraphProjection,
) -> GraphProjection:
    """事务内尚未落盘时，构造带 terminal 的投影供 workflow-state staging。"""
    del change_dir, invocation_id  # 签名保留与 Task 11 一致；投影由调用方传入
    terminal_map = {
        "graph_completed": "completed",
        "graph_stopped": "stopped",
        "graph_failed": "failed",
    }
    return projection.model_copy(
        update={
            "terminal": terminal_map[event_type],
            "terminal_reason": reason,
            "event_seq": projection.event_seq + 1,
        }
    )


def _exit_for_status(
    status: Literal["running", "interrupted", "completed", "stopped", "failed"],
) -> Literal[0, 20, 30, 40]:
    if status == "completed":
        return EXIT_COMPLETED  # type: ignore[return-value]
    if status == "stopped":
        return EXIT_STOPPED  # type: ignore[return-value]
    if status == "interrupted":
        return EXIT_HUMAN_REVIEW  # type: ignore[return-value]
    if status == "failed":
        return EXIT_ERROR  # type: ignore[return-value]
    return EXIT_ERROR  # type: ignore[return-value]


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _task_outputs(task: ExecutableTask) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, dict):
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
            return tuple(outputs)
    return ()


def _child_invocation_from_ns(checkpoint_ns: str, root_ns: str) -> str | None:
    """``<parent-ns>/<node>/<child-invocation-id>`` → child invocation id。"""
    if checkpoint_ns == root_ns or not checkpoint_ns.startswith(root_ns + "/"):
        return None
    parts = checkpoint_ns.split("/")
    if len(parts) < 3:
        return None
    return parts[-1]


def _invocation_ids_along_ns(checkpoint_ns: str) -> list[str]:
    """Return invocation ids embedded in a checkpoint namespace.

    Namespace shape is ``inv0/node1/inv1/node2/inv2/...`` — even-index segments
    are invocation ids, odd-index segments are node ids.
    """
    parts = [part for part in checkpoint_ns.split("/") if part]
    return [parts[index] for index in range(0, len(parts), 2)]


def _resume_anchors_for(
    pending: InterruptProjection,
    *,
    leaf_node_id: str | None = None,
) -> tuple[ResumeAnchor, ...]:
    invocation_ids = _invocation_ids_along_ns(pending.checkpoint_ns)
    anchors: list[ResumeAnchor] = []
    for invocation_id in invocation_ids:
        anchors.append(
            ResumeAnchor(
                invocation_id=invocation_id,
                checkpoint_ns=_checkpoint_ns_for_invocation(pending.checkpoint_ns, invocation_id),
                node_id=_node_id_for_invocation(
                    pending.checkpoint_ns,
                    invocation_id,
                    leaf_node_id or pending.node_id,
                ),
                interrupt_id=pending.interrupt_id,
            )
        )
    return tuple(anchors)


def _committed_leaf_interrupt(
    events: list[dict[str, object]],
    *,
    interrupt_id: str,
    owner_invocation_id: str,
) -> GraphInterruptedEvent:
    for event in reversed(events):
        if event.get("type") != "graph_interrupted":
            continue
        if event.get("interrupt_id") != interrupt_id:
            continue
        if event.get("invocation_id") != owner_invocation_id:
            continue
        payload = {key: value for key, value in event.items() if key not in {"seq", "ts"}}
        return GraphInterruptedEvent.model_validate(payload)
    raise GraphRuntimeError(
        f"committed leaf interrupt {interrupt_id} missing for owner {owner_invocation_id}"
    )


def _checkpoint_ns_for_invocation(full_ns: str, invocation_id: str) -> str:
    parts = [part for part in full_ns.split("/") if part]
    for index in range(0, len(parts), 2):
        if parts[index] == invocation_id:
            return "/".join(parts[: index + 1])
    return parts[0] if parts else full_ns


def _node_id_for_invocation(full_ns: str, invocation_id: str, default_node_id: str) -> str:
    parts = [part for part in full_ns.split("/") if part]
    for index in range(0, len(parts), 2):
        if parts[index] == invocation_id and index + 1 < len(parts):
            return parts[index + 1]
    return default_node_id


def _strip_sha_prefix(value: str) -> str:
    if value.startswith("sha256:"):
        return value[len("sha256:") :]
    return value


def ensure_retro_params(params: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
    """Inject a generated retro_id when params.retro_id is empty/missing.

    Called before plan_superstep when entrypoint == "retro" so the retro_id
    path segment is always non-empty and path-safe before any node runs.
    """
    from assurance_agent.identifiers import assert_path_segment_safe

    out = dict(params)
    rid = out.get("retro_id")
    if not isinstance(rid, str) or not rid.strip():
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        out["retro_id"] = f"retro-{stamp}"
    assert_path_segment_safe(str(out["retro_id"]), label="retro id")
    return out


__all__ = [
    "GraphDefinitionChanged",
    "GraphIntegrityError",
    "GraphRuntime",
    "GraphRuntimeError",
    "ResumeCompatibilityBarrier",
    "ensure_retro_params",
    "graph_status_from_projection",
]
