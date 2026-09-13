"""Local response-schema registry used by generated API tests."""

from __future__ import annotations

from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover - optional for generated tests
    jsonschema = None  # type: ignore[assignment]

_LOCAL_SCHEMAS: dict[str, dict[str, Any]] = {}


def assert_matches_schema(body: dict[str, Any], key: str) -> None:
    if key not in _LOCAL_SCHEMAS:
        raise LookupError(f"unregistered local schema: {key}")
    if jsonschema is not None:
        jsonschema.validate(instance=body, schema=_LOCAL_SCHEMAS[key])
