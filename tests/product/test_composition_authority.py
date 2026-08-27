from __future__ import annotations

import base64
import csv
import hashlib
import json
from pathlib import Path

import pytest

from graph_engine.composition import ConfigTreePluginSource, DeclarativePluginRejected, WheelPluginSource
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError
from graph_engine.composition.source_fs import SourceSnapshotError

from tests.product.composition_harness import copy_config_tree, request_for


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
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["opencode"],
        configuration_tree=ConfigTreePluginSource(path=tmp_path / "missing-config"),
    )
    with pytest.raises((AssuranceCompositionError, SourceSnapshotError, ResolutionError)):
        resolve_assurance_composition(request)


def test_extra_config_file_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

    tree = copy_config_tree(tmp_path / "extra-config")
    (tree.path / "undeclared.txt").write_text("no", encoding="utf-8")
    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["opencode"],
        configuration_tree=tree,
    )
    with pytest.raises((DeclarativePluginRejected, ResolutionError, AssuranceCompositionError)):
        resolve_assurance_composition(request)


def test_forged_deployment_declaration_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

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
    _drop_modules_from_roots(extract)
    try:
        with pytest.raises((ResolutionError, SourceSnapshotError, AssuranceCompositionError)):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(forged, extract)
        _drop_modules_from_roots(forged)


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
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

    request = AssuranceCompositionRequest(
        product_entrypoint="assurance-opencode",
        deployment_source=installed_sources.deployments["cursor"],
        configuration_tree=installed_sources.configuration_tree,
    )
    with pytest.raises((DependencyConflict, ResolutionError, AssuranceCompositionError)):
        resolve_assurance_composition(request)


def test_missing_binding_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionError, resolve_assurance_composition

    request, original, mutated = _mutated_deployment_request(
        installed_sources,
        tmp_path,
        lambda document: document["bindings"].pop(),
    )
    _swap_sys_path(original, mutated)
    _drop_modules_from_roots(original)
    try:
        with pytest.raises((AssuranceCompositionError, ResolutionError, SourceSnapshotError)):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(mutated, original)
        _drop_modules_from_roots(mutated)


def test_extra_owned_binding_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionError, resolve_assurance_composition

    def add_extra(document: dict[str, object]) -> None:
        bindings = document["bindings"]
        assert isinstance(bindings, list)
        extra = dict(bindings[0])
        extra["capability_id"] = "assurance.product.agent.extra.prepare"
        bindings.append(extra)

    request, original, mutated = _mutated_deployment_request(installed_sources, tmp_path, add_extra)
    _swap_sys_path(original, mutated)
    _drop_modules_from_roots(original)
    try:
        with pytest.raises((AssuranceCompositionError, ResolutionError, SourceSnapshotError)):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(mutated, original)
        _drop_modules_from_roots(mutated)


def test_duplicate_binding_id_fails_closed(installed_sources, tmp_path: Path):
    from assurance_product.product import AssuranceCompositionError, resolve_assurance_composition

    def duplicate(document: dict[str, object]) -> None:
        bindings = document["bindings"]
        assert isinstance(bindings, list)
        bindings.append(dict(bindings[0]))

    request, original, mutated = _mutated_deployment_request(installed_sources, tmp_path, duplicate)
    _swap_sys_path(original, mutated)
    _drop_modules_from_roots(original)
    try:
        with pytest.raises((AssuranceCompositionError, ResolutionError, SourceSnapshotError)):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(mutated, original)
        _drop_modules_from_roots(mutated)


def test_forged_alias_target_fails_closed(installed_sources, tmp_path: Path) -> None:
    from assurance_product.product import AssuranceCompositionError, resolve_assurance_composition

    def forge_alias_target(document: dict[str, object]) -> None:
        bindings = document["bindings"]
        assert isinstance(bindings, list)
        execute = next(
            binding
            for binding in bindings
            if isinstance(binding, dict) and str(binding.get("capability_id", "")).endswith(".execute")
        )
        execute["target_capability_id"] = "assurance.intake.intake.prepare"

    request, original, mutated = _mutated_deployment_request(
        installed_sources,
        tmp_path,
        forge_alias_target,
    )
    _swap_sys_path(original, mutated)
    _drop_modules_from_roots(original)
    try:
        with pytest.raises(AssuranceCompositionError, match="execute alias target drifted"):
            resolve_assurance_composition(request)
    finally:
        _swap_sys_path(mutated, original)
        _drop_modules_from_roots(mutated)


