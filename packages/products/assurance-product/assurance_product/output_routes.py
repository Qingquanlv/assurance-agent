from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_JOBS
from assurance_generation.contracts.attempts import OUTPUT_ROUTE_TEMPLATES as GENERATION_OUTPUTS
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_JOBS
from assurance_healing.contracts.attempts import OUTPUT_ROUTE_TEMPLATES as HEALING_OUTPUTS
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS as IMPROVEMENT_JOBS
from assurance_improvement.contracts.attempts import OUTPUT_ROUTE_TEMPLATES as IMPROVEMENT_OUTPUTS
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS as INTAKE_JOBS
from assurance_intake.contracts.attempts import OUTPUT_ROUTE_TEMPLATES as INTAKE_OUTPUTS
from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
from assurance_product.change_workspace import safe_change_id
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_JOBS
from assurance_quality.contracts.attempts import OUTPUT_ROUTE_TEMPLATES as QUALITY_OUTPUTS


def _route_templates() -> Mapping[str, tuple[str, ...]]:
    routes: dict[str, tuple[str, ...]] = {}
    for contracts, templates in (
        (INTAKE_JOBS, INTAKE_OUTPUTS),
        (GENERATION_JOBS, GENERATION_OUTPUTS),
        (QUALITY_JOBS, QUALITY_OUTPUTS),
        (HEALING_JOBS, HEALING_OUTPUTS),
        (IMPROVEMENT_JOBS, IMPROVEMENT_OUTPUTS),
    ):
        for base, paths in templates.items():
            routes[contracts[base].contract_id] = paths
    return MappingProxyType(routes)


_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = _route_templates()


class OutputRouteCatalog:
    """Closed installed-product map from semantic Agent contracts to exact logical outputs."""

    def aliases(self) -> tuple[str, ...]:
        expected = tuple(sorted(AGENT_EXECUTION_CONTRACTS))
        actual = tuple(sorted(_ROUTE_TEMPLATES))
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise ValueError(f"output route catalog drifted; missing={missing}, extra={extra}")
        return expected

    def outputs(self, contract_id: str, change_id: str) -> tuple[str, ...]:
        templates = _ROUTE_TEMPLATES.get(contract_id)
        if templates is None:
            raise ValueError(f"unknown capability output route: {contract_id}")
        token = safe_change_id(change_id)
        rendered = tuple(path.replace("{change_id}", token, 1) for path in templates)
        return tuple(path for path in rendered if "{" not in path and "}" not in path)

    def resource_claims(self, contract_id: str, change_id: str) -> tuple[str, ...]:
        """Return task-store claims; provider output admission remains exact."""

        from graph_engine.plugin_api import ResourceClaimTemplate

        contract = AGENT_EXECUTION_CONTRACTS.get(contract_id)
        if contract is None:
            raise ValueError(f"unknown capability output route: {contract_id}")
        if isinstance(contract.resources, ResourceClaimTemplate):
            return contract.resources.resolve({"change_id": change_id}).writes
        return contract.resources.writes


__all__ = [
    "OutputRouteCatalog",
]
