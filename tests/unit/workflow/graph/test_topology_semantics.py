"""Closed historical_topology_safety/v1 manifest coverage."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.historical_topology_v6 import V6_SEMANTICS_ID, WIRING_STATUSES
from assurance_agent.workflow.graph.topology_semantics import (
    SEMANTICS_ID,
    TopologySemanticsManifest,
    build_topology_semantics_manifest,
    topology_safety_semantics_bytes,
    topology_safety_semantics_digest,
    topology_safety_semantics_object_digest,
)


def _dependency_names() -> tuple[str, ...]:
    return tuple(item.qualified_name for item in build_topology_semantics_manifest().dependencies)


def _module_exists(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


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


def _implementation_digest(obj: object) -> str:
    from assurance_agent.workflow.orchestration.gate_semantics import (
        normalized_ast_digest,
        normalized_value_digest,
    )

    if inspect.isfunction(obj) or inspect.isclass(obj) or inspect.ismethod(obj):
        return normalized_ast_digest(inspect.getsource(obj))
    return normalized_value_digest(obj)


def test_topology_semantics_ids_and_versions() -> None:
    manifest = build_topology_semantics_manifest()
    assert SEMANTICS_ID == "historical_topology_safety/v1"
    assert SEMANTICS_ID == V6_SEMANTICS_ID
    assert manifest.semantics_id == SEMANTICS_ID
    assert manifest.schema_version == "1"
    assert set(manifest.runtime_versions) >= {"python", "pydantic", "pydantic_core", "pyyaml"}
    assert WIRING_STATUSES == frozenset({"wired", "legacy_unwired", "partial"})


def test_topology_canonical_bytes_and_digests_are_stable() -> None:
    first = build_topology_semantics_manifest()
    second = build_topology_semantics_manifest()
    assert first.canonical_bytes == second.canonical_bytes
    assert first.canonical_bytes == topology_safety_semantics_bytes()
    assert first.canonical_bytes.endswith(b"\n")
    assert first.object_digest == topology_safety_semantics_object_digest()
    assert first.semantic_digest == topology_safety_semantics_digest()
    assert first.object_digest != first.semantic_digest
    assert len(first.object_digest) == 64
    assert len(first.semantic_digest) == 64


def test_topology_inventories_are_sorted() -> None:
    manifest = build_topology_semantics_manifest()
    dep_names = [item.qualified_name for item in manifest.dependencies]
    consumer_ids = list(manifest.consumers)
    assert dep_names == sorted(dep_names)
    assert consumer_ids == sorted(consumer_ids)
    assert len(dep_names) == len(set(dep_names))
    assert len(consumer_ids) == len(set(consumer_ids))


def test_topology_requires_discovery_cfg_dominance_truth_status_runtime() -> None:
    manifest = build_topology_semantics_manifest()
    names = {item.qualified_name for item in manifest.dependencies}
    required_suffixes = (
        "historical_roles.discover_historical_assurance_roles",
        "historical_topology_v6.classify_historical_layer_topology_v6",
        "historical_topology_v6.WIRING_STATUSES",
        "topology_analysis.build_cfg",
        "topology_analysis.dominates",
        "topology_analysis.reachable_from",
        "topology_analysis.expressions_truth_equivalent",
        "topology_analysis.layer_selection_domain",
    )
    for suffix in required_suffixes:
        assert any(name.endswith(suffix) for name in names), suffix
    assert "discovery" in manifest.consumers
    assert "cfg" in manifest.consumers
    assert "dominance" in manifest.consumers
    assert "truth_table" in manifest.consumers
    assert "status" in manifest.consumers
    assert "runtime_versions" in manifest.consumers


def test_topology_closed_consumer_set_ast_import_inventory() -> None:
    manifest = build_topology_semantics_manifest()
    for item in manifest.dependencies:
        obj = _resolve(item.qualified_name)
        module_name, _, _attr = item.qualified_name.rpartition(".")
        # Multi-level attrs keep the defining module prefix before the class.
        while module_name and not _module_exists(module_name):
            module_name, _, _ = module_name.rpartition(".")
        assert module_name
        path = Path(importlib.import_module(module_name).__file__ or "")
        assert path.is_file()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        short = item.qualified_name.rsplit(".", 1)[-1]
        found = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == short:
                found = True
                break
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == short:
                        found = True
                        break
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == short
            ):
                found = True
        assert found, f"inventory member not defined in module AST: {item.qualified_name}"
        assert _implementation_digest(obj) == item.source_digest


@pytest.mark.parametrize(
    "qualified_name",
    sorted(_dependency_names()),
)
def test_topology_mutate_each_dependency_changes_semantic_digest(qualified_name: str) -> None:
    baseline = build_topology_semantics_manifest()
    distinct = "a" * 64 if baseline.dependencies[0].source_digest != "a" * 64 else "b" * 64
    mutated = build_topology_semantics_manifest(source_digest_overrides={qualified_name: distinct})
    assert mutated.semantic_digest != baseline.semantic_digest
    assert mutated.object_digest != baseline.object_digest
    assert topology_safety_semantics_digest() == baseline.semantic_digest


def test_topology_key_reorder_without_byte_change_is_stable() -> None:
    baseline = topology_safety_semantics_bytes()
    # Reconstruct from parsed object with insertion-scrambled dict order.
    import json

    payload = json.loads(baseline.decode("utf-8"))
    scrambled = {
        "runtime_versions": {
            k: payload["runtime_versions"][k] for k in reversed(list(payload["runtime_versions"]))
        },
        "consumers": list(reversed(payload["consumers"])),
        "dependencies": list(reversed(payload["dependencies"])),
        "semantics_id": payload["semantics_id"],
        "schema_version": payload["schema_version"],
        "semantic_digest": payload["semantic_digest"],
    }
    # Canonical builder always re-sorts; direct re-encode of scrambled must differ,
    # while rebuild from overrides/none stays identical to baseline.
    scrambled_bytes = (json.dumps(scrambled, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    assert scrambled_bytes != baseline
    assert topology_safety_semantics_bytes() == baseline
    rebuilt = TopologySemanticsManifest.from_canonical_bytes(baseline)
    assert rebuilt.canonical_bytes == baseline
