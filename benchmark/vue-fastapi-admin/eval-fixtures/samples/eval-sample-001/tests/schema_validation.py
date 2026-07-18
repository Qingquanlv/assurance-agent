"""Response body schema validation for happy-path API tests."""

from __future__ import annotations

from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore[assignment]

_LOCAL_SCHEMAS: dict[str, dict[str, Any]] = {}

_SUCCESS_WRAPPER: dict[str, Any] = {
    "type": "object",
    "required": ["code", "msg"],
    "properties": {
        "code": {"type": "integer"},
        "msg": {"type": ["string", "null"]},
        "data": {},
    },
    "additionalProperties": True,
}


def assert_matches_schema(body: dict[str, Any], key: str) -> None:
    """Validate body against a registered local schema key (METHOD /api/path)."""
    if key not in _LOCAL_SCHEMAS:
        registered = ", ".join(sorted(_LOCAL_SCHEMAS)) or "(none)"
        raise LookupError(
            f"Schema key {key!r} is not registered in tests.schema_validation._LOCAL_SCHEMAS. "
            f"Registered: {registered}"
        )
    if jsonschema is None:
        return
    schema = _LOCAL_SCHEMAS[key]
    try:
        jsonschema.validate(instance=body, schema=schema)
    except jsonschema.ValidationError as exc:
        raise AssertionError(f"Schema validation failed for {key}: {exc.message}") from exc
