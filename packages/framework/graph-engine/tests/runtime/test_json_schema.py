from __future__ import annotations

import pytest

from graph_engine.runtime.json_schema import match_json_schema, validate_json_schema


_OVERFLOWING_PATTERN = "a{999999999999999999999999999999999999}"


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


@pytest.mark.parametrize(
    ("schema", "message"),
    (
        ({"$defs": None}, r"schema \$defs must be an object"),
        ({"$ref": None}, "schema reference must be text"),
        ({"$schema": None}, "schema dialect must be text"),
        ({"additionalProperties": None}, "schema additionalProperties must be a schema"),
        ({"anyOf": None}, "schema anyOf must be a non-empty array"),
        ({"enum": None}, "schema enum must be a non-empty array"),
        ({"items": None}, "schema items must be a schema"),
        ({"minItems": None}, "schema minItems must be a non-negative integer"),
        ({"minLength": None}, "schema minLength must be a non-negative integer"),
        ({"minimum": None}, "schema minimum must be a number"),
        ({"pattern": None}, "schema pattern must be text"),
        ({"properties": None}, "schema properties must be an object"),
        ({"required": None}, "schema required must be an array of unique strings"),
        ({"title": None}, "schema title must be text"),
        ({"type": None}, "unsupported schema type"),
        ({"type": ["string"]}, "unsupported schema type"),
        ({1: {}}, "schema keyword must be text"),
        ({"$defs": {1: {}}}, "schema definition names must be text"),
        ({"properties": {1: {}}}, "schema property names must be text"),
        ({"const": object()}, "schema const must be a JSON value"),
        ({"default": object()}, "schema default must be a JSON value"),
        ({"enum": [object()]}, "schema enum value must be a JSON value"),
        ({"minimum": float("inf")}, "schema minimum must be a finite number"),
    ),
)
def test_closed_runtime_schema_rejects_present_malformed_keyword_values(
    schema: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        match_json_schema("value", schema)


def test_closed_runtime_schema_accepts_null_json_values_for_const_and_default() -> None:
    match_json_schema(None, {"const": None, "default": None})


def test_closed_runtime_schema_rejects_direct_object_cycles() -> None:
    schema: dict[str, object] = {}
    schema["properties"] = {"loop": schema}

    with pytest.raises(ValueError, match="cyclic schema object"):
        match_json_schema({}, schema)


def test_closed_runtime_schema_accepts_arbitrary_precision_integer_minimum() -> None:
    value = 10**1000

    match_json_schema(value, {"type": "integer", "minimum": value})


def test_closed_runtime_match_normalizes_overflowing_pattern() -> None:
    with pytest.raises(ValueError, match="schema pattern must be a valid regular expression"):
        match_json_schema("a", {"type": "string", "pattern": _OVERFLOWING_PATTERN})


def test_closed_runtime_validate_normalizes_overflowing_pattern() -> None:
    schema = b'{"type":"string","pattern":"a{999999999999999999999999999999999999}"}'

    with pytest.raises(ValueError, match="schema pattern must be a valid regular expression"):
        validate_json_schema("a", schema)
