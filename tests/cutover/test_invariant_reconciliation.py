from __future__ import annotations

import pytest

from graph_engine.composition import SchemaEntry
from graph_engine.json_schema import match_json_schema


def test_runtime_schema_rejects_unknown_keywords_at_direct_use() -> None:
    with pytest.raises(ValueError, match="unsupported schema keyword: format"):
        match_json_schema("ok", {"type": "string", "format": "email"})


def test_product_schema_entry_accepts_standard_rich_keywords() -> None:
    schema = (
        b'{"$defs":{"nonempty":{"type":"string","minLength":1}},'
        b'"properties":{"name":{"$ref":"#/$defs/nonempty"}},'
        b'"required":["name"],"type":"object"}'
    )

    entry = SchemaEntry.from_content(
        schema_id="phase.six.rich-product-schema",
        owner_id="phase.six",
        media_type="application/schema+json",
        content=schema,
    )

    assert entry.content == schema


def test_schema_entry_rejects_present_null_dialect() -> None:
    with pytest.raises(ValueError, match="schema entry dialect must be text"):
        SchemaEntry.from_content(
            schema_id="phase.six.null-dialect",
            owner_id="phase.six",
            media_type="application/schema+json",
            content=b'{"$schema":null}',
        )


def test_closed_schema_accepts_declared_standard_dialects() -> None:
    schema = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}'

    match_json_schema(
        "ok",
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "string"},
    )

    entry = SchemaEntry.from_content(
        schema_id="phase.six.standard-dialect",
        owner_id="phase.six",
        media_type="application/schema+json",
        content=schema,
    )
    assert entry.dialect == "https://json-schema.org/draft/2020-12/schema"
