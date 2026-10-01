from __future__ import annotations

from configparser import ConfigParser
from copy import deepcopy
from pathlib import Path
import sys
import zipfile

import pytest
import yaml

from tests.product.composition_harness import evict_generated_binding_modules
from tests.product.conformance import ALL_BINDING_IDS, PREPARE_IDS, load_yaml

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deployment"
_OPENCODE_MANIFEST = _FIXTURE_DIR / "opencode.yaml"


def valid_deployment_document() -> dict[str, object]:
    return deepcopy(load_yaml(_OPENCODE_MANIFEST))


def test_deployment_rejects_cursor_binding() -> None:
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    document = valid_deployment_document()
    document["runtime_plugin_id"] = "runtime.cursor"
    document["adapter_binding"] = {"schema_version": "1"}
    with pytest.raises(ValidationError):
        DeploymentBindingsV1.model_validate(document)


@pytest.fixture
def opencode_manifest() -> Path:
    return _OPENCODE_MANIFEST


@pytest.fixture
def opencode_document() -> dict[str, object]:
    return deepcopy(load_yaml(_OPENCODE_MANIFEST))


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
    assert first.wheel.name == canonical_wheel_filename(first.distribution, "1.1.0")


def test_success_leaves_only_the_final_wheel(tmp_path, opencode_manifest):
    from assurance_product.binding_builder import build_deployment_wheel

    output = tmp_path / "out"
    built = build_deployment_wheel(opencode_manifest, output)

    assert list(output.iterdir()) == [built.wheel]


