from __future__ import annotations

import unicodedata

import pytest
from pydantic import ValidationError

from graph_engine.graph.compiler import CompileError
from graph_engine.graph.output_projection import (
    OutputProjectionError,
    parse_output_projection,
    project_subgraph_output,
    validate_output_projection_compile,
)


def test_child_output_projection_exposes_only_selected_fields() -> None:
    projection = parse_output_projection(
        {
            "type": "object",
            "fields": {"status": {"type": "child_output_pointer", "pointer": "/internal/status"}},
        }
    )
    assert project_subgraph_output(
        projection, child_output={"internal": {"status": "passed", "secret": 1}}
    ) == {"status": "passed"}


def test_output_projection_rejects_input_sources() -> None:
    for payload in (
        {"type": "root_pointer", "pointer": "/change_id"},
        {"type": "config_pointer", "pointer": "/policy"},
        {"type": "predecessor_pointer", "predecessor": "left", "pointer": "/value"},
        {"type": "predecessor", "predecessor": "left"},
        {"type": "all_predecessor_tokens"},
        {"type": "graph_input_pointer", "pointer": "/change_id"},
    ):
        with pytest.raises(ValidationError):
            parse_output_projection(payload)


def test_child_output_pointer_miss_fails_deterministically() -> None:
    projection = parse_output_projection({"type": "child_output_pointer", "pointer": "/missing"})
    with pytest.raises(OutputProjectionError, match="missing pointer"):
        project_subgraph_output(projection, child_output={"present": 1})


def test_output_object_projection_rejects_duplicate_normalized_field_names() -> None:
    key_a = "caf" + "e\u0301"
    key_b = "caf\u00e9"
    assert unicodedata.normalize("NFC", key_a) == unicodedata.normalize("NFC", key_b)
    assert key_a != key_b
    with pytest.raises(ValidationError, match="duplicate object projection field"):
        parse_output_projection(
            {
                "type": "object",
                "fields": {key_a: {"type": "literal", "value": 1}, key_b: {"type": "literal", "value": 2}},
            }
        )


def test_compile_rejects_output_projection_cycles() -> None:
    shared: dict[str, object] = {"type": "object", "fields": {}}
    shared["fields"] = {"loop": shared}
    with pytest.raises(CompileError, match="cycle"):
        validate_output_projection_compile(shared, location="root/child")
