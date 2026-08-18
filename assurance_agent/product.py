"""Installed-product discovery. Do not register at import time."""

from __future__ import annotations

import re
from importlib.abc import Traversable
from importlib.metadata import EntryPoint, distributions
from importlib.resources import files

from assurance_agent.artifacts.registry import ArtifactSpec
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.driver.capability_catalog import build_default_catalog
from assurance_agent.workflow.graph.capability_state import CapabilityView
from assurance_agent.workflow.graph.handlers.operation import OperationFn

PRODUCT_ENTRY_GROUP = "assurance_agent.products"
_PRODUCT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ProductError(AaError):
    """Product discovery or selection failed."""


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


class AssuranceProduct:
    id = "assurance"

    def resource_root(self) -> Traversable:
        return files("assurance_agent") / "_resources"

    def register(self) -> tuple[CapabilityView, dict[str, OperationFn], tuple[ArtifactSpec, ...]]:
        return build_default_catalog()


def load_product(product_id: str) -> AssuranceProduct:
    validate_product_id(product_id)
    ep = _entry_point_for(product_id)
    loaded = ep.load()
    product = loaded() if isinstance(loaded, type) else loaded
    if getattr(product, "id", None) != product_id:
        raise ProductError(f"product id mismatch: entry {product_id!r} != {getattr(product, 'id', None)!r}")
    return product
