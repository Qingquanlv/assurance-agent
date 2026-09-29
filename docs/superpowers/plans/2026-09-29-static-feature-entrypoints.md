# Static Feature Entrypoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make six capability wheels expose one static `feature.py` interface and make Product consume those interfaces for graph factories, contracts, routes, and Agent Task binding.

**Architecture:** A small immutable `FeatureSpec` in graph-engine boot holds existing references without replacing their owners. Every wheel exports one `FEATURE`; Product explicitly lists six and validates their closed composition. Runtime topology and behavior remain untouched.

**Tech Stack:** Python 3.11, uv workspace, pytest, pyright, ruff, import-linter.

**Spec:** `docs/superpowers/specs/2026-09-29-static-feature-entrypoints-design.md`

## Global Constraints

- No graph topology, semantic ID, contract, OpenCode transport, retry, host, or Kernel behavior changes.
- No auto-discovery or project-provided executable code; keep pinned source coordinates.
- Product has exactly six explicitly imported Feature values.
- Do not carry uncommitted changes from `benchmark-test-optimze-phase2` into this branch.

## Review Focus

1. A missing or duplicate Feature must fail before Product boot, not silently override another.
2. A Task class with the wrong Agent contract must not be bound to a runtime request.
3. A route template missing an Agent contract must fail the existing output-route completeness check.
4. Source allowlist and Feature plugin identities must agree without weakening source authentication.
5. Existing 28 graph exports and 15 Product roots must still build from installed wheels.

---

### Task 1: Define the static Feature interface

**Files:** Create `packages/framework/graph-engine/graph_engine/boot/feature.py`; modify `boot/__init__.py`; test `packages/framework/graph-engine/tests/boot/test_feature_spec.py`.

**Interfaces:** `FeatureSpec[AgentContractT]` has `plugin`, `agent_contracts`, `task_contracts`, `output_route_templates`, `graph_factory`, and `agent_task_types`. The descriptor is frozen and validates owner and local duplicates.

- [ ] Write tests constructing a valid FeatureSpec and rejecting a plugin/graph owner mismatch and duplicate Task contract IDs; use real `CapabilityPlugin` fixtures or small local classes.
- [ ] Run `uv run pytest packages/framework/graph-engine/tests/boot/test_feature_spec.py -q` and observe expected missing-module failure.
- [ ] Implement the minimal frozen descriptor and export it from `graph_engine.boot`.
- [ ] Re-run that test file; run `uv run pyright` and `uv run ruff check` on the touched files.
- [ ] Commit Task 1.

### Task 2: Export six capability Features

**Files:** Create `packages/capabilities/assurance-{intake,generation,execution,quality,healing,improvement}/assurance_*/feature.py`; test `tests/product/test_feature_entrypoints.py`.

**Interfaces:** Each module exports `FEATURE: FeatureSpec[AgentExecutionContract[Any, Any, Any]]`; execution has empty Agent catalog and Task tuple. Existing `plugin.py`, `contracts/attempts.py`, `operations/agent_tasks.py`, and `graphs/factory.py` remain the implementation owners.

- [ ] Write a parameterized test that imports six `FEATURE` values, checks literal owner IDs, factory symbols, Agent/Task counts (26/19), and matching Task class contract IDs.
- [ ] Run `uv run pytest tests/product/test_feature_entrypoints.py -q`; observe missing feature module failure.
- [ ] Add six minimal `feature.py` files with explicit imports and static construction; no decorators or scanning.
- [ ] Re-run the test and `uv run pyright`.
- [ ] Commit Task 2.

### Task 3: Move Product composition to the six interfaces

**Files:** Create `assurance_product/features.py`; modify `agent_contracts.py`, `graph_factories.py`, `output_routes.py`, `runtime_bindings.py`, Product `graphs/factory.py`, and six capability `graphs/factory.py` files; update six `feature.py` files and `tests/product/test_feature_entrypoints.py`.

**Interfaces:** `FEATURES` is the six-item pinned tuple; `FEATURE_GRAPH_FACTORIES`, `AGENT_EXECUTION_CONTRACTS`, `FEATURE_TASK_ATTEMPT_CONTRACTS`, `_ROUTE_TEMPLATES`, and `_AGENT_TASK_TYPES` derive from it. Keep public helper names and factory symbols stable.

- [ ] Add Product tests proving six entries yield 26 Agent contracts, 19 Task contracts, 26 matching Task types, exact route coverage, and public graph-bundle types; duplicate Feature and Task identities fail closed.
- [ ] Run the Product test file and observe that `assurance_product.features` is absent.
- [ ] Implement the single Product tuple and migrate four consumers; preserve static `source_catalog.py` allowlist and validate identities against it.
- [ ] Run Product graph/contract/route/runtime tests and compare 28 exports, 48 Attempt registrations, and 15 roots against the pre-change expectations.
- [ ] Commit Task 3.

### Task 4: Verify packaged behavior and finish

**Files:** Update README only if the Feature author interface needs a short note; otherwise no production changes.

- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`.
- [ ] Run `uv run pytest -q --tb=short` and the three smoke scripts named in `AGENTS.md`; report any environment-limited run separately.
- [ ] Inspect `git diff --check`, source authentication tests, and wheel contents for all six `feature.py` modules.
- [ ] Review the branch against this spec; commit final test/doc changes if any.
