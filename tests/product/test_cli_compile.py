from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from typing import cast

import pytest

from tests.product.cli_support import (
    command_names,
    nested_command_names,
    source_args,
)
from tests.product.composition_harness import copy_config_tree

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
        "compile",
        "bindings",
        "start",
        "run",
        "status",
        "resume",
        "lock",
    }
    assert nested_command_names(cli_runner, app, "bindings") == {"build"}
    assert nested_command_names(cli_runner, app, "lock") == {"show"}


def test_compile_emits_authenticated_v3_lock_without_secrets_or_invocation(
    cli_runner, installed_sources, opencode_composition, tmp_path: Path
):
    from assurance_product.application import ProductBuildArtifacts
    from assurance_product.cli import app
    from assurance_product.runtime_bindings import raw_agent_runtime_binding_rows
    from tests.product.test_python_native_cutover import (
        count_raw_agent_runtime_bindings,
        count_semantic_agent_contracts,
    )

    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    result = cli_runner.invoke(
        app,
        ["compile", "--json", *source_args(installed_sources), "--engine-root", str(engine_root)],
    )
    assert result.exit_code == 0, result.output
    assert not (engine_root / "invocations").exists() or not any((engine_root / "invocations").iterdir())
    document = json.loads(result.stdout)
    assert set(document) == {"product_lock", "graph_manifest"}
    artifacts = ProductBuildArtifacts(
        product_lock=document["product_lock"],
        graph_manifest=document["graph_manifest"],
    )
    assert artifacts.product_lock.schema_version == "3"
    assert artifacts.product_lock.digest == opencode_composition.lock.digest
    assert artifacts.graph_manifest.revision.revision_id
    dumped = artifacts.product_lock.model_dump(mode="json")
    assert "compiled_workflow" not in dumped
    assert "compiled_workflow_digest" not in dumped
    assert "workflow_digest" not in dumped
    assert "execution_host" not in dumped
    assert "legacy_invocation_lock" not in document
    assert "invocation_lock" not in document
    assert "coexistence" not in document
    encoded = json.dumps(document, sort_keys=True)
    assert "saver" not in encoded
    assert "AssuranceAttemptKernel" not in encoded
    assert "secret" not in encoded.lower() or "secret_handles" in encoded
    assert "/Users/" not in json.dumps(artifacts.graph_manifest.model_dump(mode="json"))
    assert len(raw_agent_runtime_binding_rows(opencode_composition)) == 28
    assert count_semantic_agent_contracts() == 28
    assert count_raw_agent_runtime_bindings() == 28

    tree = copy_config_tree(tmp_path / "org-config")
    policy = tree.path / ".aa" / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "# organization-config\n", encoding="utf-8")
    args = source_args(installed_sources)
    args[args.index("--config-tree") + 1] = str(tree.path)
    changed = cli_runner.invoke(app, ["compile", "--json", *args])
    assert changed.exit_code == 0, changed.output
    second = json.loads(changed.stdout)
    assert second["product_lock"]["digest"] != document["product_lock"]["digest"]
    assert (
        second["graph_manifest"]["revision"]["revision_id"]
        != document["graph_manifest"]["revision"]["revision_id"]
    )
    assert (
        second["graph_manifest"]["revision"]["factory_symbols"]
        == document["graph_manifest"]["revision"]["factory_symbols"]
    )
    assert (
        second["graph_manifest"]["entrypoint_contract_digests"]
        == document["graph_manifest"]["entrypoint_contract_digests"]
    )


def test_installed_assurance_contributions_accept_rich_product_schemas(opencode_composition):
    schema = opencode_composition.registries.schemas.entries["assurance.intake.schema.case-authoring.v1"]
    assert schema.media_type == "application/schema+json"


def test_installed_assurance_effect_schemas_use_the_audited_closed_keyword_set(
    opencode_composition,
):
    composition = opencode_composition
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


def test_runtime_selection_module_is_absent() -> None:
    assert importlib.util.find_spec("assurance_product.runtime_selection") is None
    product_root = Path(__file__).resolve().parents[2]
    assert not (
        product_root
        / "packages"
        / "products"
        / "assurance-product"
        / "assurance_product"
        / "runtime_selection.py"
    ).exists()


def test_new_invocations_are_langgraph_without_a_selection_branch() -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.models import PRODUCT_ENTRYPOINTS

    source = inspect.getsource(AssuranceProductApplication.start)
    assert "select_runtime" not in source
    assert "use_test_runtime_selector" not in source
    assert "ENTRYPOINT_RUNTIME_CUTOVER" not in source
    assert "InvocationIdentityRecord" in source
    assert set(PRODUCT_ENTRYPOINTS)


def test_application_uses_invocation_identity_for_lifecycle() -> None:
    from assurance_product.application import AssuranceProductApplication

    start_source = inspect.getsource(AssuranceProductApplication.start)
    run_source = inspect.getsource(AssuranceProductApplication.run)
    resume_source = inspect.getsource(AssuranceProductApplication.resume)
    resolve_source = inspect.getsource(AssuranceProductApplication._resolve_existing)
    assert "load_identity" in start_source
    assert "load_identity" in run_source
    assert "load_identity" in resolve_source
    assert "_resolve_existing" in resume_source
    for source in (start_source, run_source, resume_source, resolve_source):
        assert "leftover workflow execution is deleted" not in source
        assert "read_legacy_ledger" not in source
        assert "fold_legacy_events" not in source
        assert "legacy-v2" not in source


def test_aa_topology_override_is_rejected(cli_runner, installed_sources, tmp_path: Path):
    from assurance_product.cli import app

    tree = copy_config_tree(tmp_path / "topology-config")
    (tree.path / ".aa" / "topology.yaml").write_text("graphs: []\n", encoding="utf-8")
    args = source_args(installed_sources)
    args[args.index("--config-tree") + 1] = str(tree.path)
    result = cli_runner.invoke(app, ["compile", "--json", *args])
    assert result.exit_code == 40, result.output
    assert "topology" in result.output.lower() or "organization" in result.output.lower()


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
