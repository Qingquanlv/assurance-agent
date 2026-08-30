from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import ValidationError
import yaml

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    ConfigTreePluginSource,
    DeclarativePlugin,
    DeclarativePluginRejected,
    load_config_tree,
)
from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.plugin_api import PluginContribution, ResourceContribution

from assurance_product.models import (
    CONFIGURATION_PLUGIN_ID,
    CONFIGURATION_PLUGIN_VERSION,
    ENGINE_API,
    ProjectConfigV1,
)

ConfigTree = ConfigTreePluginSource

_PLUGIN_MANIFEST = "plugin.yaml"
_CONFIG_PATH = ".aa/config.yaml"
_POLICY_PATH = ".aa/policy.yaml"
_KNOWLEDGE_PATH = ".aa/data-knowledge.yaml"
_CATALOG_PATH = ".aa/capability-catalog.json"
_ENVELOPE_RESOURCE_ID = "assurance.product.configuration.project-config"
_POLICY_RESOURCE_ID = "assurance.product.configuration.product-policy"
_KNOWLEDGE_RESOURCE_ID = "assurance.product.configuration.data-knowledge"
_CATALOG_RESOURCE_ID = "assurance.product.configuration.capability-catalog"
_NODE_POLICY_RESOURCE_ID = "assurance.product.configuration.node-policy-values"
_REQUIRED_FILES = frozenset({_PLUGIN_MANIFEST, _CONFIG_PATH, _POLICY_PATH, _KNOWLEDGE_PATH, _CATALOG_PATH})
_ALLOWED_DECLARED_RESOURCE_IDS = frozenset(
    {_POLICY_RESOURCE_ID, _KNOWLEDGE_RESOURCE_ID, _CATALOG_RESOURCE_ID, _ENVELOPE_RESOURCE_ID}
)
_REQUIRED_DEPENDENCIES = (
    ("assurance.intake", "==0.2.0"),
    ("assurance.generation", "==0.2.0"),
    ("assurance.execution", "==0.2.0"),
    ("assurance.healing", "==0.2.0"),
    ("assurance.quality", "==0.2.0"),
    ("assurance.improvement", "==0.2.0"),
)
_AUTHORITY_KEYS = frozenset(
    {
        "adapter",
        "binding",
        "bindings",
        "callable",
        "command",
        "commit_validators",
        "capability",
        "distribution",
        "endpoint",
        "entrypoint",
        "export",
        "implementation",
        "entrypoint_group",
        "entrypoint_name",
        "entrypoint_value",
        "executable",
        "handler",
        "handlers",
        "host",
        "import",
        "installer",
        "model",
        "module",
        "path",
        "permission_grant",
        "permission_profile",
        "python",
        "resource_port",
        "schema",
        "secret",
        "secret_handle",
        "secret_source",
        "secret_value",
        "task_handlers",
        "worker_profile",
    }
)


