"""Gate semantics manifest coverage, stability, and digest sensitivity."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import pytest
from pydantic import BaseModel, field_validator

from assurance_agent.artifacts.models.review import Review
from assurance_agent.workflow.orchestration import dsl
from assurance_agent.workflow.orchestration.gate_semantics import (
    build_gate_semantics_manifest,
    discover_replay_semantic_dependencies,
    gate_semantics_digest,
    normalized_ast_digest,
    symbol_implementation_digest,
)


def test_discover_replay_dependencies_match_manifest_symbols() -> None:
    manifest = build_gate_semantics_manifest()
    assert discover_replay_semantic_dependencies() == {
        symbol.qualified_name for symbol in manifest.symbols
    }


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


def test_constant_mutation_changes_symbol_and_aggregate_digest() -> None:
    qualified_name = "assurance_agent.workflow.orchestration.dsl.BUILTIN_ARITY"
    baseline_manifest = build_gate_semantics_manifest()
    baseline_symbol = next(s for s in baseline_manifest.symbols if s.qualified_name == qualified_name)
    mutated = dict(dsl.BUILTIN_ARITY)
    mutated["mutated_probe"] = 0

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
    class Probe(BaseModel):
        alpha: int
        beta: int

        @field_validator("alpha")
        @classmethod
        def _validate_alpha(cls, value: int) -> int:
            return value

    original = textwrap.dedent(inspect.getsource(Probe))
    mutated = original.replace('@field_validator("alpha")', '@field_validator("beta")', 1)
    assert normalized_ast_digest(mutated) != normalized_ast_digest(original)


def _resolve(qualified_name: str) -> Any:
    module_name, _, attr = qualified_name.rpartition(".")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)
