"""Assurance product: default catalog, resource root, and domain hooks."""

from __future__ import annotations

import importlib
from importlib.abc import Traversable
from importlib.resources import files

from assurance_agent.artifacts.registry import REGISTRY, ArtifactSpec
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.capability_state import (
    DEFAULT_PRODUCT_ID,
    CapabilityCatalog,
    CapabilityView,
)
from assurance_agent.workflow.graph.handlers.operation import OperationFn
from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS
from assurance_agent.workflow.graph.product_hooks import ProductHooks


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
        from assurance_agent.workflow.retro_outputs import (
            complete_candidate_outputs,
            complete_signal_outputs,
        )

        return ProductHooks(
            load_product_code_roots=load_product_code_roots,
            candidate_document_digest=candidate_document_digest,
            commit_healing_allocation_ledger=commit_healing_allocation_ledger,
            complete_issue_analyzer_outputs=complete_issue_analyzer_outputs,
            complete_improvement_reviewer_outputs=complete_improvement_reviewer_outputs,
            complete_signal_outputs=complete_signal_outputs,
            complete_candidate_outputs=complete_candidate_outputs,
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
            resolve_semantic_pin=resolve_semantic_pin,
        )


def resolve_semantic_pin(qualified_name: str) -> object:
    """Import a leftover ``assurance_agent.*`` digest pin from this wheel."""
    parts = qualified_name.split(".")
    last_error: Exception | None = None
    for i in range(len(parts), 0, -1):
        module_name = ".".join(parts[:i])
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
            continue
        obj: object = module
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            last_error = exc
            continue
        return obj
    raise ImportError(f"cannot resolve semantic dependency: {qualified_name}") from last_error
