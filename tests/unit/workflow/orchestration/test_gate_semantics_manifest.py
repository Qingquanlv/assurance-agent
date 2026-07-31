"""Gate semantics manifest coverage, stability, and digest sensitivity."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from assurance_agent.artifacts.models.plan_checks import CheckEvidence
from assurance_agent.artifacts.models.review import Review
from assurance_agent.workflow.orchestration.gate_semantics import (
    build_gate_semantics_manifest,
    discover_replay_semantic_dependencies,
    gate_semantics_digest,
    normalized_ast_digest,
    symbol_implementation_digest,
)


def test_discover_replay_dependencies_match_manifest_symbols() -> None:
    manifest = build_gate_semantics_manifest()
    assert discover_replay_semantic_dependencies() == {symbol.qualified_name for symbol in manifest.symbols}


def test_repeated_gate_semantics_construction_is_identical() -> None:
    first = build_gate_semantics_manifest()
    second = build_gate_semantics_manifest()
    assert first.digest == second.digest
    assert gate_semantics_digest() == first.digest


def test_runtime_versions_are_present() -> None:
    manifest = build_gate_semantics_manifest()
    assert set(manifest.runtime_versions) >= {"python", "pydantic", "pydantic_core", "pyyaml"}


@pytest.mark.parametrize(
    "qualified_name",
    sorted(
        {
            "assurance_agent.workflow.orchestration.dsl._eval_call",
            "assurance_agent.verification.gate_state.plan_assurance_state",
            "assurance_agent.knowledge.capabilities.plan_review_route",
            "assurance_agent.workflow.orchestration.gates._latest_graph_gate_decision",
            "assurance_agent.workflow.orchestration.gates._apply_gate_decision",
            "assurance_agent.workflow.orchestration.gates._decision_matches_source_epoch",
            "assurance_agent.workflow.orchestration.gates.resolve_checkpoint_gate_id",
        }
    ),
)
def test_ast_branch_mutation_changes_symbol_and_aggregate_digest(qualified_name: str) -> None:
    baseline_manifest = build_gate_semantics_manifest()
    baseline_symbol = next(s for s in baseline_manifest.symbols if s.qualified_name == qualified_name)
    original_source = inspect.getsource(_resolve(qualified_name))
    mutated_source = original_source.replace("return ", "return  # mutated\n        return ", 1)
    assert mutated_source != original_source

    mutated_manifest = build_gate_semantics_manifest(source_overrides={qualified_name: mutated_source})
    mutated_symbol = next(s for s in mutated_manifest.symbols if s.qualified_name == qualified_name)

    assert mutated_symbol.implementation_digest != baseline_symbol.implementation_digest
    assert mutated_symbol.semantic_version == baseline_symbol.semantic_version
    assert mutated_manifest.digest != baseline_manifest.digest
    assert gate_semantics_digest() == baseline_manifest.digest


@pytest.mark.parametrize(
    ("qualified_name", "mutator"),
    [
        (
            "assurance_agent.workflow.orchestration.dsl.BUILTIN_ARITY",
            lambda value: {**dict(value), "mutated_probe": 0},
        ),
        (
            "assurance_agent.workflow.orchestration.gates.CHECKPOINT_GATE_ALIASES",
            lambda value: {**dict(value), "mutated.checkpoint": "mutated-gate"},
        ),
        (
            "assurance_agent.artifacts.models.review._PLAN_REVIEW_TYPES",
            lambda value: frozenset({*value, "mutated-plan"}),
        ),
        (
            "assurance_agent.artifacts.models.review._HUMAN_ONLY_PLAN_REVIEW_TYPES",
            lambda value: frozenset({*value, "mutated-human-only"}),
        ),
    ],
)
def test_constant_mutation_changes_symbol_and_aggregate_digest(
    qualified_name: str,
    mutator: Callable[[Any], Any],
) -> None:
    baseline_manifest = build_gate_semantics_manifest()
    baseline_symbol = next(s for s in baseline_manifest.symbols if s.qualified_name == qualified_name)
    mutated = mutator(_resolve(qualified_name))

    mutated_manifest = build_gate_semantics_manifest(constant_overrides={qualified_name: mutated})
    mutated_symbol = next(s for s in mutated_manifest.symbols if s.qualified_name == qualified_name)

    assert mutated_symbol.implementation_digest != baseline_symbol.implementation_digest
    assert mutated_symbol.semantic_version == baseline_symbol.semantic_version
    assert mutated_manifest.digest != baseline_manifest.digest


def test_model_validator_mode_mutation_changes_digests() -> None:
    qualified_name = f"{Review.__module__}.{Review.__qualname__}"
    original = inspect.getsource(Review)
    mutated = original.replace('mode="after"', 'mode="before"', 1)
    assert mutated != original

    baseline = symbol_implementation_digest(qualified_name)
    patched = symbol_implementation_digest(qualified_name, source_override=mutated)
    assert patched != baseline

    baseline_manifest = build_gate_semantics_manifest()
    mutated_manifest = build_gate_semantics_manifest(source_overrides={qualified_name: mutated})
    assert mutated_manifest.digest != baseline_manifest.digest


def test_field_validator_field_list_mutation_changes_digests() -> None:
    qualified_name = f"{CheckEvidence.__module__}.{CheckEvidence.__qualname__}"
    original = inspect.getsource(CheckEvidence)
    injected = original.replace(
        "    check_id: str\n",
        (
            "    check_id: str\n\n"
            '    @field_validator("check_id")\n'
            "    @classmethod\n"
            "    def _validate_check_id(cls, value: str) -> str:\n"
            "        return value\n"
        ),
        1,
    )
    mutated = injected.replace('@field_validator("check_id")', '@field_validator("status")', 1)
    assert normalized_ast_digest(mutated) != normalized_ast_digest(injected)

    _assert_source_override_changes_digests(qualified_name, lambda src: mutated)


_PLAN_CHECK_POLICY_BRANCHES: tuple[tuple[str, str, Callable[[str], str]], ...] = (
    (
        "assurance_agent.verification.checks.registry.validate_plan_check_document",
        "layer_not_applicable",
        lambda src: src.replace('"layer_not_applicable"', '"layer_not_applicable_probe"', 1),
    ),
    (
        "assurance_agent.verification.checks.registry.validate_plan_check_document",
        "check_not_in_profile",
        lambda src: src.replace('"check_not_in_profile"', '"check_not_in_profile_probe"', 1),
    ),
    (
        "assurance_agent.verification.checks.registry.validate_plan_check_document",
        "applicable_check_enforcement",
        lambda src: src.replace('check.status == "not_applicable"', 'check.status != "not_applicable"', 1),
    ),
    (
        "assurance_agent.artifacts.models.plan_checks.PlanCheckDocument",
        "layer_not_applicable",
        lambda src: src.replace('"layer_not_applicable"', '"layer_not_applicable_probe"', 1),
    ),
    (
        "assurance_agent.artifacts.models.plan_checks.PlanCheckDocument",
        "inapplicable_layer_status",
        lambda src: src.replace('expected_status = "not_applicable"', 'expected_status = "pass"', 1),
    ),
    (
        "assurance_agent.artifacts.models.plan_checks.CheckEvidence",
        "not_applicable_reason_required",
        lambda src: src.replace(
            'self.status == "not_applicable" and self.applicability_reason is None',
            'self.status == "not_applicable" and self.applicability_reason is not None',
            1,
        ),
    ),
)


@pytest.mark.parametrize(
    ("qualified_name", "branch_label", "mutator"),
    _PLAN_CHECK_POLICY_BRANCHES,
    ids=[f"{name.rpartition('.')[2]}:{branch}" for name, branch, _ in _PLAN_CHECK_POLICY_BRANCHES],
)
def test_plan_check_policy_branch_mutation_changes_digests(
    qualified_name: str,
    branch_label: str,
    mutator: Callable[[str], str],
) -> None:
    original_source = inspect.getsource(_resolve(qualified_name))
    mutated_source = mutator(original_source)
    assert mutated_source != original_source, branch_label
    _assert_source_override_changes_digests(qualified_name, lambda _: mutated_source)


def _assert_source_override_changes_digests(
    qualified_name: str,
    source_mutator: Callable[[str], str],
) -> None:
    baseline_manifest = build_gate_semantics_manifest()
    baseline_symbol = next(s for s in baseline_manifest.symbols if s.qualified_name == qualified_name)
    original_source = inspect.getsource(_resolve(qualified_name))
    mutated_source = source_mutator(original_source)
    assert mutated_source != original_source

    mutated_manifest = build_gate_semantics_manifest(source_overrides={qualified_name: mutated_source})
    mutated_symbol = next(s for s in mutated_manifest.symbols if s.qualified_name == qualified_name)

    assert mutated_symbol.implementation_digest != baseline_symbol.implementation_digest
    assert mutated_symbol.semantic_version == baseline_symbol.semantic_version
    assert mutated_manifest.digest != baseline_manifest.digest
    assert gate_semantics_digest() == baseline_manifest.digest


def _resolve(qualified_name: str) -> Any:
    module_name, _, attr = qualified_name.rpartition(".")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)
