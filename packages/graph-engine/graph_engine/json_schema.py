from __future__ import annotations

import json
import math
import re
from typing import NoReturn


_SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "$schema",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "enum",
        "items",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)
_SUPPORTED_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_LOCAL_DEFINITION_PREFIX = "#/$defs/"


def validate_json_schema(instance: object, schema_bytes: bytes) -> None:
    try:
        schema = json.loads(
            schema_bytes.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("intent schema is not valid JSON") from error
    match_json_schema(instance, schema)


def match_json_schema(instance: object, schema: object) -> None:
    assert_closed_json_schema(schema)
    _match_closed_json_schema(instance, schema, schema, ())


def assert_closed_json_schema(schema: object) -> None:
    _assert_closed_json_schema(schema, schema, (), frozenset())


def _assert_closed_json_schema(
    schema: object,
    root: object,
    reference_stack: tuple[str, ...],
    object_stack: frozenset[int],
) -> None:
    if schema is True or schema is False:
        return
    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    schema_identity = id(schema)
    if schema_identity in object_stack:
        raise ValueError("cyclic schema object")
    nested_object_stack = object_stack | {schema_identity}
    if any(not isinstance(key, str) for key in schema):
        raise ValueError("schema keyword must be text")
    unknown = tuple(sorted(key for key in schema if key not in _SUPPORTED_KEYWORDS))
    if unknown:
        raise ValueError(f"unsupported schema keyword: {unknown[0]}")

    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise ValueError("schema dialect must be text")
    if "title" in schema and not isinstance(schema["title"], str):
        raise ValueError("schema title must be text")

    if "type" in schema:
        expected_type = schema["type"]
        if not isinstance(expected_type, str) or expected_type not in _SUPPORTED_TYPES:
            raise ValueError(f"unsupported schema type: {expected_type!r}")

    if "$defs" in schema:
        definitions = schema["$defs"]
        if not isinstance(definitions, dict):
            raise ValueError("schema $defs must be an object")
        if any(not isinstance(name, str) for name in definitions):
            raise ValueError("schema definition names must be text")
        for nested in definitions.values():
            _assert_closed_json_schema(
                nested,
                root,
                reference_stack,
                nested_object_stack,
            )

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise ValueError("schema properties must be an object")
        if any(not isinstance(name, str) for name in properties):
            raise ValueError("schema property names must be text")
        for nested in properties.values():
            _assert_closed_json_schema(
                nested,
                root,
                reference_stack,
                nested_object_stack,
            )

    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or len(required) != len(set(required))
        ):
            raise ValueError("schema required must be an array of unique strings")

    for keyword in ("items", "additionalProperties"):
        if keyword in schema:
            nested = schema[keyword]
            if not isinstance(nested, dict | bool):
                raise ValueError(f"schema {keyword} must be a schema")
            _assert_closed_json_schema(
                nested,
                root,
                reference_stack,
                nested_object_stack,
            )

    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise ValueError("schema anyOf must be a non-empty array")
        for nested in alternatives:
            _assert_closed_json_schema(
                nested,
                root,
                reference_stack,
                nested_object_stack,
            )

    _non_negative_integer_keyword(schema, "minItems")
    _non_negative_integer_keyword(schema, "minLength")
    if "minimum" in schema:
        minimum = schema["minimum"]
        if isinstance(minimum, bool) or not isinstance(minimum, int | float):
            raise ValueError("schema minimum must be a number")
        if isinstance(minimum, float) and not math.isfinite(minimum):
            raise ValueError("schema minimum must be a finite number")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise ValueError("schema pattern must be text")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError("schema pattern must be a valid regular expression") from error

    if "const" in schema:
        _assert_json_value(schema["const"], "schema const")
    if "default" in schema:
        _assert_json_value(schema["default"], "schema default")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise ValueError("schema enum must be a non-empty array")
        for value in enum:
            _assert_json_value(value, "schema enum value")

    if "$ref" in schema:
        reference = schema["$ref"]
        target, normalized = _resolve_local_reference(root, reference)
        if normalized in reference_stack or (isinstance(target, dict) and id(target) in nested_object_stack):
            raise ValueError(f"cyclic schema reference: {normalized}")
        _assert_closed_json_schema(
            target,
            root,
            (*reference_stack, normalized),
            nested_object_stack,
        )


