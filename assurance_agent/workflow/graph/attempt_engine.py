"""One task attempt: started/settled events, workspace, and lease.

Scheduler owns wave select/execute/commit. This module owns the per-task
lifecycle only. There is one implementation; it is not a swappable port.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import (
    BudgetConsumedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptStoppedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, fold_invocation_events
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.durable_effects import (
    DurableEffectValidationError,
    EffectRegistry,
    intents_as_wire,
    validate_result_intents,
)
from assurance_agent.workflow.graph.leases import (
    Clock,
    LeaseRegistry,
    compute_next_retry_at,
    heartbeat_while,
    new_lease,
    next_attempt_decision,
    parent_task_has_child_invocation,
)
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.invariants import outputs_hit_invariants
from assurance_agent.workflow.graph.precommit import (
    CROSS_ARTIFACT_INVARIANTS_V1,
    CandidateValidationError,
    PrecommitValidationContext,
    infer_assurance_layer,
    load_case_documents_from_snapshot,
    load_plan_text_from_snapshot,
    resolve_precommit_validator,
    validate_candidate,
)
from assurance_agent.workflow.graph.resume_wire import build_graph_interrupted_event
from assurance_agent.workflow.graph.task_inputs import (
    TaskInputError,
    capture_task_input_snapshot,
    load_task_input_snapshot,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.task_runner import NodeRunner
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
    WriteSet,
)
from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger


@dataclass
class _PreparedAttempt:
    task: ExecutableTask
    attempt_id: str
    attempt_number: int
    workspace: TaskWorkspace | None
    bypass: bool = False
    bypass_write_set_id: str | None = None
    input_snapshot_id: str | None = None
    runtime_context_sha256: str | None = None
    candidate_validation_receipt_id: str | None = None


@dataclass
class _SettledAttempt:
    task_id: str
    status: Literal["succeeded", "failed", "interrupted", "stopped"]
    write_set_id: str | None = None
    retry_at: str | None = None
    candidate_validation_receipt_id: str | None = None


class AttemptEngine:
    """Run one planned task through start, handler, and settle.

    Reads live Scheduler fields so tests that rebind ``_runner`` / crash hooks
    after construction keep working. This is a back-reference, not a port.
    """

    def __init__(self, owner: object) -> None:
        self._owner = owner

    @property
    def _checkpoints(self) -> CheckpointStore:
        return self._owner._checkpoints  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _objects(self) -> TreeStore:
        return self._owner._objects  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _clock(self) -> Clock:
        return self._owner._clock  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _workspaces(self) -> WorkspaceBackend | None:
        return self._owner._workspaces  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _runner(self) -> NodeRunner | None:
        return self._owner._runner  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _contracts(self) -> ExecutionContractCatalog | None:
        return self._owner._contracts  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _effect_registry(self) -> EffectRegistry:
        return self._owner._effect_registry  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _crash_after_snapshot(self) -> Callable[[ExecutableTask, str], None] | None:
        return self._owner._crash_after_snapshot  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _crash_after_started(self) -> Callable[[ExecutableTask, str], None] | None:
        return self._owner._crash_after_started  # type: ignore[attr-defined]  # noqa: SLF001

    def _begin_attempt(
        self,
        task: ExecutableTask,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        leases: LeaseRegistry,
        *,
        base_tree_id: str | None = None,
    ) -> _PreparedAttempt | None:
        assert self._workspaces is not None
        existing = projection.tasks.get(task.task_id)
        if existing is not None and existing.status == "succeeded":
            # resume：已投影成功的 sibling 绕过 handler，重新加入 pending wave。
            return _PreparedAttempt(
                task=task,
                attempt_id=existing.latest_attempt_id or f"{task.task_id}-a{max(existing.attempts_used, 1)}",
                attempt_number=max(existing.attempts_used, 1),
                workspace=None,
                bypass=True,
                bypass_write_set_id=existing.write_set_id,
                input_snapshot_id=existing.input_snapshot_id,
                runtime_context_sha256=existing.runtime_context_sha256,
                candidate_validation_receipt_id=existing.candidate_validation_receipt_id,
            )
        if existing is not None and existing.status == "running":
            return None

        decision = next_attempt_decision(
            task=task,
            projection=projection,
            now=self._clock.now(),
            allow_graph_wrapper_child_resume=_allow_graph_wrapper_child_resume(task, context),
        )
        if decision.kind != "start" or decision.attempt_number is None:
            return None

        attempt_number = decision.attempt_number
        attempt_id = f"{task.task_id}-a{attempt_number}"
        started_at = self._clock.now()
        lease_seconds = max(
            task.timeout_policy.heartbeat_seconds * 3.0,
            task.timeout_policy.heartbeat_seconds + 1.0,
        )
        lease_expires_at = (started_at + timedelta(seconds=lease_seconds)).isoformat()

        # Reserve identity/lease before materializing the workspace.
        leases.upsert(
            new_lease(
                task_id=task.task_id,
                attempt_id=attempt_id,
                session_id=context.parent_session_id,
                started_at=started_at.isoformat(),
                lease_expires_at=lease_expires_at,
            )
        )

        effective_base = base_tree_id or projection.current_tree_id
        # Deferred nested capture leaves outer SelectedInvocationWave.synchronized_paths
        # empty when the child invocation does not exist yet. Graph tasks still carry
        # the descendant footprint on ``task.resources.synchronized``; overlay those
        # immutable siblings into the freeze/materialize base so nested apply/repair
        # into this sandbox does not look like an unauthorized write at parent freeze.
        if task.target.startswith("graph:") and task.resources.synchronized:
            effective_base = self._objects.overlay_synchronized_paths(
                effective_base,
                context.project_root,
                tuple(
                    sorted(
                        task.resources.synchronized,
                        key=lambda path: (path.root, path.pattern),
                    )
                ),
            )
        sidecar_root = self._workspaces.sidecar_root_for(task.task_id)
        workspace: TaskWorkspace | None = None
        input_snapshot_id: str | None = None
        runtime_context_sha256: str | None = None
        precommit_validator = self._resolve_started_validator(task)
        started_appended = False
        try:
            workspace = self._workspaces.create(
                task_id=task.task_id,
                base_tree_id=effective_base,
                store=self._objects,
                sidecar_root=sidecar_root,
                side_effect_free=self._is_side_effect_free(task),
                claims=task.resources,
                declared_reads_only=self._uses_declared_read_isolation(task),
                skill_name=(task.target.partition(":")[2] if task.target.startswith("skill:") else None),
                initialize_git=self._requires_convenience_git(task),
            )
            if self._uses_declared_read_isolation(task) or precommit_validator is not None:
                contract = None if self._contracts is None else self._contracts.contracts.get(task.target)
                if contract is None:
                    from assurance_agent.workflow.graph.scheduler import SchedulerError

                    raise SchedulerError(f"task {task.task_id} missing execution contract for {task.target}")
                from assurance_agent.workflow.graph.task_inputs import (
                    build_automatic_plan_fixer_runtime_context,
                    write_runtime_context_sidecar,
                )

                runtime_context = build_automatic_plan_fixer_runtime_context(
                    target=task.target,
                    change_id=context.change_id,
                    root_invocation_id=task.invocation_id,
                    invocation_id=task.invocation_id,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    base_tree_id=workspace.base_tree_id,
                    workspace=workspace,
                )
                if runtime_context is not None:
                    write_runtime_context_sidecar(workspace, runtime_context)
                snapshot_id, snapshot_bytes = capture_task_input_snapshot(
                    invocation_id=task.invocation_id,
                    task=task,
                    attempt_id=attempt_id,
                    workspace=workspace,
                    contract=contract,
                    runtime_context=runtime_context,
                )
                store_task_input_snapshot(self._objects, snapshot_id, snapshot_bytes)
                # Require the object be loadable before the started event.
                loaded = load_task_input_snapshot(self._objects, snapshot_id)
                if loaded.attempt_id != attempt_id:
                    raise TaskInputError("captured snapshot attempt_id mismatch")
                input_snapshot_id = snapshot_id
                runtime_context_sha256 = loaded.runtime_context_sha256
                if self._crash_after_snapshot is not None:
                    self._crash_after_snapshot(task, snapshot_id)

            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    TaskAttemptStartedEvent(
                        type="task_attempt_started",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=task.checkpoint_ns,
                        superstep_id=plan.superstep_id,
                        task_id=task.task_id,
                        attempt_id=attempt_id,
                        node_id=task.node_id,
                        input_sha256=task.input_sha256,
                        graph_digest=projection.graph_digest,
                        contract_digest=task.contract_digest,
                        attempt_number=attempt_number,
                        lease_expires_at=lease_expires_at,
                        started_at=started_at.isoformat(),
                        input_snapshot_id=input_snapshot_id,
                        runtime_context_sha256=runtime_context_sha256,
                        precommit_validator=precommit_validator,
                        target=task.target,
                    )
                )
            started_appended = True
            if self._crash_after_started is not None:
                self._crash_after_started(task, attempt_id)
        except BaseException:
            if not started_appended:
                self._cleanup_unreachable_attempt(
                    context=context,
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    workspace=workspace,
                    sidecar_root=sidecar_root,
                    input_snapshot_id=input_snapshot_id,
                )
                leases.remove(task.task_id, attempt_id)
            raise

        return _PreparedAttempt(
            task=task,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            workspace=workspace,
            input_snapshot_id=input_snapshot_id,
            runtime_context_sha256=runtime_context_sha256,
        )

    def _cleanup_unreachable_attempt(
        self,
        *,
        context: RuntimeContext,
        task_id: str,
        attempt_id: str,
        workspace: TaskWorkspace | None,
        sidecar_root: Path,
        input_snapshot_id: str | None,
    ) -> None:
        """Clean unreachable sidecar/CAS data left by a pre-started crash."""
        if workspace is not None:
            workspace.cleanup()
        else:
            task_root = context.change_dir / ".graph-runtime" / "tasks" / task_id
            shutil.rmtree(task_root, ignore_errors=True)
            shutil.rmtree(sidecar_root, ignore_errors=True)
        if input_snapshot_id is not None:
            # Snapshot CAS is content-addressed; deleting the unreachable object
            # is best-effort and ignored when another attempt already reused it.
            try:
                object_path = (
                    context.change_dir
                    / ".graph-runtime"
                    / "objects"
                    / "sha256"
                    / input_snapshot_id[:2]
                    / input_snapshot_id
                )
                object_path.unlink(missing_ok=True)
            except OSError:
                pass
        _ = attempt_id

    def _run_attempt(
        self,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        leases: LeaseRegistry,
    ) -> _SettledAttempt:
        assert self._runner is not None
        task = prepared.task
        workspace = prepared.workspace
        assert workspace is not None
        heartbeat_seconds = task.timeout_policy.heartbeat_seconds
        lease_extension = max(heartbeat_seconds * 3.0, heartbeat_seconds + 1.0)
        try:
            if task.evidence_bindings:
                from assurance_agent.workflow.graph.evidence import (
                    EvidenceResolutionError,
                    resolve_evidence_values,
                )
                from assurance_agent.workflow.graph.task_runner import task_failure

                try:
                    values = resolve_evidence_values(projection, task.evidence_bindings)
                except EvidenceResolutionError as exc:
                    return self._persist_result(
                        prepared=prepared,
                        plan=plan,
                        projection=projection,
                        context=context,
                        result=task_failure("contract", f"evidence resolution failed: {exc}"),
                        workspace=workspace,
                    )
                task = task.model_copy(update={"resolved_evidence": values})
            with heartbeat_while(
                leases,
                task_id=task.task_id,
                attempt_id=prepared.attempt_id,
                clock=self._clock,
                heartbeat_seconds=heartbeat_seconds,
                lease_extension_seconds=lease_extension,
            ):
                result = self._runner.execute(
                    task,
                    workspace,
                    context.with_task_attempt_id(prepared.attempt_id),
                )
            return self._persist_result(
                prepared=prepared,
                plan=plan,
                projection=projection,
                context=context,
                result=result,
                workspace=workspace,
            )
        finally:
            leases.remove(task.task_id, prepared.attempt_id)
            workspace.cleanup()

    def _persist_result(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        projection: GraphProjection,
        context: RuntimeContext,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> _SettledAttempt:
        task = prepared.task
        if result.status == "failed":
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind=result.error_kind or "internal",
                message=result.error or "task failed",
            )

        if result.status == "stopped":
            reason = result.error or "stopped"
            if isinstance(result.value, dict):
                raw = result.value.get("reason")
                if isinstance(raw, str) and raw.strip():
                    reason = raw
            with transaction(context.change_dir) as txn:
                txn.append_strict(
                    TaskAttemptStoppedEvent(
                        type="task_attempt_stopped",
                        invocation_id=task.invocation_id,
                        checkpoint_ns=task.checkpoint_ns,
                        superstep_id=plan.superstep_id,
                        task_id=task.task_id,
                        attempt_id=prepared.attempt_id,
                        reason=reason,
                        value=result.value,
                    )
                )
            return _SettledAttempt(task_id=task.task_id, status="stopped", write_set_id=None)

        try:
            write_set_id = self._freeze_if_needed(task, result, workspace)
        except WorkspaceError as exc:
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind=_freeze_error_kind(exc),
                message=str(exc),
            )

        if result.status == "interrupted":
            # Same-ns interrupt still appends task_attempt_succeeded so resume can
            # route from a settled attempt. When the contract names a precommit
            # validator that success is commit-shaped and must carry a receipt —
            # otherwise fold rejects the ledger as unfoldable.
            receipt_id: str | None = None
            if result.interrupt is not None:
                try:
                    receipt_id = self._run_precommit_if_needed(
                        prepared=prepared,
                        projection=projection,
                        context=context,
                        write_set_id=write_set_id,
                    )
                except CandidateValidationError as exc:
                    return self._persist_failure(
                        prepared=prepared,
                        plan=plan,
                        context=context,
                        error_kind="invalid_output",
                        message=str(exc),
                    )
                schema_version = self._checkpoints.project(task.invocation_id).event_schema_version
                outputs = dict(result.outputs_sha256)
                if write_set_id is not None and not outputs:
                    try:
                        outputs = dict(self._objects.load_write_set(write_set_id).outputs_sha256)
                    except WorkspaceError:
                        outputs = {}
                with transaction(context.change_dir) as txn:
                    txn.append_strict(
                        TaskAttemptSucceededEvent(
                            type="task_attempt_succeeded",
                            invocation_id=task.invocation_id,
                            checkpoint_ns=task.checkpoint_ns,
                            superstep_id=plan.superstep_id,
                            task_id=task.task_id,
                            attempt_id=prepared.attempt_id,
                            write_set_id=write_set_id,
                            outputs_sha256=outputs,
                            frozen_outputs=dict(result.frozen_outputs),
                            gate_report=result.gate_report,
                            state_updates=dict(result.state_updates),
                            value=result.value,
                            input_snapshot_id=prepared.input_snapshot_id,
                            runtime_context_sha256=prepared.runtime_context_sha256,
                            candidate_validation_receipt_id=receipt_id,
                        )
                    )
                    txn.append_strict(
                        build_graph_interrupted_event(
                            task=task,
                            interrupt=result.interrupt,
                            event_schema_version=schema_version,
                            checkpoint_ns=result.interrupt.checkpoint_ns,
                        )
                    )
            return _SettledAttempt(
                task_id=task.task_id,
                status="interrupted",
                write_set_id=write_set_id,
                candidate_validation_receipt_id=receipt_id,
            )

        try:
            receipt_id = self._run_precommit_if_needed(
                prepared=prepared,
                projection=projection,
                context=context,
                write_set_id=write_set_id,
            )
        except CandidateValidationError as exc:
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind="invalid_output",
                message=str(exc),
            )

        try:
            durable_effects = self._validate_durable_effects(
                prepared=prepared,
                result=result,
            )
        except DurableEffectValidationError as exc:
            return self._persist_failure(
                prepared=prepared,
                plan=plan,
                context=context,
                error_kind="invalid_output",
                message=str(exc),
            )

        prepared.candidate_validation_receipt_id = receipt_id
        self._persist_success(
            prepared=prepared,
            plan=plan,
            context=context,
            result=result,
            write_set_id=write_set_id,
            candidate_validation_receipt_id=receipt_id,
            durable_effects=durable_effects,
        )
        return _SettledAttempt(
            task_id=task.task_id,
            status="succeeded",
            write_set_id=write_set_id,
            candidate_validation_receipt_id=receipt_id,
        )

    def _freeze_if_needed(
        self,
        task: ExecutableTask,
        result: TaskResult,
        workspace: TaskWorkspace,
    ) -> str | None:
        if result.status == "interrupted" and result.write_set_id is None:
            interrupt = result.interrupt
            # Nested child bubbles publish an audited view, not a partial parent
            # write-set. Same-namespace interrupts still freeze so a named
            # precommit validator can bind a receipt to the success event.
            if interrupt is not None and interrupt.checkpoint_ns != task.checkpoint_ns:
                return None
        if result.write_set_id is not None:
            return result.write_set_id
        if self._is_side_effect_free(task):
            return None
        write_set = self._objects.freeze_write_set(
            workspace, claims=task.resources, outputs=_task_outputs(task)
        )
        return write_set.write_set_id

    def _node_validate(self, task: ExecutableTask) -> str | None:
        runner = self._runner
        compiled = getattr(runner, "_compiled", None) if runner is not None else None
        if compiled is None:
            return None
        graph = compiled.graphs.get(task.graph_id)
        if graph is None:
            return None
        node = graph.nodes.get(task.node_id)
        if node is None:
            return None
        return node.definition.validate_

    def _resolve_started_validator(self, task: ExecutableTask) -> str | None:
        contract = None if self._contracts is None else self._contracts.contracts.get(task.target)
        contract_validator = None if contract is None else contract.precommit_validator
        named = resolve_precommit_validator(
            node_validate=self._node_validate(task),
            contract_validator=contract_validator,
        )
        if named is not None:
            return named
        if self._contracts is None:
            return None
        if (task.target or "").startswith("graph:"):
            return None
        outputs = list(_task_outputs(task))
        runner = self._runner
        compiled = getattr(runner, "_compiled", None) if runner is not None else None
        if compiled is not None:
            graph = compiled.graphs.get(task.graph_id)
            node = None if graph is None else graph.nodes.get(task.node_id)
            if node is not None:
                outputs.extend(node.definition.outputs)
        if outputs_hit_invariants(outputs):
            return CROSS_ARTIFACT_INVARIANTS_V1
        return None

    def _run_precommit_if_needed(
        self,
        *,
        prepared: _PreparedAttempt,
        projection: GraphProjection,
        context: RuntimeContext,
        write_set_id: str | None,
    ) -> str | None:
        """Run node-selected validator after freeze; return receipt CAS id."""
        validator_id = self._resolve_started_validator(prepared.task)
        if validator_id is None:
            return None
        if write_set_id is None:
            raise CandidateValidationError("precommit validator requires a frozen write set")
        if prepared.input_snapshot_id is None:
            raise CandidateValidationError("precommit validator requires an input snapshot")
        write_set = self._objects.load_write_set(write_set_id)
        outputs = dict(sorted(write_set.outputs_sha256.items()))
        snapshot = load_task_input_snapshot(self._objects, prepared.input_snapshot_id)
        task_input = prepared.task.input if isinstance(prepared.task.input, Mapping) else None
        from assurance_agent.workflow.graph.precommit import (
            CODEGEN_FIX_CANDIDATE_V1,
            GENERATED_FILES_CANDIDATE_V1,
            PLAN_MECHANICAL_CANDIDATE_V1,
        )

        if validator_id in {
            GENERATED_FILES_CANDIDATE_V1,
            PLAN_MECHANICAL_CANDIDATE_V1,
            CODEGEN_FIX_CANDIDATE_V1,
        }:
            layer = infer_assurance_layer(prepared.task.target, task_input)
        else:
            layer = "api"
        if validator_id in {GENERATED_FILES_CANDIDATE_V1, PLAN_MECHANICAL_CANDIDATE_V1}:
            plan_text = load_plan_text_from_snapshot(self._objects, snapshot, layer=layer)
            cases = load_case_documents_from_snapshot(self._objects, snapshot)
        else:
            plan_text = ""
            cases = []
        root_invocation_id = projection.parent_invocation_id or projection.invocation_id
        policy_digest = projection.policy_digest or ("0" * 64)
        policy_object_id = policy_digest if len(policy_digest) == 64 else ("0" * 64)
        definition_semantics = {
            "assurance_profile_digest": projection.assurance_profile_digest or "unbound",
            "commit_safety_semantics_digest": projection.commit_safety_semantics_digest or "unbound",
            "commit_safety_semantics_object_id": (projection.commit_safety_semantics_object_id or "unbound"),
            "contract_digest": prepared.task.contract_digest,
            "gate_semantics_digest": projection.gate_semantics_digest or "unbound",
            "gate_semantics_object_id": projection.gate_semantics_object_id or "unbound",
            "graph_digest": projection.graph_digest,
            "topology_safety_semantics_digest": (projection.topology_safety_semantics_digest or "unbound"),
            "topology_safety_semantics_object_id": (
                projection.topology_safety_semantics_object_id or "unbound"
            ),
        }
        context_model = PrecommitValidationContext(
            root_invocation_id=root_invocation_id,
            invocation_id=prepared.task.invocation_id,
            task_id=prepared.task.task_id,
            attempt_id=prepared.attempt_id,
            target=prepared.task.target,
            base_tree_id=write_set.base_tree_id,
            current_tree_id=projection.current_tree_id,
            input_snapshot_id=prepared.input_snapshot_id,
            contract_digest=prepared.task.contract_digest,
            policy_object_id=policy_object_id,
            policy_digest=policy_digest if policy_digest.startswith("sha256:") else f"sha256:{policy_digest}",
            gate_attempt_id=None,
            interrupt_id=None,
            output_digests=outputs,
            write_set_id=write_set_id,
            definition_semantics=definition_semantics,
        )
        receipt_id, _receipt = validate_candidate(
            validator_id,
            context_model,
            store=self._objects,
            write_set=write_set,
            input_snapshot=snapshot,
            plan_text=plan_text,
            cases=cases,
            change_id=context.change_id,
            layer=layer,
            current_change_repo_path=_current_change_repo_path(context, write_set),
            project_root=context.project_root,
            host_change_dir=context.change_dir,
        )
        return receipt_id

    def _validate_durable_effects(
        self,
        *,
        prepared: _PreparedAttempt,
        result: TaskResult,
    ) -> list[dict[str, object]]:
        declared: tuple[str, ...] = ()
        if self._contracts is not None:
            contract = self._contracts.contracts.get(prepared.task.target)
            if contract is not None:
                declared = contract.durable_effects
        intents = validate_result_intents(
            declared_kinds=declared,
            intents=result.durable_effects,
            invocation_id=prepared.task.invocation_id,
            task_id=prepared.task.task_id,
            attempt_id=prepared.attempt_id,
            target=prepared.task.target,
            registry=self._effect_registry,
        )
        return intents_as_wire(intents)

    def _legacy_allocate_ledger_hook_applies(self, task: ExecutableTask) -> bool:
        """Pre-activation compatibility: host ledger write for packaged allocate only.

        Runs only when the task target is allocate-healing-attempt, the bound
        contract digest matches the packaged pre-activation contract, and that
        contract still declares ``durable_effects == ()``. Task 15 flips packaged
        selection; this frozen consumer stays.
        """
        if task.target != "operation:allocate-healing-attempt":
            return False
        if self._contracts is None:
            return False
        contract = self._contracts.contracts.get(task.target)
        if contract is None or contract.durable_effects:
            return False
        expected = canonical_digest(contract)
        return bool(task.contract_digest) and task.contract_digest == expected

    def _persist_success(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        context: RuntimeContext,
        result: TaskResult,
        write_set_id: str | None,
        candidate_validation_receipt_id: str | None = None,
        durable_effects: list[dict[str, object]] | None = None,
    ) -> None:
        task = prepared.task
        outputs = dict(result.outputs_sha256)
        if write_set_id is not None and not outputs:
            try:
                outputs = dict(self._objects.load_write_set(write_set_id).outputs_sha256)
            except WorkspaceError:
                outputs = {}
        frozen_wire = dict(result.frozen_outputs)
        effect_wire = list(durable_effects or ())
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptSucceededEvent(
                    type="task_attempt_succeeded",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=prepared.attempt_id,
                    write_set_id=write_set_id,
                    outputs_sha256=outputs,
                    frozen_outputs=frozen_wire,
                    gate_report=result.gate_report,
                    state_updates=dict(result.state_updates),
                    value=result.value,
                    input_snapshot_id=prepared.input_snapshot_id,
                    runtime_context_sha256=prepared.runtime_context_sha256,
                    candidate_validation_receipt_id=candidate_validation_receipt_id,
                    durable_effects=effect_wire,
                )
            )
            if task.budget is not None:
                live = fold_invocation_events(task.invocation_id, read_events_strict(context.change_dir))
                if not _budget_already_consumed(live, task):
                    txn.append_strict(
                        BudgetConsumedEvent(
                            type="budget_consumed",
                            invocation_id=task.invocation_id,
                            checkpoint_ns=task.checkpoint_ns,
                            graph_id=task.graph_id,
                            budget_id=task.budget.budget_id,
                            consumption_id=task.budget.consumption_id,
                            task_id=task.task_id,
                        )
                    )
        if self._legacy_allocate_ledger_hook_applies(task) and isinstance(result.value, Mapping):
            allocation = result.value
            required = (
                "episode_id",
                "attempt_id",
                "attempt_number",
                "operation_id",
                "source_batch_id",
                "baseline_sha256",
                "entry_batch_id",
            )
            if all(
                isinstance(allocation.get(key), str) for key in required if key != "attempt_number"
            ) and isinstance(allocation.get("attempt_number"), int):
                commit_healing_allocation_ledger(
                    context.change_dir,
                    episode_id=str(allocation["episode_id"]),
                    attempt_id=str(allocation["attempt_id"]),
                    attempt_number=int(allocation["attempt_number"]),
                    operation_id=str(allocation["operation_id"]),
                    source_batch_id=str(allocation["source_batch_id"]),
                    baseline_sha256=str(allocation["baseline_sha256"]),
                    entry_batch_id=str(allocation["entry_batch_id"]),
                )

    def _persist_failure(
        self,
        *,
        prepared: _PreparedAttempt,
        plan: PlanResult,
        context: RuntimeContext,
        error_kind: ErrorKind,
        message: str,
    ) -> _SettledAttempt:
        task = prepared.task
        retryable = error_kind in task.retry_policy.retry_on and error_kind in task.retryable_errors
        next_retry: str | None = None
        if retryable and prepared.attempt_number < task.retry_policy.max_attempts:
            next_retry = compute_next_retry_at(
                task.retry_policy,
                task.task_id,
                prepared.attempt_number,
                self._clock.now(),
            )
        with transaction(context.change_dir) as txn:
            txn.append_strict(
                TaskAttemptFailedEvent(
                    type="task_attempt_failed",
                    invocation_id=task.invocation_id,
                    checkpoint_ns=task.checkpoint_ns,
                    superstep_id=plan.superstep_id,
                    task_id=task.task_id,
                    attempt_id=prepared.attempt_id,
                    error_kind=error_kind,
                    message=message,
                    next_retry_at=next_retry,
                )
            )
        return _SettledAttempt(task_id=task.task_id, status="failed", retry_at=next_retry)

    def _is_side_effect_free(self, task: ExecutableTask) -> bool:
        if self._contracts is not None:
            contract = self._contracts.contracts.get(task.target)
            if contract is not None:
                return contract.side_effect_free
        claims = task.resources
        return not claims.writes and not claims.exclusive and not claims.authorization_writes

    def _uses_declared_read_isolation(self, task: ExecutableTask) -> bool:
        if self._contracts is None:
            return False
        contract = self._contracts.contracts.get(task.target)
        return contract is not None and contract.read_isolation == "declared_only"

    def _requires_convenience_git(self, task: ExecutableTask) -> bool:
        """Only agent handlers need a task-local ``git diff`` baseline.

        Declared-only isolation forbids convenience ``.git/**`` inside the agent
        project root. Unknown targets keep the historical fail-closed behavior.
        """
        if self._uses_declared_read_isolation(task):
            return False
        if self._contracts is not None:
            contract = self._contracts.contracts.get(task.target)
            if contract is not None:
                return contract.handler == "agent"
        return True


def _allow_graph_wrapper_child_resume(task: ExecutableTask, context: RuntimeContext) -> bool:
    if not (task.target or "").startswith("graph:"):
        return False
    return parent_task_has_child_invocation(
        context.change_dir,
        parent_invocation_id=task.invocation_id,
        parent_task_id=task.task_id,
    )


def _current_change_repo_path(context: RuntimeContext, write_set: WriteSet) -> str:
    """Resolve the repo-relative current change path for precommit evidence.

    Root invocations keep ``project_root`` as the host SUT root, so
    ``change_dir.relative_to(project_root)`` works. Child subgraph contexts bind
    ``project_root``/``repo_root`` to the parent task workspace while
    ``change_dir`` remains the host ledger path — relative_to then fails
    (including macOS ``/var`` vs ``/private/var`` resolve skew). Prefer the
    write-set's pinned ``base_tree_roots.change`` map.
    """
    roots = write_set.base_tree_roots or {}
    pinned = roots.get("change")
    if isinstance(pinned, str) and pinned not in {"", "."}:
        return pinned
    try:
        return context.change_dir.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        return f"qa/changes/{context.change_id}"


def _task_outputs(task: ExecutableTask) -> tuple[str, ...]:
    payload = task.input
    if isinstance(payload, Mapping):
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and all(isinstance(item, str) for item in outputs):
            return tuple(outputs)
    return ()


def _budget_already_consumed(projection: GraphProjection, task: ExecutableTask) -> bool:
    if task.budget is None:
        return False
    existing = projection.tasks.get(task.task_id)
    return (
        existing is not None
        and existing.status == "succeeded"
        and projection.budgets.get(task.budget.budget_id, 0) > 0
    )


def _freeze_error_kind(exc: WorkspaceError) -> ErrorKind:
    message = str(exc).lower()
    if "output" in message:
        return "invalid_output"
    return "forbidden_write"
