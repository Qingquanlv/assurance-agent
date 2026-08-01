"""Ledger 投影、checkpoint snapshot 与 ``workflow-state.yaml`` 兼容视图。

严格 ledger（``events.jsonl`` 中的 graph 事件）是唯一权威；checkpoint JSON
快照与 ``workflow-state.yaml`` 都只是可重建的投影/缓存。snapshot 仅在其
``event_seq`` 与 digest 三元组（graph/contract/params）和 ledger 投影完全
一致时才被接受，否则从 ledger 重建并覆盖缓存。``workflow-state.yaml`` 只经
``ProgressionTxn.set_workflow_state_projection`` 落盘，运行时决策从不读它。

显式 ``import-checkpoint`` 校验也落在本模块：fixture digest、structural path、
前驱闭包、输出 hash 与 gate 重求值；裸 artifact 存在绝不伪造 completed task。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import LedgerIntegrityError, read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GRAPH_EVENT_ADAPTER,
    BudgetConsumedEvent,
    CheckpointImportedEvent,
    DurableEffectAcknowledgedEvent,
    DurableEffectIntegrityFailedEvent,
    FanOutExpandedEvent,
    GraphInterruptedEvent,
    GraphInvocationStartedEvent,
    GraphResumedEvent,
    GraphTerminalEvent,
    ManualPlanRevisionEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    SuperstepCommittedEvent,
    SuperstepPlannedEvent,
    TaskAttemptAbandonedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptStoppedEvent,
    TaskAttemptSucceededEvent,
    TaskImportedEvent,
    TaskRecoveryRoutedEvent,
    TaskSchedulingDeferredEvent,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.graph.migrate_events import migrate_events_for_fold
from assurance_agent.workflow.graph.models import (
    CompiledGraph,
    CompiledWorkflow,
    FanOutExpansion,
    GraphProjection,
    ImportManifest,
    ImportedBudget,
    ImportedTask,
    InterruptProjection,
    RecoveryProjection,
    RuntimeContext,
    TaskProjection,
    WorkflowStateProjection,
)
from assurance_agent.workflow.graph.node_history import GenerationFoldState
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.orchestration.dsl import (
    DslError,
    MISSING,
    Scope,
    evaluate,
    is_satisfied,
    parse_expression,
)
from assurance_agent.workflow.orchestration.gates import (
    GateEvaluationContext,
    check_gate_in_view,
)

CHECKPOINT_DIR_RELPATH = ".graph-runtime/checkpoints"
_LEDGER_ENVELOPE_KEYS = frozenset({"seq", "ts"})
_TERMINAL_BY_TYPE: dict[str, Literal["completed", "stopped", "failed"]] = {
    "graph_completed": "completed",
    "graph_stopped": "stopped",
    "graph_failed": "failed",
}

_AttemptOutcomeEvent = (
    TaskAttemptSucceededEvent | TaskAttemptStoppedEvent | TaskAttemptFailedEvent | TaskAttemptAbandonedEvent
)


class CheckpointImportError(AaError):
    """显式 import-checkpoint 校验失败；不写入任何 ledger 事件。"""


@dataclass(frozen=True)
class ResolvedImportTask:
    task: ImportedTask
    structural_path: str
    task_id: str
    gate_report: dict[str, object] | None


@dataclass(frozen=True)
class ValidatedImport:
    manifest: ImportManifest
    manifest_sha256: str
    resolved: tuple[ResolvedImportTask, ...]
    budgets: tuple[ImportedBudget, ...]
    input_sha256: dict[str, str]


def parse_import_manifest(raw: Mapping[str, object] | str | bytes) -> ImportManifest:
    """把 YAML/dict manifest 正规化为冻结 ``ImportManifest``。

    ``source.kind`` / ``source.fixture_id`` / ``source.fixture_digest`` 扁平化到
    顶层字段；拒绝额外字段与缺少 ``path`` 的模糊 graph/node-only 引用。
    """
    if isinstance(raw, (str, bytes)):
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise CheckpointImportError(f"invalid import manifest YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise CheckpointImportError("import manifest root must be a mapping")
        payload: dict[str, object] = loaded
    else:
        payload = dict(raw)

    if "source" in payload:
        source = payload.pop("source")
        if not isinstance(source, dict):
            raise CheckpointImportError("import manifest source must be a mapping")
        unknown_source = sorted(str(k) for k in source if k not in {"kind", "fixture_id", "fixture_digest"})
        if unknown_source:
            raise CheckpointImportError("unknown source fields: " + ", ".join(unknown_source))
        if "kind" in source:
            payload["source_kind"] = source["kind"]
        if "fixture_id" in source:
            payload["fixture_id"] = source["fixture_id"]
        if "fixture_digest" in source:
            payload["fixture_digest"] = source["fixture_digest"]

    completed_raw = payload.get("completed")
    if isinstance(completed_raw, list):
        for index, item in enumerate(completed_raw):
            if not isinstance(item, dict):
                raise CheckpointImportError(f"completed[{index}] must be a mapping")
            if "path" not in item:
                raise CheckpointImportError(
                    f"completed[{index}] ambiguous graph/node-only task reference; path is required"
                )

    try:
        return ImportManifest.model_validate(payload)
    except ValidationError as exc:
        raise CheckpointImportError(f"invalid import manifest: {exc}") from exc


def validate_import(
    compiled: CompiledWorkflow,
    manifest: ImportManifest,
    context: RuntimeContext,
    *,
    projection: GraphProjection | None = None,
) -> ValidatedImport:
    """校验 fixture digest、路径安全、structural path、前驱闭包、输出 hash 与 gate。"""
    if manifest.entrypoint not in compiled.entrypoints:
        raise CheckpointImportError(f"unknown entrypoint '{manifest.entrypoint}'")

    _verify_fixture_digest(context, manifest.fixture_id, manifest.fixture_digest)

    input_sha256: dict[str, str] = {}
    for logical, expected in sorted(manifest.inputs.items()):
        path = _resolve_logical_path(context, logical)
        actual = sha256_file(path)
        if actual is None or actual != _strip_sha_prefix(expected):
            raise CheckpointImportError(f"input hash mismatch for {logical}")
        input_sha256[logical] = actual

    imported_ids: set[str] = set()
    ledger_complete = _ledger_succeeded_nodes(projection) if projection is not None else set()
    resolved: list[ResolvedImportTask] = []
    # Same-named nodes in nested/sibling graphs must not overwrite one another;
    # gate/edge/route evaluation only sees the current graph instance's locals.
    node_results_by_structural_path: dict[str, dict[str, dict[str, object]]] = {}
    if projection is not None:
        _seed_node_results_from_projection(projection, node_results_by_structural_path)
    state_values = _state_values_from_change(context)

    for task in manifest.completed:
        structural_path = _resolve_structural_path(compiled, manifest.entrypoint, task)
        graph = compiled.graphs[task.graph]
        node = graph.nodes[task.node]
        if node.definition.fan_out is not None and not task.task_key:
            raise CheckpointImportError(f"fan-out node '{task.node}' import requires task_key")
        if projection is not None and task.task_key is not None:
            expansion = projection.fan_out_expansions.get(task.node)
            if expansion is not None and task.task_key not in expansion.task_keys:
                raise CheckpointImportError(
                    f"task_key {task.task_key!r} not present in frozen expansion for '{task.node}'"
                )

        task_id = _import_task_id(structural_path, task.node, task.task_key)
        local_results = node_results_by_structural_path.setdefault(structural_path, {})
        _assert_predecessor_closure(
            compiled,
            entrypoint=manifest.entrypoint,
            task=task,
            structural_path=structural_path,
            imported_ids=imported_ids,
            ledger_complete=ledger_complete,
            params=context.params,
            state_values=state_values,
            local_results=local_results,
        )

        for logical, expected in sorted(task.outputs.items()):
            path = _resolve_logical_path(context, logical)
            actual = sha256_file(path)
            if actual is None or actual != _strip_sha_prefix(expected):
                raise CheckpointImportError(f"output hash mismatch for {logical}")

        local_payload = local_results.setdefault(task.node, {})
        local_payload["status"] = "succeeded"
        gate_report = _reevaluate_gate(
            compiled, context, task, state_values=state_values, node_results=local_results
        )
        if gate_report is not None:
            gate_payload = local_payload.setdefault("gate", {})
            if isinstance(gate_payload, dict):
                gate_payload.update(gate_report)
            else:
                local_payload["gate"] = dict(gate_report)
        imported_ids.add(_node_identity(structural_path, task.node, task.task_key))
        resolved.append(
            ResolvedImportTask(
                task=task,
                structural_path=structural_path,
                task_id=task_id,
                gate_report=gate_report,
            )
        )

    _validate_budgets(compiled, manifest, resolved)

    manifest_sha = hashlib.sha256(
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return ValidatedImport(
        manifest=manifest,
        manifest_sha256=manifest_sha,
        resolved=tuple(resolved),
        budgets=manifest.budgets,
        input_sha256=input_sha256,
    )


def _verify_fixture_digest(context: RuntimeContext, fixture_id: str, fixture_digest: str) -> None:
    lock_path = context.project_root / "eval-fixtures" / "fixture-lock.json"

    class _FixtureLockEntry(BaseModel):
        model_config = ConfigDict(extra="ignore")
        aggregate_sha256: str

    class _FixtureLock(BaseModel):
        model_config = ConfigDict(extra="ignore")
        fixtures: dict[str, _FixtureLockEntry]

    try:
        lock = _FixtureLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise CheckpointImportError(f"fixture lock missing or invalid: {exc}") from exc
    entry = lock.fixtures.get(fixture_id)
    if entry is None:
        raise CheckpointImportError(f"fixture_id not found in fixture lock: {fixture_id}")
    expected = _strip_sha_prefix(fixture_digest)
    if entry.aggregate_sha256 != expected:
        raise CheckpointImportError(
            f"fixture digest mismatch for {fixture_id}: manifest={expected} lock={entry.aggregate_sha256}"
        )


def _strip_sha_prefix(value: str) -> str:
    if value.startswith("sha256:"):
        return value[len("sha256:") :]
    return value


def _resolve_logical_path(context: RuntimeContext, logical: str) -> Path:
    if not logical or "\\" in logical:
        raise CheckpointImportError(f"unsafe logical path: {logical!r}")
    root, sep, rest = logical.partition(":")
    if not sep or root not in {"change", "project", "repo"}:
        raise CheckpointImportError(f"unsafe logical path root: {logical!r}")
    segments = rest.split("/")
    if rest.startswith("/") or any(seg in ("", ".", "..") for seg in segments):
        raise CheckpointImportError(f"unsafe logical path: {logical!r}")
    base = {
        "change": context.change_dir,
        "project": context.project_root,
        "repo": context.repo_root,
    }[root]
    return base / rest


def _resolve_structural_path(
    compiled: CompiledWorkflow,
    entrypoint_name: str,
    task: ImportedTask,
) -> str:
    """校验 ``path`` 是从 entrypoint root 起的真实 ``graph:*`` 调用链。"""
    entry = compiled.entrypoints[entrypoint_name]
    parts = [p for p in task.path.split("/") if p]
    if not parts:
        raise CheckpointImportError("impossible structural path: empty")
    if any(seg in (".", "..") or not seg for seg in parts):
        raise CheckpointImportError(f"unsafe structural path: {task.path!r}")
    if parts[0] != entry.graph_id:
        raise CheckpointImportError(
            f"structural path must start at entrypoint graph '{entry.graph_id}', got {task.path!r}"
        )
    current_graph_id = entry.graph_id
    index = 1
    while index < len(parts):
        if index + 1 >= len(parts):
            raise CheckpointImportError(
                f"impossible structural path (incomplete graph:* edge): {task.path!r}"
            )
        node_id, next_graph = parts[index], parts[index + 1]
        graph = compiled.graphs.get(current_graph_id)
        if graph is None or node_id not in graph.nodes:
            raise CheckpointImportError(
                f"impossible structural path: unknown node '{node_id}' in graph '{current_graph_id}'"
            )
        uses = graph.nodes[node_id].definition.uses
        if not uses.startswith("graph:") or uses[len("graph:") :] != next_graph:
            raise CheckpointImportError(
                f"impossible structural path: node '{node_id}' is not graph:{next_graph}"
            )
        if next_graph not in compiled.graphs:
            raise CheckpointImportError(f"impossible structural path: unknown graph '{next_graph}'")
        current_graph_id = next_graph
        index += 2
    if current_graph_id != task.graph:
        raise CheckpointImportError(
            f"structural path resolves to graph '{current_graph_id}', not '{task.graph}'"
        )
    if task.node not in compiled.graphs[task.graph].nodes:
        raise CheckpointImportError(
            f"impossible structural path: unknown node '{task.node}' in graph '{task.graph}'"
        )
    return "/".join(parts)


def _import_task_id(structural_path: str, node_id: str, task_key: str | None) -> str:
    if task_key is not None:
        return f"{structural_path}:{node_id}:{task_key}"
    return f"{structural_path}:{node_id}"


def _node_identity(structural_path: str, node_id: str, task_key: str | None) -> str:
    return _import_task_id(structural_path, node_id, task_key)


def _ledger_succeeded_nodes(projection: GraphProjection) -> set[str]:
    done: set[str] = set()
    for task in projection.tasks.values():
        if task.status == "succeeded":
            # projection task_id 已是 structural 形式
            done.add(task.task_id)
    return done


def _successors_from(graph: CompiledGraph, node_id: str) -> tuple[str, ...]:
    if node_id == "START":
        return tuple(
            edge.to
            for compiled in graph.nodes.values()
            for edge in compiled.incoming
            if edge.from_ == "START"
        )
    compiled = graph.nodes.get(node_id)
    if compiled is None:
        return ()
    return tuple(edge.to for edge in compiled.outgoing)


def _can_reach_without_node(
    graph: CompiledGraph,
    *,
    start: str,
    target: str,
    avoid: str,
) -> bool:
    """Return True when ``target`` is reachable from ``start`` without visiting ``avoid``."""
    if start == target:
        return True
    visited: set[str] = set()
    queue = [node for node in _successors_from(graph, start) if node != avoid]
    while queue:
        current = queue.pop(0)
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        for successor in _successors_from(graph, current):
            if successor != avoid:
                queue.append(successor)
    return False


def _is_mandatory_predecessor(graph: CompiledGraph, *, pred: str, node: str) -> bool:
    """Predecessor is mandatory when every START→node path must pass through ``pred``."""
    if pred == "START":
        return False
    return not _can_reach_without_node(graph, start="START", target=node, avoid=pred)


def _seed_node_results_from_projection(
    projection: GraphProjection,
    node_results_by_structural_path: dict[str, dict[str, dict[str, object]]],
) -> None:
    """Mirror committed succeeded tasks into structural-path local node results."""
    for task in projection.tasks.values():
        if task.status != "succeeded":
            continue
        suffix = f":{task.node_id}"
        if task.task_key is not None:
            suffix = f":{task.node_id}:{task.task_key}"
        if not task.task_id.endswith(suffix):
            continue
        structural_path = task.task_id[: -len(suffix)]
        local_results = node_results_by_structural_path.setdefault(structural_path, {})
        payload = local_results.setdefault(task.node_id, {})
        payload["status"] = "succeeded"
        if task.value is not None:
            payload["value"] = task.value
        if task.gate_report is not None:
            gate_payload = payload.setdefault("gate", {})
            if isinstance(gate_payload, dict):
                gate_payload.update(task.gate_report)
            else:
                payload["gate"] = dict(task.gate_report)


def _import_eval_scope(
    *,
    params: Mapping[str, object],
    state_values: Mapping[str, object],
    local_results: Mapping[str, Mapping[str, object]],
) -> Scope:
    def node_result(node_id: str) -> object:
        result = local_results.get(node_id)
        return dict(result) if isinstance(result, Mapping) else {}

    return Scope(
        {"params": dict(params), "state": dict(state_values)},
        node_result=node_result,
    )


def _condition_satisfied(expression: str, scope: Scope, *, nid: str) -> bool:
    try:
        return is_satisfied(parse_expression(expression), scope)
    except DslError as exc:
        raise CheckpointImportError(
            f"import predecessor condition on '{nid}' failed to evaluate: {exc}"
        ) from exc


def _resolve_import_route_target(route, scope: Scope, *, src: str) -> str | None:
    """Resolve a route select to a target label; missing/default-less → None (no delivery)."""
    try:
        label = evaluate(parse_expression(route.select), scope)
    except DslError as exc:
        raise CheckpointImportError(f"import route from '{src}' select failed to evaluate: {exc}") from exc
    case_label: str | None = None
    if isinstance(label, bool):
        case_label = "true" if label else "false"
    elif label is not MISSING and isinstance(label, str):
        case_label = label
    chosen = route.cases.get(case_label) if case_label is not None else None
    if chosen is not None:
        return chosen
    return route.default


def _known_structural_deliverers(
    graph: CompiledGraph,
    *,
    structural_path: str,
    known: set[str],
    target: str,
) -> tuple[str, ...]:
    """Already-validated predecessors that have an edge/route case targeting ``target``."""
    deliverers: list[str] = []
    for pred_id, compiled_node in graph.nodes.items():
        if _import_task_id(structural_path, pred_id, None) not in known:
            continue
        if any(edge.to == target for edge in compiled_node.outgoing):
            deliverers.append(pred_id)
            continue
        if any(target in route.cases.values() or route.default == target for route in compiled_node.routes):
            deliverers.append(pred_id)
    return tuple(deliverers)


def _selected_by_known_predecessors(
    graph: CompiledGraph,
    *,
    structural_path: str,
    known: set[str],
    scope: Scope,
) -> set[str]:
    """Targets currently selected by already-validated predecessors under frozen scope."""
    selected: set[str] = set()
    for pred_id, compiled_node in graph.nodes.items():
        if _import_task_id(structural_path, pred_id, None) not in known:
            continue
        for edge in compiled_node.outgoing:
            if edge.when is not None and not _condition_satisfied(edge.when, scope, nid=pred_id):
                continue
            selected.add(edge.to)
        for route in compiled_node.routes:
            target = _resolve_import_route_target(route, scope, src=pred_id)
            if target is not None and target not in {"END", "STOP", "FAIL"}:
                selected.add(target)
    return selected


def _assert_predecessor_closure(
    compiled: CompiledWorkflow,
    *,
    entrypoint: str,
    task: ImportedTask,
    structural_path: str,
    imported_ids: set[str],
    ledger_complete: set[str],
    params: Mapping[str, object],
    state_values: Mapping[str, object],
    local_results: Mapping[str, Mapping[str, object]],
) -> None:
    """Mandatory predecessors present; known deliverers must actually select the successor."""
    del entrypoint
    graph = compiled.graphs[task.graph]
    node = graph.nodes[task.node]
    known = imported_ids | ledger_complete
    for edge in node.incoming:
        if edge.from_ == "START":
            continue
        if not _is_mandatory_predecessor(graph, pred=edge.from_, node=task.node):
            continue
        pred_id = _import_task_id(structural_path, edge.from_, None)
        if pred_id in known:
            continue
        raise CheckpointImportError(
            f"missing predecessor closure: node '{task.node}' requires predecessor '{edge.from_}'"
        )

    # Join tokens are declared via join.sources, not ordinary edge/route delivery.
    if node.definition.join is not None:
        return

    deliverers = _known_structural_deliverers(
        graph, structural_path=structural_path, known=known, target=task.node
    )
    if not deliverers:
        # Fixture imports may omit optional alternate-path predecessors; presence
        # of mandatory preds (above) remains the baseline. Selection is enforced
        # only when a known deliverer already claims this successor.
        return

    scope = _import_eval_scope(params=params, state_values=state_values, local_results=local_results)
    selected = _selected_by_known_predecessors(
        graph, structural_path=structural_path, known=known, scope=scope
    )
    if task.node not in selected:
        raise CheckpointImportError(
            f"missing predecessor closure: node '{task.node}' is not selected by any "
            "validated predecessor edge/route under frozen params/state "
            "(stop/skip precheck cannot satisfy codegen)"
        )


def _state_values_from_change(context: RuntimeContext) -> dict[str, object]:
    """Load workflow-state.yaml into gate ``state.*`` (legacy phase/run_context stamps)."""
    path = context.change_dir / "workflow-state.yaml"
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key not in {"_integrity", "schema_version"} and not str(key).startswith("_")
    }


def _resolved_gate_id(node: NodeDef) -> str | None:
    if node.gate is not None:
        return node.gate
    if node.uses == "builtin:gate":
        candidate = node.with_.get("gate")
        if isinstance(candidate, str):
            return candidate
    return None


def _reevaluate_gate(
    compiled: CompiledWorkflow,
    context: RuntimeContext,
    task: ImportedTask,
    *,
    state_values: Mapping[str, object] | None = None,
    node_results: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    node = compiled.graphs[task.graph].nodes[task.node]
    gate_id = _resolved_gate_id(node.definition)
    if gate_id is None and task.gate is None:
        return None
    if gate_id is None:
        raise CheckpointImportError(f"node '{task.node}' has no attached gate but manifest supplies one")
    if task.gate is None:
        raise CheckpointImportError(f"gated node '{task.node}' import requires gate block")
    if task.gate.id != gate_id:
        raise CheckpointImportError(
            f"gate id mismatch for node '{task.node}': manifest={task.gate.id} schema={gate_id}"
        )
    eval_context = GateEvaluationContext(
        project_root=context.project_root,
        repo_root=context.repo_root,
        change_dir=context.change_dir,
        change_id=context.change_id,
        params=context.params,
        state_values=dict(state_values) if state_values is not None else _state_values_from_change(context),
        node_results=dict(node_results) if node_results is not None else {},
        audit_events_dir=context.change_dir,
    )
    report = check_gate_in_view(compiled.schema.gates, gate_id, eval_context)
    if report.verdict.value != task.gate.verdict:
        raise CheckpointImportError(
            f"gate verdict mismatch for '{gate_id}': "
            f"manifest={task.gate.verdict} evaluated={report.verdict.value}"
        )
    evaluated_reads = {k: _strip_sha_prefix(v) for k, v in report.reads_sha256.items()}
    manifest_reads = {k: _strip_sha_prefix(v) for k, v in task.gate.reads_sha256.items()}
    if evaluated_reads != manifest_reads:
        raise CheckpointImportError(f"gate reads_sha256 mismatch for '{gate_id}'")
    return {
        "gate_id": report.gate_id,
        "verdict": report.verdict.value,
        "matched_rule": report.matched_rule,
        "reason": report.reason,
        "reads_sha256": dict(report.reads_sha256),
        "value": report.verdict.value,
        **({"details": dict(report.details)} if report.details is not None else {}),
    }


def _validate_budgets(
    compiled: CompiledWorkflow,
    manifest: ImportManifest,
    resolved: list[ResolvedImportTask],
) -> None:
    consumers: dict[str, tuple[str, str]] = {}
    for item in resolved:
        node = compiled.graphs[item.task.graph].nodes[item.task.node]
        budget = node.definition.budget
        if budget is None:
            continue
        consumers[item.task_id] = (item.structural_path, budget.consume)

    budget_by_task = {b.task_path: b for b in manifest.budgets}
    for task_id, (path, budget_id) in consumers.items():
        entry = budget_by_task.get(task_id)
        if entry is None:
            raise CheckpointImportError(f"budget consumer '{task_id}' imported without matching budget event")
        if entry.budget_id != budget_id:
            raise CheckpointImportError(
                f"budget id mismatch for '{task_id}': manifest={entry.budget_id} schema={budget_id}"
            )
        if entry.path != path:
            raise CheckpointImportError(
                f"budget path mismatch for '{task_id}': manifest={entry.path} resolved={path}"
            )
    for entry in manifest.budgets:
        if entry.task_path not in consumers:
            raise CheckpointImportError(f"budget event for unknown consumer task_path '{entry.task_path}'")


def _require_task(tasks: dict[str, TaskProjection], event: _AttemptOutcomeEvent) -> TaskProjection:
    prev = tasks.get(event.task_id)
    if prev is None:
        raise LedgerIntegrityError(f"{event.type} references unknown task {event.task_id}")
    return prev


@dataclass(frozen=True)
class _TaskStartMeta:
    generation_ordinal: int | None
    task_key: str | None
    fan_out_child: bool
    fan_out_aggregate: bool


def _task_start_meta(
    event: TaskAttemptStartedEvent,
    fan_outs: dict[str, FanOutExpansion],
    generation: GenerationFoldState,
) -> _TaskStartMeta:
    expansion = fan_outs.get(event.node_id)
    generation_ordinal = generation.task_generation.get(event.task_id)
    fan_out_child = False
    fan_out_aggregate = False
    task_key: str | None = None
    if expansion is not None and event.task_id in expansion.task_ids:
        fan_out_child = True
        index = expansion.task_ids.index(event.task_id)
        task_key = expansion.task_keys[index]
        generation_ordinal = generation_ordinal or generation.task_generation.get(event.task_id)
    else:
        history = generation.node_histories.get(
            f"{generation.checkpoint_ns}\x1f{generation.graph_id}\x1f{event.node_id}"
        )
        if history is not None:
            for gen in history.generations_by_ordinal.values():
                if gen.aggregate_task_id == event.task_id:
                    fan_out_aggregate = True
                    generation_ordinal = gen.generation_ordinal
                    task_key = "__aggregate__"
                    break
            if generation_ordinal is None and history.latest_generation_ordinal >= 0:
                latest = history.generations_by_ordinal.get(history.latest_generation_ordinal)
                if latest is not None and (
                    latest.aggregate_task_id is None or latest.aggregate_task_id == event.task_id
                ):
                    generation_ordinal = latest.generation_ordinal
    return _TaskStartMeta(
        generation_ordinal=generation_ordinal,
        task_key=task_key,
        fan_out_child=fan_out_child,
        fan_out_aggregate=fan_out_aggregate,
    )


def _imported_task_id(event: TaskImportedEvent, fan_outs: dict[str, FanOutExpansion]) -> str:
    """导入事件的 task ID：fan-out child 复用 expansion 冻结的 ID，否则按 structural path 派生。"""
    if event.task_key is not None:
        expansion = fan_outs.get(event.node_id)
        if expansion is not None and event.task_key in expansion.task_keys:
            return expansion.task_ids[expansion.task_keys.index(event.task_key)]
        return f"{event.structural_path}:{event.node_id}:{event.task_key}"
    return f"{event.structural_path}:{event.node_id}"


def _require_v5_epoch(started: GraphInvocationStartedEvent | None, *, what: str) -> None:
    if started is None or started.event_schema_version < 5:
        raise LedgerIntegrityError(f"{what} requires event_schema_version >= 5")


def _fold_manual_plan_revision(
    *,
    event: ManualPlanRevisionEvent,
    started: GraphInvocationStartedEvent | None,
    invocation_id: str,
    current_tree_id: str,
    interrupts: dict[str, InterruptProjection],
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent],
    consumed_revision_transitions: set[str],
) -> None:
    _require_v5_epoch(started, what="manual_plan_revision")
    pending = interrupts.get(event.interrupt_id)
    if pending is None or pending.resolved_action is not None:
        raise LedgerIntegrityError(
            f"manual_plan_revision requires unresolved interrupt {event.interrupt_id} "
            f"in invocation {invocation_id}"
        )
    if pending.revision_owner_invocation_id != event.invocation_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision owner mismatch for interrupt {event.interrupt_id}: "
            f"expected {pending.revision_owner_invocation_id!r}, got {event.invocation_id!r}"
        )
    if event.base_tree_id != current_tree_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision base_tree mismatch for invocation {invocation_id}: "
            f"expected {current_tree_id}, got {event.base_tree_id}"
        )
    if event.target_tree_id == event.base_tree_id:
        raise LedgerIntegrityError(
            f"manual_plan_revision target_tree must differ from base_tree in invocation {invocation_id}"
        )
    logical = list(event.logical_paths)
    before = dict(event.before_sha256)
    after = dict(event.after_sha256)
    if set(logical) != set(before) or set(logical) != set(after):
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    if pending.revision_paths is not None and list(pending.revision_paths) != logical:
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    if pending.revision_before_sha256 is not None and dict(pending.revision_before_sha256) != before:
        raise LedgerIntegrityError(
            f"manual_plan_revision path/digest mismatch for invocation {invocation_id}"
        )
    transition_id = event.revision_transition_id
    if transition_id in unconsumed_revision_transitions or transition_id in consumed_revision_transitions:
        raise LedgerIntegrityError(
            f"manual_plan_revision reuses revision transition {transition_id} in invocation {invocation_id}"
        )
    unconsumed_revision_transitions[transition_id] = event


def _fold_graph_resumed_revision_and_source(
    *,
    event: GraphResumedEvent,
    pending: InterruptProjection,
    started: GraphInvocationStartedEvent | None,
    invocation_id: str,
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent],
    consumed_revision_transitions: set[str],
) -> None:
    has_revision_triple = event.revision_transition_id is not None
    if has_revision_triple:
        _require_v5_epoch(started, what="revision-tagged graph_resumed")
    if started is not None and started.event_schema_version >= 5:
        pending_pair = (pending.source_gate_attempt_id, pending.source_gate_tree_id)
        resume_pair = (event.source_gate_attempt_id, event.source_gate_tree_id)
        if pending_pair != resume_pair:
            raise LedgerIntegrityError(
                f"graph_resumed source_gate pair mismatch for interrupt {event.interrupt_id} "
                f"in invocation {invocation_id}"
            )
    if not has_revision_triple:
        return
    transition_id = event.revision_transition_id
    assert transition_id is not None
    is_revision_owner = pending.revision_owner_invocation_id == event.invocation_id
    if not is_revision_owner:
        # Ancestor resume: validate the ordered triple only; tree stays unchanged.
        return
    if transition_id in consumed_revision_transitions:
        raise LedgerIntegrityError(
            f"graph_resumed reuses revision transition {transition_id} in invocation {invocation_id}"
        )
    prior = unconsumed_revision_transitions.pop(transition_id, None)
    if prior is None:
        raise LedgerIntegrityError(
            f"graph_resumed missing prior unconsumed revision transition {transition_id} "
            f"in invocation {invocation_id}"
        )
    if prior.interrupt_id != event.interrupt_id:
        raise LedgerIntegrityError(
            f"graph_resumed revision transition {transition_id} interrupt mismatch "
            f"in invocation {invocation_id}"
        )
    consumed_revision_transitions.add(transition_id)


def fold_invocation_events(invocation_id: str, events: list[dict[str, object]]) -> GraphProjection:
    """纯函数：把 strict ledger 事件折叠成 ``GraphProjection``（不触碰磁盘）。

    只折叠 ``source == "graph"`` 且属于该 invocation 的事件；投影绝不反向
    覆盖 ledger。重复 ``graph_invocation_started``、未知 task 的 attempt 结局、
    未配对的 ``graph_resumed``、重复 ``budget_consumed``（按
    ``(invocation_id, budget_id, consumption_id)`` 去重）都是完整性失败。
    """
    events = migrate_events_for_fold(events)
    started: GraphInvocationStartedEvent | None = None
    event_seq = 0
    supersteps = 0
    current_superstep_task_ids: list[str] = []
    current_tree_id = ""
    latest_checkpoint_id: str | None = None
    state_values: dict[str, object] = {}
    tasks: dict[str, TaskProjection] = {}
    budgets: dict[str, int] = {}
    seen_consumptions: set[tuple[str, str]] = set()
    fan_outs: dict[str, FanOutExpansion] = {}
    interrupts: dict[str, InterruptProjection] = {}
    recoveries: dict[str, RecoveryProjection] = {}
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
    generation = GenerationFoldState()
    unconsumed_revision_transitions: dict[str, ManualPlanRevisionEvent] = {}
    consumed_revision_transitions: set[str] = set()
    seen_deferrals: dict[str, TaskSchedulingDeferredEvent] = {}
    seen_effect_acks: dict[str, DurableEffectAcknowledgedEvent] = {}

    for raw in events:
        if raw.get("source") != "graph":
            continue
        payload = {k: v for k, v in raw.items() if k not in _LEDGER_ENVELOPE_KEYS}
        try:
            event = GRAPH_EVENT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise LedgerIntegrityError(f"invalid graph event payload: {exc}") from exc
        if event.invocation_id != invocation_id:
            continue
        seq = raw.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            event_seq = max(event_seq, seq)

        if isinstance(event, GraphInvocationStartedEvent):
            if started is not None:
                raise LedgerIntegrityError(
                    f"duplicate graph_invocation_started for invocation {invocation_id}"
                )
            started = event
            current_tree_id = event.root_tree_id
            generation.invocation_id = event.invocation_id
            generation.checkpoint_ns = event.checkpoint_ns
            generation.graph_id = event.graph_id
            generation.structural_path = event.structural_path
        elif isinstance(event, NodeActivatedEvent):
            generation.apply_node_activated(event)
        elif isinstance(event, NodeSkippedEvent):
            generation.apply_node_skipped(event)
        elif isinstance(event, FanOutExpandedEvent):
            expansion = FanOutExpansion(
                items=tuple(event.items),
                task_keys=tuple(event.task_keys),
                task_ids=tuple(event.task_ids),
                source_reads_sha256=dict(event.source_reads_sha256),
            )
            fan_outs[event.node_id] = expansion
            generation.apply_fan_out_expanded(event, expansion=expansion)
        elif isinstance(event, SuperstepPlannedEvent):
            supersteps += 1
            current_superstep_task_ids = list(event.task_ids)
        elif isinstance(event, TaskAttemptStartedEvent):
            meta = _task_start_meta(event, fan_outs, generation)
            prev = tasks.get(event.task_id)
            tasks[event.task_id] = TaskProjection(
                task_id=event.task_id,
                node_id=event.node_id,
                status="running",
                generation_ordinal=meta.generation_ordinal,
                task_key=meta.task_key,
                fan_out_child=meta.fan_out_child,
                fan_out_aggregate=meta.fan_out_aggregate,
                attempts_used=max(prev.attempts_used if prev else 0, event.attempt_number),
                latest_attempt_id=event.attempt_id,
                write_set_id=prev.write_set_id if prev else None,
                outputs_sha256=prev.outputs_sha256 if prev else {},
                frozen_outputs=dict(prev.frozen_outputs) if prev else {},
                outputs_committed=prev.outputs_committed if prev else False,
                gate_report=prev.gate_report if prev else None,
                state_updates=prev.state_updates if prev else {},
                lease_expires_at=event.lease_expires_at,
                input_snapshot_id=event.input_snapshot_id,
                runtime_context_sha256=event.runtime_context_sha256,
                precommit_validator=event.precommit_validator
                if event.precommit_validator is not None
                else (prev.precommit_validator if prev else None),
                deferral_ordinal=prev.deferral_ordinal if prev else 0,
                latest_deferral_id=prev.latest_deferral_id if prev else None,
            )
            generation.apply_task_started(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskAttemptSucceededEvent):
            prev = _require_task(tasks, event)
            if prev.input_snapshot_id is not None and event.input_snapshot_id != prev.input_snapshot_id:
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded input_snapshot_id mismatch for {event.task_id}"
                )
            if (
                prev.runtime_context_sha256 is not None
                and event.runtime_context_sha256 != prev.runtime_context_sha256
            ):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded runtime_context_sha256 mismatch for {event.task_id}"
                )
            if (
                prev.candidate_validation_receipt_id is not None
                and event.candidate_validation_receipt_id != prev.candidate_validation_receipt_id
            ):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded candidate_validation_receipt_id mismatch for {event.task_id}"
                )
            if prev.precommit_validator is not None and event.candidate_validation_receipt_id is None:
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded missing candidate_validation_receipt_id for "
                    f"validator {prev.precommit_validator} on {event.task_id}"
                )
            durable_effects = tuple(dict(item) for item in event.durable_effects)
            effect_ids = []
            for item in durable_effects:
                effect_id = item.get("effect_id")
                if not isinstance(effect_id, str) or not effect_id.strip():
                    raise LedgerIntegrityError(
                        f"task_attempt_succeeded durable_effects missing effect_id for {event.task_id}"
                    )
                effect_ids.append(effect_id)
            if len(set(effect_ids)) != len(effect_ids):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded durable_effects duplicate effect_id for {event.task_id}"
                )
            if tuple(sorted(effect_ids)) != tuple(effect_ids):
                raise LedgerIntegrityError(
                    f"task_attempt_succeeded durable_effects must be sorted by effect_id for {event.task_id}"
                )
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "succeeded",
                    "latest_attempt_id": event.attempt_id,
                    "write_set_id": event.write_set_id,
                    "outputs_sha256": dict(event.outputs_sha256),
                    "frozen_outputs": dict(event.frozen_outputs),
                    "outputs_committed": False,
                    "gate_report": event.gate_report,
                    "state_updates": dict(event.state_updates),
                    "value": event.value,
                    "error_kind": None,
                    "error": None,
                    "next_retry_at": None,
                    "input_snapshot_id": event.input_snapshot_id
                    if event.input_snapshot_id is not None
                    else prev.input_snapshot_id,
                    "runtime_context_sha256": event.runtime_context_sha256
                    if event.runtime_context_sha256 is not None
                    else prev.runtime_context_sha256,
                    "candidate_validation_receipt_id": event.candidate_validation_receipt_id
                    if event.candidate_validation_receipt_id is not None
                    else prev.candidate_validation_receipt_id,
                    "precommit_validator": prev.precommit_validator,
                    "durable_effects": durable_effects,
                    "acknowledged_effect_ids": prev.acknowledged_effect_ids,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskSchedulingDeferredEvent):
            prior = seen_deferrals.get(event.deferral_id)
            if prior is not None:
                if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting task_scheduling_deferred payload for {event.deferral_id}"
                    )
                continue
            seen_deferrals[event.deferral_id] = event
            prev = tasks.get(event.task_id)
            if prev is not None and event.deferral_ordinal < prev.deferral_ordinal:
                continue
            if prev is not None and event.deferral_ordinal == prev.deferral_ordinal:
                if prev.latest_deferral_id not in (None, event.deferral_id):
                    raise LedgerIntegrityError(
                        f"conflicting deferral ordinal {event.deferral_ordinal} for {event.task_id}"
                    )
            if prev is None:
                tasks[event.task_id] = TaskProjection(
                    task_id=event.task_id,
                    node_id=event.node_id,
                    status="pending",
                    next_retry_at=event.next_retry_at,
                    deferral_ordinal=event.deferral_ordinal,
                    latest_deferral_id=event.deferral_id,
                )
            else:
                tasks[event.task_id] = prev.model_copy(
                    update={
                        "next_retry_at": event.next_retry_at,
                        "deferral_ordinal": event.deferral_ordinal,
                        "latest_deferral_id": event.deferral_id,
                        "error_kind": None,
                        "error": None,
                    }
                )
        elif isinstance(event, TaskAttemptStoppedEvent):
            prev = _require_task(tasks, event)
            value = event.value
            if value is None:
                value = {"reason": event.reason}
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "stopped",
                    "latest_attempt_id": event.attempt_id,
                    "value": value,
                    "error_kind": None,
                    "error": None,
                    "next_retry_at": None,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskAttemptFailedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={
                    "status": "failed",
                    "latest_attempt_id": event.attempt_id,
                    "error_kind": event.error_kind,
                    "error": event.message,
                    "next_retry_at": event.next_retry_at,
                }
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, TaskRecoveryRoutedEvent):
            if (
                started is None
                or event.checkpoint_ns != started.checkpoint_ns
                or event.graph_id != started.graph_id
            ):
                raise LedgerIntegrityError(
                    f"task_recovery_routed canonical identity does not match invocation {invocation_id}"
                )
            failed = tasks.get(event.task_id)
            if failed is None:
                raise LedgerIntegrityError(
                    f"task_recovery_routed references unknown task {event.task_id} "
                    f"in invocation {invocation_id}"
                )
            if event.task_id in recoveries:
                raise LedgerIntegrityError(
                    f"duplicate task recovery route for task {event.task_id} in invocation {invocation_id}"
                )
            if (
                failed.status != "failed"
                or failed.node_id != event.node_id
                or failed.generation_ordinal != event.generation_ordinal
            ):
                raise LedgerIntegrityError(
                    f"task_recovery_routed node/generation does not match failed task "
                    f"{event.task_id} in invocation {invocation_id}"
                )
            if failed.error_kind != event.error_kind or failed.error != event.message:
                raise LedgerIntegrityError(
                    f"task_recovery_routed error context does not match failed task "
                    f"{event.task_id} in invocation {invocation_id}"
                )
            recoveries[event.task_id] = RecoveryProjection(
                task_id=event.task_id,
                node_id=event.node_id,
                generation_ordinal=event.generation_ordinal,
                error_kind=event.error_kind,
                message=event.message,
                via=event.via,
                continue_to=event.continue_to,
            )
        elif isinstance(event, TaskAttemptAbandonedEvent):
            prev = _require_task(tasks, event)
            tasks[event.task_id] = prev.model_copy(
                update={"status": "abandoned", "latest_attempt_id": event.attempt_id}
            )
            generation.apply_task_outcome(event, tasks=tasks, fan_outs=fan_outs)
        elif isinstance(event, BudgetConsumedEvent):
            key = (event.budget_id, event.consumption_id)
            if key in seen_consumptions:
                raise LedgerIntegrityError(
                    "duplicate budget_consumed event for "
                    f"(invocation {invocation_id}, budget {event.budget_id}, "
                    f"consumption {event.consumption_id})"
                )
            seen_consumptions.add(key)
            budgets[event.budget_id] = budgets.get(event.budget_id, 0) + 1
        elif isinstance(event, GraphInterruptedEvent):
            interrupts[event.interrupt_id] = InterruptProjection(
                interrupt_id=event.interrupt_id,
                checkpoint_ns=event.checkpoint_ns,
                node_id=event.node_id,
                checkpoint=event.checkpoint,
                actions=tuple(event.actions),
                audited_reads_sha256=dict(event.audited_reads_sha256),
                artifact_view=event.artifact_view,
                revision_owner_invocation_id=event.revision_owner_invocation_id,
                revision_base_tree_id=event.revision_base_tree_id,
                revision_view=event.revision_view,
                revision_paths=(tuple(event.revision_paths) if event.revision_paths is not None else None),
                revision_before_sha256=(
                    dict(event.revision_before_sha256) if event.revision_before_sha256 is not None else None
                ),
                source_gate_attempt_id=event.source_gate_attempt_id,
                source_gate_tree_id=event.source_gate_tree_id,
            )
            # 嵌套 child 上抛的 interrupt：父 task 不能算成功完成，否则 resume
            # 不会重进 SubgraphHandler。同 namespace 的 builtin:interrupt 节点保持
            # succeeded，以便 resolved 后按 resume.action 路由。
            if started is not None and event.checkpoint_ns != started.checkpoint_ns:
                for task_id, task in list(tasks.items()):
                    if task.node_id == event.node_id and task.status == "succeeded":
                        tasks[task_id] = task.model_copy(update={"status": "interrupted"})
            # §5.5：interrupt 落在本图某代时，驱动 NodeGeneration.status=interrupted。
            if started is not None and event.checkpoint_ns == started.checkpoint_ns:
                generation.apply_graph_interrupted(event.node_id)
        elif isinstance(event, ManualPlanRevisionEvent):
            _fold_manual_plan_revision(
                event=event,
                started=started,
                invocation_id=invocation_id,
                current_tree_id=current_tree_id,
                interrupts=interrupts,
                unconsumed_revision_transitions=unconsumed_revision_transitions,
                consumed_revision_transitions=consumed_revision_transitions,
            )
            current_tree_id = event.target_tree_id
        elif isinstance(event, GraphResumedEvent):
            pending = interrupts.get(event.interrupt_id)
            if pending is None:
                raise LedgerIntegrityError(
                    f"graph_resumed references unknown interrupt {event.interrupt_id} "
                    f"in invocation {invocation_id}"
                )
            _fold_graph_resumed_revision_and_source(
                event=event,
                pending=pending,
                started=started,
                invocation_id=invocation_id,
                unconsumed_revision_transitions=unconsumed_revision_transitions,
                consumed_revision_transitions=consumed_revision_transitions,
            )
            interrupts[event.interrupt_id] = pending.model_copy(update={"resolved_action": event.action})
        elif isinstance(event, SuperstepCommittedEvent):
            # sibling state 直到 Update（commit）才可见：state_values 只在这里推进。
            generation.apply_outputs_commit(list(event.committed_task_ids), tasks)
            # Recovery may close an earlier mixed-result wave and commit its
            # successful siblings together with the recovery task.  Mark the
            # IDs declared by the commit event, not merely the tasks planned
            # in the latest wave, or their old write-sets will be merged again
            # and can regress the current tree pointer.
            for task_id in set(current_superstep_task_ids) | set(event.committed_task_ids):
                task = tasks.get(task_id)
                if task is not None and task.status == "succeeded":
                    tasks[task_id] = task.model_copy(update={"outputs_committed": True})
            current_superstep_task_ids = []
            latest_checkpoint_id = event.checkpoint_id
            current_tree_id = event.target_tree_id
            state_values = dict(event.state_values)
        elif isinstance(event, DurableEffectAcknowledgedEvent):
            prior_ack = seen_effect_acks.get(event.effect_id)
            if prior_ack is not None:
                if prior_ack.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise LedgerIntegrityError(
                        f"conflicting durable_effect_acknowledged payload for {event.effect_id}"
                    )
                continue
            task = tasks.get(event.task_id)
            if task is None or task.status != "succeeded":
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged without succeeded task {event.task_id}"
                )
            if not task.outputs_committed:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged before superstep commit for {event.effect_id}"
                )
            intent_ids = {
                item.get("effect_id")
                for item in task.durable_effects
                if isinstance(item.get("effect_id"), str)
            }
            if event.effect_id not in intent_ids:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged without matching inline intent {event.effect_id}"
                )
            if event.attempt_id != task.latest_attempt_id:
                raise LedgerIntegrityError(
                    f"durable_effect_acknowledged attempt mismatch for {event.effect_id}"
                )
            seen_effect_acks[event.effect_id] = event
            tasks[event.task_id] = task.model_copy(
                update={
                    "acknowledged_effect_ids": tuple(
                        sorted({*task.acknowledged_effect_ids, event.effect_id})
                    )
                }
            )
        elif isinstance(event, DurableEffectIntegrityFailedEvent):
            terminal = "failed"
            terminal_reason = "durable_effect_integrity_failed"
        elif isinstance(event, GraphTerminalEvent):
            terminal = _TERMINAL_BY_TYPE[event.type]
            terminal_reason = event.reason
        elif isinstance(event, TaskImportedEvent):
            task_id = _imported_task_id(event, fan_outs)
            if task_id in tasks:
                raise LedgerIntegrityError(
                    f"task_imported duplicates existing task {task_id} in invocation {invocation_id}"
                )
            tasks[task_id] = TaskProjection(
                task_id=task_id,
                node_id=event.node_id,
                status="succeeded",
                attempts_used=0,  # 导入不伪造物理 attempt
                outputs_sha256=dict(event.outputs_sha256),
                gate_report=event.gate_report,
            )
            generation.apply_imported_task(
                graph_id=event.graph_id,
                node_id=event.node_id,
                task_id=task_id,
            )
        elif isinstance(event, CheckpointImportedEvent):
            pass  # fixture 导入记录不改动任务/预算投影

    if started is None:
        raise LedgerIntegrityError(f"no graph_invocation_started event for invocation {invocation_id}")
    generation.finalize_legacy_fan_out_generations(fan_outs, tasks)
    ir_digest = started.ir_digest or started.graph_digest
    return GraphProjection(
        invocation_id=started.invocation_id,
        entrypoint=started.entrypoint,
        checkpoint_ns=started.checkpoint_ns,
        parent_invocation_id=started.parent_invocation_id,
        parent_task_id=started.parent_task_id,
        structural_path=started.structural_path,
        graph_digest=started.graph_digest,
        event_schema_version=started.event_schema_version,
        ir_digest=ir_digest,
        ingest_catalog_digest=started.ingest_catalog_digest,
        contract_digests=dict(started.contract_digests),
        policy_digest=started.policy_digest,
        policy_origin=started.policy_origin,
        gate_semantics_digest=started.gate_semantics_digest,
        assurance_profile_digest=started.assurance_profile_digest,
        params=dict(started.params),
        root_tree_id=started.root_tree_id,
        current_tree_id=current_tree_id,
        latest_checkpoint_id=latest_checkpoint_id,
        event_seq=event_seq,
        supersteps=supersteps,
        state_values=state_values,
        tasks=tasks,
        budgets=budgets,
        fan_out_expansions=fan_outs,
        node_histories=generation.node_histories,
        interrupts=interrupts,
        recoveries=recoveries,
        terminal=terminal,
        terminal_reason=terminal_reason,
    )


def project_invocation(change_dir: Path, invocation_id: str) -> GraphProjection:
    """从 strict ledger 重建指定 invocation 的投影（ledger 是唯一权威）。"""
    projection = fold_invocation_events(invocation_id, read_events_strict(change_dir))
    _verify_candidate_receipts_in_store(change_dir, projection)
    return projection


def _verify_candidate_receipts_in_store(change_dir: Path, projection: GraphProjection) -> None:
    """Fail closed when a named validator's receipt is missing or unbound in CAS."""
    from assurance_agent.workflow.graph.precommit import (
        CandidateValidationError,
        bind_receipt_to_success_event,
        load_candidate_receipt,
    )
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError

    required = [
        task
        for task in projection.tasks.values()
        if task.status == "succeeded" and task.precommit_validator is not None
    ]
    if not required:
        return
    store = TreeStore(change_dir)
    for task in required:
        validator_id = task.precommit_validator
        if validator_id is None:
            continue
        receipt_id = task.candidate_validation_receipt_id
        if receipt_id is None:
            raise LedgerIntegrityError(
                f"committed task {task.task_id} missing candidate_validation_receipt_id "
                f"for validator {validator_id}"
            )
        try:
            receipt = load_candidate_receipt(store, receipt_id)
        except (CandidateValidationError, WorkspaceError) as exc:
            raise LedgerIntegrityError(
                f"candidate receipt CAS verify failed for {task.task_id}: {exc}"
            ) from exc
        try:
            bind_receipt_to_success_event(
                receipt,
                validator_id=validator_id,
                invocation_id=projection.invocation_id,
                task_id=task.task_id,
                attempt_id=task.latest_attempt_id or "",
                input_snapshot_id=task.input_snapshot_id,
                write_set_id=task.write_set_id,
            )
        except CandidateValidationError as exc:
            raise LedgerIntegrityError(
                f"candidate receipt identity mismatch for {task.task_id}: {exc}"
            ) from exc


