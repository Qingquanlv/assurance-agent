# Benchmark Plan Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent malformed plan capability references from stopping benchmark items and ensure all active assurance layers settle before an overall business STOP.

**Architecture:** Add a cross-artifact review validation boundary, reroute capability remediation through a second reviewer pass, classify benchmark interrupts by node, and introduce an opt-in child-STOP settlement policy plus aggregate gate for the assurance fan-out. Existing codegen prechecks and default child STOP propagation stay fail-closed.

**Tech Stack:** Python 3.11, Pydantic v2, YAML workflow schema v2, Bash benchmark helpers, pytest, uv.

## Global Constraints

- Do not weaken `capabilities_present` or any codegen precondition gate.
- Undeclared missing capability references are `invalid_output`; proposal-declared knowledge gaps remain valid.
- `settle_child_stop` defaults to false and is enabled only for API/E2E/Fuzz/Performance nodes in `assurance`.
- Use `uv run ...` for Python quality commands.

---

### Task 1: Validate PlanReview capability references

**Files:**
- Modify: `assurance_agent/workflow/graph/finalize.py`
- Modify: `assurance_agent/artifacts/models/review.py`
- Test: `tests/unit/workflow/graph/test_finalize_review_validation.py`
- Test: `tests/unit/verification/test_contract_render.py`

**Interfaces:**
- Consumes: `compute_missing_capabilities`, `collect_leaf_entries`, layer proposal YAML.
- Produces: `_validate_plan_review_capability_references(workspace, authored) -> TaskResult | None`.

- [ ] Write tests showing an L1 scalar descendant and a symbol-derived alias fail as undeclared references.
- [ ] Write tests showing an existing typed leaf and a proposal-declared missing leaf pass.
- [ ] Run the focused tests and confirm they fail for the missing cross-artifact validation.
- [ ] Implement the validator and strengthen output-contract prompt notes.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Add deterministic second-pass review routing

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/skills/aa-api-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Test: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`
- Test: `tests/unit/test_fuzz_performance_skills.py`
- Test: `tests/integration/test_four_layer_resume.py`

**Interfaces:**
- Consumes: the current `knowledge-remediation` interrupt and mechanical check artifact.
- Produces: `fix_and_proceed -> review` for all four plan cycles.

- [ ] Write schema and integration assertions for the new route and reviewer second pass.
- [ ] Run them and confirm the old route fails.
- [ ] Change the four routes and clarify first-pass/second-pass reviewer inputs and capability-key rules.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Classify autonomous benchmark interrupts

**Files:**
- Modify: `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Modify: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh`
- Test: `tests/unit/benchmark/test_cursor_loop_helpers.py`

**Interfaces:**
- Consumes: proposal state plus `pending_interrupts[0].node_id` from status JSON.
- Produces: `benchmark_interrupt_action <proposal_state> <node_id>`.

- [ ] Replace the unconditional-action test with a decision-table test and run it RED.
- [ ] Implement fail-closed action classification and pass the node id from all three loop drivers.
- [ ] Run shell-helper tests GREEN and run `bash -n` on every modified script.

### Task 4: Settle assurance child STOPs before aggregate failure

**Files:**
- Modify: `assurance_agent/workflow/graph/schema_v2.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Test: `tests/unit/workflow/graph/test_finalize_and_child_stop.py`
- Test: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`
- Test: `tests/integration/test_four_layer_codegen_only.py`

**Interfaces:**
- Produces: `NodeDef.settle_child_stop: bool = False` and parent task value `{child_status, reason}`.
- Consumes: that value in `assurance-generation-status-gate` after `generation-join`.

- [ ] Add a runtime test where one opt-in child stops and a sibling still executes; retain the existing default-propagation test.
- [ ] Add packaged-schema assertions for opt-in scope and aggregate gate placement; run RED.
- [ ] Implement stopped-child freezing/value propagation and the aggregate gate; run GREEN.
- [ ] Run four-layer integration tests to confirm all active branches settle before STOP.

### Task 5: Verify, review, and commit

**Files:**
- Review all modified files from Tasks 1-4.

**Interfaces:**
- Consumes: all completed tasks.
- Produces: a verified commit on `codex/benchmark-test`.

- [ ] Run focused regression tests for all four tasks.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, and `uv run pytest`.
- [ ] Run `bash scripts/packaging_smoke_test.sh`.
- [ ] Review `git diff --check`, inspect the full diff, and confirm only scoped files changed.
- [ ] Commit the implementation with a focused message.

