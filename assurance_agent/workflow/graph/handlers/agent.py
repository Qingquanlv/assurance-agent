"""agent adapter 桥 target handler（``skill:<name>``）。

把 skill node 翻译成 ``AgentRequest``：prompt 列出 contract 授权写范围，agent
进程在 task 私有 workspace 的物化 root 中运行。adapter 返回后 handler 用
``TreeStore.freeze_write_set`` 独立验证实际写入与声明 outputs：

- 授权范围外的实际写入 / 变化的 symlink / 路径逃逸 → ``forbidden_write``；
- 声明 output 缺失或无法冻结 → ``invalid_output``；
- adapter 失败 → 透传其 typed error kind（缺失时归一化为 ``internal``）。

handler 不写 strict events；write-set 的 ledger 持久化归 scheduler（Task 10）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from assurance_agent.change_location import ChangeLocation
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.agent_api import AgentInvoker, AgentRequest, build_node_prompt
from assurance_agent.workflow.graph.codegen_manifest import (
    CodegenManifestError,
    complete_codegen_manifest,
    verify_frozen_codegen_completion,
)
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
)
from assurance_agent.workflow.graph.model_routing import ModelRouteContext, ModelRouter
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError
from assurance_agent.workflow.issues.analyzer_output import (
    IssueAnalyzerOutputError,
    complete_issue_analyzer_outputs,
)
from assurance_agent.workflow.improvements.reviewer_output import (
    ImprovementReviewerOutputError,
    complete_improvement_reviewer_outputs,
)
from assurance_agent.workflow.retro_outputs import (
    CandidateOutputError,
    SignalInvalidError,
    complete_candidate_outputs,
    complete_signal_outputs,
)
from assurance_agent.workflow.orchestration.gates import valid_ancestor_accept_risk_decisions

_PROTECTED_CANONICAL_AGENT_INPUTS = (Path(".aa/data-knowledge.yaml"),)


@dataclass(frozen=True)
class _CanonicalInputSnapshot:
    path: Path
    existed: bool
    payload: bytes | None
    mode: int | None


def _snapshot_canonical_agent_inputs(context: RuntimeContext) -> tuple[_CanonicalInputSnapshot, ...]:
    snapshots: list[_CanonicalInputSnapshot] = []
    for rel in _PROTECTED_CANONICAL_AGENT_INPUTS:
        path = context.project_root / rel
        if path.is_file() and not path.is_symlink():
            snapshots.append(
                _CanonicalInputSnapshot(
                    path=path,
                    existed=True,
                    payload=path.read_bytes(),
                    mode=path.stat().st_mode & 0o777,
                )
            )
        else:
            snapshots.append(_CanonicalInputSnapshot(path=path, existed=False, payload=None, mode=None))
    return tuple(snapshots)


def _restore_escaped_canonical_agent_writes(
    snapshots: tuple[_CanonicalInputSnapshot, ...],
) -> tuple[str, ...]:
    escaped: list[str] = []
    for snapshot in snapshots:
        path = snapshot.path
        current = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        if snapshot.existed and current == snapshot.payload:
            continue
        if not snapshot.existed and current is None and not path.is_symlink():
            continue
        escaped.append(path.as_posix())
        if not snapshot.existed:
            path.unlink(missing_ok=True)
            continue
        assert snapshot.payload is not None
        assert snapshot.mode is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.agent-guard.tmp")
        try:
            temp.write_bytes(snapshot.payload)
            temp.chmod(snapshot.mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return tuple(escaped)


class AgentHandler:
    def __init__(
        self,
        invoker: AgentInvoker,
        store: TreeStore,
        *,
        contracts: ExecutionContractCatalog,
        compiled: CompiledWorkflow,
        model_router: ModelRouter | None = None,
        adapter_name: str | None = None,
        cli_model_override: str | None = None,
    ) -> None:
        self._invoker = invoker
        self._store = store
        self._contracts = contracts
        self._compiled = compiled
        self._model_router = model_router
        self._adapter_name = adapter_name
        self._cli_model_override = cli_model_override

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        node_def = self._node_def(task)
        if node_def is None:
            return task_failure(
                "contract",
                f"cannot resolve compiled node for task {task.task_id} "
                f"(graph '{task.graph_id}', node '{task.node_id}')",
            )
        claims = self._claims(task, node_def)
        outputs = self._outputs(task, node_def)
        allowed = tuple(_display_path(path) for path in claims.authorization_writes)
        skill = task.target.partition(":")[2]
        resolution = None
        if self._model_router is not None and self._adapter_name == "opencode":
            resolution = self._model_router.resolve(
                ModelRouteContext(
                    adapter="opencode",
                    skill=skill,
                    prior_error_kind=task.prior_error_kind,
                    contract_failure_kinds_seen=task.contract_failure_kinds_seen,
                    cli_override=self._cli_model_override,
                )
            )
        accepted_risks = valid_ancestor_accept_risk_decisions(
            self._compiled.schema,
            ChangeLocation(
                project_root=workspace.project_root,
                change_id=context.change_id,
                path=workspace.change_dir,
                source="changes",
            ),
            checkpoint_ns=task.checkpoint_ns,
            events_dir=context.change_dir,
        )
        prompt = build_node_prompt(
            skill,
            task.node_id,
            context.change_id,
            allowed_writes=allowed,
            item=_fan_out_item(task),
            workspace_root=str(Path(workspace.root)),
            memory_root=Path(workspace.project_root),
            prior_failure=task.prior_failure,
            prior_error_kind=task.prior_error_kind,
            evidence=task.resolved_evidence or None,
            outputs=outputs,
            accepted_risks=tuple(
                {
                    "gate_id": decision.gate_id,
                    "interrupt_id": decision.interrupt_id,
                    "checkpoint_ns": decision.checkpoint_ns,
                    "reason": decision.reason,
                    "who": decision.who,
                }
                for decision in accepted_risks
            ),
        )
        request = AgentRequest(
            target=task.target,
            node_id=task.node_id,
            change_id=context.change_id,
            workspace_root=Path(workspace.root),
            allowed_writes=allowed,
            prompt=prompt,
            timeout_seconds=task.timeout_policy.run_seconds,
            reconnect_session_id=context.parent_session_id,
            # The schema binding is the workflow author's explicit capability
            # choice. Name-based routing exists only for legacy/omitted bindings.
            agent=node_def.agent or agent_for_skill(skill),
            model=resolution.model if resolution is not None else None,
            model_route_source=resolution.source if resolution is not None else None,
            model_policy_sha256=resolution.policy_sha256 if resolution is not None else None,
        )
        protected_inputs = _snapshot_canonical_agent_inputs(context)
        try:
            result = self._invoker.invoke(request)
        except Exception as exc:  # adapter/plugin boundary must become a typed Graph failure
            escaped = _restore_escaped_canonical_agent_writes(protected_inputs)
            if escaped:
                return task_failure(
                    "forbidden_write",
                    "agent escaped task workspace and modified protected canonical input(s): "
                    + ", ".join(escaped),
                )
            return task_failure("internal", f"{type(exc).__name__}: {exc}")
        escaped = _restore_escaped_canonical_agent_writes(protected_inputs)
        if escaped:
            return task_failure(
                "forbidden_write",
                "agent escaped task workspace and modified protected canonical input(s): "
                + ", ".join(escaped),
            )
        if not result.ok:
            return task_failure(
                result.error_kind or "internal",
                result.error or f"agent invocation failed for {task.target}",
            )
        try:
            codegen_receipt = complete_codegen_manifest(
                task=task,
                workspace=workspace,
                context=context,
                claims=claims,
            )
            complete_issue_analyzer_outputs(workspace.change_dir, outputs)
            complete_signal_outputs(workspace.project_root, outputs)
            complete_candidate_outputs(workspace.project_root, outputs)
            if skill == "aa-improvement-reviewer":
                review_id = str(context.params.get("review_id", ""))
                improvement_id = str(context.params.get("improvement_id", ""))
                subject_sha256 = str(context.params.get("subject_sha256", ""))
                raw_expected_version = context.params.get("expected_improvement_version")
                expected_version = (
                    raw_expected_version
                    if isinstance(raw_expected_version, int) and not isinstance(raw_expected_version, bool)
                    else 0
                )
                review_dir = workspace.project_root / "qa" / "improvements" / "reviews" / review_id
                complete_improvement_reviewer_outputs(
                    subject_path=workspace.project_root
                    / "qa"
                    / "improvements"
                    / "review-subjects"
                    / f"{subject_sha256}.json",
                    assessment_path=review_dir / "assessment.json",
                    summary_path=review_dir / "summary.md",
                    expected_review_id=review_id,
                    expected_improvement_id=improvement_id,
                    expected_version=expected_version,
                    expected_subject_sha256=subject_sha256,
                )
        except (
            IssueAnalyzerOutputError,
            SignalInvalidError,
            CandidateOutputError,
            ImprovementReviewerOutputError,
            CodegenManifestError,
            TypeError,
            ValueError,
        ) as exc:
            return task_failure("invalid_output", str(exc))
        try:
            write_set = self._store.freeze_write_set(workspace, claims=claims, outputs=outputs)
        except WorkspaceError as exc:
            return task_failure(_freeze_error_kind(exc), str(exc))
        if codegen_receipt is not None:
            try:
                verify_frozen_codegen_completion(
                    store=self._store,
                    write_set_id=write_set.write_set_id,
                    receipt=codegen_receipt,
                )
            except CodegenManifestError as exc:
                return task_failure("invalid_output", str(exc))
        return TaskResult(
            status="succeeded",
            write_set_id=write_set.write_set_id,
            outputs_sha256=dict(write_set.outputs_sha256),
        )

    def _node_def(self, task: ExecutableTask) -> NodeDef | None:
        graph = self._compiled.graphs.get(task.graph_id)
        if graph is None:
            return None
        node = graph.nodes.get(task.node_id)
        return node.definition if node is not None else None

    def _claims(self, task: ExecutableTask, node_def: NodeDef) -> ResourceClaims:
        """Prefer planner-narrowed ``task.resources``; fall back to catalog claims."""
        if task.resources.reads or task.resources.writes or task.resources.authorization_writes:
            return task.resources
        claims = self._contracts.claims_for(node_def)
        payload = task.input
        if isinstance(payload, Mapping):
            expanded = payload.get("resources")
            if isinstance(expanded, Mapping):
                writes = expanded.get("writes")
                if isinstance(writes, list) and writes:
                    narrowed = tuple(ResourcePath.parse(str(value)) for value in writes)
                    claims = ResourceClaims(
                        reads=claims.reads,
                        writes=claims.writes,
                        synchronized=claims.synchronized,
                        exclusive=claims.exclusive,
                        authorization_writes=narrowed,
                    )
        return claims

    @staticmethod
    def _outputs(task: ExecutableTask, node_def: NodeDef) -> tuple[str, ...]:
        """声明 outputs：fan-out child 用冻结展开值，否则用 node 定义原文。"""
        payload = task.input
        if isinstance(payload, Mapping):
            expanded = payload.get("outputs")
            if isinstance(expanded, list):
                return tuple(str(value) for value in expanded)
        return tuple(node_def.outputs)


def agent_for_skill(skill: str) -> str | None:
    """Route a phase skill to its bounded aa-* worker agent (opencode persona).

    Each aa-* agent declares the phases it serves (see ``.opencode/agents/*.md``)
    plus a restrictive permission floor and ``external_directory: deny``. Running
    a node under its matching worker keeps it focused on producing declared
    outputs (instead of an aggressive general coding preset that over-explores
    and never writes) and blocks writes/reads outside the task sandbox.

    Mapping is keyword-based so new sibling skills route correctly:
    - ``aa-explore``                    -> aa-explorer    (explore/ + aa risk)
    - ``*codegen*``                     -> aa-test-author (tests/ + codegen/)
    - ``*reviewer*`` / ``*inspect*``    -> aa-reviewer   (review/ + inspect/)
    - ``*report*``                      -> aa-reporter   (report/)
    - ``*archive*``                     -> aa-archiver   (qa/cases + qa/archive)
    - case-design / *-plan / *-fixer / fact-baseline /
      fix-proposal (the rest)           -> aa-doc-author (authoring/design/plan)

    Returns ``None`` for an unknown/empty skill so the adapter keeps its default.
    """
    if not skill:
        return None
    if skill == "aa-explore":
        return "aa-explorer"
    if "codegen" in skill:
        return "aa-test-author"
    if "reviewer" in skill or "inspect" in skill:
        return "aa-reviewer"
    if "report" in skill:
        return "aa-reporter"
    if "archive" in skill:
        return "aa-archiver"
    return "aa-doc-author"


def _display_path(path: ResourcePath) -> str:
    return f"{path.root}:{path.pattern}"


def _fan_out_item(task: ExecutableTask) -> str | None:
    payload = task.input
    if isinstance(payload, Mapping):
        fan_out = payload.get("fan_out")
        if isinstance(fan_out, Mapping):
            key = fan_out.get("task_key")
            if isinstance(key, str):
                return key
    return task.task_key


def _freeze_error_kind(exc: WorkspaceError) -> ErrorKind:
    """freeze 失败的 typed 归一化：outputs 校验 → invalid_output，其余写策略 → forbidden_write。"""
    if "declared output" in str(exc):
        return "invalid_output"
    return "forbidden_write"


__all__ = ["AgentHandler", "agent_for_skill"]
