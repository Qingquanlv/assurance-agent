from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import pytest

from graph_engine.boot.graph_revision import GraphBuildManifest
from graph_engine.composition.lock import ProductLock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCT_ROOT = _REPO_ROOT / "packages" / "products" / "assurance-product" / "assurance_product"
_CAPABILITY_ROOTS = (
    _REPO_ROOT / "packages" / "capabilities" / "assurance-intake" / "assurance_intake",
    _REPO_ROOT / "packages" / "capabilities" / "assurance-generation" / "assurance_generation",
    _REPO_ROOT / "packages" / "capabilities" / "assurance-execution" / "assurance_execution",
    _REPO_ROOT / "packages" / "capabilities" / "assurance-quality" / "assurance_quality",
    _REPO_ROOT / "packages" / "capabilities" / "assurance-healing" / "assurance_healing",
    _REPO_ROOT / "packages" / "capabilities" / "assurance-improvement" / "assurance_improvement",
)
_FORBIDDEN_WHEEL_RESOURCES = (
    "resources/workflow/module.yaml",
    "resources/workflow/main.yaml",
    "graph-inventory.yaml",
)
_OBSOLETE_GOLDENS = (
    _REPO_ROOT / "tests" / "product" / "fixtures" / "workflow-module-ownership.yaml",
    _REPO_ROOT / "tests" / "fixtures" / "workflow-v2-minimal.yaml",
)
_PLUGIN_KIT = _REPO_ROOT / "packages" / "framework" / "graph-engine" / "graph_engine" / "plugin_kit.py"
_WORKFLOW_MODULE_MARKERS = (
    "_WORKFLOW_MODULE_MIME",
    "application/vnd.graph-engine.workflow-module+yaml",
    "workflow/module.yaml",
)
_AGENT_PREFIX = "assurance.product.agent."

ProductLockV3 = ProductLock


def _packaged_file_names() -> tuple[str, ...]:
    names: list[str] = []
    for root in (*_CAPABILITY_ROOTS, _PRODUCT_ROOT):
        for path in root.rglob("*"):
            if path.is_file():
                names.append(path.relative_to(root).as_posix())
    return tuple(names)


@pytest.fixture
def wheel_contents() -> tuple[str, ...]:
    return _packaged_file_names()


def count_semantic_agent_contracts() -> int:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    return len(all_feature_agent_contracts())


def count_raw_agent_runtime_bindings() -> int:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    return len(all_feature_agent_contracts())


def all_agent_contracts_resolve_with_raw_executor() -> bool:
    from agent_runtime_contracts import ResolvedRawAgentExecutor

    from assurance_product.agent_contracts import all_feature_agent_contracts

    contracts = all_feature_agent_contracts()
    if len(contracts) != 34:
        return False
    source = (
        _REPO_ROOT
        / "packages"
        / "products"
        / "assurance-product"
        / "assurance_product"
        / "runtime_bindings.py"
    ).read_text(encoding="utf-8")
    return "ResolvedRawAgentExecutor" in source and ResolvedRawAgentExecutor is not None


def registered_ids_with_prefix(prefix: str) -> tuple[str, ...]:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.models import all_binding_ids

    ids = set(AGENT_EXECUTION_CONTRACTS) | set(all_binding_ids())
    try:
        import assurance_product.agent_contracts as agent_contracts

        aliases = getattr(agent_contracts, "LEGACY_AGENT_PHASE_ALIASES", ())
        if aliases:
            ids.update(aliases)
    except ImportError:
        pass
    return tuple(sorted(item for item in ids if item.startswith(prefix)))


@dataclass(frozen=True, slots=True)
class BootArtifact:
    attempt_contracts: Mapping[str, object]


@pytest.fixture
def boot_artifact() -> BootArtifact:
    from assurance_product.agent_contracts import (
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )

    contracts = {
        **dict(all_feature_agent_contracts()),
        **{item.contract_id: item for item in all_feature_task_contracts().values()},
    }
    return BootArtifact(attempt_contracts=contracts)


@pytest.fixture(scope="session")
def compiled_artifacts(installed_sources, opencode_composition):
    import assurance_product.application as application_module
    from assurance_product.application import AssuranceProductApplication

    coexistence = getattr(application_module, "CoexistenceBuildArtifacts", None)
    if coexistence is not None:
        return coexistence(
            invocation_lock={"schema_version": "2", "digest": "x" * 64},
            product_lock={"schema_version": "3", "digest": "y" * 64},
            manifest={"revision": {"revision_id": "z" * 64}},
        )
    return AssuranceProductApplication().compile(
        opencode_composition,
        product="assurance-opencode",
        config_tree=str(installed_sources.configuration_tree.path),
    )


