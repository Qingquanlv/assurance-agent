from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from assurance_execution.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as EXECUTION_OUTPUTS
from assurance_generation.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as GENERATION_OUTPUTS
from assurance_healing.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as HEALING_OUTPUTS
from assurance_improvement.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as IMPROVEMENT_OUTPUTS
from assurance_intake.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as INTAKE_OUTPUTS
from assurance_product.change_workspace import safe_change_id
from assurance_product.models import PREPARE_IDS
from assurance_quality.contracts.workflow import OUTPUT_ROUTE_TEMPLATES as QUALITY_OUTPUTS


def execute_alias_for_prepare(prepare_id: str) -> str:
    return prepare_id.removesuffix(".prepare") + ".execute"


def _route_templates() -> Mapping[str, tuple[str, ...]]:
    routes: dict[str, tuple[str, ...]] = {}
    for feature, templates in (
        ("intake", INTAKE_OUTPUTS),
        ("generation", GENERATION_OUTPUTS),
        ("execution", EXECUTION_OUTPUTS),
        ("quality", QUALITY_OUTPUTS),
        ("healing", HEALING_OUTPUTS),
        ("improvement", IMPROVEMENT_OUTPUTS),
    ):
        for base, paths in templates.items():
            routes[f"assurance.{feature}.{base}.execute"] = paths
    return MappingProxyType(routes)


_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = _route_templates()


class OutputRouteCatalog:
    """Closed installed-product map from execute aliases to exact logical outputs."""

    def aliases(self) -> tuple[str, ...]:
        expected = tuple(execute_alias_for_prepare(prepare_id) for prepare_id in PREPARE_IDS)
        actual = tuple(sorted(_ROUTE_TEMPLATES))
        if actual != tuple(sorted(expected)):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise ValueError(f"output route catalog drifted; missing={missing}, extra={extra}")
        return expected

    def outputs(self, capability_alias: str, change_id: str) -> tuple[str, ...]:
        templates = _ROUTE_TEMPLATES.get(capability_alias)
        if templates is None:
            raise ValueError(f"unknown capability output route: {capability_alias}")
        token = safe_change_id(change_id)
        return tuple(path.replace("{change_id}", token, 1) for path in templates)

    def resource_claims(self, capability_alias: str, change_id: str) -> tuple[str, ...]:
        """Return task-store claims; provider output admission remains exact."""

        from graph_engine.plugin_api import ResourceClaimTemplate

        from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

        prepare_id = capability_alias.removesuffix(".execute") + ".prepare"
        contract = AGENT_EXECUTION_CONTRACTS.get(prepare_id)
        if contract is None:
            raise ValueError(f"unknown capability output route: {capability_alias}")
        if isinstance(contract.resources, ResourceClaimTemplate):
            return contract.resources.resolve({"workspace": {"scope_id": change_id}}).writes
        return contract.resources.writes


__all__ = [
    "OutputRouteCatalog",
    "execute_alias_for_prepare",
]