class ProjectConfigurationError(Exception):
    """Raised when a project configuration tree is not closed business data."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _flatten_constraint_keys(constraints: Mapping[str, object]) -> tuple[str, ...]:
    keys: list[str] = []
    for key, value in constraints.items():
        if isinstance(value, Mapping):
            for attribute in value:
                suffix = "has_max_length" if attribute == "max_length" else str(attribute)
                keys.append(f"{key}_{suffix}")
        else:
            keys.append(str(key))
    return tuple(keys)


def capability_leafs_from_knowledge(document: Mapping[str, object]) -> tuple[str, ...]:
    """Return the exact closed L1 typed-leaf catalog for one knowledge document."""

    leafs: set[str] = set()
    for root in ("accounts", "auth", "entities", "auth_matrix"):
        for name, value in _mapping(document.get(root)).items():
            if isinstance(value, Mapping):
                leafs.add(f"{root}.{name}")
                if root == "entities":
                    constraints = _mapping(value.get("constraints"))
                    leafs.update(
                        f"entities.{name}.constraints.{key}" for key in _flatten_constraint_keys(constraints)
                    )
    capabilities = _mapping(document.get("capabilities"))
    for module, names in _mapping(capabilities.get("domain_factories")).items():
        for name, value in _mapping(names).items():
            if isinstance(value, Mapping):
                leafs.add(f"capabilities.domain_factories.{module}.{name}")
    for layer, modules in _mapping(capabilities.get("adapters")).items():
        for module, names in _mapping(modules).items():
            for name, value in _mapping(names).items():
                if isinstance(value, Mapping):
                    leafs.add(f"capabilities.adapters.{layer}.{module}.{name}")
    for name, value in _mapping(capabilities.get("cleanup")).items():
        if isinstance(value, Mapping):
            leafs.add(f"capabilities.cleanup.{name}")
    return tuple(sorted(leafs))


def parse_project_config(document: Mapping[str, object] | object) -> ProjectConfigV1:
    if not isinstance(document, Mapping):
        raise ProjectConfigurationError("project configuration must be a mapping")
    mapping = cast(Mapping[str, object], document)
    _reject_authority_keys(mapping, "project configuration")
    try:
        parsed = ProjectConfigV1.model_validate(mapping)
    except ValidationError as error:
        if any(item.get("type") == "extra_forbidden" for item in error.errors()):
            extras = [str(item.get("loc", ("unknown",))[-1]) for item in error.errors()]
            raise ProjectConfigurationError(f"unknown configuration: {', '.join(extras)}") from error
        raise ProjectConfigurationError(str(error)) from error
    if parsed.resources:
        raise ProjectConfigurationError("project configuration resources must be empty")
    _reject_authority_keys(thaw_json(parsed.product_policy), "product policy")
    _reject_authority_keys(thaw_json(parsed.data_knowledge), "data knowledge")
    _reject_authority_keys(thaw_json(parsed.capability_catalog), "capability catalog")
    _reject_authority_keys(thaw_json(dict(parsed.node_policy_values)), "node policy values")
    return parsed


def load_project_configuration(tree: ConfigTree) -> PluginContribution:
    source = _config_tree_source(tree)
    if not source.path.is_absolute():
        raise ProjectConfigurationError("config tree path must be absolute")
    try:
        loaded = load_config_tree(source)
    except DeclarativePluginRejected as error:
        raise ProjectConfigurationError(str(error)) from error
    _authenticate_configuration_plugin(loaded)
    files = {item.path: item.content for item in loaded.snapshot.files}
    if set(files) != _REQUIRED_FILES:
        raise ProjectConfigurationError("unknown or missing project configuration files")
    parsed = parse_project_config(_yaml_mapping(files[_CONFIG_PATH], "project configuration"))
    policy = _yaml_mapping(files[_POLICY_PATH], "product policy")
    knowledge = _yaml_mapping(files[_KNOWLEDGE_PATH], "data knowledge")
    catalog_source = _yaml_mapping(files[_CATALOG_PATH], "capability catalog")
    _reject_authority_keys(policy, "product policy")
    _reject_authority_keys(knowledge, "data knowledge")
    if thaw_json(freeze_json(policy)) != thaw_json(parsed.product_policy):
        raise ProjectConfigurationError("product policy file disagrees with project configuration")
    if thaw_json(freeze_json(knowledge)) != thaw_json(parsed.data_knowledge):
        raise ProjectConfigurationError("data knowledge file disagrees with project configuration")
    catalog = {
        "schema_version": "1",
        "typed_leafs": list(capability_leafs_from_knowledge(knowledge)),
    }
    if catalog_source != catalog:
        raise ProjectConfigurationError("capability catalog file disagrees with typed knowledge leaves")
    if thaw_json(parsed.capability_catalog) != catalog:
        raise ProjectConfigurationError("capability catalog file disagrees with project configuration")
    return PluginContribution(
        resources=(
            ResourceContribution(
                resource_id=_POLICY_RESOURCE_ID,
                media_type="application/yaml",
                content=files[_POLICY_PATH],
            ),
            ResourceContribution(
                resource_id=_KNOWLEDGE_RESOURCE_ID,
                media_type="application/yaml",
                content=files[_KNOWLEDGE_PATH],
            ),
            ResourceContribution(
                resource_id=_CATALOG_RESOURCE_ID,
                media_type="application/json",
                content=files[_CATALOG_PATH],
            ),
            ResourceContribution(
                resource_id=_NODE_POLICY_RESOURCE_ID,
                media_type="application/json",
                content=canonical_json_bytes(thaw_json(dict(parsed.node_policy_values))),
            ),
        )
    )


def _config_tree_source(tree: ConfigTree) -> ConfigTreePluginSource:
    if isinstance(tree, ConfigTreePluginSource):
        return tree
    raise ProjectConfigurationError("project configuration requires an explicit ConfigTree path")


def _authenticate_configuration_plugin(loaded: DeclarativePlugin) -> None:
    descriptor = loaded.descriptor
    if (
        descriptor.plugin_id != CONFIGURATION_PLUGIN_ID
        or descriptor.plugin_version != CONFIGURATION_PLUGIN_VERSION
        or descriptor.engine_api != ENGINE_API
    ):
        raise ProjectConfigurationError("project configuration plugin identity is not closed")
    if loaded.contribution.bindings:
        raise ProjectConfigurationError("project configuration must not contribute bindings")
    if loaded.contribution.task_handlers:
        raise ProjectConfigurationError("project configuration must not contribute handlers")
    if loaded.contribution.commit_validators:
        raise ProjectConfigurationError("project configuration must not contribute validators")
    if loaded.contribution.effects:
        raise ProjectConfigurationError("project configuration must not contribute effects")
    declared_ids = {item.resource_id for item in loaded.document.files}
    unknown = declared_ids - _ALLOWED_DECLARED_RESOURCE_IDS
    if unknown:
        raise ProjectConfigurationError(f"unknown project configuration resource: {min(unknown)}")
    missing = _ALLOWED_DECLARED_RESOURCE_IDS - declared_ids
    if missing:
        raise ProjectConfigurationError(f"missing project configuration resource: {min(missing)}")
    actual_dependencies = tuple((item.plugin_id, item.version_specifier) for item in descriptor.dependencies)
    if actual_dependencies != _REQUIRED_DEPENDENCIES:
        raise ProjectConfigurationError(
            "project configuration dependencies must be the six capability plugins"
        )


def _yaml_mapping(content: bytes, label: str) -> dict[str, object]:
    try:
        raw = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ProjectConfigurationError(f"{label} is not safe YAML") from error
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ProjectConfigurationError(f"{label} must be a string-keyed mapping")
    return cast(dict[str, object], raw)


def _reject_authority_keys(value: object, label: str, seen: set[int] | None = None) -> None:
    seen = set() if seen is None else seen
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ProjectConfigurationError(f"{label} contains aliases or cycles")
        seen.add(identity)
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in _AUTHORITY_KEYS:
                raise ProjectConfigurationError(f"{label} contains unknown configuration: {key}")
            _reject_authority_keys(child, label, seen)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise ProjectConfigurationError(f"{label} contains aliases or cycles")
        seen.add(identity)
        for child in value:
            _reject_authority_keys(child, label, seen)
