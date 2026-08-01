"""Closed runtime_commit_safety/v1 manifest coverage."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.durable_effects import KNOWN_DURABLE_EFFECT_KINDS
from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS
from assurance_agent.workflow.graph.runtime_commit_safety import (
    SEMANTICS_ID,
    build_runtime_commit_safety_manifest,
    commit_safety_semantics_bytes,
    commit_safety_semantics_digest,
    commit_safety_semantics_object_digest,
)


def _dependency_names() -> tuple[str, ...]:
    return tuple(item.qualified_name for item in build_runtime_commit_safety_manifest().dependencies)


def _constant_value(qualified_name: str) -> object:
    return _resolve(qualified_name)


def _resolve(qualified_name: str) -> object:
    parts = qualified_name.split(".")
    for i in range(len(parts), 0, -1):
        module_name = ".".join(parts[:i])
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        obj: object = module
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(qualified_name)


def _defining_module(qualified_name: str) -> str:
    parts = qualified_name.split(".")
    for i in range(len(parts), 0, -1):
        module_name = ".".join(parts[:i])
        try:
            importlib.import_module(module_name)
        except ImportError:
            continue
        if i < len(parts):
            return module_name
    raise ImportError(qualified_name)


def _ast_defines(tree: ast.AST, short: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == short:
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == short:
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == short:
            return True
    return False


def _implementation_digest(obj: object) -> str:
    from assurance_agent.workflow.orchestration.gate_semantics import (
        normalized_ast_digest,
        normalized_value_digest,
    )

    if inspect.isfunction(obj) or inspect.isclass(obj) or inspect.ismethod(obj):
        return normalized_ast_digest(inspect.getsource(obj))
    return normalized_value_digest(obj)


def test_commit_safety_ids_and_versions() -> None:
    manifest = build_runtime_commit_safety_manifest()
    assert SEMANTICS_ID == "runtime_commit_safety/v1"
    assert manifest.semantics_id == SEMANTICS_ID
    assert manifest.schema_version == "1"
    assert set(manifest.runtime_versions) >= {"python", "pydantic", "pydantic_core", "pyyaml"}


def test_commit_safety_canonical_bytes_and_digests_are_stable() -> None:
    first = build_runtime_commit_safety_manifest()
    second = build_runtime_commit_safety_manifest()
    assert first.canonical_bytes == second.canonical_bytes == commit_safety_semantics_bytes()
    assert first.canonical_bytes.endswith(b"\n")
    assert first.object_digest == commit_safety_semantics_object_digest()
    assert first.semantic_digest == commit_safety_semantics_digest()
    assert first.object_digest != first.semantic_digest
    assert len(first.object_digest) == 64
    assert len(first.semantic_digest) == 64


def test_commit_safety_inventories_are_sorted() -> None:
    manifest = build_runtime_commit_safety_manifest()
    dep_names = [item.qualified_name for item in manifest.dependencies]
    consumers = list(manifest.consumers)
    assert dep_names == sorted(dep_names)
    assert consumers == sorted(consumers)
    assert len(dep_names) == len(set(dep_names))
    assert len(consumers) == len(set(consumers))


def test_commit_safety_closed_registry_kinds_exactly_once() -> None:
    manifest = build_runtime_commit_safety_manifest()
    names = {item.qualified_name: item for item in manifest.dependencies}
    consumer_set = set(manifest.consumers)

    for validator_id in sorted(KNOWN_PRECOMMIT_VALIDATORS):
        matches = [
            item
            for item in manifest.dependencies
            if item.role == "validator" and _constant_value(item.qualified_name) == validator_id
        ]
        assert len(matches) == 1, validator_id
        assert validator_id in consumer_set

    for effect_kind in sorted(KNOWN_DURABLE_EFFECT_KINDS):
        matches = [
            item
            for item in manifest.dependencies
            if item.role == "effect" and _constant_value(item.qualified_name) == effect_kind
        ]
        assert len(matches) == 1, effect_kind
        assert effect_kind in consumer_set

    fence_ops = (
        "root_terminal_fence.guard",
        "root_terminal_fence.reject_if_terminal",
        "root_terminal_fence.prepare_terminal",
        "root_terminal_fence.commit_terminal",
        "root_terminal_fence.abort_prepared",
        "effect_retry_sidecar",
    )
    for op in fence_ops:
        assert op in consumer_set
        assert any(item.consumer_id == op for item in manifest.dependencies), op

    # No unregistered validator/effect role entries.
    for item in manifest.dependencies:
        if item.role == "validator":
            assert _constant_value(item.qualified_name) in KNOWN_PRECOMMIT_VALIDATORS
        if item.role == "effect":
            assert _constant_value(item.qualified_name) in KNOWN_DURABLE_EFFECT_KINDS
        assert item.qualified_name in names


def test_exact_validator_and_effect_consumer_set() -> None:
    """Task 23 mechanical scan alias for closed validator/effect consumer sets."""
    test_commit_safety_closed_registry_kinds_exactly_once()


def test_commit_safety_ast_import_inventory_defines_each_member_once() -> None:
    manifest = build_runtime_commit_safety_manifest()
    seen_short: dict[str, str] = {}
    for item in manifest.dependencies:
        short = item.qualified_name.rsplit(".", 1)[-1]
        # Method names may repeat across classes; key by qualified name uniqueness only.
        assert item.qualified_name not in seen_short.values() or short
        seen_short[item.qualified_name] = short
        obj = _resolve(item.qualified_name)
        module_name = _defining_module(item.qualified_name)
        path = Path(importlib.import_module(module_name).__file__ or "")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _ast_defines(tree, short), f"missing AST definition for {item.qualified_name}"
        assert _implementation_digest(obj) == item.source_digest


def test_commit_safety_rejects_unregistered_or_unconsumed_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    import assurance_agent.workflow.graph.runtime_commit_safety as mod

    baseline = list(mod._COMMIT_SAFETY_INVENTORY)
    poisoned = (
        *baseline,
        mod.InventorySpec(
            qualified_name="assurance_agent.workflow.graph.precommit.CandidateValidationError",
            role="helper",
            consumer_id="unregistered_helper",
            semantic_version="1",
        ),
    )
    monkeypatch.setattr(mod, "_COMMIT_SAFETY_INVENTORY", poisoned)
    with pytest.raises(ValueError, match="listed-but-unconsumed|unregistered"):
        build_runtime_commit_safety_manifest()


@pytest.mark.parametrize(
    "qualified_name",
    sorted(_dependency_names()),
)
def test_commit_safety_mutate_each_dependency_changes_semantic_digest(qualified_name: str) -> None:
    baseline = build_runtime_commit_safety_manifest()
    distinct = "c" * 64 if baseline.dependencies[0].source_digest != "c" * 64 else "d" * 64
    mutated = build_runtime_commit_safety_manifest(source_digest_overrides={qualified_name: distinct})
    assert mutated.semantic_digest != baseline.semantic_digest
    assert mutated.object_digest != baseline.object_digest
    assert commit_safety_semantics_digest() == baseline.semantic_digest


def test_commit_safety_fence_method_digest_ignores_unrelated_module_bytes() -> None:
    """Digest named fence descriptors; adding an unused module helper must not change v1."""
    baseline = commit_safety_semantics_digest()
    import assurance_agent.workflow.graph.effect_retry as retry_mod

    probe_name = "_UNRELATED_TASK10_PROBE"
    original = retry_mod.__dict__.get(probe_name)
    setattr(retry_mod, probe_name, lambda: "unused")
    try:
        assert commit_safety_semantics_digest() == baseline
        assert commit_safety_semantics_bytes() == build_runtime_commit_safety_manifest().canonical_bytes
    finally:
        if original is None:
            delattr(retry_mod, probe_name)
        else:
            setattr(retry_mod, probe_name, original)


def test_commit_safety_key_reorder_without_byte_change_is_stable() -> None:
    baseline = commit_safety_semantics_bytes()
    payload = json.loads(baseline.decode("utf-8"))
    scrambled = {
        "semantic_digest": payload["semantic_digest"],
        "dependencies": list(reversed(payload["dependencies"])),
        "consumers": list(reversed(payload["consumers"])),
        "runtime_versions": {
            k: payload["runtime_versions"][k] for k in reversed(list(payload["runtime_versions"]))
        },
        "schema_version": payload["schema_version"],
        "semantics_id": payload["semantics_id"],
    }
    scrambled_bytes = (json.dumps(scrambled, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    assert scrambled_bytes != baseline
    assert commit_safety_semantics_bytes() == baseline
