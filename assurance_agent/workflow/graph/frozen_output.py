"""FrozenOutput envelope（v7.1 §4）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_SYMBOL_BYTES = 64 * 1024
MAX_TASK_AGGREGATE_BYTES = 256 * 1024


class FrozenOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: object
    source_path: str
    source_sha256: str
    model_id: str
    model_schema_digest: str
    catalog_symbol: str

    def canonical_value_bytes(self) -> bytes:
        if isinstance(self.value, BaseModel):
            payload = self.value.model_dump(mode="json", exclude_unset=True)
        else:
            payload = self.value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def frozen_outputs_wire(outputs: dict[str, FrozenOutput]) -> dict[str, object]:
    return {symbol: fo.to_wire() for symbol, fo in outputs.items()}


def frozen_outputs_from_wire(raw: dict[str, object] | None) -> dict[str, FrozenOutput]:
    if not raw:
        return {}
    out: dict[str, FrozenOutput] = {}
    for symbol, item in raw.items():
        if isinstance(item, dict):
            out[str(symbol)] = FrozenOutput.model_validate(item)
    return out


def candidate_value_map(outputs: dict[str, FrozenOutput]) -> dict[str, object]:
    return {symbol: fo.value for symbol, fo in outputs.items()}


def enforce_size_limits(outputs: dict[str, FrozenOutput]) -> None:
    total = 0
    for symbol, fo in outputs.items():
        size = len(fo.canonical_value_bytes())
        if size > MAX_SYMBOL_BYTES:
            raise ValueError(f"frozen output '{symbol}' exceeds {MAX_SYMBOL_BYTES} bytes")
        total += size
    if total > MAX_TASK_AGGREGATE_BYTES:
        raise ValueError(f"task frozen_outputs aggregate exceeds {MAX_TASK_AGGREGATE_BYTES} bytes")


def deep_merge_frozen(
    base: dict[str, FrozenOutput],
    overlay: dict[str, FrozenOutput],
) -> dict[str, FrozenOutput]:
    merged = dict(base)
    merged.update(overlay)
    return merged