@pytest.mark.parametrize(
    "cut",
    [
        "builder-before-file-publication",
        "builder-after-file-publication",
        "builder-before-wheel-publication",
        "builder-after-wheel-publication",
    ],
)
def test_interrupted_publication_is_retryable(
    tmp_path: Path,
    opencode_manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    from assurance_product import binding_builder

    expected = binding_builder.build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"

    def crash(selected: str) -> None:
        if selected == cut:
            raise RuntimeError(cut)

    monkeypatch.setattr(binding_builder, "_publication_cut", crash)
    with pytest.raises(RuntimeError, match=cut):
        binding_builder.build_deployment_wheel(opencode_manifest, output)

    monkeypatch.setattr(binding_builder, "_publication_cut", lambda _cut: None)
    retried = binding_builder.build_deployment_wheel(opencode_manifest, output)
    assert retried.wheel.read_bytes() == expected.wheel.read_bytes()
    assert list(output.iterdir()) == [retried.wheel]
    assert retried.wheel.stat().st_nlink == 1


def test_partial_temporary_wheel_is_recovered(
    tmp_path: Path,
    opencode_manifest: Path,
) -> None:
    from assurance_product.binding_builder import build_deployment_wheel

    expected = build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"
    output.mkdir()
    temporary = output / f".{expected.wheel.name}.publishing"
    temporary.write_bytes(b"partial")

    retried = build_deployment_wheel(opencode_manifest, output)

    assert list(output.iterdir()) == [retried.wheel]
    assert retried.wheel.read_bytes() == expected.wheel.read_bytes()


def test_output_directory_symlink_is_rejected(
    tmp_path: Path,
    opencode_manifest: Path,
) -> None:
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "output"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(BindingBuildError, match="output directory"):
        build_deployment_wheel(opencode_manifest, output)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("entry", ["temporary", "final"])
def test_symlink_publication_entry_is_rejected(
    tmp_path: Path,
    opencode_manifest: Path,
    entry: str,
) -> None:
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    expected = build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"
    output.mkdir()
    external = tmp_path / "external"
    external.write_bytes(expected.wheel.read_bytes())
    name = expected.wheel.name if entry == "final" else f".{expected.wheel.name}.publishing"
    (output / name).symlink_to(external)

    with pytest.raises(BindingBuildError, match="safe regular file"):
        build_deployment_wheel(opencode_manifest, output)
    assert external.read_bytes() == expected.wheel.read_bytes()


def test_final_created_late_is_not_overwritten(
    tmp_path: Path,
    opencode_manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import binding_builder

    expected = binding_builder.build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"
    final = output / expected.wheel.name
    competing = b"competing-publisher"

    def publish_at_cut(cut: str) -> None:
        if cut == "builder-before-wheel-publication":
            final.write_bytes(competing)

    monkeypatch.setattr(binding_builder, "_publication_cut", publish_at_cut)
    with pytest.raises(binding_builder.BindingBuildError, match="occupied"):
        binding_builder.build_deployment_wheel(opencode_manifest, output)
    assert final.read_bytes() == competing


def test_same_inode_rewrite_after_file_publication_is_rejected(
    tmp_path: Path,
    opencode_manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import binding_builder

    expected = binding_builder.build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"
    temporary = output / f".{expected.wheel.name}.publishing"

    def rewrite_at_cut(cut: str) -> None:
        if cut == "builder-after-file-publication":
            temporary.write_bytes(b"x" * len(expected.wheel.read_bytes()))

    monkeypatch.setattr(binding_builder, "_publication_cut", rewrite_at_cut)
    with pytest.raises(binding_builder.BindingBuildError, match="authenticated content"):
        binding_builder.build_deployment_wheel(opencode_manifest, output)
    assert not (output / expected.wheel.name).exists()


def test_output_directory_mode_drift_fails_closed(
    tmp_path: Path,
    opencode_manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import binding_builder

    output = tmp_path / "output"

    def drift_at_cut(cut: str) -> None:
        if cut == "builder-after-file-publication":
            output.chmod(0o777)

    monkeypatch.setattr(binding_builder, "_publication_cut", drift_at_cut)
    with pytest.raises(binding_builder.BindingBuildError, match="trust changed"):
        binding_builder.build_deployment_wheel(opencode_manifest, output)


def test_concurrent_cooperating_builder_is_rejected(
    tmp_path: Path,
    opencode_manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_product import binding_builder

    output = tmp_path / "output"
    nested_error: Exception | None = None

    def compete_at_cut(cut: str) -> None:
        nonlocal nested_error
        if cut == "builder-before-file-publication" and nested_error is None:
            try:
                binding_builder.build_deployment_wheel(opencode_manifest, output)
            except binding_builder.BindingBuildError as error:
                nested_error = error

    monkeypatch.setattr(binding_builder, "_publication_cut", compete_at_cut)
    built = binding_builder.build_deployment_wheel(opencode_manifest, output)
    assert built.wheel.is_file()
    assert nested_error is not None
    assert "locked" in str(nested_error)


@pytest.mark.parametrize("matches", [True, False])
def test_existing_final_is_accepted_only_when_bytes_match(
    tmp_path: Path,
    opencode_manifest: Path,
    matches: bool,
) -> None:
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    expected = build_deployment_wheel(opencode_manifest, tmp_path / "expected")
    output = tmp_path / "output"
    output.mkdir()
    final = output / expected.wheel.name
    final.write_bytes(expected.wheel.read_bytes() if matches else b"different")

    if not matches:
        with pytest.raises(BindingBuildError, match="authenticated content"):
            build_deployment_wheel(opencode_manifest, output)
        return
    built = build_deployment_wheel(opencode_manifest, output)
    assert built.wheel == final
    assert list(output.iterdir()) == [final]


def test_duplicate_route_assignment_is_rejected_before_build(
    tmp_path: Path,
    opencode_manifest: Path,
) -> None:
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    raw = opencode_manifest.read_text(encoding="utf-8")
    first_route = "  assurance.intake.agent.case-design.v1:\n"
    next_route = "  assurance.intake.agent.case-review.v1:\n"
    start = raw.index(first_route)
    end = raw.index(next_route)
    duplicate = raw[start:end]
    insertion = raw.index("permission_profiles:\n")
    manifest = tmp_path / "duplicate-route.yaml"
    manifest.write_text(raw[:insertion] + duplicate + raw[insertion:], encoding="utf-8")

    with pytest.raises(BindingBuildError, match="duplicate"):
        build_deployment_wheel(manifest, tmp_path / "output")
    assert not (tmp_path / "output").exists()


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
    with pytest.raises(ValidationError, match="semantic Agent contract IDs"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_extra_prepare_assignment_is_rejected(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    routes["assurance.product.unknown.prepare"] = routes[PREPARE_IDS[0]]
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="semantic Agent contract IDs"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_unknown_prepare_assignment_is_rejected(opencode_document):
    from pydantic import ValidationError

    from assurance_product.models import DeploymentBindingsV1

    routes = dict(opencode_document["routes"])
    assignment = routes.pop(PREPARE_IDS[0])
    routes["assurance.intake.not-a-capability.prepare"] = assignment
    opencode_document["routes"] = routes
    with pytest.raises(ValidationError, match="semantic Agent contract IDs"):
        DeploymentBindingsV1.model_validate(opencode_document)


def test_output_directory_must_be_empty(tmp_path, opencode_manifest):
    from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stale.txt").write_text("no", encoding="utf-8")
    with pytest.raises(BindingBuildError, match="empty"):
        build_deployment_wheel(opencode_manifest, occupied)


def test_generated_provider_contributes_exactly_32_semantic_bindings(tmp_path, opencode_manifest):
    import json

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
        evict_generated_binding_modules()
        sys.path.remove(str(installed))
    assert built.plugin_id == "assurance.product.agent"
    assert built.plugin_version == "1.1.0"
    assert descriptor.plugin_id == "assurance.product.agent"
    assert descriptor.plugin_version == "1.1.0"
    assert descriptor.task_handlers == ()
    assert descriptor.commit_validators == ()
    assert descriptor.effects == ()
    assert descriptor.schemas == ()
    assert set(descriptor.bindings) == set(ALL_BINDING_IDS)
    assert len(descriptor.bindings) == 27
    assert contribution.task_handlers == {}
    assert contribution.commit_validators == {}
    assert contribution.effects == ()
    assert contribution.schemas == ()
    assert {binding.capability_id for binding in contribution.bindings} == set(ALL_BINDING_IDS)
    assert len(contribution.bindings) == 27
    resources = {resource.resource_id: resource for resource in contribution.resources}
    adapter = resources["assurance.product.agent.adapter-binding"]
    assert adapter.media_type == "application/json"
    assert json.loads(adapter.content) == {
        "adapter_configuration_digest": "b" * 64,
        "cancel_timeout_seconds": 5.0,
        "endpoint": "http://127.0.0.1:4096/",
        "max_response_bytes": 65536,
        "observation_horizon_seconds": 30.0,
        "poll_interval_seconds": 0.5,
        "progress_timeout_seconds": 10.0,
        "project_scope": "fixture-project",
        "protocol_profile": "opencode-http-v1",
        "request_timeout_seconds": 5.0,
        "schema_version": "1",
        "secret_handle": "opencode.token",
        "tls_identity_digest": "a" * 64,
    }


def test_alias_targets_and_binding_data_follow_section_14(tmp_path, opencode_manifest):
    from graph_engine.plugin_api import RegistryPorts

    from agent_runtime_contracts.ops import AgentBindingDataV1
    from assurance_product.binding_builder import build_deployment_wheel

    built = build_deployment_wheel(opencode_manifest, tmp_path / "out")
    installed = _install_wheel(built.wheel, tmp_path / "installed")
    sys.path.insert(0, str(installed))
    try:
        module = __import__(f"{built.import_package}.provider", fromlist=["DeploymentPlugin"])
        contribution = module.DeploymentPlugin.contribute(RegistryPorts("2.0"))
    finally:
        evict_generated_binding_modules()
        sys.path.remove(str(installed))
    bindings = {item.capability_id: item for item in contribution.bindings}
    assert set(bindings) == set(PREPARE_IDS)
    for contract_id in PREPARE_IDS:
        binding = bindings[contract_id]
        assert binding.capability_id == contract_id
        assert binding.contract_id == contract_id
        assert binding.target_capability_id == "runtime.opencode.execute"
        assert binding.secret_handles == ("opencode.token",)
        AgentBindingDataV1.model_validate(binding.data)


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
    assert metadata["Version"] == "1.1.0"
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
