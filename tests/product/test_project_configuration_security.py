from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
import shutil
from typing import cast

import pytest
import yaml

from graph_engine.composition import ConfigTreePluginSource

from tests.product.conformance import load_yaml

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "project-config"


@pytest.fixture
def config_tree() -> ConfigTreePluginSource:
    return ConfigTreePluginSource(path=_FIXTURE_DIR)


@pytest.fixture
def config_document() -> dict[str, object]:
    return deepcopy(load_yaml(_FIXTURE_DIR / ".aa" / "config.yaml"))


def _copy_tree(destination: Path) -> Path:
    shutil.copytree(_FIXTURE_DIR, destination)
    return destination


def _rewrite_yaml(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _object_list(document: Mapping[str, object], key: str) -> list[object]:
    value = document[key]
    if not isinstance(value, list):
        raise AssertionError(f"{key} must be a list")
    return list(cast(list[object], value))


def _load(path: Path):
    from assurance_product.configuration import load_project_configuration

    return load_project_configuration(ConfigTreePluginSource(path=path))


@pytest.mark.parametrize(
    "key", ["model", "endpoint", "executable", "permission_profile", "secret", "adapter"]
)
def test_project_config_rejects_runtime_authority(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document[key] = "forbidden"
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


@pytest.mark.parametrize(
    "key", ["model", "endpoint", "executable", "permission_profile", "secret", "adapter"]
)
def test_nested_payload_rejects_runtime_authority(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    policy = dict(config_document["product_policy"])
    policy[key] = "forbidden"
    config_document["product_policy"] = policy
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


@pytest.mark.parametrize(
    "key",
    ["import", "module", "callable", "worker_profile", "command", "installer", "secret_source"],
)
def test_nested_payload_rejects_section_13_5_authority_keys(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    policy = dict(config_document["product_policy"])
    policy[key] = "forbidden"
    config_document["product_policy"] = policy
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


def test_parse_project_config_rejects_nonempty_resources(config_document):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document["resources"] = [
        {
            "resource_id": "assurance.config.extra-note",
            "schema_id": "assurance.config.extra-note.v1",
            "media_type": "application/yaml",
            "content": "note",
        }
    ]
    with pytest.raises(ProjectConfigurationError, match="resources"):
        parse_project_config(config_document)


@pytest.mark.parametrize(
    "key", ["python", "entrypoint_group", "bindings", "task_handlers", "host", "resource_port"]
)
def test_project_config_rejects_ports_entry_points_and_handlers(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document[key] = "forbidden"
    with pytest.raises(ProjectConfigurationError):
        parse_project_config(config_document)


def test_config_tree_path_must_be_absolute(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError, load_project_configuration

    relative = Path("project-config")
    assert not relative.is_absolute()
    with pytest.raises(ProjectConfigurationError, match="absolute"):
        load_project_configuration(ConfigTreePluginSource(path=relative))


def test_extra_file_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "extra-file")
    (tree / "ambient.txt").write_text("undeclared\n", encoding="utf-8")
    with pytest.raises(ProjectConfigurationError, match="declared source file set|unknown"):
        _load(tree)


def test_python_file_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "python-file")
    (tree / "hook.py").write_text("def contribute():\n    return None\n", encoding="utf-8")
    with pytest.raises(ProjectConfigurationError, match="declared source file set|python|unknown"):
        _load(tree)


def test_entry_point_declaration_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "entry-point")
    document = load_yaml(tree / "plugin.yaml")
    document["entrypoint_group"] = "graph_engine.plugins"
    document["entrypoint_name"] = "configuration"
    _rewrite_yaml(tree / "plugin.yaml", document)
    with pytest.raises(ProjectConfigurationError):
        _load(tree)


def test_bindings_are_rejected_even_when_phase2_would_accept_them(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "bindings")
    document = load_yaml(tree / "plugin.yaml")
    document["bindings"] = [
        {
            "capability_id": "assurance.product.agent.intake.intake.prepare",
            "target_capability_id": "assurance.intake.intake.prepare",
            "data": None,
            "resource_ids": [],
        }
    ]
    _rewrite_yaml(tree / "plugin.yaml", document)
    with pytest.raises(ProjectConfigurationError, match="binding"):
        _load(tree)


def test_runtime_adapter_dependency_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "runtime-dep")
    document = load_yaml(tree / "plugin.yaml")
    dependencies = _object_list(document, "dependencies")
    dependencies.append({"plugin_id": "runtime.opencode", "version_specifier": "==0.1.0"})
    document["dependencies"] = dependencies
    _rewrite_yaml(tree / "plugin.yaml", document)
    with pytest.raises(ProjectConfigurationError, match="runtime|depend"):
        _load(tree)


def test_symlink_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "symlink")
    target = tmp_path / "outside-policy.yaml"
    target.write_text("schema_version: '1'\norganization: escaped\n", encoding="utf-8")
    linked = tree / ".aa" / "policy.yaml"
    linked.unlink()
    linked.symlink_to(target)
    with pytest.raises(ProjectConfigurationError, match="symlink|regular file|source"):
        _load(tree)


def test_sut_path_escape_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "escape")
    document = load_yaml(tree / "plugin.yaml")
    files = _object_list(document, "files")
    files[0] = {
        "kind": "resource",
        "resource_id": "assurance.product.configuration.product-policy",
        "path": "../secret.yaml",
        "media_type": "application/yaml",
    }
    document["files"] = files
    _rewrite_yaml(tree / "plugin.yaml", document)
    with pytest.raises(ProjectConfigurationError, match="unsafe|escape|canonical|invalid plugin"):
        _load(tree)


