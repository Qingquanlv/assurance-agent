from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, TypeVar, cast

from pydantic import BaseModel

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "oneOf",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minProperties",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "pattern",
        "title",
        "description",
        "default",
        "format",
        "discriminator",
        "prompt_notes",
    }
)
_PRIMITIVE_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})
_BEARER_TOKEN = r"\S{8,}"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*[:=](?!=)[^\r\n]+"),
    re.compile(rf"(?i)\bbearer\s+{_BEARER_TOKEN}"),
    re.compile(r"(?i)cookie\s*[=:]\s*[^;\s]+"),
    re.compile(r"sk-[A-Za-z0-9-]+"),
    re.compile(r"(?i)api[_-]?key\s*[=:](?!=)\s*\S+"),
)
MAX_DIAGNOSTIC_COUNT = 16
MAX_DIAGNOSTIC_LENGTH = 240


def freeze_json(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        items = cast(Mapping[str, object], value)
        return MappingProxyType({key: freeze_json(item) for key, item in sorted(items.items())})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(item) for item in value]
    return value


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    thawed = thaw_json(value)
    _validate_json_value(thawed)
    return json.dumps(
        thawed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _local_definition(
    reference: object,
    *,
    root_schema: Mapping[str, object],
    path: str,
) -> Mapping[str, object]:
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise ValueError(f"{path}: unsupported schema reference")
    name = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
    definitions = root_schema.get("$defs")
    if not isinstance(definitions, dict) or name not in definitions:
        raise ValueError(f"{path}: unresolved schema reference {reference!r}")
    definition = definitions[name]
    if not isinstance(definition, dict):
        raise ValueError(f"{path}: referenced schema must be an object")
    return definition


def _validate_schema_document(
    schema: object,
    *,
    path: str,
    root_schema: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be an object")
    if root_schema is None:
        root_schema = schema
    unknown = set(schema) - _ALLOWED_SCHEMA_KEYS
    if unknown:
        raise ValueError(f"{path}: unsupported schema keys {sorted(unknown)}")

    definitions = schema.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, dict) or any(not isinstance(name, str) for name in definitions):
            raise ValueError(f"{path}.$defs: definitions must be an object")
        for name, subschema in definitions.items():
            _validate_schema_document(
                subschema,
                path=f"{path}.$defs.{name}",
                root_schema=root_schema,
            )

    if "$ref" in schema:
        _local_definition(schema["$ref"], root_schema=root_schema, path=f"{path}.$ref")

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            raise ValueError(f"{path}.{keyword}: must be a non-empty list")
        for index, subschema in enumerate(branches):
            _validate_schema_document(
                subschema,
                path=f"{path}.{keyword}[{index}]",
                root_schema=root_schema,
            )

    prompt_notes = schema.get("prompt_notes")
    if prompt_notes is not None and (
        not isinstance(prompt_notes, list) or any(not isinstance(note, str) for note in prompt_notes)
    ):
        raise ValueError(f"{path}.prompt_notes: must be a list of strings")

    discriminator = schema.get("discriminator")
    if discriminator is not None and not isinstance(discriminator, dict):
        raise ValueError(f"{path}.discriminator: must be an object")

    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError(f"{path}.enum: must be a list")

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ValueError(f"{path}.pattern: must be a string")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{path}.pattern: invalid regular expression") from error

    type_name = schema.get("type")
    if type_name == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict):
            raise ValueError(f"{path}: properties must be an object")
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ValueError(f"{path}: required must be a list of strings")
        missing_required = [item for item in required if item not in properties]
        if missing_required:
            raise ValueError(f"{path}: required properties are undeclared {missing_required}")
        for name, subschema in properties.items():
            if not isinstance(name, str):
                raise ValueError(f"{path}: property names must be strings")
            _validate_schema_document(
                subschema,
                path=f"{path}.{name}",
                root_schema=root_schema,
            )
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool | dict):
            raise ValueError(f"{path}: additionalProperties must be boolean or a schema")
        if isinstance(additional, dict):
            _validate_schema_document(
                additional,
                path=f"{path}.*",
                root_schema=root_schema,
            )
        min_properties = schema.get("minProperties")
        if min_properties is not None and (
            not isinstance(min_properties, int) or isinstance(min_properties, bool) or min_properties < 0
        ):
            raise ValueError(f"{path}: minProperties must be a non-negative integer")
        if "items" in schema:
            raise ValueError(f"{path}: object schemas do not accept items")
    elif type_name == "array":
        if any(key in schema for key in ("additionalProperties", "properties", "required", "minProperties")):
            raise ValueError(f"{path}: array schemas do not accept object keywords")
        if "items" not in schema:
            raise ValueError(f"{path}: array schemas require items")
        _validate_schema_document(
            schema["items"],
            path=f"{path}[]",
            root_schema=root_schema,
        )
    elif type_name in _PRIMITIVE_TYPES:
        if any(
            key in schema
            for key in ("properties", "additionalProperties", "items", "required", "minProperties")
        ):
            raise ValueError(f"{path}: primitive schemas do not accept object or array keywords")
    elif type_name is None and (
        not schema
        or "$ref" in schema
        or "anyOf" in schema
        or "oneOf" in schema
        or set(schema)
        <= {
            "$defs",
            "const",
            "default",
            "description",
            "discriminator",
            "enum",
            "prompt_notes",
            "title",
        }
    ):
        pass
    else:
        raise ValueError(f"{path}: unsupported schema type {type_name!r}")
    return schema


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise ValueError(f"value is not JSON-compatible: {type(value).__name__}")


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


