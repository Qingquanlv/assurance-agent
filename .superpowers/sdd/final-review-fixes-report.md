# Final Review Fixes Report — Healing Authority / Precommit / Claims

Branch: `codex/capability-traceability-integration`  
Worktree: `.worktrees/capability-traceability-integration`  
Date: 2026-08-01

## Scope

Whole-branch final-review P0/P1 items for packaged healing ready-authority binding, empty-manifest fail-closed precommit, distinguishing stop reason codes, and narrowed release/progress healing claims. Cursor-loop left untouched. No git add/commit.

## Fixes

### P0 — Packaged allocate binds ready fixer authority from production path

**Problem:** `allocate_authority_bindings_from_artifacts` only became `ready` when callers supplied `codegen_write_set_ids` (and snapshot entries for `reused`). Packaged `allocate` had no `with` params → always `unverified` → healing stopped.

**Change:**
- `discover_codegen_authority_inputs` reads the change ledger for committed `generated_files_candidate/v1` / `skill:aa-*-codegen` successes and resolves write-set IDs, attempt IDs, and input-snapshot entries; `task_imported` codegen layers become `imported_targets`.
- `enhance_allocate_result_with_authority` merges discovery into params (explicit `with` still wins), and derives `active_targets` from `healing/fix-proposal.json` when absent.
- Ready path works for non-imported live manifests without test-only `with` params.

### P1 — Distinguishing stop reason codes

**Problem:** Any non-ready authority was labeled `unverified_imported_codegen`.

**Change:** `operation_fixer_authority_ready` classifies via ledger discovery:
- all non-ready targets imported → `unverified_imported_codegen`
- missing write-set binding → `missing_codegen_write_set_binding`
- otherwise → `unverified_fixer_authority`

### P1 — Precommit rejects empty manifests when selected cases exist

**Problem:** `generated_files_candidate/v1` accepted `files: []` + empty write-set when summary+manifest outputs were present.

**Change:** `_validate_generated_files_candidate` fail-closes when `selected_case_ids` or `selected_private_root_targets` are non-empty and `manifest.files` is empty.

### P1 — Narrowed healing matrix claims

**Change:** Release notes and SDD progress no longer claim a packaged GraphRuntime E2E healing-matrix proof. They state ready-authority binding on the packaged allocate path plus operation-level proofs; imported narrowing remains explicit.

## Tests added

- `test_allocate_binds_ready_from_production_ledger_without_with_params`
- `test_allocate_binds_reused_from_ledger_snapshot_entries`
- `test_missing_write_set_binding_reason_is_not_imported`
- `test_imported_codegen_stop_reason_remains_distinct`
- `test_generated_files_candidate_rejects_empty_manifest_with_selected_cases`

## Verification

```bash
uv run pytest -q \
  tests/unit/healing/test_authority_allocate.py \
  tests/integration/test_codegen_fixer_record.py \
  tests/unit/workflow/graph/test_precommit_validation.py \
  tests/unit/healing/test_allocation.py \
  -k 'authority or allocate or empty or selected or manifest or healing'
# 12 passed, 20 deselected

uv run ruff check assurance_agent/workflow/healing assurance_agent/workflow/graph/precommit.py
# All checks passed

uv run pyright
# 0 errors, 0 warnings, 0 informations
```

## Files changed

- `assurance_agent/workflow/healing/operations.py`
- `assurance_agent/workflow/graph/precommit.py`
- `tests/unit/healing/test_authority_allocate.py`
- `tests/unit/workflow/graph/test_precommit_validation.py`
- `docs/release-notes/2026-08-four-layer-assurance.md`
- `.superpowers/sdd/progress.md`
- `.superpowers/sdd/final-review-fixes-report.md`
