"""Subgraph 整包 export（v7.1 §12 / S5）。"""

from __future__ import annotations

from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.graph.frozen_output import FrozenOutput
from assurance_kernel.workflow.graph.models import CompiledExport, GraphProjection
from assurance_kernel.workflow.graph.node_history import committed_node_outputs, node_history_key


class SubgraphExportError(AaError):
    pass


def apply_subgraph_exports(
    *,
    child_projection: GraphProjection,
    child_graph_id: str,
    export_defs: tuple[CompiledExport, ...],
) -> dict[str, FrozenOutput]:
    """转发子图 committed ``FrozenOutput``（copy-by-ref，同一 ``source_sha256``）。"""
    from assurance_kernel.workflow.graph.frozen_output import frozen_outputs_from_wire

    exported: dict[str, FrozenOutput] = {}
    for spec in export_defs:
        key = node_history_key(child_projection.checkpoint_ns, child_graph_id, spec.from_node)
        history = child_projection.node_histories.get(key)
        if history is None or history.latest_generation_ordinal < 0:
            raise SubgraphExportError(f"export {spec.symbol!r}: child node {spec.from_node!r} has no history")
        generation = history.generations_by_ordinal.get(history.latest_generation_ordinal)
        if generation is None or generation.status != "succeeded" or not generation.outputs_committed:
            raise SubgraphExportError(
                f"export {spec.symbol!r}: child node {spec.from_node!r} has no committed success"
            )
        wire = frozen_outputs_from_wire(generation.frozen_outputs)
        frozen = wire.get(spec.output)
        if frozen is None:
            outputs = committed_node_outputs(
                child_projection.node_histories,
                checkpoint_ns=child_projection.checkpoint_ns,
                graph_id=child_graph_id,
                node_id=spec.from_node,
            )
            if outputs is None or spec.output not in outputs:
                raise SubgraphExportError(
                    f"export {spec.symbol!r}: output {spec.output!r} missing on child node {spec.from_node!r}"
                )
            raise SubgraphExportError(
                f"export {spec.symbol!r}: output {spec.output!r} on {spec.from_node!r} "
                "has no FrozenOutput envelope"
            )
        exported[spec.symbol] = frozen
    return exported


__all__ = ["SubgraphExportError", "apply_subgraph_exports"]