def project_workflow_state(projection: GraphProjection) -> WorkflowStateProjection:
    """派生 ``workflow-state.yaml`` 兼容视图；每个字段仅来自 ``GraphProjection``。"""
    nodes: dict[str, list[str]] = {}
    for task_id in sorted(projection.tasks):
        nodes.setdefault(projection.tasks[task_id].node_id, []).append(task_id)
    return WorkflowStateProjection(
        invocation_id=projection.invocation_id,
        entrypoint=projection.entrypoint,
        terminal=projection.terminal,
        terminal_reason=projection.terminal_reason,
        latest_checkpoint_id=projection.latest_checkpoint_id,
        event_seq=projection.event_seq,
        nodes={node_id: tuple(task_ids) for node_id, task_ids in sorted(nodes.items())},
        tasks={task_id: projection.tasks[task_id] for task_id in sorted(projection.tasks)},
        pending_interrupts=tuple(
            interrupt
            for _, interrupt in sorted(projection.interrupts.items())
            if interrupt.resolved_action is None
        ),
        budgets=dict(sorted(projection.budgets.items())),
    )


def render_workflow_state_yaml(projection: GraphProjection) -> bytes:
    """把兼容视图渲染成 ``workflow-state.yaml`` 字节（供 projection staging 落盘）。"""
    view = project_workflow_state(projection)
    text = yaml.safe_dump(view.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    return text.encode("utf-8")


def checkpoint_snapshot_relpath(projection: GraphProjection) -> str:
    name = projection.latest_checkpoint_id or f"bootstrap-{projection.invocation_id}"
    return f"{CHECKPOINT_DIR_RELPATH}/{name}.json"


def dump_checkpoint_snapshot(projection: GraphProjection) -> bytes:
    payload = json.dumps(
        projection.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (payload + "\n").encode("utf-8")


def latest_root_invocation_id(events: list[dict[str, object]], entrypoint: str | None = None) -> str | None:
    """严格 ledger 中最近一次无 parent 的 root ``graph_invocation_started``。

    ``entrypoint`` 非空时只看该 entrypoint 的 invocation：同一 change 上
    standalone entrypoint（``archive``/``retro``）与主 ``full`` 图各自独立成
    invocation，不该互相当成「已在跑/已完成」。
    """
    latest: str | None = None
    for raw in events:
        if raw.get("source") != "graph" or raw.get("type") != "graph_invocation_started":
            continue
        if raw.get("parent_invocation_id") is not None:
            continue
        if entrypoint is not None and raw.get("entrypoint") != entrypoint:
            continue
        invocation_id = raw.get("invocation_id")
        if isinstance(invocation_id, str):
            latest = invocation_id
    return latest


class CheckpointStore:
    """checkpoint snapshot 缓存：snapshot 仅是性能缓存，ledger 才是权威。"""

    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir

    @property
    def change_dir(self) -> Path:
        return self._change_dir

    def write(self, projection: GraphProjection) -> Path:
        rel = checkpoint_snapshot_relpath(projection)
        with transaction(self._change_dir) as txn:
            txn.write_file(rel, dump_checkpoint_snapshot(projection))
        return self._change_dir / rel

    def read_latest(self, invocation_id: str) -> GraphProjection:
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        return projection

    def project(self, invocation_id: str) -> GraphProjection:
        """ledger 权威投影，并修复落后/损坏的 checkpoint 与 workflow-state 缓存。"""
        projection = project_invocation(self._change_dir, invocation_id)
        if not self._snapshot_matches(projection):
            self.write(projection)
        self._repair_workflow_state(projection)
        return projection

    def latest_root_invocation(self, entrypoint: str | None = None) -> str | None:
        """严格 ledger 中最近一次无 parent 的 root ``graph_invocation_started``。

        ``entrypoint`` 非空时只看该 entrypoint 的 invocation：同一 change 上
        standalone entrypoint（``archive``/``retro``）与主 ``full`` 图各自独立成
        invocation，不该互相当成「已在跑/已完成」。
        """
        return latest_root_invocation_id(read_events_strict(self._change_dir), entrypoint)

    def _repair_workflow_state(self, projection: GraphProjection) -> None:
        """缺失或损坏的 ``workflow-state.yaml`` 只能从 ledger 投影重建，绝不反向推断。"""
        path = self._change_dir / "workflow-state.yaml"
        expected = render_workflow_state_yaml(projection)
        try:
            current = path.read_bytes()
        except OSError:
            current = b""
        if current == expected:
            return
        with transaction(self._change_dir) as txn:
            txn.set_workflow_state_projection(expected)

    def _snapshot_matches(self, projection: GraphProjection) -> bool:
        """仅当 snapshot 的 event_seq 与 digest 三元组和 ledger 投影一致时接受。"""
        path = self._change_dir / checkpoint_snapshot_relpath(projection)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        try:
            cached = GraphProjection.model_validate(raw)
        except ValidationError:
            return False
        return (
            cached.invocation_id == projection.invocation_id
            and cached.event_seq == projection.event_seq
            and cached.graph_digest == projection.graph_digest
            and cached.contract_digests == projection.contract_digests
            and cached.params == projection.params
        )


__all__ = [
    "CHECKPOINT_DIR_RELPATH",
    "CheckpointImportError",
    "CheckpointStore",
    "ValidatedImport",
    "checkpoint_snapshot_relpath",
    "dump_checkpoint_snapshot",
    "fold_invocation_events",
    "latest_root_invocation_id",
    "parse_import_manifest",
    "project_invocation",
    "project_workflow_state",
    "render_workflow_state_yaml",
    "validate_import",
]
