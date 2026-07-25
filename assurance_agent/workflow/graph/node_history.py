"""NodeHistory / generation reducer（S0a）。

``NodeHistory`` 是 DSL ``node(id).outputs`` 与 generation 完整性的权威投影；
fold 内唯一写入点为本模块的 ``GenerationFoldState`` reducer。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from assurance_agent.workflow.core.events import LedgerIntegrityError
from assurance_agent.workflow.core.graph_events import (
    FanOutExpandedEvent,
    NodeActivatedEvent,
    NodeSkippedEvent,
    TaskAttemptAbandonedEvent,
    TaskAttemptFailedEvent,
    TaskAttemptStartedEvent,
    TaskAttemptStoppedEvent,
    TaskAttemptSucceededEvent,
)
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.frozen_output import frozen_outputs_from_wire
from assurance_agent.workflow.graph.models import FanOutExpansion, GraphProjection, NodeGeneration, NodeHistory, TaskProjection

GenerationStatus = Literal[
    "activated",
    "skipped",
    "running",
    "succeeded",
    "failed",
    "abandoned",
    "interrupted",
    "stopped",
]

_TERMINAL_CHILD_FAILURE = frozenset({"failed", "abandoned"})


def node_history_key(checkpoint_ns: str, graph_id: str, node_id: str) -> str:
    return f"{checkpoint_ns}\x1f{graph_id}\x1f{node_id}"


def event_payload_canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decision_identity(event: NodeActivatedEvent | NodeSkippedEvent) -> bytes:
    """同代 activate/skip 幂等比对用的**决策身份**（G1）。

    只含决定「这一代要不要跑、跑什么」的字段：activate 用 ``activation_id`` +
    ``input_sha256``，skip 用 ``expression`` + ``input_sha256``。
    ``source_reads_sha256`` 是观测性来源快照——planner 在后续 superstep 重发同一
    决策时，上游刚落盘的文件会让它增长（例如 healing 的 ``complete-not-needed``
    在 ``proposal`` 写出 ``fix-proposal.json`` 前后各被跳过一次），那不是决策变化，
    不能算 ledger 冲突。
    """
    identity: dict[str, object] = {
        "kind": event.type,
        "node_id": event.node_id,
        "generation_ordinal": event.generation_ordinal,
        "input_sha256": event.input_sha256,
    }
    if isinstance(event, NodeActivatedEvent):
        identity["activation_id"] = event.activation_id
    else:
        identity["expression"] = event.expression
    return event_payload_canonical_bytes(identity)


def fan_out_aggregate_task_id(
    *,
    invocation_id: str,
    checkpoint_ns: str,
    structural_path: str,
    graph_id: str,
    node_id: str,
    child_count: int,
) -> str:
    return canonical_digest(
        {
            "invocation_id": invocation_id,
            "checkpoint_ns": checkpoint_ns,
            "structural_path": structural_path,
            "graph_id": graph_id,
            "node_id": node_id,
            "ordinal": child_count,
            "task_key": "__aggregate__",
        }
    )


@dataclass
class GenerationFoldState:
    node_histories: dict[str, NodeHistory] = field(default_factory=dict)
    seen_activation_payloads: dict[tuple[str, int], bytes] = field(default_factory=dict)
    task_generation: dict[str, int] = field(default_factory=dict)
    graph_id: str = ""
    structural_path: str = ""
    checkpoint_ns: str = ""
    invocation_id: str = ""

    def _put_history(self, key: str, history: NodeHistory) -> None:
        self.node_histories[key] = history

    def _history(self, node_id: str) -> NodeHistory:
        key = node_history_key(self.checkpoint_ns, self.graph_id, node_id)
        history = self.node_histories.get(key)
        if history is None:
            history = NodeHistory()
            self.node_histories[key] = history
        return history

    def _update_generation(
        self,
        node_id: str,
        generation_ordinal: int,
        **updates: object,
    ) -> NodeGeneration:
        history = self._history(node_id)
        current = history.generations_by_ordinal.get(
            generation_ordinal,
            NodeGeneration(generation_ordinal=generation_ordinal, status="activated"),
        )
        generation = current.model_copy(update=updates)
        by_ordinal = dict(history.generations_by_ordinal)
        by_ordinal[generation_ordinal] = generation
        latest = max(history.latest_generation_ordinal, generation_ordinal)
        self._put_history(
            node_history_key(self.checkpoint_ns, self.graph_id, node_id),
            history.model_copy(update={"generations_by_ordinal": by_ordinal, "latest_generation_ordinal": latest}),
        )
        return generation

    def apply_node_activated(self, event: NodeActivatedEvent) -> None:
        key_tuple = (node_history_key(event.checkpoint_ns, event.graph_id, event.node_id), event.generation_ordinal)
        canonical = decision_identity(event)
        seen = self.seen_activation_payloads.get(key_tuple)
        if seen is not None:
            if seen == canonical:
                return
            raise LedgerIntegrityError(
                f"node_activated conflict for {event.node_id} generation {event.generation_ordinal}"
            )
        self.seen_activation_payloads[key_tuple] = canonical
        self._update_generation(
            event.node_id,
            event.generation_ordinal,
            status="activated",
            reached=True,
            activation_id=event.activation_id,
        )

    def apply_node_skipped(self, event: NodeSkippedEvent) -> None:
        key_tuple = (node_history_key(event.checkpoint_ns, event.graph_id, event.node_id), event.generation_ordinal)
        canonical = decision_identity(event)
        seen = self.seen_activation_payloads.get(key_tuple)
        if seen is not None:
            if seen == canonical:
                return
            raise LedgerIntegrityError(
                f"node_skipped conflict for {event.node_id} generation {event.generation_ordinal}"
            )
        self.seen_activation_payloads[key_tuple] = canonical
        self._update_generation(
            event.node_id,
            event.generation_ordinal,
            status="skipped",
            reached=True,
        )

    def apply_fan_out_expanded(self, event: FanOutExpandedEvent, *, expansion: FanOutExpansion) -> None:
        aggregate_id = fan_out_aggregate_task_id(
            invocation_id=event.invocation_id,
            checkpoint_ns=event.checkpoint_ns,
            structural_path=self.structural_path,
            graph_id=event.graph_id,
            node_id=event.node_id,
            child_count=len(expansion.task_ids),
        )
        self._update_generation(
            event.node_id,
            event.generation_ordinal,
            aggregate_task_id=aggregate_id,
            fan_out_expansion_id=canonical_digest(
                {
                    "node_id": event.node_id,
                    "generation_ordinal": event.generation_ordinal,
                    "task_ids": list(expansion.task_ids),
                }
            ),
        )
        self.task_generation[aggregate_id] = event.generation_ordinal
        for task_id in expansion.task_ids:
            self.task_generation[task_id] = event.generation_ordinal

    def apply_task_started(
        self,
        event: TaskAttemptStartedEvent,
        *,
        tasks: dict[str, TaskProjection],
        fan_outs: dict[str, FanOutExpansion],
    ) -> None:
        task = tasks.get(event.task_id)
        if task is None:
            return
        generation_ordinal = task.generation_ordinal
        if generation_ordinal is None:
            generation_ordinal = self.task_generation.get(event.task_id)
        if generation_ordinal is None:
            history = self._history(event.node_id)
            if history.latest_generation_ordinal >= 0:
                latest = history.generations_by_ordinal.get(history.latest_generation_ordinal)
                if latest is not None and (
                    latest.aggregate_task_id is None or latest.aggregate_task_id == event.task_id
                ):
                    generation_ordinal = latest.generation_ordinal
        if generation_ordinal is None:
            return
        if task.fan_out_child:
            self._sync_fan_out_shell_status(event.node_id, generation_ordinal, tasks, fan_outs)
            return
        history = self._history(event.node_id)
        generation = history.generations_by_ordinal.get(generation_ordinal)
        if task.fan_out_aggregate:
            if generation is not None and generation.aggregate_task_id != event.task_id:
                raise LedgerIntegrityError(
                    f"aggregate task_id mismatch for node {event.node_id}: "
                    f"expected {generation.aggregate_task_id}, got {event.task_id}"
                )
            self._update_generation(event.node_id, generation_ordinal, status="running")
            return
        self._update_generation(
            event.node_id,
            generation_ordinal,
            status="running",
            aggregate_task_id=event.task_id,
        )
        self.task_generation[event.task_id] = generation_ordinal

    def apply_task_outcome(
        self,
        event: TaskAttemptSucceededEvent
        | TaskAttemptFailedEvent
        | TaskAttemptStoppedEvent
        | TaskAttemptAbandonedEvent,
        *,
        tasks: dict[str, TaskProjection],
        fan_outs: dict[str, FanOutExpansion],
    ) -> None:
        task = tasks.get(event.task_id)
        if task is None:
            return
        generation_ordinal = task.generation_ordinal or self.task_generation.get(event.task_id)
        if generation_ordinal is None:
            return
        if task.fan_out_child:
            self._sync_fan_out_shell_status(task.node_id, generation_ordinal, tasks, fan_outs)
            return
        if task.fan_out_aggregate:
            updates: dict[str, object] = {"status": _outcome_status(event)}
            if isinstance(event, TaskAttemptSucceededEvent):
                updates["frozen_outputs"] = _wire_to_frozen_dict(event.frozen_outputs)
            self._update_generation(task.node_id, generation_ordinal, **updates)
            return
        updates = {"status": _outcome_status(event)}
        if isinstance(event, TaskAttemptSucceededEvent):
            updates["frozen_outputs"] = _wire_to_frozen_dict(event.frozen_outputs)
        self._update_generation(task.node_id, generation_ordinal, **updates)

    def apply_imported_task(
        self,
        *,
        graph_id: str,
        node_id: str,
        task_id: str,
        frozen_outputs: dict[str, object] | None = None,
    ) -> None:
        """Import path: mark latest generation succeeded pending commit."""
        history = self._history(node_id)
        ordinal = max(history.latest_generation_ordinal + 1, 0)
        self._update_generation(
            node_id,
            ordinal,
            status="succeeded",
            aggregate_task_id=task_id,
            frozen_outputs=frozen_outputs if frozen_outputs is not None else {},
        )
        self.task_generation[task_id] = ordinal

    def apply_outputs_commit(self, committed_task_ids: list[str], tasks: dict[str, TaskProjection]) -> None:
        for task_id in committed_task_ids:
            task = tasks.get(task_id)
            if task is None:
                raise LedgerIntegrityError(f"commit references unknown task {task_id}")
            if task.fan_out_child:
                continue
            generation_ordinal = task.generation_ordinal or self.task_generation.get(task_id)
            if generation_ordinal is None:
                history = self._history(task.node_id)
                for gen in history.generations_by_ordinal.values():
                    if gen.aggregate_task_id == task_id:
                        generation_ordinal = gen.generation_ordinal
                        break
                if generation_ordinal is None and history.latest_generation_ordinal >= 0:
                    latest = history.generations_by_ordinal.get(history.latest_generation_ordinal)
                    if latest is not None and latest.aggregate_task_id == task_id:
                        generation_ordinal = latest.generation_ordinal
            if generation_ordinal is None:
                raise LedgerIntegrityError(f"commit task {task_id} has no generation binding")
            history = self._history(task.node_id)
            generation = history.generations_by_ordinal.get(generation_ordinal)
            if generation is None:
                raise LedgerIntegrityError(
                    f"commit task {task_id} references missing generation {generation_ordinal}"
                )
            if generation.status != "succeeded":
                raise LedgerIntegrityError(
                    f"commit task {task_id} generation status is {generation.status}, expected succeeded"
                )
            if not generation.frozen_outputs and not task.fan_out_aggregate:
                # path_only / no ingest symbols may legitimately be {}
                pass
            self._update_generation(
                task.node_id,
                generation_ordinal,
                outputs_committed=True,
            )

    def finalize_legacy_fan_out_generations(
        self,
        fan_outs: dict[str, FanOutExpansion],
        tasks: dict[str, TaskProjection],
    ) -> None:
        """Legacy ledger：child 全成功且无 aggregate 时，标记代 succeeded（v7.1 §6.4 选项 B）。"""
        for node_id, expansion in fan_outs.items():
            history = self._history(node_id)
            for ordinal, generation in history.generations_by_ordinal.items():
                if generation.aggregate_task_id is None:
                    continue
                if generation.status in ("succeeded", "failed", "skipped"):
                    continue
                if not all(self.task_generation.get(tid) == ordinal for tid in expansion.task_ids):
                    continue
                child_statuses = [tasks[tid].status for tid in expansion.task_ids if tid in tasks]
                if child_statuses and all(status == "succeeded" for status in child_statuses):
                    self._update_generation(
                        node_id,
                        ordinal,
                        status="succeeded",
                        outputs_committed=True,
                        frozen_outputs={},
                    )

    def apply_graph_interrupted(self, node_id: str) -> None:
        """§5.5：interrupt 落在本图某代 → 该代 status=interrupted（含 fan-out 壳层）。

        仅对本 invocation（reducer 的 ``checkpoint_ns``/``graph_id``）内已存在的
        node 生效；nested child 上抛的 interrupt（不同 ns）不在此驱动。已终局的代
        （succeeded/failed/skipped/abandoned/stopped）不回退。resume 后重跑会经
        ``apply_task_started`` 覆盖回 ``running``。
        """
        key = node_history_key(self.checkpoint_ns, self.graph_id, node_id)
        history = self.node_histories.get(key)
        if history is None or history.latest_generation_ordinal < 0:
            return
        generation = history.generations_by_ordinal.get(history.latest_generation_ordinal)
        if generation is None:
            return
        if generation.status in ("succeeded", "failed", "skipped", "abandoned", "stopped"):
            return
        self._update_generation(node_id, history.latest_generation_ordinal, status="interrupted")

    def _sync_fan_out_shell_status(
        self,
        node_id: str,
        generation_ordinal: int,
        tasks: dict[str, TaskProjection],
        fan_outs: dict[str, FanOutExpansion],
    ) -> None:
        expansion = fan_outs.get(node_id)
        if expansion is None:
            return
        history = self._history(node_id)
        generation = history.generations_by_ordinal.get(generation_ordinal)
        if generation is None or generation.status == "skipped":
            return
        child_statuses = [tasks[tid].status for tid in expansion.task_ids if tid in tasks]
        if not child_statuses:
            return
        if any(status in _TERMINAL_CHILD_FAILURE for status in child_statuses):
            self._update_generation(node_id, generation_ordinal, status="failed")
            return
        if any(status in ("running", "pending", "interrupted") for status in child_statuses):
            self._update_generation(node_id, generation_ordinal, status="running")
            return

def _outcome_status(
    event: TaskAttemptSucceededEvent
    | TaskAttemptFailedEvent
    | TaskAttemptStoppedEvent
    | TaskAttemptAbandonedEvent,
) -> GenerationStatus:
    if isinstance(event, TaskAttemptSucceededEvent):
        return "succeeded"
    if isinstance(event, TaskAttemptFailedEvent):
        return "failed"
    if isinstance(event, TaskAttemptStoppedEvent):
        return "stopped"
    return "abandoned"


def _wire_to_frozen_dict(raw: dict[str, object]) -> dict[str, object]:
    """Store frozen outputs on NodeGeneration as serializable wire maps."""
    parsed = frozen_outputs_from_wire(raw)
    return {symbol: fo.to_wire() for symbol, fo in parsed.items()}


def build_node_results_for_gate(
    projection: GraphProjection,
    *,
    graph_id: str,
) -> dict[str, object]:
    """Committed node outcomes for gate evaluation (DSL-shaped)."""
    results: dict[str, object] = {}
    for key, history in projection.node_histories.items():
        parts = key.split("\x1f")
        if len(parts) != 3:
            continue
        _ns, gid, node_id = parts
        if gid != graph_id:
            continue
        if history.latest_generation_ordinal < 0:
            continue
        generation = history.generations_by_ordinal.get(history.latest_generation_ordinal)
        if generation is None:
            continue
        payload: dict[str, object] = {"status": generation.status}
        outputs = committed_node_outputs(
            projection.node_histories,
            checkpoint_ns=_ns,
            graph_id=gid,
            node_id=node_id,
        )
        if outputs is not None:
            payload["outputs"] = outputs
        if generation.gate_report is not None:
            payload["gate"] = generation.gate_report
            if "value" in generation.gate_report:
                payload["value"] = generation.gate_report["value"]
        results[node_id] = payload
    return results


def committed_node_outputs(
    node_histories: dict[str, NodeHistory],
    *,
    checkpoint_ns: str,
    graph_id: str,
    node_id: str,
) -> dict[str, object] | None:
    """DSL-shaped value map from latest committed-success generation."""
    key = node_history_key(checkpoint_ns, graph_id, node_id)
    history = node_histories.get(key)
    if history is None or history.latest_generation_ordinal < 0:
        return None
    generation = history.generations_by_ordinal.get(history.latest_generation_ordinal)
    if generation is None:
        return None
    if generation.status != "succeeded" or not generation.outputs_committed:
        return None
    wire = generation.frozen_outputs
    if not isinstance(wire, dict):
        return None
    parsed = frozen_outputs_from_wire(wire)
    return {symbol: fo.value for symbol, fo in parsed.items()}
