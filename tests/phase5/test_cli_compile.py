from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.phase5.cli_support import (
    command_names,
    nested_command_names,
    source_args,
)
from tests.phase5.composition_harness import copy_config_tree, request_for

pytestmark = pytest.mark.usefixtures("installed_sources")

_EFFECT_SCHEMA_KEYWORDS = {
    "$defs",
    "$ref",
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


def test_help_exposes_exact_command_tree(cli_runner):
    from assurance_product.cli import app

    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert command_names(result.stdout) == {
        "archive",
        "compile",
        "bindings",
        "start",
        "run",
        "status",
        "resume",
        "export",
        "lock",
    }
    assert nested_command_names(cli_runner, app, "bindings") == {"build"}
    assert nested_command_names(cli_runner, app, "lock") == {"show"}


def test_compile_authenticates_sources_and_prints_lock_identity(cli_runner, installed_sources):
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    result = cli_runner.invoke(app, ["compile", "--json", *source_args(installed_sources)])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["lock_digest"] == composition.lock_digest
    assert document["composition_digest"] == composition.digest
    assert document["workflow_digest"] == composition.workflow.digest
    assert document["product"] == "assurance-opencode"
    assert document["engine_api"] == "2.0"
    assert "intake" in document["entrypoints"]
    assert document["audit"]["unreachable_nodes"] == []
    assert document["audit"]["dead_ends"] == []
    assert document["audit"]["forbidden_direct_targets"] == []
    assert document["audit"]["missing_bindings"] == []
    assert document["audit"]["uninventoried_nodes"] == []


def test_installed_assurance_contributions_accept_rich_product_schemas(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))

    schema = composition.registries.schemas.entries["assurance.intake.schema.case-authoring.v1"]
    assert schema.media_type == "application/schema+json"


def test_installed_assurance_effect_schemas_use_the_audited_closed_keyword_set(
    installed_sources,
):
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    schema_ids = {
        schema_id
        for effect in composition.registries.effects.entries.values()
        for schema_id in (effect.intent_schema_id, effect.receipt_schema_id)
    }
    keywords: set[str] = set()
    for schema_id in schema_ids:
        document = json.loads(composition.registries.schemas.entries[schema_id].content)
        _collect_schema_keywords(cast(dict[str, object] | bool, document), keywords)

    assert keywords == _EFFECT_SCHEMA_KEYWORDS


def test_compile_fails_closed_on_wrong_runtime(cli_runner, installed_sources):
    from assurance_product.cli import app

    args = source_args(installed_sources, adapter="opencode")
    args[1] = "assurance-cursor"
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code == 40, result.output
    assert "assurance-cursor" in result.output or "runtime" in result.output.lower()


def test_compile_fails_closed_on_forged_config(cli_runner, installed_sources, tmp_path: Path):
    from assurance_product.cli import app

    tree = copy_config_tree(tmp_path / "forged-config")
    (tree.path / "undeclared.txt").write_text("forged", encoding="utf-8")
    args = source_args(installed_sources)
    config_index = args.index("--config-tree") + 1
    args[config_index] = str(tree.path)
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code == 40, result.output


def test_compile_does_not_start_an_invocation(cli_runner, installed_sources, tmp_path: Path):
    from assurance_product.cli import app

    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    result = cli_runner.invoke(
        app,
        ["compile", "--json", *source_args(installed_sources), "--engine-root", str(engine_root)],
    )
    assert result.exit_code == 0, result.output
    assert not (engine_root / "invocations").exists() or not any((engine_root / "invocations").iterdir())


def _collect_schema_keywords(schema: dict[str, object] | bool, keywords: set[str]) -> None:
    if isinstance(schema, bool):
        return
    keywords.update(schema)
    for map_keyword in ("$defs", "properties"):
        nested_map = schema.get(map_keyword)
        if isinstance(nested_map, dict):
            for nested in nested_map.values():
                _collect_schema_keywords(cast(dict[str, object] | bool, nested), keywords)
    for single_keyword in ("additionalProperties", "items"):
        nested = schema.get(single_keyword)
        if isinstance(nested, dict | bool):
            _collect_schema_keywords(nested, keywords)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for nested in alternatives:
            _collect_schema_keywords(cast(dict[str, object] | bool, nested), keywords)
