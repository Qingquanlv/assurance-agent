from __future__ import annotations

import json


def validate_json_schema(instance: object, schema_bytes: bytes) -> None:
    try:
        schema = json.loads(schema_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("intent schema is not valid JSON") from error
    match_json_schema(instance, schema)


def match_json_schema(instance: object, schema: object) -> None:
    if schema is True:
        return
    if schema is False:
        raise ValueError("schema rejects all values")
    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(instance, dict) or isinstance(instance, bool):
            raise ValueError("expected a JSON object")
    elif expected_type == "array":
        if not isinstance(instance, list):
            raise ValueError("expected a JSON array")
        items = schema.get("items")
        if items is not None:
            for item in instance:
                match_json_schema(item, items)
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
    elif expected_type == "null":
        if instance is not None:
            raise ValueError("expected JSON null")
    elif expected_type is not None:
        raise ValueError(f"unsupported schema type: {expected_type!r}")
    if isinstance(instance, dict) and not isinstance(instance, bool):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise ValueError("schema required must be an array")
        for key in required:
            if key not in instance:
                raise ValueError(f"missing required property: {key}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("schema properties must be an object")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                match_json_schema(value, properties[key])
            elif additional is False:
                raise ValueError(f"unexpected property: {key}")
            elif isinstance(additional, dict | bool) and additional is not True:
                match_json_schema(value, additional)
    if "const" in schema and instance != schema["const"]:
        raise ValueError("value does not match schema const")
    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list) or instance not in allowed:
            raise ValueError("value is not in schema enum")
