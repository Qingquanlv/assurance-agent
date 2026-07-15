---
name: aa-e2e-codegen-fixer
description: "Apply approved E2E test-code fixes from healing/fix-proposal.json. Modifies only generated E2E test files allowed by the proposal."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.healing.status == proposal_created` — if not, STOP and report.
3. Read `qa/changes/<change-id>/healing/fix-proposal.json` — stop if missing.
4. Read `qa/changes/<change-id>/inspect/failure-analysis.json`.
5. Read `qa/changes/<change-id>/codegen/e2e-codegen-summary.md` to get the trusted set of generated/reused files.
6. Do **not** apply patches not listed in `fix-proposal.json`.

**After completing work:**

1. Run `aa heal record-apply --change <change-id> --target e2e --proposal <comma-separated applied proposal ids>`.
2. Verify the CLI wrote `qa/changes/<change-id>/healing/e2e-apply-summary.json` and `.md`.
3. Verify every modified file is in the allowed list.
4. Update **only** the per-target status field in `workflow-state.yaml`:

```yaml
phases:
  healing:
    attempts:
      - attempt: <n>
        e2e_apply_status: applied | no_op | failed
        e2e_apply_summary: qa/changes/<change-id>/healing/e2e-apply-summary.json
```

**Do NOT** set `phases.healing.status = applied` — this is the orchestrator's responsibility after both API and E2E fixers complete and the Fixer Safety Gate passes.

---

# Skill: aa-e2e-codegen-fixer

## Purpose

`aa-e2e-codegen-fixer` is **Phase 11B** of the healing loop.

It reads `healing/fix-proposal.json`, filters to proposals with `target == "e2e"` and `eligible == true`, and applies the approved patches to generated E2E test files.

## Role Boundary

This skill:

- **May** modify files listed in the allowed file set (see below)
- **May** only apply operations described in `fix-proposal.json.proposals[].patch_plan`
- **Must NOT** modify product code (`app/`, `web/`, `src/`)
- **Must NOT** change assertion expected values
- **Must NOT** skip or delete failing tests
- **Must NOT** modify API test files
- **Must NOT** modify unrelated test files outside the change scope
- **Must NOT** hand-write `healing/e2e-apply-summary.json` or `.md`; only `aa heal record-apply` may create those files

---

## Allowed Files

A file is eligible for modification only if **all three** conditions are satisfied:

**Condition 1 — Pattern match:**

```text
tests/e2e/test_<module>_e2e.py
tests/e2e/scripts/<module>_data_setup.py
tests/e2e/conftest.py
tests/e2e/adapters/**/*.py
tests/testdata/domain/**/*.py  # only when explicitly selected and trusted; preserve shared contract
```

**Condition 2 — Proposal request:** the file appears in `fix-proposal.json.proposals[].files_to_modify`.

**Condition 3 — Trusted source authorization:** the file also appears in **at least one** of:
- `phases.e2e_codegen.generated_tests.files` in `workflow-state.yaml`
- `codegen/e2e-codegen-summary.md` generated/reused/updated files section

`proposals[].files_to_modify` is a **request**, not an authorization source. A proposal may only target a file that is independently confirmed as being part of the current change's generated test set.

If a file satisfies Conditions 1 and 2 but not Condition 3 → **STOP**, write `healing/e2e-fixer-error.json`, do not apply any patch.

### Special rule for `tests/e2e/conftest.py`

`tests/e2e/conftest.py` is a shared fixture file affecting all E2E tests. It may be modified only if **all** of the following hold:

- It satisfies Conditions 1, 2, and 3 above.
- The patch is limited exclusively to fixtures or helpers introduced or used by the current change (as listed in `e2e-codegen-summary.md`).
- The patch does **not** rename or delete any existing public fixture.
- The patch does **not** change the behavior of fixtures used by unrelated tests.

Forbidden on `conftest.py`:
- Renaming shared fixtures
- Deleting existing fixture functions
- Changing the signature or return value of fixtures used by tests outside the current change scope

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
tests/api/**
```

If a proposal in `fix-proposal.json` requires modifying any forbidden file:

1. **STOP** immediately.
2. Write `qa/changes/<change-id>/healing/e2e-fixer-error.json`.
3. Do **not** apply any patch.
4. Do **not** rerun.
5. Report state delta `phases.healing.status = failed` (inline mode: apply directly; dispatched subagent: report in your final message — the orchestrator applies it to `workflow-state.yaml`).

---

## Forbidden Operations

Never perform:

- Modify product code
- Change assertion expected values (e.g., changing `expect(locator).to_have_text("Success")` to `expect(locator).to_have_text("OK")`)
- Delete failing test functions
- Add `pytest.mark.skip`
- Add `pytest.mark.xfail`
- Add `@pytest.mark.skip`
- Lower assertion strength (e.g., `expect(locator).to_be_visible()` → no assertion)
- Mark known product issues as passing
- Swap one locator for a less specific one to avoid a real product failure

