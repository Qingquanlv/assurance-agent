"""Six-provider registry closure helpers for Phase 4 Task 17."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TypeVar

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
    validate_contribution,
)

from assurance_execution.plugin import ExecutionPlugin
from assurance_generation.plugin import GenerationPlugin
from assurance_healing.plugin import HealingPlugin
from assurance_improvement.plugin import ImprovementPlugin
from assurance_intake.plugin import IntakePlugin
from assurance_quality.plugin import QualityPlugin
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms

_T = TypeVar("_T")

_PROVIDERS: tuple[tuple[PluginProvider, PluginExpectation], ...] = (
    (
        IntakePlugin(),
        PluginExpectation(plugin_id="assurance.intake", dependencies=(), id_prefix="assurance.intake."),
    ),
    (
        GenerationPlugin(),
        PluginExpectation(
            plugin_id="assurance.generation",
            dependencies=("assurance.intake",),
            id_prefix="assurance.generation.",
        ),
    ),
    (
        ExecutionPlugin(),
        PluginExpectation(
            plugin_id="assurance.execution",
            dependencies=("assurance.intake", "assurance.generation"),
            id_prefix="assurance.execution.",
        ),
    ),
    (
        HealingPlugin(),
        PluginExpectation(
            plugin_id="assurance.healing",
            dependencies=("assurance.intake", "assurance.generation", "assurance.execution"),
            id_prefix="assurance.healing.",
        ),
    ),
    (
        QualityPlugin(),
        PluginExpectation(
            plugin_id="assurance.quality",
            dependencies=(
                "assurance.intake",
                "assurance.generation",
                "assurance.execution",
                "assurance.healing",
            ),
            id_prefix="assurance.quality.",
        ),
    ),
    (
        ImprovementPlugin(),
        PluginExpectation(
            plugin_id="assurance.improvement",
            dependencies=(
                "assurance.intake",
                "assurance.generation",
                "assurance.execution",
                "assurance.healing",
                "assurance.quality",
            ),
            id_prefix="assurance.improvement.",
        ),
    ),
)


def all_six_provider_values() -> tuple[tuple[PluginDescriptor, ...], tuple[PluginContribution, ...]]:
    if ENGINE_API_VERSION != "2.0":
        raise AssertionError(f"Phase 4 wheels require ENGINE_API_VERSION 2.0, got {ENGINE_API_VERSION!r}")
    ports = RegistryPorts(engine_api=ENGINE_API_VERSION)
    descriptors: list[PluginDescriptor] = []
    contributions: list[PluginContribution] = []
    for provider, expected in _PROVIDERS:
        assert_plugin_conforms(provider, expected)
        descriptor = provider.descriptor()
        contribution = provider.contribute(ports)
        validate_contribution(descriptor, contribution)
        descriptors.append(descriptor)
        contributions.append(contribution)
    return tuple(descriptors), tuple(contributions)


def contribution_ids(contributions: Sequence[PluginContribution]) -> tuple[str, ...]:
    ids: list[str] = []
    for contribution in contributions:
        ids.extend(contribution.task_handlers)
        ids.extend(contribution.commit_validators)
        ids.extend(entry.schema_id for entry in contribution.schemas)
        ids.extend(entry.resource_id for entry in contribution.resources)
        ids.extend(entry.kind for entry in contribution.effects)
        ids.extend(entry.capability_id for entry in contribution.bindings)
    return tuple(ids)


def every_schema_resource_and_effect_reference_resolves(
    descriptors: Sequence[PluginDescriptor],
    contributions: Sequence[PluginContribution],
) -> bool:
    if len(descriptors) != len(contributions):
        raise AssertionError("descriptor and contribution counts must match")
    schemas = _unique_map(
        ((entry.schema_id, entry) for contribution in contributions for entry in contribution.schemas),
        "schema",
    )
    resources = _unique_map(
        ((entry.resource_id, entry) for contribution in contributions for entry in contribution.resources),
        "resource",
    )
    for descriptor, contribution in zip(descriptors, contributions, strict=True):
        prefix = f"{descriptor.plugin_id}."
        contributed = {
            "task handler": tuple(contribution.task_handlers),
            "commit validator": tuple(contribution.commit_validators),
            "schema": tuple(entry.schema_id for entry in contribution.schemas),
            "resource": tuple(entry.resource_id for entry in contribution.resources),
            "effect": tuple(entry.kind for entry in contribution.effects),
            "binding": tuple(entry.capability_id for entry in contribution.bindings),
        }
        declared = {
            "task handler": descriptor.task_handlers,
            "commit validator": descriptor.commit_validators,
            "schema": descriptor.schemas,
            "resource": descriptor.resources,
            "effect": descriptor.effects,
            "binding": descriptor.bindings,
        }
        for kind, ids in contributed.items():
            if set(ids) != set(declared[kind]):
                raise AssertionError(f"{descriptor.plugin_id} {kind} membership disagrees")
            if any(not item.startswith(prefix) for item in ids):
                raise AssertionError(f"{descriptor.plugin_id} {kind} id is outside the owner prefix")
        if descriptor.bindings or contribution.bindings:
            raise AssertionError(f"{descriptor.plugin_id} must not contribute binding targets")
        for effect in contribution.effects:
            if effect.intent_schema_id not in schemas:
                raise AssertionError(f"unresolved effect intent schema: {effect.intent_schema_id}")
            if effect.receipt_schema_id not in schemas:
                raise AssertionError(f"unresolved effect receipt schema: {effect.receipt_schema_id}")
            if not effect.kind.startswith(prefix):
                raise AssertionError(f"effect kind is outside the owner prefix: {effect.kind}")
        for resource in contribution.resources:
            if resource.resource_id not in resources:
                raise AssertionError(f"unresolved resource: {resource.resource_id}")
    return True


def _unique_map(pairs: Iterable[tuple[str, _T]], kind: str) -> Mapping[str, _T]:
    mapping: dict[str, _T] = {}
    for key, value in pairs:
        previous = mapping.get(key)
        if previous is not None and previous is not value:
            raise AssertionError(f"duplicate {kind} id: {key}")
        mapping[key] = value
    return mapping


__all__ = [
    "all_six_provider_values",
    "contribution_ids",
    "every_schema_resource_and_effect_reference_resolves",
]
