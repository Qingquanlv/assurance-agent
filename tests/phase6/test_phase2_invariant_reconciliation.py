from __future__ import annotations

import subprocess
import sys

import pytest

from graph_engine.composition import SchemaEntry
from graph_engine.runtime.json_schema import match_json_schema


_CURRENT_BEHAVIORAL_NODES = {
    "effect_crash_recovery": (
        "packages/graph-engine/tests/runtime/test_effects.py::"
        "test_executor_reconciles_after_apply_started_without_blind_reapply"
    ),
    "retry_exhaustion": (
        "packages/graph-engine/tests/runtime/test_effects.py::"
        "test_executor_exhausts_policy_as_non_retryable_failure"
    ),
    "composition_source_authentication": (
        "packages/graph-engine/tests/composition/test_wheel_sources.py::"
        "test_load_rejects_arbitrary_preloaded_module_at_authenticated_path"
    ),
    "toy_a_crash_recovery": (
        "packages/graph-engine/tests/runtime/test_effects.py::"
        "test_executor_reconciles_after_apply_started_without_blind_reapply"
    ),
    "installed_wheel_isolation": (
        "packages/graph-engine/tests/test_cli.py::test_run_executes_only_the_explicit_product_plugin_bundle"
    ),
}


@pytest.mark.parametrize("invariant_id", sorted(_CURRENT_BEHAVIORAL_NODES))
def test_phase2_retained_behavioral_node_still_passes(invariant_id: str) -> None:
    """Exercise the current node that proves a retired Phase 2 invariant."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", _CURRENT_BEHAVIORAL_NODES[invariant_id]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


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
