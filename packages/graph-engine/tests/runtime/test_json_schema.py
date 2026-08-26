from __future__ import annotations

import pytest

from graph_engine.runtime.json_schema import match_json_schema


@pytest.mark.parametrize(
    ("instance", "schema"),
    (
        (
            "valid",
            {
                "$defs": {"nonempty": {"type": "string", "minLength": 1}},
                "$ref": "#/$defs/nonempty",
                "title": "Known annotation",
                "default": "fallback",
            },
        ),
        ("abc123", {"type": "string", "pattern": "^[a-z]+[0-9]+$"}),
        (1, {"type": "integer", "minimum": 1}),
        (["one"], {"type": "array", "items": {"type": "string"}, "minItems": 1}),
        (None, {"anyOf": [{"type": "string"}, {"type": "null"}]}),
    ),
)
def test_closed_runtime_schema_matches_installed_assurance_effect_keywords(
    instance: object,
    schema: object,
) -> None:
    match_json_schema(instance, schema)


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    (
        ("", {"type": "string", "minLength": 1}, "string is shorter than minLength"),
        ("abc", {"type": "string", "pattern": "^[0-9]+$"}, "string does not match pattern"),
        (0, {"type": "integer", "minimum": 1}, "number is less than minimum"),
        ([], {"type": "array", "minItems": 1}, "array has fewer than minItems"),
        (1, {"anyOf": [{"type": "string"}, {"type": "null"}]}, "anyOf"),
    ),
)
def test_closed_runtime_schema_enforces_installed_assurance_effect_keywords(
    instance: object,
    schema: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        match_json_schema(instance, schema)


@pytest.mark.parametrize(
    ("schema", "message"),
    (
        ({"$ref": "https://example.test/schema"}, "unsupported schema reference"),
        ({"$ref": "#/$defs/missing", "$defs": {}}, "unresolved schema reference"),
        (
            {"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}},
            "cyclic schema reference",
        ),
    ),
)
def test_closed_runtime_schema_rejects_unsafe_references(schema: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        match_json_schema("value", schema)


def test_closed_runtime_schema_rejects_unknown_keywords() -> None:
    with pytest.raises(ValueError, match="unsupported schema keyword: format"):
        match_json_schema("a@example.test", {"type": "string", "format": "email"})
