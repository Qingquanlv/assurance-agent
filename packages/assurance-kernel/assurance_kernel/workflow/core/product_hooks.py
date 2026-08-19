"""Installed-product callables the graph kernel may invoke without importing domains.

Lives in ``workflow.core`` so both ``workflow.graph`` and ``workflow.orchestration``
can call the same process-global registry. ``workflow.graph.product_hooks`` re-exports
this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path

from assurance_kernel.exceptions import AaError


class ProductHooksMissing(AaError):
    """The installed product did not provide this hook."""


@dataclass(frozen=True, slots=True)
class ProductHooks:
    load_product_code_roots: Callable[[Path], list[str]]
    candidate_document_digest: Callable[..., str]
    commit_healing_allocation_ledger: Callable[..., bool]
    complete_issue_analyzer_outputs: Callable[..., None]
    complete_improvement_reviewer_outputs: Callable[..., object]
    register_healing_effects: Callable[..., None]
    project_healing_episode: Callable[..., object]
    assert_test_tree_unchanged_or_healing: Callable[..., object]
    assert_test_changes_override_allowed: Callable[..., object]
    build_test_changes_override_token: Callable[..., object]
    load_test_changes_override_policy: Callable[..., object]
    token_json_bytes: Callable[..., bytes]
    reconcile_healing_allocation: Callable[..., object]
    reconcile_fixer_proposal_approved: Callable[..., object]
    reconcile_heal_record_apply: Callable[..., object]


_hooks: ProductHooks | None = None


def _missing_hook(name: str) -> Callable[..., object]:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ProductHooksMissing(name)

    return _raise


def _fail_closed_hooks() -> ProductHooks:
    values = {field.name: _missing_hook(field.name) for field in fields(ProductHooks)}
    return ProductHooks(**values)  # type: ignore[arg-type]


_FAIL_CLOSED = _fail_closed_hooks()


def install_product_hooks(hooks: ProductHooks) -> None:
    global _hooks
    _hooks = hooks


def reset_product_hooks() -> None:
    global _hooks
    _hooks = None


def current_product_hooks() -> ProductHooks:
    if _hooks is None:
        return _FAIL_CLOSED
    return _hooks
