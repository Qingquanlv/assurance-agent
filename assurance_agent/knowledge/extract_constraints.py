"""Mechanical ORM AST → DataKnowledgeProposal cold start (spec §5-A2).

Extracts only ``unique=True``, ``max_length=N``, and ``null=False`` from Tortoise-style
field assignments. Never writes L1 — callers must promote via the human ``--yes`` path.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from assurance_agent.artifacts.models.data_knowledge import (
    DataKnowledge,
    DataKnowledgeProposal,
    EntityLeaf,
)

PROPOSAL_SCHEMA_VERSION = "1"

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _flatten_constraint_key_names(constraints: Mapping[str, Any]) -> Iterable[str]:
    """Expand nested field shapes to A2 flattened keys; pass through already-flat keys.

    Nested ``{"name": {"max_length": 20, "unique": true}}`` yields
    ``name_has_max_length`` / ``name_unique`` — never the opaque ``name`` key.
    """
    for key, value in constraints.items():
        if isinstance(value, Mapping):
            for attr in value:
                if attr == "max_length":
                    yield f"{key}_has_max_length"
                elif attr == "unique":
                    yield f"{key}_unique"
                else:
                    yield f"{key}_{attr}"
        else:
            yield str(key)


def constraint_known_keys(
    knowledge: DataKnowledge | DataKnowledgeProposal | Mapping[str, Any],
) -> frozenset[str]:
    """Closed ``entities.<entity>.constraints.<key>`` set for ``property_scan``."""
    if isinstance(knowledge, (DataKnowledge, DataKnowledgeProposal)):
        entities = knowledge.entities
        items: Iterable[tuple[str, EntityLeaf | Mapping[str, Any]]] = entities.items()
        keys: set[str] = set()
        for entity_name, leaf in items:
            constraints = leaf.constraints if isinstance(leaf, EntityLeaf) else leaf.get("constraints")
            if not isinstance(constraints, Mapping):
                continue
            for constraint_key in _flatten_constraint_key_names(constraints):
                keys.add(f"entities.{entity_name}.constraints.{constraint_key}")
        return frozenset(keys)

    entities_raw = knowledge.get("entities")
    if not isinstance(entities_raw, Mapping):
        return frozenset()
    keys = set()
    for entity_name, leaf in entities_raw.items():
        if not isinstance(leaf, Mapping):
            continue
        constraints = leaf.get("constraints")
        if not isinstance(constraints, Mapping):
            continue
        for constraint_key in _flatten_constraint_key_names(constraints):
            keys.add(f"entities.{entity_name}.constraints.{constraint_key}")
    return frozenset(keys)


def auth_matrix_known_keys(
    knowledge: DataKnowledge | DataKnowledgeProposal | Mapping[str, Any],
) -> frozenset[str]:
    """Closed ``auth_matrix.<cell_id>`` set for Task 5/6 MRC denominators."""
    if isinstance(knowledge, (DataKnowledge, DataKnowledgeProposal)):
        return frozenset(f"auth_matrix.{cell_id}" for cell_id in knowledge.auth_matrix)
    matrix = knowledge.get("auth_matrix")
    if not isinstance(matrix, Mapping):
        return frozenset()
    return frozenset(f"auth_matrix.{cell_id}" for cell_id in matrix)


def extract_entity_constraints_from_source(
    source: str,
    *,
    based_on_l1_version: int | None = None,
    filename: str = "<string>",
) -> DataKnowledgeProposal:
    """Parse one Python source string and return a proposal (never mutates L1)."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return _empty_proposal(based_on_l1_version=based_on_l1_version)

    entities: dict[str, EntityLeaf] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        extracted = _extract_entity_from_class(node)
        if extracted is None:
            continue
        entity_name, leaf = extracted
        entities[entity_name] = leaf

    return _proposal_from_entities(entities, based_on_l1_version=based_on_l1_version)


def extract_entity_constraints(
    paths: Iterable[Path],
    *,
    based_on_l1_version: int | None = None,
) -> DataKnowledgeProposal:
    """Scan ORM model files and fold entities into one proposal."""
    entities: dict[str, EntityLeaf] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        partial = extract_entity_constraints_from_source(
            text,
            based_on_l1_version=based_on_l1_version,
            filename=str(path),
        )
        entities.update(partial.entities)
    return _proposal_from_entities(entities, based_on_l1_version=based_on_l1_version)


def _empty_proposal(*, based_on_l1_version: int | None) -> DataKnowledgeProposal:
    return _proposal_from_entities({}, based_on_l1_version=based_on_l1_version)


def _proposal_from_entities(
    entities: dict[str, EntityLeaf],
    *,
    based_on_l1_version: int | None,
) -> DataKnowledgeProposal:
    mode = "delta" if based_on_l1_version is not None else "bootstrap"
    needs_review = [f"entities.{name}" for name in sorted(entities)]
    checklist = [
        "confirm ORM-extracted constraints against API validation behaviour",
        "promote via `aa knowledge promote --yes` after human review",
    ]
    return DataKnowledgeProposal(
        schema_version=PROPOSAL_SCHEMA_VERSION,
        based_on_l1_version=based_on_l1_version,
        mode=mode,
        entities=entities,
        discovered_candidates=[
            {
                "source": "orm_ast",
                "entity": name,
                "keys": sorted((leaf.constraints or {}).keys()),
            }
            for name, leaf in sorted(entities.items())
        ],
        needs_review=needs_review,
        promotion_checklist=checklist if entities else [],
    )


def _extract_entity_from_class(node: ast.ClassDef) -> tuple[str, EntityLeaf] | None:
    constraints: dict[str, Any] = {}
    required_fields: list[str] = []
    for stmt in node.body:
        field_name, call = _field_assignment(stmt)
        if field_name is None or call is None:
            continue
        if not _is_fields_call(call):
            continue
        kwargs = _keyword_map(call)
        if kwargs.get("unique") is True:
            constraints[f"{field_name}_unique"] = True
        max_length = kwargs.get("max_length")
        if isinstance(max_length, int) and not isinstance(max_length, bool) and max_length > 0:
            constraints[f"{field_name}_has_max_length"] = max_length
        if kwargs.get("null") is False:
            required_fields.append(field_name)

    if not constraints and not required_fields:
        return None
    entity_name = _entity_name(node.name)
    return entity_name, EntityLeaf(
        constraints=constraints or None,
        required_fields=required_fields or None,
    )


def _entity_name(class_name: str) -> str:
    if not class_name:
        return class_name
    # Dept → dept, APIToken → api_token, Api → api (acronym-aware CamelCase)
    stepped = _CAMEL_BOUNDARY_1.sub(r"\1_\2", class_name)
    stepped = _CAMEL_BOUNDARY_2.sub(r"\1_\2", stepped)
    return stepped.lower()


def _field_assignment(stmt: ast.stmt) -> tuple[str | None, ast.Call | None]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
            return target.id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        if isinstance(stmt.value, ast.Call):
            return stmt.target.id, stmt.value
    return None, None


def _is_fields_call(call: ast.Call) -> bool:
    func = call.func
    # fields.CharField / fields.IntField
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id == "fields" and func.attr.endswith("Field")
    return False


def _keyword_map(call: ast.Call) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        value = _literal(keyword.value)
        if value is not None or isinstance(keyword.value, ast.Constant):
            out[keyword.arg] = value
    return out


def _literal(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    return None


__all__ = [
    "auth_matrix_known_keys",
    "constraint_known_keys",
    "extract_entity_constraints",
    "extract_entity_constraints_from_source",
]