def test_source_mutation_during_reload_changes_bytes(tmp_path, config_tree):
    from assurance_product.configuration import load_project_configuration

    first = load_project_configuration(config_tree)
    mutated = _copy_tree(tmp_path / "mutated-source")
    knowledge = mutated / ".aa" / "data-knowledge.yaml"
    document = load_yaml(knowledge)
    document["notes"] = ["changed"]
    _rewrite_yaml(knowledge, document)
    envelope = load_yaml(mutated / ".aa" / "config.yaml")
    envelope["data_knowledge"] = document
    _rewrite_yaml(mutated / ".aa" / "config.yaml", envelope)
    second = _load(mutated)
    assert next(
        resource.content
        for resource in first.resources
        if resource.resource_id == "assurance.product.configuration.data-knowledge"
    ) != next(
        resource.content
        for resource in second.resources
        if resource.resource_id == "assurance.product.configuration.data-knowledge"
    )


def test_schema_drift_in_tree_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "schema-drift")
    envelope = load_yaml(tree / ".aa" / "config.yaml")
    envelope["schema_version"] = "2"
    _rewrite_yaml(tree / ".aa" / "config.yaml", envelope)
    with pytest.raises(ProjectConfigurationError):
        _load(tree)


def test_unknown_declared_resource_id_is_rejected(tmp_path):
    from assurance_product.configuration import ProjectConfigurationError

    tree = _copy_tree(tmp_path / "unknown-id")
    document = load_yaml(tree / "plugin.yaml")
    files = _object_list(document, "files")
    files.append(
        {
            "kind": "resource",
            "resource_id": "assurance.config.secret-route",
            "path": ".aa/extra.yaml",
            "media_type": "application/yaml",
        }
    )
    document["files"] = files
    _rewrite_yaml(tree / "plugin.yaml", document)
    (tree / ".aa" / "extra.yaml").write_text("value: data\n", encoding="utf-8")
    with pytest.raises(ProjectConfigurationError, match="unknown|resource"):
        _load(tree)


@pytest.mark.parametrize(
    "key",
    ["module", "distribution", "entrypoint", "path", "export", "capability", "schema", "implementation"],
)
def test_aa_cannot_select_module_or_schema_implementation(config_document, key):
    from assurance_product.configuration import ProjectConfigurationError, parse_project_config

    config_document[key] = "forged-source"
    with pytest.raises(ProjectConfigurationError, match="unknown configuration|runtime authority"):
        parse_project_config(config_document)
