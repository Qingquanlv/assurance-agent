"""One raw-suite-to-layer normalization boundary for eval selection."""

from __future__ import annotations

from typing import Final

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_agent.exceptions import AaError

# Private sentinel: key omitted at the suite boundary (runner tests may import).
MISSING: Final[object] = object()

SELECTION_NORMALIZER_VERSION: Final[str] = "selection_normalizer/v1"
_DEFAULT_LAYERS: Final[tuple[LayerName, ...]] = ("api", "e2e")
_KNOWN: Final[frozenset[str]] = frozenset(LAYER_NAMES)


class SelectionError(AaError):
    """Invalid eval suite layer selection."""


def normalize_selected_layers(
    *,
    test_type: object = MISSING,
    test_types: object = MISSING,
) -> tuple[LayerName, ...]:
    """Resolve one raw suite boundary value into canonical layer order.

    Key presence (not truthiness) decides whether the default applies. An
    explicitly empty, duplicate, unknown, or dual-key value fails closed.
    """
    type_present = test_type is not MISSING
    types_present = test_types is not MISSING
    if type_present and types_present:
        raise SelectionError("suite executor must not set both test_type and test_types")
    if not type_present and not types_present:
        return _DEFAULT_LAYERS

    raw = test_type if type_present else test_types
    tokens = _coerce_tokens(raw)
    if not tokens:
        raise SelectionError("selected layers must be a non-empty unique subset")

    seen: set[str] = set()
    unknown: list[str] = []
    duplicates: list[str] = []
    for token in tokens:
        if token not in _KNOWN:
            unknown.append(token)
            continue
        if token in seen:
            duplicates.append(token)
            continue
        seen.add(token)

    if unknown:
        raise SelectionError(f"unknown selected layer(s): {', '.join(unknown)}")
    if duplicates:
        raise SelectionError(f"duplicate selected layer(s): {', '.join(duplicates)}")

    return tuple(layer for layer in LAYER_NAMES if layer in seen)


def _coerce_tokens(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        tokens: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise SelectionError(f"selected layer must be a string, got {type(item).__name__}")
            stripped = item.strip()
            if stripped:
                tokens.append(stripped)
            else:
                # Explicit empty element is an empty selection signal.
                return []
        return tokens
    if raw is None:
        return []
    raise SelectionError(f"selected layers must be a string or list, got {type(raw).__name__}")


__all__ = [
    "MISSING",
    "SELECTION_NORMALIZER_VERSION",
    "SelectionError",
    "normalize_selected_layers",
]
