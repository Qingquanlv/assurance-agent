# Task 8 Report — Normalize Healing Events and Dark-Ship Graph-Owned Healing Ops

## Status: DONE

**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` Task 8  
**Worktree tip at start of fix pass:** `231d496`  
Edit + test only (no git add/commit). Cursor-loop helpers left alone. Packaged topology stays dark-shipped.

## Fix pass (P1 / P2 / P3)

| Priority | Item | Resolution |
|----------|------|------------|
| P1 | High-risk approval digest binding | `_bind_approval_to_snapshot_artifacts` binds proposal/authority/baseline/policy sha256 + targets/paths to snapshot artifacts; forged digests → `CandidateValidationError` |
| P1 | record verifies candidate receipt | `operation_record_codegen_fix_apply` loads CAS receipt, checks identity/`bind_receipt_to_success_event`, on-disk intent digest, then `verify_candidate_receipt`; fail closed if missing/wrong |
| P1 | Allocate authority from write-set/snapshot | `allocate_authority_bindings_from_artifacts` binds generated/updated after digests from codegen write set; reused under private root; imported → `unverified`; `with.authority_bindings` remains test seam only when artifacts absent |
| P2 | before-digest on modify | generated/updated authority before-digest enforced on content-`modify` (not only add-with-before) |
| P2 | Healing conformance mutation table | Emits `missing_approval_interrupt`, `missing_record_join`, `missing_hard_outputs`, `inactive_target_required`, plus prior codes; positive controls for API-only / E2E-only / both |
| P2 | Registry compatibility | `CodegenFixApplySummaryV1.applied` + aggregate SafetyCheck boolean shims so legacy `ApplySummary` / `SafetyCheck` must_compat readers accept new ops output (no colliding registry flip) |
| P3 | AST consumer-set | Guards raw `heal_record_apply` alongside legacy baseline/allocation names |

## What was already landed (first pass)

- Projection / consumers, durable effects + v2 events, dark-ship ops, `codegen_fix_candidate/v1`, `diff_safety.py`, production effect registry registration
- Packaged YAML / contracts unchanged

## Verify (fix pass)

```text
uv run pytest -q \
  tests/unit/healing/test_episode_projection.py \
  tests/unit/healing/test_allocation.py \
  tests/unit/healing/test_record_apply.py \
  tests/unit/healing/test_authority_allocate.py \
  tests/unit/test_healing_state.py \
  tests/unit/retro/test_workflow_history.py \
  tests/unit/workflow/graph/test_healing_topology_mutations.py \
  tests/unit/workflow/graph/test_durable_effects.py \
  tests/unit/workflow/graph/test_task_input_snapshot.py \
  tests/unit/workflow/graph/test_task_runner.py \
  tests/unit/workflow/graph/test_precommit_validation.py \
  tests/integration/test_codegen_fixer_record.py \
  tests/unit/artifacts/test_healing_codegen.py
→ 161 passed

uv run ruff check assurance_agent/workflow/healing \
  assurance_agent/workflow/graph/healing_conformance.py \
  assurance_agent/workflow/graph/diff_safety.py \
  assurance_agent/workflow/graph/precommit.py \
  assurance_agent/artifacts/models
→ All checks passed

uv run pyright → 0 errors
```

## Files ready to stage (integrator owns commit)

**Created / notably extended in fix pass**
- `tests/unit/healing/test_authority_allocate.py`
- Updates to `precommit.py`, `operations.py`, `healing_conformance.py`, `healing_codegen.py`
- `tests/integration/test_codegen_fixer_record.py`, `test_healing_topology_mutations.py`, `test_episode_projection.py`, `test_healing_codegen.py`, `test_precommit_validation.py` (forged approval)

**Do not stage**
- `benchmark/.../cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`

Suggested commit message: `feat(healing): normalize episodes and stage graph-owned records`

## Notes
- Record verification uses `TreeStore(context.change_dir)` (invocation object store), not the materialized task workspace copy.
- Aggregate `FixerSafetyCheckV1` remains unregistered on the legacy SafetyCheck path; emitted JSON includes legacy required booleans so finalize readers stay must_compat.
- Packaged topology/YAML still dark-shipped until Task 15.
