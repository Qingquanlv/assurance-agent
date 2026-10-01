"""Shared input helpers."""

from __future__ import annotations

from collections.abc import Iterable

from graph_engine.canonical import JSONValue, canonical_digest

from assurance_execution.contracts.selection import ClosedMappingV1


def leafs_of(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def mapping_digest(mapping: ClosedMappingV1) -> str:
    return canonical_digest(mapping.model_dump(mode="json"))


def json_digest(value: JSONValue) -> str:
    return canonical_digest(value)