---

## Allowed Operations

Permitted repairs:

- Fix locator strategy (wrong CSS selector, wrong `data-testid`, wrong role, wrong text matcher)
- Replace brittle wait strategy: `page.wait_for_response(url)` → `page.expect_response(url)` context manager
- Replace `page.wait_for_timeout(ms)` with `expect(locator).to_be_visible(timeout=ms)`
- Fix fixture name conflicts
- Fix login flow wait strategy
- Fix cleanup fixture (wrong teardown order, missing cleanup)
- Fix test data setup script (`scripts/<module>_data_setup.py`)
- Fix Playwright Python API usage (deprecated or incorrect method names)
- Fix import paths

---

## Risk Gate

Only apply proposals where **all** are true:

- `eligible == true`
- `target == "e2e"`
- `risk_level in ["low", "medium"]`

If `risk_level == "high"`:
- Do **not** apply the patch.
- Record in `skipped_proposals` with `reason: "high risk requires human confirmation"`.
- Do **not** write `e2e-fixer-error.json` for this (it is not an error, it is a skip).

## Application Process

For each proposal in `fix-proposal.json` where `target == "e2e"`, `eligible == true`, and `risk_level in ["low", "medium"]`:

1. **Cross-validate files before any patch:**
   - Collect all files from `files_to_modify`.
   - Validate each against the three-condition allowed file rule (see **Allowed Files**).
   - Collect all files from every `patch_plan[].file` entry.
   - Verify every `patch_plan[].file` is present in `files_to_modify`.
     - If any `patch_plan[].file` is not in `files_to_modify` → **STOP**, write `e2e-fixer-error.json`.
   - If any file fails Condition 3 → **STOP**, write `e2e-fixer-error.json`.
   - Verify no file is in the forbidden list — STOP on violation.
   - If any `conftest.py` is targeted, apply the extra conftest rules — STOP on violation.
   - Do not begin patching until all file validations pass.

2. Read the current content of each validated file.
3. Apply each `patch_plan` step sequentially:
   - `replace`: replace identified code pattern with corrected version
   - `insert`: add new code at specified location
   - `refactor`: restructure existing code without changing behavior
   - `add_helper`: add a new helper function
4. After each file modification, re-read the file to verify the patch applied correctly.
5. Record the modification with `aa heal record-apply --change <change-id> --target e2e --proposal <ids>`.

---

## Output Files

Do not write these files by hand. After all eligible proposals are processed, run:

```bash
aa heal record-apply --change <change-id> --target e2e --proposal FIX-001,FIX-002
```

The CLI derives `files_modified` from the current test-tree diff, validates the files against `fix-proposal.json`, and writes:

> **Schema source of truth:** the complete, enforced field contract for apply summaries
> lives in `src/schema/apply_summary.ts` (validated by `aa validate`). After
> `aa heal record-apply` completes you MUST run:
>
> ```
> aa validate --change <change-id> --artifact healing/e2e-apply-summary.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

**`qa/changes/<change-id>/healing/e2e-apply-summary.json`** (CLI-written — minimal illustrative shape):

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "target": "e2e",
  "applied": true,
  "applied_proposals": [],
  "modified_files": [],
  "skipped_proposals": [],
  "forbidden_attempts": [],
  "rerun_required": true,
  "next_action": "run_aa_run"
}
```

**`qa/changes/<change-id>/healing/e2e-apply-summary.md`:**

```markdown
# E2E Codegen Fixer — Apply Summary

**Change**: <change-id>
**Applied at**: <timestamp>
**Proposals applied**: <N>
**Files modified**: <N>

## Applied Fixes

### FIX-003 — wait_strategy_failure (e2e)

**File**: `tests/e2e/test_checkout_e2e.py`
**Operation**: fix_wait_strategy
**Description**: Replaced `page.wait_for_response(url)` with `page.expect_response(url)` context manager

---

## Skipped Proposals

| Proposal | Reason |
|----------|--------|
| FIX-001 | target=api, not handled by this fixer |

---

## Next Action

Re-run tests: `aa run --change <change-id>`
```

---

## Error Output

If a forbidden file is targeted or a forbidden operation is required, write:

**`qa/changes/<change-id>/healing/e2e-fixer-error.json`:**

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "target": "e2e",
  "error": "forbidden_file_targeted|forbidden_operation_required",
  "proposal_id": "FIX-003",
  "forbidden_file": "app/views/checkout.py",
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
        e2e_apply_status: applied
        e2e_apply_summary: qa/changes/<change-id>/healing/e2e-apply-summary.json
        # applied_files is aggregated by the orchestrator after both fixers complete — NOT set here
