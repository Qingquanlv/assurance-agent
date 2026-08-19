"""Installed-product discovery. Do not register at import time."""

from __future__ import annotations

import re
from importlib.abc import Traversable
from importlib.metadata import EntryPoint, distributions
from importlib.resources import files
from typing import Protocol

from assurance_agent.artifacts.registry import ArtifactSpec
from assurance_agent.exceptions import AaError
from assurance_agent.resources import set_product_resource_root
from assurance_agent.workflow.driver.capability_catalog import (
    build_default_catalog,
    install_catalog,
    reset_catalog,
)
from assurance_agent.workflow.graph.capability_state import (
    DEFAULT_PRODUCT_ID,
    CapabilityView,
    install_current_product_id,
    reset_current_product_id,
)
from assurance_agent.workflow.graph.handlers.operation import OperationFn
from assurance_agent.workflow.graph.product_hooks import (
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


class AssuranceProduct:
    id = DEFAULT_PRODUCT_ID

    def resource_root(self) -> Traversable:
        return files("assurance_agent") / "_resources"

    def register(self) -> tuple[CapabilityView, dict[str, OperationFn], tuple[ArtifactSpec, ...]]:
        return build_default_catalog()

    def product_hooks(self) -> ProductHooks:
        from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger
        from assurance_agent.workflow.healing.effects import (
            reconcile_fixer_proposal_approved,
            reconcile_heal_record_apply,
            reconcile_healing_allocation,
            register_healing_effects,
        )
        from assurance_agent.workflow.healing.override_policy import (
            assert_test_changes_override_allowed,
            build_test_changes_override_token,
            load_test_changes_override_policy,
            token_json_bytes,
        )
        from assurance_agent.workflow.healing.projection import project_healing_episode
        from assurance_agent.workflow.healing.safety import (
            assert_test_tree_unchanged_or_healing,
            load_product_code_roots,
        )
        from assurance_agent.workflow.improvements.reviewer_output import (
            complete_improvement_reviewer_outputs,
        )
        from assurance_agent.workflow.issues.analyzer_output import complete_issue_analyzer_outputs
        from assurance_agent.workflow.issues.identity import candidate_document_digest

        return ProductHooks(
            load_product_code_roots=load_product_code_roots,
            candidate_document_digest=candidate_document_digest,
            commit_healing_allocation_ledger=commit_healing_allocation_ledger,
            complete_issue_analyzer_outputs=complete_issue_analyzer_outputs,
            complete_improvement_reviewer_outputs=complete_improvement_reviewer_outputs,
            register_healing_effects=register_healing_effects,
            project_healing_episode=project_healing_episode,
            assert_test_tree_unchanged_or_healing=assert_test_tree_unchanged_or_healing,
            assert_test_changes_override_allowed=assert_test_changes_override_allowed,
            build_test_changes_override_token=build_test_changes_override_token,
            load_test_changes_override_policy=load_test_changes_override_policy,
            token_json_bytes=token_json_bytes,
            reconcile_healing_allocation=reconcile_healing_allocation,
            reconcile_fixer_proposal_approved=reconcile_fixer_proposal_approved,
            reconcile_heal_record_apply=reconcile_heal_record_apply,
        )


def load_product(product_id: str) -> Product:
    validate_product_id(product_id)
    ep = _entry_point_for(product_id)
    loaded = ep.load()
    product = loaded() if isinstance(loaded, type) else loaded
    if getattr(product, "id", None) != product_id:
        raise ProductError(f"product id mismatch: entry {product_id!r} != {getattr(product, 'id', None)!r}")
    return product


def install_product(product: Product) -> None:
    view, operations, artifacts = product.register()
    try:
        install_current_product_id(product.id)
        set_product_resource_root(product.resource_root())
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
