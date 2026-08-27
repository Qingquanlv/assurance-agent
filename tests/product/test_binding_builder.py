from __future__ import annotations

from configparser import ConfigParser
from copy import deepcopy
from pathlib import Path
import sys
import zipfile

import pytest
import yaml

from tests.product.conformance import ALL_BINDING_IDS, PREPARE_IDS, load_yaml

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deployment"
_OPENCODE_MANIFEST = _FIXTURE_DIR / "opencode.yaml"
_CURSOR_MANIFEST = _FIXTURE_DIR / "cursor.yaml"


@pytest.fixture
def opencode_manifest() -> Path:
    return _OPENCODE_MANIFEST


@pytest.fixture
def cursor_manifest() -> Path:
    return _CURSOR_MANIFEST


@pytest.fixture
def opencode_document() -> dict[str, object]:
    return deepcopy(load_yaml(_OPENCODE_MANIFEST))


@pytest.fixture
def cursor_document() -> dict[str, object]:
    return deepcopy(load_yaml(_CURSOR_MANIFEST))


def _write_manifest(path: Path, document: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _install_wheel(wheel: Path, dest: Path) -> Path:
    dest.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(dest)
    return dest


def test_binding_build_is_byte_deterministic(tmp_path, opencode_manifest):
    from assurance_product.binding_builder import (
        build_deployment_wheel,
        canonical_wheel_filename,
    )

    first = build_deployment_wheel(opencode_manifest, tmp_path / "first")
    second = build_deployment_wheel(opencode_manifest, tmp_path / "second")
    assert first.manifest_digest == second.manifest_digest
    assert first.wheel_digest == second.wheel_digest
    assert first.wheel.read_bytes() == second.wheel.read_bytes()
    prefix = first.manifest_digest.removeprefix("sha256:")[:16]
    assert first.distribution == f"assurance-product-bindings-{prefix}"
    assert first.import_package == f"assurance_product_bindings_{prefix}"
    assert first.wheel.name == canonical_wheel_filename(first.distribution, "1.0.0")


def test_reordered_yaml_mappings_produce_the_same_wheel(tmp_path, opencode_document):
    from assurance_product.binding_builder import build_deployment_wheel

    reversed_routes = {
        key: opencode_document["routes"][key] for key in reversed(tuple(opencode_document["routes"]))
    }
    reordered = {
        "secret_handles": opencode_document["secret_handles"],
        "request_policies": opencode_document["request_policies"],
        "permission_profiles": opencode_document["permission_profiles"],
        "routes": reversed_routes,
        "adapter_binding": opencode_document["adapter_binding"],
        "runtime_plugin_id": opencode_document["runtime_plugin_id"],
        "schema_version": opencode_document["schema_version"],
    }
    first = build_deployment_wheel(
        _write_manifest(tmp_path / "original.yaml", opencode_document),
        tmp_path / "first",
    )
    second = build_deployment_wheel(
        _write_manifest(tmp_path / "reordered.yaml", reordered),
        tmp_path / "second",
    )
    assert first.manifest_digest == second.manifest_digest
    assert first.wheel.read_bytes() == second.wheel.read_bytes()


def test_missing_prepare_assignment_is_rejected(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    del routes[PREPARE_IDS[0]]
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="prepare"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_extra_prepare_assignment_is_rejected(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    routes["assurance.product.unknown.prepare"] = routes[PREPARE_IDS[0]]
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="prepare"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_unknown_prepare_assignment_is_rejected(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    assignment = routes.pop(PREPARE_IDS[0])
    routes["assurance.intake.not-a-capability.prepare"] = assignment
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="prepare"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_output_directory_must_be_empty(tmp_path, opencode_manifest):
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stale.txt").write_text("no", encoding="utf-8")
    with pytest.raises(BindingBuildError, match="empty"):
        build_deployment_wheel(opencode_manifest, occupied)


def test_generated_provider_contributes_exactly_99_aliases(tmp_path, opencode_manifest):
    from graph_engine.plugin_api import RegistryPorts

    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    installed = _install_wheel(built.wheel, tmp_path / "installed")
    sys.path.insert(0, str(installed))
    try:
        module = __import__(f"{built.import_package}.provider", fromlist=["DeploymentPlugin"])
        provider = module.DeploymentPlugin
        descriptor = provider.descriptor()
        contribution = provider.contribute(RegistryPorts("2.0"))
    finally:
        sys.path.remove(str(installed))
    assert built.plugin_id == "assurance.product.agent"
    assert built.plugin_version == "1.0.0"
    assert descriptor.plugin_id == "assurance.product.agent"
    assert descriptor.plugin_version == "1.0.0"
    assert descriptor.task_handlers == ()
    assert descriptor.commit_validators == ()
    assert descriptor.effects == ()
    assert descriptor.schemas == ()
    assert set(descriptor.bindings) == set(ALL_BINDING_IDS)
    assert len(descriptor.bindings) == 99
    assert contribution.task_handlers == {}
    assert contribution.commit_validators == {}
    assert contribution.effects == ()
    assert contribution.schemas == ()
    assert {binding.capability_id for binding in contribution.bindings} == set(ALL_BINDING_IDS)
    assert len(contribution.bindings) == 99


def test_alias_targets_and_binding_data_follow_section_14(tmp_path, opencode_manifest):
    from agent_runtime_opencode.config import OpenCodeAdapterConfig
    from graph_engine.plugin_api import RegistryPorts

    from assurance_intake.contracts.agent import AgentBindingDataV1
    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    installed = _install_wheel(built.wheel, tmp_path / "installed")
    sys.path.insert(0, str(installed))
    try:
        module = __import__(f"{built.import_package}.provider", fromlist=["DeploymentPlugin"])
        contribution = module.DeploymentPlugin.contribute(RegistryPorts("2.0"))
    finally:
        sys.path.remove(str(installed))
    bindings = {item.capability_id: item for item in contribution.bindings}
    for prepare_id in PREPARE_IDS:
        stem = prepare_id.removesuffix(".prepare")
        key = stem.removeprefix("assurance.")
        prepare_alias = f"assurance.product.agent.{key}.prepare"
        execute_alias = f"assurance.product.agent.{key}.execute"
        finalize_alias = f"assurance.product.agent.{key}.finalize"
        prepare = bindings[prepare_alias]
        execute = bindings[execute_alias]
        finalize = bindings[finalize_alias]
        assert prepare.target_capability_id == prepare_id
        assert prepare.secret_handles == ()
        AgentBindingDataV1.model_validate(prepare.data)
        assert execute.target_capability_id == "runtime.opencode.execute"
        assert execute.secret_handles == ("opencode.token",)
        OpenCodeAdapterConfig.model_validate(execute.data)
        assert finalize.target_capability_id == f"{stem}.finalize"
        assert finalize.data is None
        assert finalize.secret_handles == ()


def test_cursor_wheel_keeps_confined_secret_handle(tmp_path, cursor_manifest):
    from agent_runtime_cursor.config import CursorAdapterConfig
    from graph_engine.plugin_api import RegistryPorts

    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(cursor_manifest, tmp_path / "out")
    installed = _install_wheel(built.wheel, tmp_path / "installed")
    sys.path.insert(0, str(installed))
    try:
        module = __import__(f"{built.import_package}.provider", fromlist=["DeploymentPlugin"])
        contribution = module.DeploymentPlugin.contribute(RegistryPorts("2.0"))
    finally:
        sys.path.remove(str(installed))
    execute = next(item for item in contribution.bindings if item.capability_id.endswith(".execute"))
    config = CursorAdapterConfig.model_validate(execute.data)
    assert config.secret_handle == "cursor.api-key"
    assert config.environment_names == ("PATH", "CURSOR_API_KEY")
    assert execute.secret_handles == ("cursor.api-key",)
    assert execute.target_capability_id == "runtime.cursor.execute"


class _EntryPointConfigParser(ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def test_wheel_exposes_only_the_deployment_entry_point(tmp_path, opencode_manifest):
    from email.parser import Parser

    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    with zipfile.ZipFile(built.wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        parser = _EntryPointConfigParser(interpolation=None)
        parser.read_string(archive.read(entry_points_name).decode("utf-8"))
    assert metadata["Name"] == built.distribution
    assert metadata["Version"] == "1.0.0"
    assert parser.sections() == ["graph_engine.plugins"]
    assert dict(parser.items("graph_engine.plugins")) == {
        "deployment": built.entry_point_value,
    }
    assert built.entry_point_value == f"{built.import_package}.provider:DeploymentPlugin"
    assert built.declaration_path == f"{built.import_package}/assurance-deployment-plugin.json"


def test_zip_members_are_normalized_and_contained(tmp_path, opencode_manifest):
    from zipfile import ZipInfo

    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    with zipfile.ZipFile(built.wheel) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
    assert names[-1].endswith(".dist-info/RECORD")
    assert names[:-1] == sorted(names[:-1])
    for info in infos:
        assert isinstance(info, ZipInfo)
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert "\\" not in info.filename
        assert not info.filename.startswith("/")
        assert ".." not in Path(info.filename).parts
        assert (info.external_attr >> 16) & 0o777 == 0o644