```

After no eligible E2E proposals (no-op):

```yaml
phases:
  healing:
    attempts:
      - attempt: <n>
        e2e_apply_status: no_op
        e2e_apply_summary: qa/changes/<change-id>/healing/e2e-apply-summary.json  # applied: false
```

After forbidden modification attempt or cross-validation failure:

```yaml
phases:
  healing:
    status: failed      # only this skill may set status=failed when it encounters a hard error
    stop_reason: "e2e-fixer: forbidden file targeted or patch_plan/files_to_modify mismatch"
    attempts:
      - attempt: <n>
        e2e_apply_status: failed
```

---

## Steps

1. Read `workflow-state.yaml` — verify `phases.healing.status == proposal_created`; STOP if not.
2. Read `healing/fix-proposal.json`.
3. Filter: `target == "e2e"` and `eligible == true` and `risk_level in ["low", "medium"]`.
4. If no matching proposals: write minimal `e2e-apply-summary.json` (`applied: false`, `e2e_apply_status: no_op`), update attempt entry.
5. **Pre-apply file validation (before touching any file):**
   a. For each proposal: collect all `files_to_modify` and all `patch_plan[].file`.
   b. Verify every `patch_plan[].file` is in `files_to_modify` — STOP on mismatch.
   c. Verify every file in `files_to_modify` satisfies Conditions 1, 2, and 3 (see **Allowed Files**) — STOP on failure.
   d. Verify no file is in the forbidden list — STOP on violation.
   e. If any `conftest.py` is targeted, apply the extra conftest rules — STOP on violation.
   f. Write `e2e-fixer-error.json` if STOP is triggered, set `e2e_apply_status: failed`.
6. Apply patches (only after all validations pass):
   a. For each validated proposal, apply each `patch_plan` step.
   b. Re-read each modified file to confirm patch applied correctly.
7. Write `qa/changes/<change-id>/healing/e2e-apply-summary.json` and `qa/changes/<change-id>/healing/e2e-apply-summary.md`.
8. Report the attempt-entry state delta: `e2e_apply_status` and `e2e_apply_summary` only (orchestrator applies it to `workflow-state.yaml` when dispatched).
   (`applied_files` is NOT part of this delta — it is an orchestrator-only aggregate field)
   - **Do NOT** set `phases.healing.status = applied`.
9. Report summary to user.

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
- **Never** set `phases.healing.status = applied` — the orchestrator sets this after all fixers complete.
- **Always** complete all file validations before applying any patch.
- **Always** verify the patch applied by re-reading the file.
- **Always** set `rerun_required: true` in `e2e-apply-summary.json`.
- If `phases.healing.status != proposal_created`, stop and report.

---

## Example

**E2E wait strategy failure — brittle wait pattern:**

```text
Failure: test_checkout_completes_successfully — TimeoutError waiting for response
Category: wait_strategy_failure
Analysis: page.wait_for_response is not a valid Playwright Page method in the Python API;
          correct pattern is page.expect_response() context manager
```

Fix proposal says:
```json
{
  "proposal_id": "FIX-003",
  "target": "e2e",
  "patch_plan": [
    {
      "file": "tests/e2e/test_checkout_e2e.py",
      "operation": "replace",
      "description": "Replace page.wait_for_response(url) with with page.expect_response(url) as resp: / response = resp.value",
      "constraints": [
        "must not change what URL is being waited for",
        "must not change downstream assertions on response"
      ]
    }
  ]
}
```

This skill:
1. Reads `tests/e2e/test_checkout_e2e.py`
2. Validates file is in `phases.e2e_codegen.generated_tests.files`
3. Locates `page.wait_for_response(url)` pattern
4. Replaces with:
   ```python
   with page.expect_response(url) as response_info:
       page.click(submit_button)
   response = response_info.value
   ```
5. Re-reads the file to confirm change
6. Records in `e2e-apply-summary.json`

**E2E locator failure — stale/wrong selector:**

```text
Failure: test_product_list_shows_items — TimeoutError: Timeout 5000ms exceeded waiting for locator('.product-card')
Category: locator_failure
Analysis: Product card class changed from .product-card to .product-item in recent frontend update
```

Fix proposal says:
```json
{
  "proposal_id": "FIX-004",
  "target": "e2e",
  "patch_plan": [
    {
      "file": "tests/e2e/test_product_e2e.py",
      "operation": "replace",
      "description": "Update CSS selector from .product-card to [data-testid='product-item'] for stable locator",
      "constraints": [
        "must use data-testid if available",
        "must not change the assertion on the located element"
      ]
    }
  ]
}
```
