"""Attempt-graph channel for committed artifact refs."""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, TypedDict

from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef, coerce_artifact_ref


@dataclass(frozen=True, slots=True)
class NamedWrite:
    """One named output. ``many`` is a directory; a file matches ``root`` exactly."""

    name: str
    root: str
    many: bool = False


@dataclass(frozen=True, slots=True)
class InputBinding:
    """Copy ledger refs for ``ledger_key`` onto one attempt-input field."""

    ledger_key: str
    field: str
    many: bool = False


def _ref_dict(value: object) -> dict[str, str]:
    if not isinstance(value, (ArtifactRef, Mapping)):
        raise TypeError("artifact ledger values must be artifact refs")
    ref = coerce_artifact_ref(value)
    return {"path": ref.path, "digest": ref.digest}


def _ref_list(value: object) -> list[dict[str, str]]:
    if isinstance(value, Mapping) and "path" in value and "digest" in value:
        return [_ref_dict(value)]
    if isinstance(value, (list, tuple)):
        return [_ref_dict(item) for item in value]
    raise TypeError("artifact ledger values must be a ref or a list of refs")


def merge_artifact_ledger(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> dict[str, list[dict[str, str]]]:
    """Last write wins per logical key. A missing side is an empty ledger."""
    merged: dict[str, list[dict[str, str]]] = {}
    for batch in (left, right):
        if batch is None:
            continue
        if not isinstance(batch, Mapping):
            raise TypeError("artifact_ledger must be a mapping")
        for key, value in batch.items():
            merged[str(key)] = _ref_list(value)
    return merged


class AttemptLedgerState(TypedDict, total=False):
    attempt_failure: dict[str, object]
    artifact_ledger: Annotated[dict[str, list[dict[str, str]]], merge_artifact_ledger]


def ledger_key(namespace: str, name: str) -> str:
    return f"{namespace}.{name}"


def matches_named_write(spec: NamedWrite, path: str) -> bool:
    if spec.many:
        return path == spec.root or path.startswith(f"{spec.root}/")
    return path == spec.root


def fill_artifact_ledger(
    namespace: str,
    writes: Sequence[NamedWrite],
    committed: Sequence[ArtifactRef | Mapping[str, object]],
) -> dict[str, list[dict[str, str]]]:
    """Map sealed refs onto named writes. Each value is the list of matching refs.

    A name that matches nothing is omitted, so a later commit does not erase a
    name it did not rewrite. The caller is the only ledger writer.
    """
    if not namespace or namespace.startswith(".") or namespace.endswith("."):
        raise ValueError(
            f"namespace must be a non-empty string without leading or trailing dots, got {namespace!r}"
        )
    refs = tuple(coerce_artifact_ref(item) for item in committed)
    ledger: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for spec in writes:
        if not spec.name or "." in spec.name or "/" in spec.name:
            raise ValueError(f"produced artifact name must be a single path segment, got {spec.name!r}")
        if spec.name in seen:
            raise ValueError(f"produced artifact name is declared twice: {spec.name}")
        seen.add(spec.name)
        matched = tuple(ref for ref in refs if matches_named_write(spec, ref.path))
        ordered = tuple(sorted(matched, key=lambda item: (item.path, item.digest)))
        if not ordered:
            continue
        ledger[ledger_key(namespace, spec.name)] = [
            {"path": ref.path, "digest": ref.digest} for ref in ordered
        ]
    return ledger


def ledger_refs(ledger: object, logical_name: str) -> list[dict[str, str]]:
    """Refs stored under ``logical_name``, one per path, in path order."""
    if ledger is None:
        return []
    if not isinstance(ledger, Mapping):
        raise TypeError("artifact_ledger must be a mapping")
    value = ledger.get(logical_name)
    if value is None:
        return []
    found: dict[str, dict[str, str]] = {}
    for ref in _ref_list(value):
        found[ref["path"]] = ref
    return [found[path] for path in sorted(found)]


def bind_input_slots(select: object, bindings: Sequence[InputBinding]) -> object:
    """Overlay ledger refs onto the selected attempt input before the attempt key."""

    if not callable(select):
        raise TypeError("select must be callable")

    @functools.wraps(select)
    def wrapped(state: object) -> object:
        selected = select(state)
        if not bindings or not isinstance(selected, BaseModel) or not isinstance(state, Mapping):
            return selected
        data: dict[str, object] | None = None
        ledger = state.get("artifact_ledger")
        for binding in bindings:
            refs = ledger_refs(ledger, binding.ledger_key)
            if not refs:
                continue
            if data is None:
                data = selected.model_dump(mode="json")
            if binding.many:
                data[binding.field] = refs
            elif len(refs) == 1:
                data[binding.field] = refs[0]
        if data is None:
            return selected
        return type(selected).model_validate(data)

    return wrapped


__all__ = [
    "AttemptLedgerState",
    "InputBinding",
    "NamedWrite",
    "bind_input_slots",
    "fill_artifact_ledger",
    "ledger_key",
    "ledger_refs",
    "matches_named_write",
    "merge_artifact_ledger",
]
