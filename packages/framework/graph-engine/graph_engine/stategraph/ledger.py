"""Attempt-graph channel for committed artifact refs."""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict, cast

from pydantic import BaseModel

from graph_engine.artifacts import ArtifactRef, coerce_artifact_ref


@dataclass(frozen=True, slots=True)
class NamedWrite:
    """One named output. ``many`` is a directory; a file matches ``root`` exactly.

    ``accumulate`` keeps earlier refs for this key and replaces only same-path digests.
    The ledger reducer still replaces a key wholesale; accumulation happens before that write.
    """

    name: str
    root: str
    many: bool = False
    accumulate: bool = False


@dataclass(frozen=True, slots=True)
class LedgerArtifact:
    """Handle for one named write. Callers take it from the producing op."""

    ledger_key: str
    slot: str | None = None
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


def _is_receipt_entry(value: object) -> bool:
    return isinstance(value, Mapping) and "refs" in value and not ("path" in value and "digest" in value)


def _ref_list(value: object) -> list[dict[str, str]]:
    if _is_receipt_entry(value):
        return _ref_list(cast(Mapping[str, object], value)["refs"])
    if isinstance(value, Mapping) and "path" in value and "digest" in value:
        return [_ref_dict(value)]
    if isinstance(value, (list, tuple)):
        return [_ref_dict(item) for item in value]
    raise TypeError("artifact ledger values must be a ref or a list of refs")


def _receipt_dict(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or "receipt_id" not in value or "receipt_digest" not in value:
        raise TypeError("artifact ledger receipt must carry receipt_id and receipt_digest")
    return {"receipt_id": str(value["receipt_id"]), "receipt_digest": str(value["receipt_digest"])}


def _stored_entry(value: object) -> object:
    """Keep a receipt entry as an object. A bare ref list stays a list."""
    if _is_receipt_entry(value):
        entry = cast(Mapping[str, object], value)
        stored: dict[str, object] = {"refs": _ref_list(entry["refs"])}
        receipt = entry.get("receipt")
        if receipt is not None:
            stored["receipt"] = _receipt_dict(receipt)
        return stored
    return _ref_list(value)


def merge_artifact_ledger(
    left: Mapping[str, object] | None,
    right: Mapping[str, object] | None,
) -> dict[str, object]:
    """Last write wins per logical key. A missing side is an empty ledger.

    A non-accumulating entry may be ``{"refs", "receipt"}``. ``ledger_refs`` still
    returns only ``path`` and ``digest``. An accumulating entry stays a ref list.
    """
    merged: dict[str, object] = {}
    for batch in (left, right):
        if batch is None:
            continue
        if not isinstance(batch, Mapping):
            raise TypeError("artifact_ledger must be a mapping")
        for key, value in batch.items():
            merged[str(key)] = _stored_entry(value)
    return merged


class AttemptLedgerState(TypedDict, total=False):
    attempt_failure: dict[str, object]
    artifact_ledger: Annotated[dict[str, object], merge_artifact_ledger]


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
    *,
    receipt: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Map sealed refs onto named writes.

    A name that matches nothing is omitted, so a later commit does not erase a
    name it did not rewrite. The caller is the only ledger writer.

    When ``receipt`` is set, a last-write key stores ``{"refs", "receipt"}`` for
    the commit that produced those refs. An accumulating key stays a ref list and
    does not record a receipt: later commits append paths, so one receipt would
    not describe the whole list.
    """
    if not namespace or namespace.startswith(".") or namespace.endswith("."):
        raise ValueError(
            f"namespace must be a non-empty string without leading or trailing dots, got {namespace!r}"
        )
    refs = tuple(coerce_artifact_ref(item) for item in committed)
    ledger: dict[str, object] = {}
    seen: set[str] = set()
    mapped_receipt = None if receipt is None else _receipt_dict(receipt)
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
        stored_refs = [{"path": ref.path, "digest": ref.digest} for ref in ordered]
        key = ledger_key(namespace, spec.name)
        if spec.accumulate or mapped_receipt is None:
            ledger[key] = stored_refs
        else:
            ledger[key] = {"refs": stored_refs, "receipt": mapped_receipt}
    return ledger


def merge_refs_by_path(
    existing: object,
    incoming: object,
    *,
    on_conflict: Literal["overwrite", "error"] = "overwrite",
) -> list[dict[str, str]]:
    """Union refs by path, then order by path.

    ``overwrite`` keeps the incoming digest. ``error`` rejects a path whose
    digests differ. The same digest is one ref.
    """
    if on_conflict not in {"overwrite", "error"}:
        raise ValueError(f"unknown artifact ref conflict policy: {on_conflict}")
    found: dict[str, dict[str, str]] = {}
    if existing is not None:
        for ref in _ref_list(existing):
            found[ref["path"]] = ref
    for ref in _ref_list(incoming):
        previous = found.get(ref["path"])
        if on_conflict == "error" and previous is not None and previous["digest"] != ref["digest"]:
            raise ValueError(f"conflicting artifact ref for {ref['path']}")
        found[ref["path"]] = ref
    return [found[path] for path in sorted(found)]


def ledger_refs(ledger: object, logical_name: str) -> list[dict[str, str]]:
    """Refs stored under ``logical_name``, one per path, in path order.

    Each ref is only ``path`` and ``digest``, including when the entry also
    stores the commit receipt.
    """
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


def ledger_entry_receipt(ledger: object, logical_name: str) -> dict[str, str] | None:
    """Receipt of the latest non-accumulating write of ``logical_name``, if one was stored."""
    if ledger is None:
        return None
    if not isinstance(ledger, Mapping):
        raise TypeError("artifact_ledger must be a mapping")
    value = ledger.get(logical_name)
    if not _is_receipt_entry(value):
        return None
    receipt = cast(Mapping[str, object], value).get("receipt")
    if receipt is None:
        return None
    return _receipt_dict(receipt)


def _overlay_ledger_refs(
    data: dict[str, object],
    ledger: object,
    bindings: Sequence[InputBinding],
) -> bool:
    wrote = False
    for binding in bindings:
        refs = ledger_refs(ledger, binding.ledger_key)
        if not refs:
            continue
        if binding.many:
            data[binding.field] = refs
            wrote = True
        elif len(refs) == 1:
            data[binding.field] = refs[0]
            wrote = True
    return wrote


def bind_input_slots(select: object, bindings: Sequence[InputBinding]) -> object:
    """Overlay ledger refs onto the selected attempt input before the attempt key."""

    if not callable(select):
        raise TypeError("select must be callable")

    @functools.wraps(select)
    def wrapped(state: object) -> object:
        selected = select(state)
        if not bindings or not isinstance(state, Mapping):
            return selected
        ledger = state.get("artifact_ledger")
        if isinstance(selected, BaseModel):
            data = selected.model_dump(mode="json")
            if not _overlay_ledger_refs(data, ledger, bindings):
                return selected
            return type(selected).model_validate(data)
        if isinstance(selected, Mapping):
            data = {str(key): value for key, value in selected.items()}
            _overlay_ledger_refs(data, ledger, bindings)
            return data
        return selected

    return wrapped


__all__ = [
    "AttemptLedgerState",
    "InputBinding",
    "NamedWrite",
    "bind_input_slots",
    "fill_artifact_ledger",
    "ledger_entry_receipt",
    "ledger_key",
    "ledger_refs",
    "matches_named_write",
    "merge_artifact_ledger",
    "merge_refs_by_path",
]
