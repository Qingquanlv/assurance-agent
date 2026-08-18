"""Install-time capability catalog: operation callables plus the graph-visible view.

Do not register at import time. Call ``ensure_default_catalog()`` before lookup
in production, or ``install_catalog`` / ``reset_catalog`` in tests.
"""

from __future__ import annotations

from collections.abc import Mapping

from assurance_agent.artifacts.registry import (
    REGISTRY,
    ArtifactSpec,
    install_artifact_table,
    reset_artifact_table,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.capability_state import (
    CapabilityCatalog,
    CapabilityCatalogError,
    CapabilityView,
    current_capability_view,
    install_capability_view,
    reset_capability_view,
)
from assurance_agent.workflow.graph.handlers.operation import OperationFn
from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS

_installed_operations: dict[str, OperationFn] | None = None


def build_default_catalog() -> tuple[CapabilityView, dict[str, OperationFn], tuple[ArtifactSpec, ...]]:
    operations = default_operations()
    artifacts = tuple(REGISTRY)
    builder = CapabilityCatalog()
    for name in operations:
        builder.register_operation(name)
    for validator_id in sorted(KNOWN_PRECOMMIT_VALIDATORS):
        builder.register_validator(validator_id)
    for spec in artifacts:
        builder.register_artifact(spec)
    return builder.freeze(), operations, artifacts


def install_catalog(
    view: CapabilityView,
    *,
    operations: Mapping[str, OperationFn],
    artifacts: tuple[ArtifactSpec, ...],
) -> None:
    if frozenset(operations) != view.operation_names:
        raise CapabilityCatalogError("operations map does not match capability view")
    install_artifact_table(artifacts)
    install_capability_view(view)
    global _installed_operations
    _installed_operations = dict(operations)


def reset_catalog() -> None:
    reset_artifact_table()
    reset_capability_view()
    global _installed_operations
    _installed_operations = None


def ensure_default_catalog() -> CapabilityView:
    existing = current_capability_view()
    if existing is not None:
        return existing
    view, operations, artifacts = build_default_catalog()
    install_catalog(view, operations=operations, artifacts=artifacts)
    return view


def current_operations() -> dict[str, OperationFn]:
    if _installed_operations is None:
        return default_operations()
    return dict(_installed_operations)
