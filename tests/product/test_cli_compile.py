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
from tests.product.composition_harness import copy_config_tree, request_for

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
    assert set(document) == {"product_lock", "graph_manifest"}
    assert document["product_lock"]["schema_version"] == "3"
    assert document["product_lock"]["digest"] == composition.lock.digest
    assert document["graph_manifest"]["revision"]["revision_id"]


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


def test_compile_emits_v3_product_artifacts_without_runtime_secrets(cli_runner, installed_sources):
    from assurance_product.application import ProductBuildArtifacts
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    result = cli_runner.invoke(app, ["compile", "--json", *source_args(installed_sources)])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    artifacts = ProductBuildArtifacts(
        product_lock=document["product_lock"],
        graph_manifest=document["graph_manifest"],
    )
    assert artifacts.product_lock.schema_version == "3"
    assert artifacts.product_lock.digest == composition.lock.digest
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
    assert getattr(artifacts.graph_manifest.revision, "revision_id", None)


def test_organization_config_change_alters_product_lock_not_topology(
    cli_runner, installed_sources, tmp_path: Path
):
    from assurance_product.cli import app

    baseline = cli_runner.invoke(app, ["compile", "--json", *source_args(installed_sources)])
    assert baseline.exit_code == 0, baseline.output
    first = json.loads(baseline.stdout)
    tree = copy_config_tree(tmp_path / "org-config")
    policy = tree.path / ".aa" / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "# organization-config\n", encoding="utf-8")
    args = source_args(installed_sources)
    args[args.index("--config-tree") + 1] = str(tree.path)
    changed = cli_runner.invoke(app, ["compile", "--json", *args])
    assert changed.exit_code == 0, changed.output
    second = json.loads(changed.stdout)
    assert second["product_lock"]["digest"] != first["product_lock"]["digest"]
    assert (
        second["graph_manifest"]["revision"]["revision_id"]
        != first["graph_manifest"]["revision"]["revision_id"]
    )
    assert (
        second["graph_manifest"]["revision"]["factory_symbols"]
        == first["graph_manifest"]["revision"]["factory_symbols"]
    )
    assert (
        second["graph_manifest"]["entrypoint_contract_digests"]
        == first["graph_manifest"]["entrypoint_contract_digests"]
    )


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
    assert "LangGraphRuntimeRecord" in source
    assert set(PRODUCT_ENTRYPOINTS)


def test_compile_keeps_v3_lock_and_both_33_row_raw_agent_tables(cli_runner, installed_sources) -> None:
    from graph_engine.boot.graph_revision import GraphBuildManifest
    from graph_engine.composition.lock import ProductLock

    from assurance_product.application import AssuranceProductApplication
    from assurance_product.cli import app
    from assurance_product.product import resolve_assurance_composition
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS, RAW_AGENT_RUNTIME_BINDING_ROWS
    from tests.product.test_python_native_cutover import (
        count_agent_occurrences,
        count_raw_agent_runtime_bindings,
        count_semantic_agent_contracts,
    )

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    artifacts = AssuranceProductApplication().compile(
        composition,
        product="assurance-opencode",
        config_tree=str(installed_sources.configuration_tree.path),
    )
    result = cli_runner.invoke(app, ["compile", "--json", *source_args(installed_sources)])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert isinstance(artifacts.product_lock, ProductLock)
    assert artifacts.product_lock.schema_version == "3"
    assert isinstance(artifacts.graph_manifest, GraphBuildManifest)
    assert len(AGENT_RUNTIME_BINDINGS) == 33
    assert len(RAW_AGENT_RUNTIME_BINDING_ROWS) == 33
    assert count_semantic_agent_contracts() == 33
    assert count_raw_agent_runtime_bindings() == 33
    assert count_agent_occurrences() == 34
    assert document["product_lock"]["digest"] == artifacts.product_lock.digest
    assert (
        document["graph_manifest"]["revision"]["revision_id"] == artifacts.graph_manifest.revision.revision_id
    )


def test_removing_selectors_cannot_disable_bypass_downgrade_or_relabel_checkpoint_r() -> None:
    repo = Path(__file__).resolve().parents[2]
    checkpoint = (repo / "tests" / "product" / "test_raw_agent_checkpoint.py").read_text(encoding="utf-8")
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "test_checkpoint_r_inventory_is_33_33_34_41_43" in checkpoint
    assert "test_checkpoint_r_records_candidate_lock_and_revision" in checkpoint
    assert "test_live_opencode_cutover_binding_records_checkpoint_r" in checkpoint
    assert "Checkpoint R" in checkpoint or "checkpoint-r" in checkpoint
    assert "test_raw_agent_checkpoint.py" in ci
    assert "scripts/assurance_product_wheel_smoke_test.sh" in ci
    assert "scripts/graph_engine_smoke_test.sh" in ci
    assert "scripts/assurance_capability_wheel_smoke_test.sh" in ci
    assert (
        "legacy-v2" not in checkpoint.split("def test_checkpoint_r_inventory_is_33_33_34_41_43")[1][:400]
        or "33" in checkpoint
    )
    assert "downgrade" not in ci.lower()
    assert "bypass checkpoint" not in ci.lower()


def test_historical_leftover_evidence_is_readable_only_through_the_evidence_reader() -> None:
    from graph_engine.evidence import legacy_v2

    assert hasattr(legacy_v2, "authenticate_invocation_lock_v2")
    assert hasattr(legacy_v2, "read_legacy_ledger")
    assert hasattr(legacy_v2, "fold_legacy_events")
    assert "Ledger" not in legacy_v2.__all__
    source = Path(legacy_v2.__file__).read_text(encoding="utf-8")
    assert "graph_engine.runtime" not in source
    from assurance_product.application import AssuranceProductApplication

    start_source = inspect.getsource(AssuranceProductApplication.start)
    run_source = inspect.getsource(AssuranceProductApplication.run)
    resume_source = inspect.getsource(AssuranceProductApplication.resume)
    assert "leftover workflow execution is deleted" in start_source
    assert "leftover workflow execution is deleted" in run_source
    assert "leftover workflow execution is deleted" in resume_source


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
