"""Graph ledger v1→v2 事件流迁移（S0a）。

有状态深模块：按 seq 顺序回填 ``generation_ordinal``、``ir_digest`` 等 v2 字段。
fold **只**消费 migrate 后的 payload。
"""

from __future__ import annotations

import copy
import json


def event_payload_canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


_GRAPH_TYPES = frozenset(
    {
        "graph_invocation_started",
        "node_activated",
        "node_skipped",
        "fan_out_expanded",
        "superstep_planned",
        "task_attempt_started",
        "task_attempt_succeeded",
        "task_attempt_failed",
        "task_attempt_stopped",
        "task_attempt_abandoned",
        "budget_consumed",
        "graph_interrupted",
        "graph_resumed",
        "superstep_committed",
        "graph_completed",
        "graph_stopped",
        "graph_failed",
        "task_imported",
        "checkpoint_imported",
    }
)


def migrate_graph_event_stream(raw_graph_events: list[dict[str, object]]) -> list[dict[str, object]]:
    """按 seq 顺序迁移 graph 事件 payload（不含 ledger 信封键）。"""
    next_generation: dict[tuple[str, str, str], int] = {}
    task_generation: dict[str, int] = {}
    fan_out_by_node: dict[str, dict[str, object]] = {}
    seen_activation: dict[tuple[str, int], bytes] = {}
    out: list[dict[str, object]] = []

    for raw in raw_graph_events:
        event = copy.deepcopy(raw)
        etype = event.get("type")
        if not isinstance(etype, str) or etype not in _GRAPH_TYPES:
            out.append(event)
            continue

        if etype == "graph_invocation_started":
            _migrate_invocation_started(event)
            out.append(event)
            continue

        ns = str(event.get("checkpoint_ns", ""))
        graph_id = str(event.get("graph_id", ""))
        node_id = event.get("node_id")
        node_key = (ns, graph_id, str(node_id)) if isinstance(node_id, str) else None

        if etype in ("node_activated", "node_skipped") and node_key is not None:
            ordinal = event.get("generation_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                ordinal = next_generation.get(node_key, 0)
                event["generation_ordinal"] = ordinal
                next_generation[node_key] = ordinal + 1
            payload = _graph_payload(event)
            ordinal_value = event["generation_ordinal"]
            assert isinstance(ordinal_value, int) and not isinstance(ordinal_value, bool)
            slot = (f"{node_key[0]}\x1f{node_key[1]}\x1f{node_key[2]}", ordinal_value)
            canonical = event_payload_canonical_bytes(payload)
            prev = seen_activation.get(slot)
            if prev is not None and prev == canonical:
                out.append(event)
                continue
            seen_activation[slot] = canonical

        if etype == "fan_out_expanded" and node_key is not None:
            ordinal = event.get("generation_ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                ordinal = next_generation.get(node_key, 0)
                if node_key not in next_generation:
                    next_generation[node_key] = ordinal + 1
                event["generation_ordinal"] = ordinal
            fan_out_by_node[str(node_id)] = event
            task_ids = event.get("task_ids")
            generation_ordinal = event.get("generation_ordinal")
            if isinstance(task_ids, list) and isinstance(generation_ordinal, int) and not isinstance(
                generation_ordinal, bool
            ):
                for task_id in task_ids:
                    if isinstance(task_id, str):
                        task_generation[task_id] = generation_ordinal

        if etype == "task_attempt_succeeded":
            event.setdefault("frozen_outputs", {})

        if etype == "superstep_committed":
            event.setdefault("committed_task_ids", [])

        if etype == "graph_resumed":
            event.setdefault("payload", {})

        out.append(event)

    return out


def merge_preserving_seq(all_events: list[dict[str, object]], migrated_graph: list[dict[str, object]]) -> list[dict[str, object]]:
    """用 migrated graph payload 替换原流中的 graph 行，non-graph 逐字节保留。"""
    graph_iter = iter(migrated_graph)
    merged: list[dict[str, object]] = []
    for event in all_events:
        if event.get("source") == "graph":
            migrated = next(graph_iter)
            merged.append({**event, **migrated})
        else:
            merged.append(event)
    return merged


def migrate_events_for_fold(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """fold 入口：提取 graph payload → migrate → 写回。"""
    graph_payloads: list[dict[str, object]] = []
    for event in events:
        if event.get("source") != "graph":
            continue
        graph_payloads.append({k: v for k, v in event.items() if k not in ("seq", "ts")})
    if not graph_payloads:
        return events
    migrated = migrate_graph_event_stream(graph_payloads)
    return merge_preserving_seq(events, migrated)


def _migrate_invocation_started(event: dict[str, object]) -> None:
    version = event.get("event_schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        event["event_schema_version"] = 1
    elif version > 3:
        raise ValueError(f"unsupported graph event_schema_version {version}")
    if "ir_digest" not in event:
        graph_digest = event.get("graph_digest")
        if isinstance(graph_digest, str):
            event["ir_digest"] = graph_digest
        else:
            event["ir_digest"] = ""
    if "ingest_catalog_digest" not in event:
        event["ingest_catalog_digest"] = ""


def _graph_payload(event: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in event.items() if k not in ("seq", "ts")}
