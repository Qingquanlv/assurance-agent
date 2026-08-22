from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_engine.composition import ConfigTreePluginSource, DeclarativePluginRejected, WheelPluginSource
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError
from graph_engine.composition.source_fs import SourceSnapshotError

from tests.phase5.composition_harness import copy_config_tree, request_for


def test_unknown_product_entrypoint_is_rejected(installed_sources):
    from pydantic import ValidationError

    from assurance_product.product import AssuranceCompositionRequest

    with pytest.raises(ValidationError):
        AssuranceCompositionRequest.model_validate(
            {
                "product_entrypoint": "assurance-wildcard",
                "deployment_source": installed_sources.deployments["opencode"],
                "configuration_tree": installed_sources.configuration_tree,
            }
        )


def test_missing_configuration_tree_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["opencode"],
        configuration_tree=ConfigTreePluginSource(path=tmp_path / "missing-config"),
    )
    with pytest.raises((FileNotFoundError, ResolutionError, SourceSnapshotError, ValueError)):
        resolve_assurance_composition(request)


def test_extra_config_file_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    tree = copy_config_tree(tmp_path / "extra-config")
    (tree.path / "undeclared.txt").write_text("no", encoding="utf-8")
    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["opencode"],
        configuration_tree=tree,
    )
    with pytest.raises((DeclarativePluginRejected, ResolutionError, SourceSnapshotError, ValueError)):
        resolve_assurance_composition(request)


def test_forged_deployment_declaration_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    extract = installed_sources.extract_roots["opencode"]
    declaration = next(extract.rglob("assurance-deployment-plugin.json"))
    document = json.loads(declaration.read_text(encoding="utf-8"))
    document["descriptor"]["plugin_version"] = "9.9.9"
    forged = tmp_path / "forged-extract"
    _copy_extract(extract, forged)
    forged_declaration = next(forged.rglob("assurance-deployment-plugin.json"))
    forged_declaration.write_text(json.dumps(document), encoding="utf-8")
    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=WheelPluginSource(
            distribution=installed_sources.deployments["opencode"].distribution,
            entrypoint_name="deployment",
            declaration_path=installed_sources.deployments["opencode"].declaration_path,
        ),
        configuration_tree=installed_sources.configuration_tree,
    )
    _swap_sys_path(extract, forged)
    try:
        with pytest.raises((ResolutionError, SourceSnapshotError, ValueError)):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(forged, extract)


def test_mutated_config_tree_changes_lock(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    original = resolve_assurance_composition(request_for("opencode", installed_sources))
    mutated = copy_config_tree(tmp_path / "mutated-config")
    policy = mutated.path / ".aa" / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    changed = resolve_assurance_composition(
        AssuranceCompositionRequest(
            product_entrypoint="assurance-opencode",
            deployment_source=installed_sources.deployments["opencode"],
            configuration_tree=mutated,
        )
    )
    assert changed.lock.digest != original.lock.digest
    assert changed.digest != original.digest


def test_wrong_runtime_is_rejected_before_fallback(installed_sources):
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["cursor"],
        configuration_tree=installed_sources.configuration_tree,
    )
    with pytest.raises((DependencyConflict, ResolutionError, ValueError)):
        resolve_assurance_composition(request)


def _copy_extract(source: Path, destination: Path) -> None:
    import shutil

    shutil.copytree(source, destination)


def _swap_sys_path(old: Path, new: Path) -> None:
    import sys

    old_s = str(old)
    new_s = str(new)
    sys.path = [new_s if item == old_s else item for item in sys.path]
    if new_s not in sys.path:
        sys.path.insert(0, new_s)
