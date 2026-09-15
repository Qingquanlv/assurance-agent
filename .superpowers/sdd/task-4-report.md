# Task 4 Report: Finalize exact lock, no path rewrite

**Status:** DONE

**Commit:** `83a87635` `feat(generation): reject codegen writes outside the host lock`

**Review fix:** `729f4608` `fix(generation): drop obsolete reviewed_plan codegen tests`

**Branch:** `codex/durable-qa-execute` (in-place, no worktree)

## What landed

- Finalize rebuilds the host lock via `validate_codegen_input(..., context.project_root)` after `_finalize_authoring`.
- Receipt `files[].repo_path` must equal `scope.locked_outputs` under `qa/tests/` exactly. Testdata is required. Extra files such as `qa/tests/api/conftest.py` are illegal.
- Mapping `target_file` must equal the locked test file for that case. No `target_file` / `repo_path` rewrite.
- `_complete_files` uses exact membership in `locked_generated` instead of `family_allows_target` / `under_write_root`.
- Invalid on-disk case path (`qa/cases/dept.yaml`, no `**/case.yaml`) is `InputError`.
- `reviewed_mapping` stays optional exact-match. `scope_case_ids` / `required_capabilities` checks are unchanged.
- Fixtures: `durable_oracle_path` / `locked_oracle_paths` / `codegen_result` follow the `items` lock. Testdata is `support` with empty `case_ids`.

## TDD RED

Added the five brief tests plus `_write_locked_generated`. Rewrote `test_codegen_finalize_keeps_support_as_extra_hashed_entry` so testdata is the only support file.

```
uv run pytest ...::test_codegen_finalize_accepts_host_locked_paths \
  ...::test_codegen_finalize_rejects_write_root_as_file \
  ...::test_codegen_finalize_rejects_missing_testdata \
  ...::test_codegen_finalize_rejects_target_file_mismatch \
  ...::test_codegen_finalize_rejects_invalid_case_path -v
```

**Result:** 3 failed, 2 passed.

| Test | RED result |
|---|---|
| `accepts_host_locked_paths` | PASSED immediately (prefix allowlist already accepted testdata) |
| `rejects_write_root_as_file` | FAILED: `qa/tests/api` still succeeded via `family_allows_target` |
| `rejects_missing_testdata` | FAILED: receipt without testdata still succeeded |
| `rejects_target_file_mismatch` | PASSED already (`_complete_files` mapping check) |
| `rejects_invalid_case_path` | FAILED: `invalid_output` (no host rebuild), not `invalid_input` |

This matches the brief's intent: testdata was not required; write-root paths still passed prefix policy.

## GREEN implementation

`CodegenFinalizeHandler.execute` calls `validate_codegen_input` with the brief payload, then enforces receipt set equality and `target_file` lock match as `OutputError`. `_complete_files(allowed_paths=locked_generated)` rejects any path not in that set.

Existing finalize tests now write `qa/cases/items/case.yaml` and list testdata. Cycle / handoff / review-audit fixtures follow the same lock.

## TDD GREEN

```
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py \
  packages/capabilities/assurance-generation/tests/test_codegen_scope.py -v
```

**Result:** 123 passed, 9 failed. All finalize and scope tests passed. Failures are pre-existing Task 3 leftovers, not lock regressions:

- `test_codegen_input_rejects_*` / performance scenario drift: `KeyError: 'reviewed_plan'`
- `test_e2e_codegen_skill_reads_family_prefixed_review`: still looks for `qa/results/review/e2e-plan-review.json`

```
uv run pytest packages/capabilities/assurance-generation/tests -q
```

**Result:** 570 passed, 72 failed. Failures outside this task: plan-review routing / `review_audit`, graph factory, review-audit helpers. Generation cycle and reviewed-plan handoff were updated to the `items` lock and pass.

## Concerns

1. **RED accept test did not fail.** Prefix policy already allowed the locked testdata pair. Missing-testdata and write-root-as-file were the real RED proofs.
2. **Pre-existing `test_codegen.py` failures left untouched** (reviewed plan field, e2e skill path). Same set Task 3 reported.
3. **Full generation suite still red** on plan-review / codegen-review / review-audit (parallel work; spec says graph / codegen-review unchanged).
4. **Ruff collapsed** the brief's multiline `locked_generated` / `test_by_case` literals. Behavior is unchanged.

## Files committed

- `packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py`
- `packages/capabilities/assurance-generation/tests/test_codegen.py`
- `packages/capabilities/assurance-generation/tests/codegen_fixtures.py`
- `packages/capabilities/assurance-generation/tests/test_generation_cycle.py`
- `packages/capabilities/assurance-generation/tests/test_codegen_characterization.py`
- `packages/capabilities/assurance-generation/tests/review_audit_fixtures.py`
- `packages/capabilities/assurance-generation/tests/test_reviewed_plan_handoff.py`

## Review fix

Deleted eight `CodegenInputV1` tests that still indexed `reviewed_plan` (`KeyError`). Host `codegen_scope` / on-disk cases already own that lock. Rewrote the leftover e2e skill assertion to host `codegen_scope` / `locked_outputs` / `qa/cases/**/case.yaml`.

```
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/test_codegen_scope.py -q
```

```
........................................................................ [ 58%]
....................................................                     [100%]
124 passed in 1.30s
```

## Critical review fix

Mapping case IDs now lock to the rebuilt host `scope.case_ids`, not `payload.scope_case_ids`. Dropped the `scope_case_ids is None → InputError` gate. Four `aa-*-codegen` skills list testdata as required (host `locked_outputs` always include it).

### TDD RED

```
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_succeeds_without_scope_case_ids packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_rejects_mapping_that_omits_host_scope_case -v
```

**Result:** 2 failed.

| Test | RED result |
|---|---|
| `succeeds_without_scope_case_ids` | FAILED: `invalid_input` / `host codegen scope case_ids are missing` |
| `rejects_mapping_that_omits_host_scope_case` | FAILED: `invalid_input` instead of `invalid_output` |

### TDD GREEN

Moved the check to after `validate_codegen_input`. `fake_agent_result(..., include_scope_case_ids=False)` leaves the host lock as the source of truth.

```
uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/test_codegen_scope.py -q
```

```
........................................................................ [ 55%]
..........................................................               [100%]
130 passed in 1.81s
```
