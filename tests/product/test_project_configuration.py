from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from graph_engine.composition import ConfigTreePluginSource, load_config_tree
from graph_engine.plugin_api import ResourceContribution

from tests.product.conformance import load_yaml

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "project-config"

_EXPECTED_RESOURCE_IDS = {
    "assurance.product.configuration.product-policy",
    "assurance.product.configuration.data-knowledge",
    "assurance.product.configuration.capability-catalog",
    "assurance.product.configuration.node-policy-values",
}


@pytest.fixture
def config_tree() -> ConfigTreePluginSource:
    return ConfigTreePluginSource(path=_FIXTURE_DIR)


@pytest.fixture
def config_document() -> dict[str, object]:
    return deepcopy(load_yaml(_FIXTURE_DIR / ".aa" / "config.yaml"))


def test_project_config_contributes_business_data_only(config_tree):
    from assurance_product.configuration import load_project_configuration

    contribution = load_project_configuration(config_tree)
    assert contribution.bindings == ()
    assert contribution.task_handlers == {}
    assert contribution.commit_validators == {}
    assert contribution.effects == ()
    assert {resource.resource_id for resource in contribution.resources} == {
        "assurance.product.configuration.product-policy",
        "assurance.product.configuration.data-knowledge",
        "assurance.product.configuration.capability-catalog",
        "assurance.product.configuration.node-policy-values",
    }


@pytest.mark.parametrize(
    "key", ["model", "endpoint", "executable", "permission_profile", "secret", "adapter"]
)
def test_project_config_rejects_runtime_authority(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document[key] = "forbidden"
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


def test_project_config_plugin_identity_is_exact(config_tree):
    from assurance_product.configuration import load_project_configuration
    from assurance_product.models import CONFIGURATION_PLUGIN_ID, CONFIGURATION_PLUGIN_VERSION

    loaded = load_config_tree(config_tree)
    assert loaded.descriptor.plugin_id == CONFIGURATION_PLUGIN_ID == "assurance.product.configuration"
    assert loaded.descriptor.plugin_version == CONFIGURATION_PLUGIN_VERSION == "1.0.0"
    assert loaded.snapshot.identity.plugin_id == "assurance.product.configuration"
    assert loaded.snapshot.identity.plugin_version == "1.0.0"
    contribution = load_project_configuration(config_tree)
    assert contribution.bindings == ()
    assert {resource.resource_id for resource in contribution.resources} == _EXPECTED_RESOURCE_IDS


def test_project_config_resource_bytes_are_authenticated_source_bytes(config_tree):
    from assurance_product.configuration import load_project_configuration

    contribution = load_project_configuration(config_tree)
    by_id = {resource.resource_id: resource for resource in contribution.resources}
    assert (
        by_id["assurance.product.configuration.product-policy"].content
        == (_FIXTURE_DIR / ".aa" / "policy.yaml").read_bytes()
    )
    assert (
        by_id["assurance.product.configuration.data-knowledge"].content
        == (_FIXTURE_DIR / ".aa" / "data-knowledge.yaml").read_bytes()
    )
    assert all(isinstance(resource, ResourceContribution) for resource in contribution.resources)
    assert all(isinstance(resource.content, bytes) for resource in contribution.resources)


def test_parse_project_config_rejects_unknown_fields(config_document):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document["unknown_policy_hook"] = True
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


def test_parse_project_config_rejects_schema_drift(config_document):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document["schema_version"] = "2"
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


def test_same_id_version_different_bytes_change_contribution(tmp_path, config_tree):
    from assurance_product.configuration import load_project_configuration

    first = load_project_configuration(config_tree)
    mutated = _copy_tree(config_tree.path, tmp_path / "mutated")
    policy_path = mutated / ".aa" / "policy.yaml"
    document = load_yaml(policy_path)
    document["organization"] = "other-org"
    policy_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    envelope = load_yaml(mutated / ".aa" / "config.yaml")
    envelope["product_policy"] = document
    (mutated / ".aa" / "config.yaml").write_text(
        yaml.safe_dump(envelope, sort_keys=False),
        encoding="utf-8",
    )
    second = load_project_configuration(ConfigTreePluginSource(path=mutated))
    first_policy = _resource(first, "assurance.product.configuration.product-policy").content
    second_policy = _resource(second, "assurance.product.configuration.product-policy").content
    assert first_policy != second_policy
    original = load_config_tree(config_tree)
    changed = load_config_tree(ConfigTreePluginSource(path=mutated))
    assert original.descriptor.plugin_id == changed.descriptor.plugin_id
    assert original.descriptor.plugin_version == changed.descriptor.plugin_version
    assert original.snapshot.digest != changed.snapshot.digest


def _copy_tree(source: Path, destination: Path) -> Path:
    import shutil

    shutil.copytree(source, destination)
    return destination


def _resource(contribution: object, resource_id: str) -> ResourceContribution:
    resources = getattr(contribution, "resources")
    return next(resource for resource in resources if resource.resource_id == resource_id)
