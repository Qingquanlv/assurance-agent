---
name: aa-api-codegen-fixer
description: "Apply approved API test-code fixes from healing/fix-proposal.json. Modifies only generated API test files allowed by the proposal."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/healing/fix-proposal.json` — stop if missing.
2. Read `qa/changes/<change-id>/healing/entry-baseline.json` — stop if missing (healing attempt not allocated).
3. Verify `healing/entry-baseline.json` `entry_batch_id` equals latest `execution/execution-manifest.yaml` `batch_id`.
4. Read `qa/changes/<change-id>/inspect/failure-analysis.json`.
5. Read `qa/changes/<change-id>/codegen/api-codegen-summary.md` to get the trusted set of generated/reused files.
6. Do **not** apply patches not listed in `fix-proposal.json`.

**After completing work (mandatory — graph node requires this file):**

1. Always run `aa heal record-apply` so the CLI writes `healing/api-apply-summary.json`:
   - Patches applied: `aa heal record-apply --change <change-id> --target api --proposal <ids>`
   - No eligible proposals: `aa heal record-apply --change <change-id> --target api --outcome no_op --reason "no eligible api proposals"`
   - Validation/Condition STOP (no patches applied): `aa heal record-apply --change <change-id> --target api --outcome skipped --reason "<why>" --proposal <ids>`
2. Verify the CLI wrote `qa/changes/<change-id>/healing/api-apply-summary.json` and `.md`.
3. Verify every modified file is in the allowed list (applied path only).
4. Report the attempt-entry state delta in your final message (`api_apply_status`, `api_apply_summary`) — do not edit `workflow-state.yaml` (graph v2 coordinator owns it).

---

# Skill: aa-api-codegen-fixer

## Purpose

`aa-api-codegen-fixer` is **Phase 11A** of the healing loop.

It reads `healing/fix-proposal.json`, filters to proposals with `target == "api"` and `eligible == true`, and applies the approved patches to generated API test files.

## Role Boundary

This skill:

- **May** modify files listed in the allowed file set (see below)
- **May** only apply operations described in `fix-proposal.json.proposals[].patch_plan`
- **Must NOT** modify product code (`app/`, `web/`, `src/`)
- **Must NOT** change assertion expected values
- **Must NOT** skip or delete failing tests
- **Must NOT** modify E2E test files
- **Must NOT** modify unrelated test files outside the change scope
- **Must NOT** hand-write `healing/api-apply-summary.json` or `.md`; only `aa heal record-apply` may create those files
- **Must** call `aa heal record-apply` on every exit path (applied / no_op / skipped) — the graph node fails closed without that file

---

## Allowed Files

A file is eligible for modification only if **all three** conditions are satisfied:

**Condition 1 — Pattern match:**

```text
tests/api/test_<module>_api.py
tests/api/helpers/<module>_api.py
tests/api/conftest.py
tests/api/adapters/**/*.py
tests/testdata/domain/**/*.py  # only when explicitly selected and trusted; preserve shared contract
```

**Condition 2 — Proposal request:** the file appears in `fix-proposal.json.proposals[].files_to_modify`.

**Condition 3 — Trusted source authorization:** the file also appears in **at least one** of:
- `phases.api_codegen.generated_tests.files` in `workflow-state.yaml`
- `codegen/api-codegen-summary.md` generated/reused/updated files section

`proposals[].files_to_modify` is a **request**, not an authorization source. A proposal may only target a file that is independently confirmed as being part of the current change's generated test set.

If a file satisfies Conditions 1 and 2 but not Condition 3 → **STOP**, do not apply any patch; optionally write `healing/api-fixer-error.json` for humans, then **must** run `aa heal record-apply --outcome skipped --reason "Condition 3: <file> not in codegen trusted set" --proposal <ids>`.

### Special rule for `tests/api/conftest.py`

`tests/api/conftest.py` is a shared fixture file affecting all API tests. It may be modified only if **all** of the following hold:

- It satisfies Conditions 1, 2, and 3 above.
- The patch is limited exclusively to fixtures or helpers introduced or used by the current change (as listed in `api-codegen-summary.md`).
- The patch does **not** rename or delete any existing public fixture.
- The patch does **not** change the behavior of fixtures used by unrelated tests.

Forbidden on `conftest.py`:
- Renaming shared fixtures
- Deleting existing fixture functions
- Changing parameters or return values of fixtures used by tests outside the current change scope

---

## Forbidden Files

Absolutely forbidden — never modify:

```text
app/**
web/**
src/**
qa/changes/<change-id>/cases/**
qa/changes/<change-id>/plans/**
qa/changes/<change-id>/review/**
.aa/config.yaml
.aa/data-knowledge.yaml
tests/e2e/**
```

If a proposal in `fix-proposal.json` requires modifying any forbidden file:

1. **STOP** immediately.
2. Do **not** apply any patch.
3. Optionally write `qa/changes/<change-id>/healing/api-fixer-error.json` for humans.
4. **Must** run `aa heal record-apply --change <change-id> --target api --outcome skipped --reason "forbidden file targeted" --proposal <ids>`.
5. Report `api_apply_status: failed` in your final message — do not edit `workflow-state.yaml`.

---

## Forbidden Operations

Never perform:

- Modify product code
- Change assertion expected values (e.g., changing `assert response.status_code == 201` to `assert response.status_code == 200`)
- Delete failing test functions
- Add `pytest.mark.skip`
- Add `pytest.mark.xfail`
- Add `@pytest.mark.skip`
- Lower assertion strength (e.g., `assert len(items) > 0` → `assert items is not None`)
- Mark known product issues as passing
- Weaken HTTP status code assertions

---

## Allowed Operations

Permitted repairs:

- Correct wrong helper function call signatures
- Fix test data generation (incorrect field names, missing required fields, wrong data types)
- Fix fixture name conflicts
- Correct `httpx` client parameter usage
- Add missing but schema-required request fields
- Fix request payload structure to match API schema
- Fix API helper return value parsing
- Fix cleanup fixture ordering
- Correct import paths

---

## Risk Gate

Only apply proposals where **all** are true:

- `eligible == true`
- `target == "api"`
- `risk_level in ["low", "medium"]`

If `risk_level == "high"`:
- Do **not** apply the patch.
- Record in `skipped_proposals` with `reason: "high risk requires human confirmation"`.
- Do **not** write `api-fixer-error.json` for this (it is not an error, it is a skip).

## Application Process

For each proposal in `fix-proposal.json` where `target == "api"`, `eligible == true`, and `risk_level in ["low", "medium"]`:

1. **Cross-validate files before any patch:**
   - Collect all files from `files_to_modify`.
   - Validate each against the three-condition allowed file rule (see **Allowed Files**).
   - Collect all files from every `patch_plan[].file` entry.
   - Verify every `patch_plan[].file` is present in `files_to_modify`.
     - If any `patch_plan[].file` is not in `files_to_modify` → **STOP**, write `api-fixer-error.json`.
   - If any file fails Condition 3 → **STOP**, write `api-fixer-error.json`.
   - Do not begin patching until all file validations pass.

2. Read the current content of each validated file.
3. Apply each `patch_plan` step sequentially:
   - `replace`: replace identified code pattern with corrected version
   - `insert`: add new code at specified location
   - `refactor`: restructure existing code without changing behavior
   - `add_helper`: add a new helper function
4. After each file modification, re-read the file to verify the patch applied correctly.
5. Record the modification with `aa heal record-apply --change <change-id> --target api --proposal <ids>`.

---

## Output Files

Do not write apply-summary files by hand. **Every exit path** must call `aa heal record-apply` so the graph declared output exists:

```bash
# Patches applied
aa heal record-apply --change <change-id> --target api --proposal FIX-001 --proposal FIX-002

# No eligible proposals (or nothing to do)
aa heal record-apply --change <change-id> --target api --outcome no_op --reason "no eligible api proposals"

# Validation / Condition STOP without modifying tests
aa heal record-apply --change <change-id> --target api --outcome skipped --reason "Condition 3: file not in trusted set" --proposal FIX-001
```

On the applied path the CLI derives `files_modified` from the current test-tree diff, validates the files against `fix-proposal.json`, and writes:

> **Schema source of truth:** the complete, enforced field contract for apply summaries
> lives in `src/schema/apply_summary.ts` (validated by `aa validate`). After
> `aa heal record-apply` completes you MUST run:
>
> ```
> aa validate --change <change-id> --artifact healing/api-apply-summary.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

**`qa/changes/<change-id>/healing/api-apply-summary.json`** (CLI-written — minimal illustrative shape):

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "target": "api",
  "applied": true,
  "applied_proposals": [],
  "modified_files": [],
  "skipped_proposals": [],
  "forbidden_attempts": [],
  "rerun_required": true,
  "next_action": "run_aa_run"
}
```

**`qa/changes/<change-id>/healing/api-apply-summary.md`:**

```markdown
# API Codegen Fixer — Apply Summary

**Change**: <change-id>
**Applied at**: <timestamp>
**Proposals applied**: <N>
**Files modified**: <N>

## Applied Fixes

### FIX-001 — test_code_error (api)

**File**: `tests/api/test_menu_api.py`
**Operation**: fix_request_payload
**Description**: Added missing required `category_id` field to POST /api/v1/menu/create payload

---

## Skipped Proposals

| Proposal | Reason |
|----------|--------|
| FIX-002 | target=e2e, not handled by this fixer |

---

## Next Action

Re-run tests: `aa run --change <change-id>`
```

---

## Error Output

If a forbidden file is targeted or a forbidden operation is required, write:

**`qa/changes/<change-id>/healing/api-fixer-error.json`:**

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "target": "api",
  "error": "forbidden_file_targeted|forbidden_operation_required",
  "proposal_id": "FIX-001",
  "forbidden_file": "app/api/menu.py",
  "reason": "Proposal requires modifying product code; this is not allowed in healing loop",
  "applied": false,
  "rerun_required": false,
  "next_action": "developer_fix_required"
}
```

---

## Workflow-state Update

This skill updates only the per-target fields in the current attempt entry.

After successful application (patches applied):

```yaml
phases:
  healing:
    # status remains: proposal_created (NOT changed to applied — orchestrator's job)
    attempts:
      - attempt: <n>
        api_apply_status: applied
        api_apply_summary: qa/changes/<change-id>/healing/api-apply-summary.json
        # applied_files is aggregated by the orchestrator after both fixers complete — NOT set here
```

After no eligible API proposals (no-op):

```yaml
phases:
  healing:
    attempts:
      - attempt: <n>
        api_apply_status: no_op
        api_apply_summary: qa/changes/<change-id>/healing/api-apply-summary.json  # applied: false
```

After forbidden modification attempt or cross-validation failure:

```yaml
phases:
  healing:
    status: failed      # only this skill may set status=failed when it encounters a hard error
    stop_reason: "api-fixer: forbidden file targeted or patch_plan/files_to_modify mismatch"
    attempts:
      - attempt: <n>
        api_apply_status: failed
```

---

## Steps

1. Read `healing/fix-proposal.json` and `healing/entry-baseline.json` — stop if either is missing (still call `record-apply --outcome skipped` with reason if baseline/proposal missing after allocate should have run).
2. Filter: `target == "api"` and `eligible == true` and `risk_level in ["low", "medium"]`.
3. If no matching proposals: run `aa heal record-apply --change <change-id> --target api --outcome no_op --reason "no eligible api proposals"` and report `api_apply_status: no_op`.
4. **Pre-apply file validation (before touching any file):**
   a. For each proposal: collect all `files_to_modify` and all `patch_plan[].file`.
   b. Verify every `patch_plan[].file` is in `files_to_modify` — STOP on mismatch.
   c. Verify every file in `files_to_modify` satisfies Conditions 1, 2, and 3 (see **Allowed Files**) — STOP on failure.
   d. Verify no file is in the forbidden list — STOP on violation.
   e. If any `conftest.py` is targeted, apply the extra conftest rules — STOP on violation.
   f. On STOP: do not patch; run `aa heal record-apply --change <change-id> --target api --outcome skipped --reason "<why>" --proposal <ids>`; optionally write `api-fixer-error.json`; report `api_apply_status: failed`.
5. Apply patches (only after all validations pass):
   a. For each validated proposal, apply each `patch_plan` step.
   b. Re-read each modified file to confirm patch applied correctly.
6. Run `aa heal record-apply --change <change-id> --target api --proposal <ids>` — the CLI writes `healing/api-apply-summary.json` and `.md`.
7. Report the attempt-entry state delta: `api_apply_status` and `api_apply_summary` only.
8. Report summary to user.

---

## Hard Rules

- **Never** modify files outside the allowed set.
- **Never** modify `app/`, `web/`, or `src/`.
- **Never** change assertion expected values.
- **Never** add `pytest.mark.skip`, `xfail`, or equivalent.
- **Never** delete failing tests.
- **Never** apply a `high` risk proposal automatically.
- **Never** apply a patch whose `patch_plan[].file` is not listed in `files_to_modify`.
- **Never** accept `proposals[].files_to_modify` as authorization alone — require Condition 3 trusted source.
- **Never** set `phases.healing.status = applied` — the graph coordinator sets terminal healing status after all fixers complete.
- **Always** complete all file validations before applying any patch.
- **Always** verify the patch applied by re-reading the file.
- **Always** produce `healing/api-apply-summary.json` via `aa heal record-apply` (applied / no_op / skipped) — never leave the graph without this declared output.
- If `healing/entry-baseline.json` is missing or its `entry_batch_id` mismatches the latest execution manifest, stop and report (and still close with `--outcome skipped` when allocation ledger exists).

---

## Example

**API test payload missing required schema field:**

```text
Failure: test_create_menu_returns_201 — 422 Unprocessable Entity
Category: test_code_error
Analysis: POST /api/v1/menu/create payload missing required field `category_id`
```

Fix proposal says:
```json
{
  "proposal_id": "FIX-001",
  "target": "api",
  "patch_plan": [
    {
      "file": "tests/api/test_menu_api.py",
      "operation": "replace",
      "description": "Add missing category_id field in menu create payload",
      "constraints": ["must not change assertion status code check"]
    }
  ]
}
```

This skill:
1. Reads `tests/api/test_menu_api.py`
2. Validates file is in generated_tests.files
3. Locates the payload dict in `test_create_menu_returns_201`
4. Adds `"category_id": menu_category_fixture["id"]` to the payload
5. Re-reads the file to confirm change
6. Records in `api-apply-summary.json`
