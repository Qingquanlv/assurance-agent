"""Installed-product discovery. Do not register at import time."""

from __future__ import annotations

import re
from collections.abc import Iterator
from importlib.abc import Traversable
from importlib.metadata import EntryPoint, distributions
from typing import Protocol

from assurance_kernel.artifacts.registry import ArtifactSpec
from assurance_kernel.exceptions import AaError
from assurance_kernel.resources import set_product_resource_root
from assurance_kernel.workflow.driver.capability_catalog import (
    install_catalog,
    reset_catalog,
)
from assurance_kernel.workflow.graph.capability_state import (
    CapabilityView,
    install_current_product_id,
    reset_current_product_id,
)
from assurance_kernel.workflow.graph.handlers.operation import OperationFn
from assurance_kernel.workflow.graph.product_hooks import (
    ProductHooks,
    install_product_hooks,
    reset_product_hooks,
)

PRODUCT_ENTRY_GROUP = "assurance_agent.products"
_PRODUCT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ProductError(AaError):
    """Product discovery or selection failed."""


class Product(Protocol):
    id: str

    def resource_root(self) -> Traversable: ...

    def register(self) -> tuple[CapabilityView, dict[str, OperationFn], tuple[ArtifactSpec, ...]]: ...


def validate_product_id(product_id: str) -> str:
    if not _PRODUCT_ID_RE.fullmatch(product_id):
        raise ProductError(f"invalid product id: {product_id}")
    return product_id


def _entry_points_by_name() -> dict[str, list[tuple[EntryPoint, str]]]:
    found: dict[str, list[tuple[EntryPoint, str]]] = {}
    for dist in distributions():
        name = dist.metadata["Name"] if dist.metadata is not None else "unknown"
        for ep in dist.entry_points:
            if ep.group == PRODUCT_ENTRY_GROUP:
                found.setdefault(ep.name, []).append((ep, name))
    return found


def _entry_point_for(product_id: str) -> EntryPoint:
    matches = _entry_points_by_name().get(product_id, [])
    if len(matches) > 1:
        dists = ", ".join(sorted({item[1] for item in matches}))
        raise ProductError(f"duplicate product id {product_id!r} declared by: {dists}")
    if not matches:
        raise ProductError(f"unknown product: {product_id}")
    return matches[0][0]


def iter_product_entry_points() -> Iterator[tuple[str, str]]:
    """Yield ``(product_id, distribution_name)`` for uniquely installed products."""
    found = _entry_points_by_name()
    for product_id, matches in sorted(found.items()):
        if len(matches) > 1:
            dists = ", ".join(sorted({item[1] for item in matches}))
            raise ProductError(f"duplicate product id {product_id!r} declared by: {dists}")
        yield product_id, matches[0][1]


def _require_product_protocol(product: object, product_id: str) -> Product:
    if getattr(product, "id", None) != product_id:
        raise ProductError(f"product id mismatch: entry {product_id!r} != {getattr(product, 'id', None)!r}")
    if not callable(getattr(product, "resource_root", None)):
        raise ProductError(f"product {product_id!r} resource_root is not callable")
    if not callable(getattr(product, "register", None)):
        raise ProductError(f"product {product_id!r} register is not callable")
    return product  # type: ignore[return-value]


def load_product(product_id: str) -> Product:
    validate_product_id(product_id)
    ep = _entry_point_for(product_id)
    try:
        loaded = ep.load()
    except Exception as exc:
        raise ProductError(f"failed to load product {product_id}: {exc}") from exc
    try:
        product = loaded() if isinstance(loaded, type) else loaded
    except Exception as exc:
        raise ProductError(f"failed to construct product {product_id}: {exc}") from exc
    return _require_product_protocol(product, product_id)


def install_product(product: Product) -> None:
    try:
        install_current_product_id(product.id)
        set_product_resource_root(product.resource_root())
        view, operations, artifacts = product.register()
        install_catalog(view, operations=operations, artifacts=artifacts)
        hooks_fn = getattr(product, "product_hooks", None)
        if callable(hooks_fn):
            hooks = hooks_fn()
            if isinstance(hooks, ProductHooks):
                install_product_hooks(hooks)
                return
        reset_product_hooks()
    except Exception:
        reset_product()
        raise


def select_product(product_id: str) -> None:
    install_product(load_product(product_id))


def reset_product() -> None:
    set_product_resource_root(None)
    reset_current_product_id()
    reset_catalog()
    reset_product_hooks()
