"""Explicit import-checkpoint validation. Does not write ledger events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.artifacts.paths import existing_with_alias
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.tree_hash import sha256_file
from assurance_agent.workflow.graph.models import (
    CompiledGraph,
    CompiledWorkflow,
    GraphProjection,
    ImportManifest,
    ImportedBudget,
    ImportedTask,
    RuntimeContext,
)
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
    checkpoint_ns: str,
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
    state_values = state_values_for_import(context, projection)

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
            compiled,
            context,
            task,
            checkpoint_ns=checkpoint_ns,
            state_values=state_values,
            # Per-structural-path locals (MERGE_HEAD); not a missing global.
            node_results=local_results,
            projection=projection,
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


def state_values_for_import(context: RuntimeContext, projection: GraphProjection | None) -> dict[str, object]:
    """Gate ``state.*`` for import-checkpoint: ledger projection wins over YAML."""
    if projection is not None:
        return dict(projection.state_values)
    return _state_values_from_change(context)


def _state_values_from_change(context: RuntimeContext) -> dict[str, object]:
    """Load workflow-state.yaml into gate ``state.*`` (legacy phase/run_context stamps)."""
    path = existing_with_alias(context.change_dir / "workflow-state.json")
    if path is None:
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
    checkpoint_ns: str,
    state_values: Mapping[str, object] | None = None,
    node_results: Mapping[str, object] | None = None,
    projection: GraphProjection | None = None,
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
        state_values=(
            dict(state_values) if state_values is not None else state_values_for_import(context, projection)
        ),
        node_results=dict(node_results) if node_results is not None else {},
        audit_events_dir=context.change_dir,
        checkpoint_ns=checkpoint_ns,
        host_project_root=context.resolved_host_root,
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
