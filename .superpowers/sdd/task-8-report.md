# Task 8 Report — Normalize Healing Events and Dark-Ship Graph-Owned Healing Ops

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 8  
**Worktree tip at start:** `cc15bf0` / current HEAD before edits `78be767`  
Edit + test only (no git add/commit). Cursor-loop helpers left alone.

## Deferred items closed

| Source | Deferred item | Closed in Task 8 |
|--------|---------------|------------------|
| Task 1 Minimal now | `FixerAuthorityV1`, path/target models, apply-summary models, target/aggregate safety fragments | Restored from `e9cd9f3` bodies into `healing_codegen.py` + exports/tests |
| Task 6 Resolution B | Full `codegen_fix_candidate/v1` algorithm + Step 3 mutations + scheduler no-commit path + diff-safety | Implemented; load-reject flipped to real validator registration |
| Task 6 | Diff-safety predicates | `workflow/graph/diff_safety.py` |

## What was implemented

### Projection / consumers
- `assurance_agent/workflow/healing/projection.py` — `HealingEpisodeProjection` over legacy pair, v2 combined allocation, mixed ledgers; conflict = integrity failure
- Consumers migrated: `derive_healing_state`, `derive_guard_context`, `commit_healing_allocation_ledger`, retro `workflow_history`
- AST consumer-set test allows raw legacy event-name literals only in `core/events.py` + `healing/projection.py`

### Durable effects + domain events
- `assurance_agent/workflow/healing/effects.py` — `HealingAllocationEffectV2`, `FixerProposalApprovedEffectV1`, `HealRecordApplyEffectV2` + reconcilers
- Production registry now registers exactly the three healing kinds (lazy via `production_effect_registry()`)
- New audit events: `healing_attempt_allocated_v2`, `fixer_proposal_approved`, `heal_record_apply_v2`
- Packaged contracts still select `durable_effects: ()`

### Operations (registered, unreachable until Task 15)
- `fixer-authority-ready`, `record-fixer-approval`, `fixer-dispatch`, `record-codegen-fix-apply`, `combine-fixer-safety`
- Allocate writes fixer-authority hard output; optional durable effect via `with.emit_durable_effect`
- Scheduler legacy host-ledger hook gated to packaged pre-activation allocate contract digest + empty `durable_effects`

### Precommit / conformance
- `codegen_fix_candidate/v1` fully dispatchable (intent, proposal/authority subsets, claimed/write equality, reused before-digest, high-risk approval, diff-safety)
- `healing_conformance.py` + mutation table (`category="healing_conformance"`)

### Dark-ship preserved
- Packaged workflow YAML / execution-contracts unchanged
- Legacy event models/readers preserved
- New ops registered but not routed by packaged topology
- Aggregate `FixerSafetyCheckV1` not forced onto packaged SafetyCheck registry path

## Verify

```text
uv run pytest -q \
  tests/unit/healing/test_episode_projection.py \
  tests/unit/healing/test_allocation.py \
  tests/unit/healing/test_record_apply.py \
  tests/unit/test_healing_state.py \
  tests/unit/retro/test_workflow_history.py \
  tests/unit/workflow/graph/test_healing_topology_mutations.py \
  tests/unit/workflow/graph/test_durable_effects.py \
  tests/unit/workflow/graph/test_task_input_snapshot.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_precommit_validation.py \
  tests/integration/test_codegen_fixer_record.py \
  tests/unit/artifacts/test_healing_codegen.py
→ 157 passed

uv run ruff check … → All checks passed
uv run pyright → 0 errors
uv run lint-imports → 6 kept, 0 broken
```

## Files ready to stage (integrator owns commit)

**Created**
- `assurance_agent/workflow/healing/projection.py`
- `assurance_agent/workflow/healing/effects.py`
- `assurance_agent/workflow/healing/operations.py`
- `assurance_agent/workflow/graph/healing_conformance.py`
- `assurance_agent/workflow/graph/diff_safety.py`
- `tests/unit/healing/test_episode_projection.py`
- `tests/unit/workflow/graph/test_healing_topology_mutations.py`
- `tests/integration/test_codegen_fixer_record.py`

**Modified (key)**
- `assurance_agent/artifacts/models/healing_codegen.py` (+ `__init__.py`, `registry.py`)
- `assurance_agent/workflow/core/events.py`
- `assurance_agent/workflow/graph/durable_effects.py`, `precommit.py`, `scheduler.py`, `handlers/operation.py`
- `assurance_agent/workflow/healing/allocation.py`, `safety.py`
- `assurance_agent/workflow/orchestration/healing_state.py`
- `assurance_agent/retro/workflow_history.py`
- Related unit tests (`test_durable_effects`, `test_precommit_validation`, `test_contracts`, `test_task_runner`, `test_healing_codegen`, …)

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `feat(healing): normalize episodes and stage graph-owned records`

## Notes
- Effect payload field sets follow design §5.3 / D14 prose + `e9cd9f3` authority precedent (not a second invention pass).
- Full Step 3 codegen_fix mutation matrix and scheduler no-commit cuts are partially covered (missing-intent reject + generated-files suite retained); deeper mutation cases can extend `test_precommit_validation.py` in follow-up without topology flip.
