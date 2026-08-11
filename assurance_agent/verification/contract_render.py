"""把 node 声明 outputs 绑定的 pydantic 模型渲染为 prompt 的 OUTPUT CONTRACT 子句。

契约的唯一事实源是 artifact registry 里的模型：顶层摘要读 ``model_fields``，strict
shape 读 validation JSON Schema，人话规则读
``model_config['json_schema_extra']['prompt_notes']``。未注册的 output 不产生子句——
渲染器绝不猜测契约。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, TypeAlias, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from assurance_agent.artifacts.registry import artifacts_under, match_artifact

_ROOT_PREFIXES = ("change:", "project:", "repo:")
JsonSchema: TypeAlias = dict[str, Any] | bool


def _strip_root(output: str) -> str | None:
    rel = output.strip()
    for prefix in _ROOT_PREFIXES:
        if rel.startswith(prefix):
            rel = rel[len(prefix) :]
            break
    if not rel:
        return None
    return rel


def _literal_values(field: FieldInfo) -> tuple[str, ...]:
    if get_origin(field.annotation) is not Literal:
        return ()
    return tuple(str(value) for value in get_args(field.annotation))


def _prompt_notes(model: type[BaseModel]) -> tuple[str, ...]:
    extra = model.model_config.get("json_schema_extra")
    if not isinstance(extra, dict):
        return ()
    notes = extra.get("prompt_notes")
    if not isinstance(notes, (list, tuple)):
        return ()
    return tuple(str(note) for note in notes)


def _schema_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class _ShapeRenderer:
    def __init__(self, schema: JsonSchema) -> None:
        self._schema = schema
        raw_defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}
        self._defs: dict[str, JsonSchema] = (
            {
                name: definition
                for name, definition in raw_defs.items()
                if isinstance(name, str) and isinstance(definition, (dict, bool))
            }
            if isinstance(raw_defs, dict)
            else {}
        )
        self._expanded_refs: dict[str, str] = {}
        self._next_definition = 1
        self.parts: list[str] = []

    def render(self) -> tuple[str, ...]:
        self._render_node(self._schema, "root")
        return tuple(self.parts)

    def _resolve_ref(self, ref: str) -> JsonSchema | None:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            return None
        key = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
        return self._defs.get(key)

    @staticmethod
    def _modifiers(*, optional: bool, nullable: bool) -> str:
        modifiers = []
        if optional:
            modifiers.append("optional")
        if nullable:
            modifiers.append("nullable")
        return (" " + " ".join(modifiers)) if modifiers else ""

    @staticmethod
    def _is_null_schema(schema: JsonSchema) -> bool:
        if isinstance(schema, bool):
            return False
        return schema.get("type") == "null" or schema.get("const", object()) is None

    @classmethod
    def _is_nullable(cls, schema: object) -> bool:
        if not isinstance(schema, dict):
            return False
        schema_type = schema.get("type")
        if isinstance(schema_type, list) and "null" in schema_type:
            return True
        for union_key in ("anyOf", "oneOf"):
            variants = schema.get(union_key)
            if isinstance(variants, list) and any(
                cls._is_null_schema(variant) for variant in variants if isinstance(variant, (dict, bool))
            ):
                return True
        return cls._is_null_schema(schema)

    @staticmethod
    def _plain_scalar_type(schema: object) -> str | None:
        if not isinstance(schema, dict):
            return None
        schema_type = schema.get("type")
        if not isinstance(schema_type, str) or schema_type not in {
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        }:
            return None
        if any(key in schema for key in ("$ref", "anyOf", "oneOf", "enum", "const")):
            return None
        return str(schema_type)

    def _render_node(
        self,
        schema: JsonSchema,
        path: str,
        *,
        optional: bool = False,
        nullable: bool = False,
        emit_plain_scalar: bool = True,
    ) -> None:
        modifiers = self._modifiers(optional=optional, nullable=nullable)
        if isinstance(schema, bool):
            meaning = "any value" if schema else "forbidden (no value)"
            self.parts.append(f"{path}{modifiers} {meaning}")
            return

        ref = schema.get("$ref")
        if isinstance(ref, str):
            target = self._resolve_ref(ref)
            if target is None:
                self.parts.append(
                    f"{path}{self._modifiers(optional=optional, nullable=nullable)} reference {ref}"
                )
                return
            name = ref.rsplit("/", maxsplit=1)[-1]
            expanded_path = self._expanded_refs.get(ref)
            if expanded_path is not None:
                self.parts.append(
                    f"{path}{self._modifiers(optional=optional, nullable=nullable)} -> {expanded_path}"
                )
                return
            if path == "root":
                expanded_path = "root"
            else:
                expanded_path = f"D{self._next_definition}"
                self._next_definition += 1
            self._expanded_refs[ref] = expanded_path
            if path != "root":
                self.parts.append(
                    f"{path}{self._modifiers(optional=optional, nullable=nullable)} -> {expanded_path}={name}"
                )
            self._render_node(
                target,
                expanded_path,
                optional=optional if path == "root" else False,
                nullable=nullable if path == "root" else False,
                emit_plain_scalar=emit_plain_scalar,
            )
            return

        for union_key in ("anyOf", "oneOf"):
            raw_variants = schema.get(union_key)
            if not isinstance(raw_variants, list):
                continue
            variants: list[JsonSchema] = [
                variant for variant in raw_variants if isinstance(variant, (dict, bool))
            ]
            non_null = [variant for variant in variants if not self._is_null_schema(variant)]
            union_nullable = nullable or len(non_null) != len(variants)
            if len(non_null) == 1:
                self._render_node(
                    non_null[0],
                    path,
                    optional=optional,
                    nullable=union_nullable,
                    emit_plain_scalar=emit_plain_scalar,
                )
                return
            self.parts.append(f"{path}{self._modifiers(optional=optional, nullable=union_nullable)} union")
            for index, variant in enumerate(non_null, start=1):
                self._render_node(
                    variant,
                    f"{path}<variant {index}>",
                    emit_plain_scalar=True,
                )
            return

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            non_null_types = [item for item in schema_type if item != "null"]
            nullable = nullable or len(non_null_types) != len(schema_type)
            if len(non_null_types) == 1:
                schema = {**schema, "type": non_null_types[0]}
                schema_type = non_null_types[0]
            elif non_null_types:
                self.parts.append(f"{path}{self._modifiers(optional=optional, nullable=nullable)} union")
                for index, variant_type in enumerate(non_null_types, start=1):
                    self._render_node(
                        {"type": variant_type},
                        f"{path}<variant {index}>",
                    )
                return

        modifiers = self._modifiers(optional=optional, nullable=nullable)
        if "const" in schema:
            self.parts.append(f"{path}{modifiers} const {_schema_value(schema['const'])}")
            return
        enum = schema.get("enum")
        if isinstance(enum, list):
            self.parts.append(f"{path}{modifiers} enum {_schema_value(enum)}")
            return
        if schema_type == "object" or isinstance(schema.get("properties"), dict):
            self._render_object(
                schema,
                path,
                optional=optional,
                nullable=nullable,
            )
            return
        if schema_type == "array":
            self._render_array(schema, path, modifiers=modifiers)
            return
        if isinstance(schema_type, str):
            if emit_plain_scalar:
                self.parts.append(f"{path}{modifiers} {schema_type}")
            return
        self.parts.append(f"{path}{modifiers} any value")

    def _render_object(
        self,
        schema: dict[str, Any],
        path: str,
        *,
        optional: bool,
        nullable: bool,
    ) -> None:
        raw_properties = schema.get("properties", {})
        properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
        additional = schema.get("additionalProperties")
        details: list[str] = []
        raw_required = schema.get("required", [])
        required = (
            {field for field in raw_required if isinstance(field, str)}
            if isinstance(raw_required, list)
            else set()
        )
        if properties:
            nullable_fields = {
                field for field, field_schema in properties.items() if self._is_nullable(field_schema)
            }
            marked_fields = ", ".join(
                field + ("*" if field in required else "") + ("?" if field in nullable_fields else "")
                for field in properties
            )
            marker_key = "*=required"
            if nullable_fields:
                marker_key += ", ?=nullable"
            details.append(f"allowed fields ({marker_key}): {marked_fields}")
        elif additional is False:
            details.append("allowed fields: (none)")
            details.append("required fields: (none)")
        if additional is False:
            details.append("no undeclared fields")
        elif isinstance(additional, (dict, bool)):
            map_label = "additional map values" if properties else "map values"
            if additional is True:
                details.append(f"{map_label}: any value")
            elif scalar_type := self._plain_scalar_type(additional):
                details.append(f"{map_label}: {scalar_type}")
            else:
                details.append(map_label)

        modifiers = self._modifiers(optional=optional, nullable=nullable)
        suffix = f" ({'; '.join(details)})" if details else ""
        self.parts.append(f"{path}{modifiers} object{suffix}")
        if isinstance(additional, dict) and self._plain_scalar_type(additional) is None:
            self._render_node(additional, f"{path}{{*}}")

        for name, property_schema in properties.items():
            if not isinstance(property_schema, (dict, bool)):
                continue
            property_path = name if path == "root" else f"{path}.{name}"
            self._render_node(
                property_schema,
                property_path,
                optional=name not in required,
                emit_plain_scalar=False,
            )

    def _render_array(self, schema: dict[str, Any], path: str, *, modifiers: str) -> None:
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            constraints = [
                f"{keyword}={_schema_value(schema[keyword])}"
                for keyword in ("minItems", "maxItems")
                if keyword in schema
            ]
            suffix = f" ({', '.join(constraints)})" if constraints else ""
            tuple_kind = (
                "fixed tuple"
                if schema.get("minItems") == schema.get("maxItems") == len(prefix_items)
                else "tuple"
            )
            self.parts.append(f"{path}{modifiers} {tuple_kind}{suffix}")
            for index, item_schema in enumerate(prefix_items):
                if isinstance(item_schema, (dict, bool)):
                    self._render_node(item_schema, f"{path}[{index}]")
            items = schema.get("items")
            if isinstance(items, (dict, bool)):
                self._render_node(items, f"{path}[additional]")
            return

        constraints = [
            f"{keyword}={_schema_value(schema[keyword])}"
            for keyword in ("minItems", "maxItems")
            if keyword in schema
        ]
        suffix = f" ({', '.join(constraints)})" if constraints else ""
        items = schema.get("items")
        if scalar_type := self._plain_scalar_type(items):
            self.parts.append(f"{path}{modifiers} array<{scalar_type}>{suffix}")
            return
        self.parts.append(f"{path}{modifiers} array{suffix}")
        if isinstance(items, (dict, bool)):
            self._render_node(items, f"{path}[]")


def _strict_shape(model: type[BaseModel]) -> tuple[str, ...]:
    schema = model.model_json_schema(mode="validation")
    return _ShapeRenderer(schema).render()


def _render_model(rel: str, model: type[BaseModel]) -> str:
    parts = [f"{rel} must be a {model.__name__}"]
    required = [name for name, field in model.model_fields.items() if field.is_required()]
    if required:
        parts.append("required fields: " + ", ".join(required))
    for name, field in model.model_fields.items():
        values = _literal_values(field)
        if len(values) == 1:
            parts.append(f"{name} '{values[0]}'")
        elif len(values) > 1:
            parts.append(f"{name} one of {', '.join(values)}")
    parts.extend(_prompt_notes(model))
    strict_shape = _strict_shape(model)
    if strict_shape:
        parts.append("strict shape: " + "; ".join(strict_shape))
    return "; ".join(parts) + "."


def render_output_contract(outputs: Sequence[str]) -> str:
    rendered: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        rel = _strip_root(output)
        if rel is None:
            continue
        if rel.endswith("/"):
            contracts = ((spec.pattern, spec.authoring_model or spec.model) for spec in artifacts_under(rel))
        else:
            spec = match_artifact(rel)
            contracts = () if spec is None else ((rel, spec.authoring_model or spec.model),)
        for label, model in contracts:
            if label in seen:
                continue
            seen.add(label)
            rendered.append(_render_model(label, model))
    if not rendered:
        return ""
    return " OUTPUT CONTRACT: " + " ".join(rendered)
