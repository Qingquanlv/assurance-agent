"""Live contribute() proof for migrate operation and artifact ledger rows."""

from __future__ import annotations

import importlib
import re
from functools import lru_cache

import pytest
from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginContribution

from assurance_execution.plugin import ExecutionPlugin
from assurance_generation.plugin import GenerationPlugin
from assurance_healing.plugin import HealingPlugin
from assurance_improvement.plugin import ImprovementPlugin
from assurance_intake.plugin import IntakePlugin
from assurance_quality.plugin import QualityPlugin
from tests.capabilities.ownership import OWNERSHIP_PATH, OwnershipItem, load_ownership_ledger

_OWNER_PLUGINS = {
    "assurance.intake": IntakePlugin,
    "assurance.generation": GenerationPlugin,
    "assurance.execution": ExecutionPlugin,
    "assurance.healing": HealingPlugin,
    "assurance.quality": QualityPlugin,
    "assurance.improvement": ImprovementPlugin,
}

_CONTRACT_MODULES = {
    "assurance.intake": "assurance_intake.contracts",
    "assurance.generation": "assurance_generation.contracts",
    "assurance.execution": "assurance_execution.contracts",
    "assurance.healing": "assurance_healing.contracts",
    "assurance.quality": "assurance_quality.contracts",
    "assurance.improvement": "assurance_improvement.contracts",
}

_VERSION_SUFFIX = re.compile(r"-v\d+$")


def _migrate_items(kind: str) -> tuple[OwnershipItem, ...]:
    ledger = load_ownership_ledger(OWNERSHIP_PATH)
    return tuple(item for item in ledger.items if item.kind == kind and item.disposition == "migrate")


def _kebab(value: str) -> str:
    stepped = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    stepped = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", stepped)
    return stepped.replace("_", "-").replace(".", "-").lower()


def _stems(value: str) -> frozenset[str]:
    kebab = _kebab(value)
    stems = {kebab, _VERSION_SUFFIX.sub("", kebab)}
    if kebab.endswith("-document"):
        stems.add(kebab[: -len("-document")])
    return frozenset(stem for stem in stems if stem)


@lru_cache(maxsize=None)
def _live_contribution(owner: str) -> PluginContribution:
    plugin = _OWNER_PLUGINS[owner]()
    return plugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))


def live_handler_ids(owner: str) -> frozenset[str]:
    return frozenset(_live_contribution(owner).task_handlers)


def live_schema_ids(owner: str) -> frozenset[str]:
    return frozenset(entry.schema_id for entry in _live_contribution(owner).schemas)


@lru_cache(maxsize=None)
def live_export_names(owner: str) -> frozenset[str]:
    module = importlib.import_module(_CONTRACT_MODULES[owner])
    names: set[str] = set(getattr(module, "__all__", ()))
    names.update(name for name in dir(module) if name[:1].isupper() and not name.startswith("_"))
    return frozenset(names)


def _hyphen_tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in _kebab(value).split("-") if token and not re.fullmatch(r"v\d+", token))


def artifact_type_is_live(artifact_type: str, owner: str) -> bool:
    type_stems = _stems(artifact_type)
    type_tokens = _hyphen_tokens(artifact_type)
    for schema_id in live_schema_ids(owner):
        schema_kebab = _kebab(schema_id)
        schema_tokens = set(_hyphen_tokens(schema_id))
        if any(stem in schema_kebab for stem in type_stems if len(stem) >= 8 or "-" in stem):
            return True
        if len(type_tokens) == 1 and type_tokens[0] in schema_tokens and len(type_tokens[0]) >= 4:
            return True
        local = schema_id.rsplit(".", 2)
        local_name = _VERSION_SUFFIX.sub("", _kebab(local[-2] if local[-1].startswith("v") else local[-1]))
        if "-" in local_name:
            for stem in type_stems:
                if stem == local_name or stem.startswith(f"{local_name}-"):
                    return True
    for export_name in live_export_names(owner):
        export_stems = _stems(export_name)
        if type_stems & export_stems:
            return True
        for type_stem in type_stems:
            for export_stem in export_stems:
                if type_stem == export_stem:
                    return True
                if len(export_stem) >= 10 and type_stem.endswith(f"-{export_stem}"):
                    return True
                if len(type_stem) >= 10 and export_stem.endswith(f"-{type_stem}"):
                    return True
    return False


_MIGRATE_OPERATIONS = _migrate_items("operation")
_MIGRATE_ARTIFACTS = _migrate_items("artifact")


@pytest.mark.parametrize(
    "legacy_id",
    [item.legacy_id for item in _MIGRATE_OPERATIONS],
    ids=[item.legacy_id for item in _MIGRATE_OPERATIONS],
)
def test_migrate_operation_is_live_handler(legacy_id: str) -> None:
    item = next(row for row in _MIGRATE_OPERATIONS if row.legacy_id == legacy_id)
    assert item.owner is not None
    assert item.new_id is not None
    assert item.new_id in live_handler_ids(item.owner), (item.legacy_id, item.new_id, item.owner)


@pytest.mark.parametrize(
    "legacy_id",
    [item.legacy_id for item in _MIGRATE_ARTIFACTS],
    ids=[item.legacy_id for item in _MIGRATE_ARTIFACTS],
)
def test_migrate_artifact_is_live_contract(legacy_id: str) -> None:
    item = next(row for row in _MIGRATE_ARTIFACTS if row.legacy_id == legacy_id)
    assert item.owner is not None
    assert artifact_type_is_live(item.legacy_id, item.owner), (item.legacy_id, item.owner)
