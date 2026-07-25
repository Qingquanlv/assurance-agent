"""EvidenceBinding：按 producer_task_id 读取已 commit 的 frozen_outputs（v7.1 §9）。"""

from __future__ import annotations

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.frozen_output import FrozenOutput, frozen_outputs_from_wire
from assurance_agent.workflow.graph.models import EvidenceBinding, GraphProjection
from assurance_agent.workflow.graph.schema_v2 import EvidenceRef, NodeDef


class EvidenceResolutionError(AaError):
    pass


def resolve_committed_frozen_output(
    projection: GraphProjection,
    binding: EvidenceBinding,
) -> FrozenOutput:
    """读取 producer task 已 commit 的 ``FrozenOutput``；禁止按 node latest 重解析。"""
    task = projection.tasks.get(binding.producer_task_id)
    if task is None:
        raise EvidenceResolutionError(f"evidence producer task {binding.producer_task_id} not found")
    if task.status != "succeeded":
        raise EvidenceResolutionError(
            f"evidence producer task {binding.producer_task_id} status is {task.status}, expected succeeded"
        )
    if not task.outputs_committed:
        raise EvidenceResolutionError(
            f"evidence producer task {binding.producer_task_id} outputs are not committed"
        )
    parsed = frozen_outputs_from_wire(task.frozen_outputs)
    frozen = parsed.get(binding.symbol)
    if frozen is None:
        raise EvidenceResolutionError(
            f"evidence symbol {binding.symbol!r} missing on task {binding.producer_task_id}"
        )
    if frozen.source_sha256 != binding.source_sha256:
        raise EvidenceResolutionError(
            f"evidence source_sha256 drift for task {binding.producer_task_id} "
            f"symbol {binding.symbol!r}"
        )
    return frozen


def resolve_committed_value(projection: GraphProjection, binding: EvidenceBinding) -> object:
    return resolve_committed_frozen_output(projection, binding).value


def binding_from_task(
    projection: GraphProjection, *, alias: str, producer_task_id: str, symbol: str
) -> EvidenceBinding:
    """Plan-time freeze：从已 commit 的 task 投影构造 binding。"""
    task = projection.tasks.get(producer_task_id)
    if task is None:
        raise EvidenceResolutionError(f"evidence producer task {producer_task_id} not found")
    if task.status != "succeeded" or not task.outputs_committed:
        raise EvidenceResolutionError(
            f"evidence producer task {producer_task_id} is not committed-success"
        )
    parsed = frozen_outputs_from_wire(task.frozen_outputs)
    frozen = parsed.get(symbol)
    if frozen is None:
        raise EvidenceResolutionError(
            f"evidence symbol {symbol!r} missing on task {producer_task_id}"
        )
    return EvidenceBinding(
        alias=alias,
        producer_task_id=producer_task_id,
        symbol=symbol,
        source_sha256=frozen.source_sha256,
    )


def _evidence_specs(node_def: NodeDef) -> dict[str, EvidenceRef]:
    specs = dict(node_def.evidence)
    with_evidence = node_def.with_.get("evidence")
    if isinstance(with_evidence, dict):
        for alias, raw in with_evidence.items():
            if alias in specs or not isinstance(raw, dict):
                continue
            node = raw.get("node")
            symbol = raw.get("symbol")
            if isinstance(node, str) and isinstance(symbol, str):
                task_key = raw.get("task_key")
                specs[alias] = EvidenceRef(
                    node=node,
                    symbol=symbol,
                    task_key=task_key if isinstance(task_key, str) else None,
                )
    return specs


def _producer_task_id(projection: GraphProjection, ref: EvidenceRef) -> str:
    candidates = [
        task
        for task in projection.tasks.values()
        if task.node_id == ref.node
        and task.status == "succeeded"
        and task.outputs_committed
        and (ref.task_key is None or task.task_key == ref.task_key)
    ]
    if ref.task_key is None:
        non_child = [task for task in candidates if not task.fan_out_child]
        if non_child:
            candidates = non_child
    if len(candidates) != 1:
        found = sorted(task.task_id for task in candidates)
        raise EvidenceResolutionError(
            f"evidence producer node {ref.node!r} task_key={ref.task_key!r}: "
            f"expected 1 committed-success task, found {len(candidates)}: {found}"
        )
    return candidates[0].task_id


def resolve_evidence_bindings(projection: GraphProjection, node_def: NodeDef) -> tuple[EvidenceBinding, ...]:
    """Plan-time freeze：解析 node evidence 声明为 ``EvidenceBinding`` 元组。"""
    bindings: list[EvidenceBinding] = []
    for alias, ref in sorted(_evidence_specs(node_def).items()):
        producer_task_id = _producer_task_id(projection, ref)
        bindings.append(
            binding_from_task(
                projection, alias=alias, producer_task_id=producer_task_id, symbol=ref.symbol
            )
        )
    return tuple(bindings)


def resolve_evidence_values(
    projection: GraphProjection, bindings: tuple[EvidenceBinding, ...]
) -> dict[str, object]:
    """Execution-time：把冻结 bindings 解析为 ``{alias: value}``；drift 时 fail closed。"""
    values: dict[str, object] = {}
    for binding in bindings:
        values[binding.alias] = resolve_committed_value(projection, binding)
    return values


__all__ = [
    "EvidenceResolutionError",
    "binding_from_task",
    "resolve_committed_frozen_output",
    "resolve_committed_value",
    "resolve_evidence_bindings",
    "resolve_evidence_values",
]