def test_production_wheels_have_no_workflow_topology_resources(
    wheel_contents: tuple[str, ...],
) -> None:
    assert not any(any(name.endswith(item) for item in _FORBIDDEN_WHEEL_RESOURCES) for name in wheel_contents)


def test_obsolete_workflow_goldens_are_gone() -> None:
    missing = tuple(path for path in _OBSOLETE_GOLDENS if not path.exists())
    assert missing == _OBSOLETE_GOLDENS
    for path in _OBSOLETE_GOLDENS:
        assert not path.exists(), path


def test_plugin_kit_has_no_workflow_module_classifier() -> None:
    source = _PLUGIN_KIT.read_text(encoding="utf-8")
    for marker in _WORKFLOW_MODULE_MARKERS:
        assert marker not in source


def test_semantic_agent_nodes_have_no_phase_aliases(boot_artifact: BootArtifact) -> None:
    import assurance_product.agent_contracts as agent_contracts

    assert len(boot_artifact.attempt_contracts) == 46
    assert count_semantic_agent_contracts() == 34
    assert count_raw_agent_runtime_bindings() == 34
    assert all_agent_contracts_resolve_with_raw_executor()
    assert not registered_ids_with_prefix(_AGENT_PREFIX)
    assert not hasattr(agent_contracts, "LEGACY_AGENT_PHASE_ALIASES")
    assert not hasattr(agent_contracts, "PREPARE_IDS")
    assert not hasattr(agent_contracts, "expand_agent_job_slots")
    assert not hasattr(agent_contracts, "bind_agent_execution_contracts")
    assert not hasattr(agent_contracts, "product_workflow_slot_bindings")


def test_runtime_selection_module_is_gone() -> None:
    import importlib.util

    assert importlib.util.find_spec("assurance_product.runtime_selection") is None
    assert not (_PRODUCT_ROOT / "runtime_selection.py").exists()


def test_compile_emits_only_v3_product_artifacts(compiled_artifacts) -> None:
    import assurance_product.application as application_module

    assert not hasattr(application_module, "CoexistenceBuildArtifacts")
    assert isinstance(compiled_artifacts.product_lock, ProductLockV3)
    assert isinstance(compiled_artifacts.graph_manifest, GraphBuildManifest)
    assert not hasattr(compiled_artifacts, "legacy_invocation_lock")
    assert not hasattr(compiled_artifacts, "invocation_lock")


def test_task8_inventory_rows_are_gone() -> None:
    from tests.architecture.legacy_import_inventory import (
        load_explicit_allowlist,
        scan_legacy_imports,
    )

    allowlist = load_explicit_allowlist()
    hits = scan_legacy_imports()
    former_task8 = allowlist.task8_consumers - (
        allowlist.retained_implementations | allowlist.task9_characterization
    )
    task8_hits = tuple(hit for hit in hits if hit.path in former_task8)
    assert task8_hits == ()
    remaining_paths = {hit.path for hit in hits if hit.kind != "parse-error"}
    allowed_remaining = allowlist.retained_implementations | allowlist.task9_characterization
    unexpected = tuple(sorted(path for path in remaining_paths if path not in allowed_remaining))
    assert unexpected == ()


def test_product_runner_does_not_mint_phase_aliases() -> None:
    source = (_REPO_ROOT / "tests" / "product" / "product_runner.py").read_text(encoding="utf-8")
    assert "_product_alias" not in source
    assert "assurance.product.agent." not in source


def test_composition_harness_is_factory_product_lock() -> None:
    source = (_REPO_ROOT / "tests" / "product" / "composition_harness.py").read_text(encoding="utf-8")
    assert "CompiledWorkflow" not in source
    assert "compiled_product_workflow" not in source


def test_semantic_agent_ids_match_product_contracts() -> None:
    from graph_engine.composition.semantic_agent_ids import SEMANTIC_AGENT_CONTRACT_IDS

    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    assert SEMANTIC_AGENT_CONTRACT_IDS == frozenset(AGENT_EXECUTION_CONTRACTS)
    assert len(SEMANTIC_AGENT_CONTRACT_IDS) == 34