def _validate_value(
    value: object,
    schema: Mapping[str, object],
    *,
    path: str,
    root_schema: Mapping[str, object],
) -> None:
    if "$ref" in schema:
        referenced = _local_definition(schema["$ref"], root_schema=root_schema, path=path)
        _validate_value(value, referenced, path=path, root_schema=root_schema)

    for keyword, exact in (("anyOf", False), ("oneOf", True)):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        matches = 0
        for branch in branches:
            try:
                _validate_value(
                    value,
                    cast(Mapping[str, object], branch),
                    path=path,
                    root_schema=root_schema,
                )
            except ValueError:
                continue
            matches += 1
        if matches == 0 or (exact and matches != 1):
            raise ValueError(f"{path}: value must match {keyword}")

    expected = schema.get("type")
    if expected is None:
        if "const" in schema and not _json_equal(value, schema["const"]):
            raise ValueError(f"{path}: value must equal const")
        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list) or not any(_json_equal(value, option) for option in options):
                raise ValueError(f"{path}: value must be one of enum")
        return
    actual = _json_type(value)
    if expected == "number":
        if actual not in {"integer", "number"}:
            raise ValueError(f"{path}: expected number")
    elif actual != expected:
        raise ValueError(f"{path}: expected {expected}")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise ValueError(f"{path}: value must equal const")
    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list) or not any(_json_equal(value, option) for option in options):
            raise ValueError(f"{path}: value must be one of enum")
    if expected == "object":
        assert isinstance(value, dict)
        min_properties = schema.get("minProperties")
        if isinstance(min_properties, int) and len(value) < min_properties:
            raise ValueError(f"{path}: below minProperties")
        properties = cast(dict[str, Mapping[str, object]], schema.get("properties", {}))
        required = cast(list[str], schema.get("required", []))
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path}: missing required properties {missing}")
        extra = sorted(set(value) - set(properties))
        additional = schema.get("additionalProperties", True)
        if extra and additional is False:
            raise ValueError(f"{path}: additional properties {extra}")
        for name, item in value.items():
            if name in properties:
                _validate_value(
                    item,
                    properties[name],
                    path=f"{path}.{name}",
                    root_schema=root_schema,
                )
            elif isinstance(additional, Mapping):
                _validate_value(
                    item,
                    additional,
                    path=f"{path}.{name}",
                    root_schema=root_schema,
                )
    elif expected == "array":
        assert isinstance(value, list)
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ValueError(f"{path}: below minItems")
        if isinstance(max_items, int) and len(value) > max_items:
            raise ValueError(f"{path}: above maxItems")
        item_schema = cast(Mapping[str, object], schema["items"])
        for index, item in enumerate(value):
            _validate_value(
                item,
                item_schema,
                path=f"{path}[{index}]",
                root_schema=root_schema,
            )
    elif expected == "string":
        assert isinstance(value, str)
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ValueError(f"{path}: below minLength")
        if isinstance(max_length, int) and len(value) > max_length:
            raise ValueError(f"{path}: above maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ValueError(f"{path}: string does not match pattern")
    elif expected in {"number", "integer"}:
        assert isinstance(value, int | float) and not isinstance(value, bool)
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_minimum = schema.get("exclusiveMinimum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(minimum, int | float) and value < minimum:
            raise ValueError(f"{path}: below minimum")
        if isinstance(maximum, int | float) and value > maximum:
            raise ValueError(f"{path}: above maximum")
        if isinstance(exclusive_minimum, int | float) and value <= exclusive_minimum:
            raise ValueError(f"{path}: not above exclusiveMinimum")
        if isinstance(exclusive_maximum, int | float) and value >= exclusive_maximum:
            raise ValueError(f"{path}: not below exclusiveMaximum")


def validate_result_schema(
    schema: object,
    *,
    schema_digest: str,
) -> Mapping[str, object]:
    thawed_schema = thaw_json(schema)
    if canonical_digest(thawed_schema) != schema_digest:
        raise ValueError("schema digest is not canonical")
    return _validate_schema_document(thawed_schema, path="$")


def validate_structured_result(
    value: object,
    *,
    schema: object,
    schema_digest: str,
) -> object:
    checked_schema = validate_result_schema(schema, schema_digest=schema_digest)
    thawed_value = thaw_json(value)
    _validate_value(thawed_value, checked_schema, path="$", root_schema=checked_schema)
    return thawed_value


def _contains_credential(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _credential_texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        texts: list[str] = []
        for key, item in value.items():
            if isinstance(key, str):
                texts.append(key)
                if key.casefold() == "authorization":
                    texts.append("Authorization: [redacted]")
            texts.extend(_credential_texts(item))
        return tuple(texts)
    if isinstance(value, list | tuple):
        texts: list[str] = []
        for item in value:
            texts.extend(_credential_texts(item))
        return tuple(texts)
    return ()


def reject_credentials_in_digest_input(value: object) -> None:
    for text in _credential_texts(thaw_json(value)):
        if _contains_credential(text):
            raise ValueError("credentials must not enter result or digest input")


def _redact(message: str) -> str:
    text = message
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def bound_redacted_diagnostics(messages: Sequence[str]) -> tuple[str, ...]:
    if len(messages) > MAX_DIAGNOSTIC_COUNT:
        raise ValueError("diagnostics exceed the bound")
    redacted: list[str] = []
    for message in messages:
        if not isinstance(message, str):
            raise ValueError("diagnostics must be text")
        text = _redact(message)
        if len(text) > MAX_DIAGNOSTIC_LENGTH:
            text = text[:MAX_DIAGNOSTIC_LENGTH]
        redacted.append(text)
    return tuple(redacted)


_FIXTURE_RESULT_SCHEMAS: tuple[object, ...] = (
    {
        "additionalProperties": False,
        "properties": {"ok": {"const": True, "type": "boolean"}},
        "required": ["ok"],
        "type": "object",
    },
    {
        "additionalProperties": False,
        "properties": {
            "ok": {"const": True, "type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["ok", "note"],
        "type": "object",
    },
    {
        "additionalProperties": False,
        "properties": {
            "artifact": {"type": "string"},
            "status": {"const": "ok", "type": "string"},
        },
        "required": ["artifact", "status"],
        "type": "object",
    },
)


def validate_result_schema_document(schema: object) -> Mapping[str, object]:
    return _validate_schema_document(schema, path="schema")


def resolve_result_schema(schema_digest: str, schema_document: object | None = None) -> object:
    if schema_document is not None:
        thawed = thaw_json(schema_document)
        if canonical_digest(thawed) != schema_digest:
            raise ValueError("result schema digest is not canonical")
        validate_result_schema_document(thawed)
        return thawed
    for schema in _FIXTURE_RESULT_SCHEMAS:
        if canonical_digest(schema) == schema_digest:
            return schema
    raise ValueError("result schema is missing")


def result_schema_from_model(model: type[BaseModel]) -> object:
    schema = thaw_json(freeze_json(model.model_json_schema()))
    validate_result_schema_document(schema)
    return schema


def validate_local_agent_result(
    payload: object,
    *,
    result_model: type[_ModelT],
    context: Mapping[str, object] | None = None,
) -> tuple[object, str, _ModelT]:
    schema = result_schema_from_model(result_model)
    exact = validate_structured_result(
        payload,
        schema=schema,
        schema_digest=canonical_digest(schema),
    )
    reject_credentials_in_digest_input(exact)
    digest = canonical_digest(exact)
    validated = result_model.model_validate(
        exact,
        context=None if context is None else dict(context),
    )
    return exact, digest, validated