def test_deployment_wheel_drift_after_resolution_fails_before_provider_import(
    installed_sources,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import metadata

    import graph_engine.composition.resolver as resolver_module

    from assurance_product.product import resolve_assurance_composition

    original = installed_sources.extract_roots["opencode"]
    drifted = tmp_path / "drifted-extract"
    _copy_extract(original, drifted)
    contribution = next(drifted.rglob("assurance-deployment-contribution.json"))
    loaded_deployment_entrypoints: list[str] = []
    drift_injected = False
    original_binding_load = resolver_module._load_snapshotted_entrypoint_binding
    original_entrypoint_load = metadata.EntryPoint.load

    def track_entrypoint_load(entrypoint: metadata.EntryPoint) -> object:
        if entrypoint.name == "deployment":
            loaded_deployment_entrypoints.append(entrypoint.name)
        return original_entrypoint_load(entrypoint)

    def drift_before_provider_import(source, snapshot, metadata_provider, binding_cache):
        nonlocal drift_injected
        if (
            source.distribution == installed_sources.deployments["opencode"].distribution
            and not drift_injected
        ):
            drift_injected = True
            contribution.write_bytes(contribution.read_bytes() + b" ")
        return original_binding_load(source, snapshot, metadata_provider, binding_cache)

    monkeypatch.setattr(metadata.EntryPoint, "load", track_entrypoint_load)
    monkeypatch.setattr(
        resolver_module,
        "_load_snapshotted_entrypoint_binding",
        drift_before_provider_import,
    )
    _swap_sys_path(original, drifted)
    _drop_modules_from_roots(original, drifted)
    try:
        with pytest.raises(SourceSnapshotError, match="RECORD hash mismatch|changed after snapshot"):
            resolve_assurance_composition(request_for("opencode", installed_sources))
    finally:
        _swap_sys_path(drifted, original)
        _drop_modules_from_roots(drifted)

    assert drift_injected is True
    assert loaded_deployment_entrypoints == []


def _mutated_deployment_request(installed_sources, tmp_path: Path, mutate):
    from assurance_product.product import AssuranceCompositionRequest

    extract = installed_sources.extract_roots["opencode"]
    contribution_path = next(extract.rglob("assurance-deployment-contribution.json"))
    document = json.loads(contribution_path.read_text(encoding="utf-8"))
    mutate(document)
    mutated = tmp_path / "mutated-extract"
    _copy_extract(extract, mutated)
    mutated_contribution = next(mutated.rglob("assurance-deployment-contribution.json"))
    payload = json.dumps(document).encode("utf-8")
    mutated_contribution.write_bytes(payload)
    _rewrite_record_hash(
        mutated,
        mutated_contribution.relative_to(mutated).as_posix(),
        payload,
    )
    return (
        AssuranceCompositionRequest(
            product_entrypoint="assurance-opencode",
            deployment_source=WheelPluginSource(
                distribution=installed_sources.deployments["opencode"].distribution,
                entrypoint_name="deployment",
                declaration_path=installed_sources.deployments["opencode"].declaration_path,
            ),
            configuration_tree=installed_sources.configuration_tree,
        ),
        extract,
        mutated,
    )


def _rewrite_record_hash(extract: Path, relative_path: str, content: bytes) -> None:
    record = next(extract.rglob("*.dist-info/RECORD"))
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    with record.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    rewritten = False
    for index, row in enumerate(rows):
        if row and row[0] == relative_path:
            rows[index] = [relative_path, f"sha256={encoded}", str(len(content))]
            rewritten = True
    if not rewritten:
        raise AssertionError(f"RECORD is missing {relative_path}")
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)


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


def _drop_modules_from_roots(*roots: Path) -> None:
    import sys

    resolved = tuple(root.resolve() for root in roots)
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            continue
        try:
            path = Path(origin).resolve()
        except OSError:
            continue
        if any(path.is_relative_to(root) for root in resolved):
            sys.modules.pop(name, None)
