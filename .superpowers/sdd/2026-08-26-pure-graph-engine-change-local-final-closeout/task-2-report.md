# Task 2 — Phase 2 Tail Behavioral Reconciliation

Status: complete.

## Method

Inspected `916b68d`, `e1ada99`, `36eafa1`, `f6f9098`, `3bfe0c`, and `fc50036`
without cherry-picking. `phase2-equivalence.json` maps each retained invariant to
a current behavioral test node and records whether the current engine was already
equivalent or required a minimal port.

## Findings

- `916b68d`: equivalent. The current durable ledger plus
  `test_executor_reconciles_after_apply_started_without_blind_reapply` preserves
  recovery by reconciliation rather than blind re-application.
- `e1ada99`: equivalent. Retry exhaustion remains terminal and non-retryable in
  `test_executor_exhausts_policy_as_non_retryable_failure`.
- `36eafa1`: equivalent. The current authenticated wheel loader rejects an
  arbitrary preloaded module at an authenticated source location.
- `f6f9098`: equivalent. Explicit installed-wheel product/plugin execution is
  retained; the installed-wheel smoke also passed.
- `3bfe0c`: equivalent at the product-neutral effect seam. Toy A no longer owns
  Phase 2 effect business semantics, so the generic durable effect recovery node
  is the behavioral replacement; no Toy A behavior was restored.
- `fc50036`: ported. Runtime matching and `SchemaEntry` construction now reject
  unknown JSON Schema keywords recursively, so unsupported schemas fail before
  execution or registry admission.

No SnapshotStore, workspace tree/HEAD state, whole-tree export, Assurance
meaning, project-loaded extension point, removed Change-local module, or
historical conformance module was restored.

## Verification

- `uv run pytest tests/phase6/test_phase2_invariant_reconciliation.py packages/graph-engine/tests/runtime/test_effects.py packages/graph-engine/tests/composition -q` — 408 passed.
- `bash scripts/graph_engine_smoke_test.sh` — passed after the required
  unsandboxed retry for uv's local cache.

The brief's literal focused command also names the deleted
`packages/graph-engine/tests/runtime/test_json_schema.py`; it predictably stops
with “file or directory not found”. The Phase 6 reconciliation test is the
replacement schema coverage, and the equivalent runnable focused command above
is green.
