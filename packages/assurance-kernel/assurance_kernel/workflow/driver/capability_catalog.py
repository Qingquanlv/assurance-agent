"""Install-time capability catalog: operation callables plus the graph-visible view.

Do not register at import time. Call ``ensure_default_catalog()`` before lookup
in production, or ``install_catalog`` / ``reset_catalog`` in tests.
"""

from __future__ import annotations

from collections.abc import Mapping

from assurance_kernel.artifacts.registry import (
    ArtifactSpec,
    install_artifact_table,
    reset_artifact_table,
)
from assurance_kernel.workflow.graph.capability_state import (
    CapabilityCatalogError,
    CapabilityView,
    current_capability_view,
    install_capability_view,
    reset_capability_view,
)
from assurance_kernel.workflow.graph.handlers.operation import OperationFn

_installed_operations: dict[str, OperationFn] | None = None


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
    raise CapabilityCatalogError("no capability catalog is installed; call select_product first")


def current_operations() -> dict[str, OperationFn]:
    if _installed_operations is None:
        return {}
    return dict(_installed_operations)
