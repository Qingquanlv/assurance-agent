"""Drift-matrix helpers: one-facet resolve and open-without-append."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import cast

import pytest
from graph_engine import Engine
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.runtime.engine import EngineError
from graph_engine.runtime.invocation_lock import InvocationDrift
from graph_engine.runtime.secret_sources import empty_runtime_authorization

from tests.phase4.six_wheel_harness import (
    FIXTURE_ROOT,
    SixWheelComposition,
    SixWheelTaskHost,
    _engine_call,
    _import_activation,
    _start_until_blocked,
    _workspace_binding,
    resolve_fixture,
)

_CATALOG = FIXTURE_ROOT / "capability-catalog.v1.json"
_STARTED: dict[int, tuple[Path, Path, str]] = {}


def resolve_with_one_mutation(facet: str) -> tuple[SixWheelComposition, SixWheelComposition]:
    mutator = _MUTATORS.get(facet)
    if mutator is None:
        raise ValueError(f"unknown drift facet: {facet}")
    if facet == "capability-catalog":
        original = resolve_fixture("phase4-opencode", mutate=_include_capability_catalog)
        drifted = resolve_fixture("phase4-opencode", mutate=_mutate_capability_catalog)
        return original, drifted
    original = _cached_original()
    drifted = resolve_fixture("phase4-opencode", mutate=mutator)
    return original, drifted


def assert_open_rejects_drift_without_ledger_append(
    original: SixWheelComposition,
    drifted: SixWheelComposition,
) -> None:
    engine_root, invocation_root, invocation_id = _started_invocation(original)
    before = _ledger_snapshot(invocation_root)
    host = SixWheelTaskHost(
        adapter_id="runtime.opencode",
        provider_state_dir=original.workspace / "drift-open-provider",
    )
    with _import_activation(original.product_root, original.workspace):
        with pytest.raises((InvocationDrift, EngineError)):
            with Engine(engine_root, host=host) as engine:
                engine.open(
                    invocation_id,
                    drifted.composition,
                    authorization=empty_runtime_authorization(),
                    workspace_binding=_workspace_binding(engine_root),
                )
    assert _ledger_snapshot(invocation_root) == before


def _cached_original() -> SixWheelComposition:
    cached = getattr(_cached_original, "_value", None)
    if cached is None:
        cached = resolve_fixture("phase4-opencode")
        setattr(_cached_original, "_value", cached)
    return cast(SixWheelComposition, cached)


def _started_invocation(original: SixWheelComposition) -> tuple[Path, Path, str]:
    key = id(original)
    existing = _STARTED.get(key)
    if existing is not None:
        return existing
    host = SixWheelTaskHost(
        adapter_id="runtime.opencode",
        provider_state_dir=original.workspace / "drift-start-provider",
    )
    engine_root = original.workspace / "drift-engine"
    invocation_id = "phase4-drift-inv"
    with _import_activation(original.product_root, original.workspace):
        result, invocation_root = _engine_call(
            _start_until_blocked,
            engine_root,
            host,
            original.composition,
            invocation_id,
        )
    if result.status != "succeeded":
        raise AssertionError(f"drift original start failed: {result}")
    started = (engine_root, invocation_root, invocation_id)
    _STARTED[key] = started
    return started


def _ledger_snapshot(invocation_root: Path) -> bytes:
    ledger = invocation_root / "ledger"
    parts: list[bytes] = []
    if ledger.exists():
        for path in sorted(ledger.rglob("*")):
            if path.is_file() and not path.is_symlink():
                parts.append(path.relative_to(ledger).as_posix().encode("utf-8"))
                parts.append(path.read_bytes())
    return b"\n".join(parts)


def _include_capability_catalog(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del workspace, fixtures
    dest = product_root / "test_assurance_phase4_product" / "capability-catalog.v1.json"
    dest.write_bytes(_CATALOG.read_bytes())


def _mutate_capability_catalog(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del workspace, fixtures
    dest = product_root / "test_assurance_phase4_product" / "capability-catalog.v1.json"
    document = json.loads(_CATALOG.read_text(encoding="utf-8"))
    document["digest_input"]["leafs"] = [
        *document["digest_input"]["leafs"],
        "entities.item.update",
    ]
    dest.write_bytes(canonical_json_bytes(cast(JSONValue, document)))


def _mutate_plugin_code(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del product_root, fixtures
    path = workspace / "wheels" / "assurance-intake" / "assurance_intake" / "operations" / "finalize.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# phase4-drift-plugin-code\n", encoding="utf-8")


def _mutate_plugin_version(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del workspace
    yaml_path = fixtures / "bindings-opencode" / "plugin.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("plugin_version: 1.0.0", "plugin_version: 1.0.1", 1),
        encoding="utf-8",
    )
    product = product_root / "test_assurance_phase4_product" / "product.py"
    product.write_text(
        product.read_text(encoding="utf-8").replace(
            'plugin_id="test.assurance.bindings", version_specifier="==1.0.0"',
            'plugin_id="test.assurance.bindings", version_specifier="==1.0.1"',
        ),
        encoding="utf-8",
    )


def _mutate_schema_bytes(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del product_root, fixtures
    path = (
        workspace
        / "wheels"
        / "assurance-intake"
        / "assurance_intake"
        / "resources"
        / "schemas"
        / "case.v1.schema.json"
    )
    path.write_bytes(path.read_bytes() + b"\n")


def _mutate_skill_bytes(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del product_root, fixtures
    path = (
        workspace
        / "wheels"
        / "assurance-intake"
        / "assurance_intake"
        / "resources"
        / "skills"
        / "aa-case-reviewer"
        / "SKILL.md"
    )
    path.write_text(path.read_text(encoding="utf-8") + "\n<!-- phase4-drift-skill -->\n", encoding="utf-8")


def _mutate_persona_bytes(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del product_root, fixtures
    path = (
        workspace
        / "wheels"
        / "assurance-intake"
        / "assurance_intake"
        / "resources"
        / "personas"
        / "reviewer.md"
    )
    path.write_text(path.read_text(encoding="utf-8") + "\n<!-- phase4-drift-persona -->\n", encoding="utf-8")


def _mutate_result_contract_bytes(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del product_root, fixtures
    path = (
        workspace
        / "wheels"
        / "assurance-intake"
        / "assurance_intake"
        / "resources"
        / "result-contracts"
        / "case-review.v1.schema.json"
    )
    path.write_bytes(path.read_bytes() + b"\n")


def _mutate_policy_bytes(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del workspace, product_root
    path = fixtures / "bindings-opencode" / "model-policy.json"
    path.write_bytes(path.read_bytes() + b"\n")


def _mutate_binding_data(workspace: Path, product_root: Path, fixtures: Path) -> None:
    del workspace, product_root
    path = fixtures / "bindings-opencode" / "plugin.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("fixture-profile", "fixture-profile-drifted"),
        encoding="utf-8",
    )


_MUTATORS: dict[str, Callable[[Path, Path, Path], None]] = {
    "plugin-code": _mutate_plugin_code,
    "plugin-version": _mutate_plugin_version,
    "schema-bytes": _mutate_schema_bytes,
    "skill-bytes": _mutate_skill_bytes,
    "persona-bytes": _mutate_persona_bytes,
    "result-contract-bytes": _mutate_result_contract_bytes,
    "policy-bytes": _mutate_policy_bytes,
    "binding-data": _mutate_binding_data,
    "capability-catalog": _mutate_capability_catalog,
}