def _match_closed_json_schema(
    instance: object,
    schema: object,
    root: object,
    reference_stack: tuple[str, ...],
) -> None:
    if schema is True:
        return
    if schema is False:
        raise ValueError("schema rejects all values")
    if not isinstance(schema, dict):  # pragma: no cover - assertion closes the shape first.
        raise ValueError("schema must be a JSON object")

    if "$ref" in schema:
        reference = schema["$ref"]
        target, normalized = _resolve_local_reference(root, reference)
        if normalized in reference_stack:  # pragma: no cover - assertion catches cycles first.
            raise ValueError(f"cyclic schema reference: {normalized}")
        _match_closed_json_schema(instance, target, root, (*reference_stack, normalized))

    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list):  # pragma: no cover - assertion closes the shape.
            raise ValueError("schema anyOf must be a non-empty array")
        for nested in alternatives:
            try:
                _match_closed_json_schema(instance, nested, root, reference_stack)
            except ValueError:
                continue
            break
        else:
            raise ValueError("value does not match anyOf")

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(instance, dict) or isinstance(instance, bool):
            raise ValueError("expected a JSON object")
    elif expected_type == "array":
        if not isinstance(instance, list):
            raise ValueError("expected a JSON array")
    elif expected_type == "string":
        if not isinstance(instance, str):
            raise ValueError("expected a JSON string")
    elif expected_type == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            raise ValueError("expected a JSON integer")
    elif expected_type == "number":
        if isinstance(instance, bool) or not isinstance(instance, int | float):
            raise ValueError("expected a JSON number")
    elif expected_type == "boolean":
        if not isinstance(instance, bool):
            raise ValueError("expected a JSON boolean")
    elif expected_type == "null" and instance is not None:
        raise ValueError("expected JSON null")

    if isinstance(instance, dict) and not isinstance(instance, bool):
        required = schema["required"] if "required" in schema else []
        if not isinstance(required, list):  # pragma: no cover - assertion closes the shape.
            raise ValueError("schema required must be an array of unique strings")
        for key in required:
            if key not in instance:
                raise ValueError(f"missing required property: {key}")
        properties = schema["properties"] if "properties" in schema else {}
        if not isinstance(properties, dict):  # pragma: no cover - assertion closes the shape.
            raise ValueError("schema properties must be an object")
        additional = schema["additionalProperties"] if "additionalProperties" in schema else True
        for key, value in instance.items():
            if key in properties:
                _match_closed_json_schema(value, properties[key], root, reference_stack)
            elif additional is False:
                raise ValueError(f"unexpected property: {key}")
            elif isinstance(additional, dict):
                _match_closed_json_schema(value, additional, root, reference_stack)

    if isinstance(instance, list):
        minimum_items = schema["minItems"] if "minItems" in schema else None
        if isinstance(minimum_items, int) and len(instance) < minimum_items:
            raise ValueError("array has fewer than minItems")
        if "items" in schema:
            items = schema["items"]
            for item in instance:
                _match_closed_json_schema(item, items, root, reference_stack)

    if isinstance(instance, str):
        minimum_length = schema["minLength"] if "minLength" in schema else None
        if isinstance(minimum_length, int) and len(instance) < minimum_length:
            raise ValueError("string is shorter than minLength")
        pattern = schema["pattern"] if "pattern" in schema else None
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            raise ValueError("string does not match pattern")

    if isinstance(instance, int | float) and not isinstance(instance, bool):
        minimum = schema["minimum"] if "minimum" in schema else None
        if isinstance(minimum, int | float) and not isinstance(minimum, bool) and instance < minimum:
            raise ValueError("number is less than minimum")

    if "const" in schema and instance != schema["const"]:
        raise ValueError("value does not match schema const")
    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list):  # pragma: no cover - assertion closes the shape.
            raise ValueError("schema enum must be a non-empty array")
        if instance not in allowed:
            raise ValueError("value is not in schema enum")


def _resolve_local_reference(root: object, reference: object) -> tuple[object, str]:
    if not isinstance(reference, str):
        raise ValueError("schema reference must be text")
    if not reference.startswith(_LOCAL_DEFINITION_PREFIX):
        raise ValueError(f"unsupported schema reference: {reference!r}")
    encoded_name = reference.removeprefix(_LOCAL_DEFINITION_PREFIX)
    if not encoded_name or "/" in encoded_name:
        raise ValueError(f"unsupported schema reference: {reference!r}")
    name = _decode_json_pointer_token(encoded_name)
    if not isinstance(root, dict):
        raise ValueError(f"unresolved schema reference: {reference}")
    definitions = root.get("$defs")
    if not isinstance(definitions, dict) or name not in definitions:
        raise ValueError(f"unresolved schema reference: {reference}")
    return definitions[name], f"{_LOCAL_DEFINITION_PREFIX}{encoded_name}"


def _decode_json_pointer_token(value: str) -> str:
    index = 0
    decoded: list[str] = []
    while index < len(value):
        char = value[index]
        if char != "~":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError("schema reference contains an invalid JSON Pointer escape")
        decoded.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _non_negative_integer_keyword(schema: dict[str, object], keyword: str) -> None:
    if keyword not in schema:
        return
    value = schema[keyword]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"schema {keyword} must be a non-negative integer")


def _assert_json_value(
    value: object,
    label: str,
    object_stack: frozenset[int] = frozenset(),
) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError(f"{label} must be a JSON value")
    if isinstance(value, list):
        identity = id(value)
        if identity in object_stack:
            raise ValueError(f"{label} must be a JSON value")
        for item in value:
            _assert_json_value(item, label, object_stack | {identity})
        return
    if isinstance(value, dict):
        identity = id(value)
        if identity in object_stack or any(not isinstance(key, str) for key in value):
            raise ValueError(f"{label} must be a JSON value")
        for item in value.values():
            _assert_json_value(item, label, object_stack | {identity})
        return
    raise ValueError(f"{label} must be a JSON value")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


__all__ = ["assert_closed_json_schema", "match_json_schema", "validate_json_schema"]
