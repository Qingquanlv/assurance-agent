---
name: aa-api-plan-fixer
description: "Apply safe, automatic fixes to AA API plan artifacts based on api-plan-review.json. Use after aa-api-plan-reviewer returns decision=needs_fix with auto_fix_allowed=true and human_review_required=false. Never invents endpoints, auth, fixtures, or expected response schema."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Read `events.jsonl` and determine the execution mode before validating the old review fields:
   - **automatic mode**: `phases.api_plan_review.status == needs_fix`.
   - **human-approved mode**: the latest `human_decision` for checkpoint `api-plan-review-gate` (or legacy checkpoint `api-plan-review`) has action `fix_and_proceed`, and its `review_sha256` matches the current `review/api-plan-review.json`.
   - Otherwise stop.
3. Read input files from disk: `review/api-plan-review.json`, `plans/api-plan.md`, `plans/api-test-data-plan.md`, `plans/api-codegen-plan.md`, `plans/m3-review-summary.md`.
4. Always verify `review_type == "api-plan"` and `change_id == <change-id>`. Then apply mode-specific validation:
   - In automatic mode, verify all default gate fields below.
   - In human-approved mode, the hash-bound human decision is the authorization source: apply only findings and plan changes explicitly resolved by its `reason`, using review findings and files on disk as evidence. **human-approved mode bypasses the default auto-fix gate-field requirements** for `decision`, `next_action`, `human_review_required`, `auto_fix_allowed`, and non-empty `auto_fix_plan`.
5. Default automatic-mode gate fields:
   - `decision == "needs_fix"` — if not, stop.
   - `next_action == "run_api_plan_fixer"` — if not, stop as reviewer contract error.
   - `human_review_required == false` — if not, stop **unless** human_approved exception applies (see below).
   - `auto_fix_allowed == true` — if not, stop.
   - `auto_fix_plan` exists and is non-empty — if missing or empty, stop as reviewer contract error.

**human-approved exception**: In human-approved mode, allow fixing blocker/high severity findings explicitly resolved by the decision reason and safe mechanical findings explicitly approved there. Record them in `api-plan-review-apply-summary.md` under `human_approved_fixes[]`. Never write review JSON.

6. In automatic mode, resolve every `auto_fix_plan` item before applying fixes. In human-approved mode, validate each approved change against the decision reason, matching finding, and target-file allowlist.
7. Use files as the sole source of truth.

**After completing work:**

1. Write updated files (only those actually modified):
   - `qa/changes/<change-id>/plans/api-plan.md`
   - `qa/changes/<change-id>/plans/api-test-data-plan.md`
   - `qa/changes/<change-id>/plans/api-codegen-plan.md`
   - `qa/changes/<change-id>/plans/m3-review-summary.md`
   - `qa/changes/<change-id>/plans/data-knowledge.proposal.yaml` (only if changed per authorized `auto_fix_plan` entry — see **data-knowledge.proposal.yaml** rules)
   - `qa/changes/<change-id>/review/api-plan-review-apply-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - Append to `phases.api_plan_review.fix_attempts`:
     ```yaml
     - attempt: <n>
       source_review: review/api-plan-review.json
       apply_summary: review/api-plan-review-apply-summary.md
       fixed: [<finding-id>, ...]
       skipped: [<finding-id>, ...]
       files_modified: [<path>, ...]
       timestamp: <now>
     ```
   - **Do NOT** create or update `phases.api_plan_fixer.status` unless the workflow schema explicitly defines it.
   - **Do NOT** modify `phases.api_plan_review.status` — only `aa-api-plan-reviewer` updates gate status.

---

# AA API Plan Fixer

## Purpose

Apply safe, automatic fixes to AA API plan artifacts based on `api-plan-review.json`.

Use this skill after `aa-api-plan-reviewer` returns:

```text
decision = needs_fix
auto_fix_allowed = true
human_review_required = false
```

This skill must only apply safe, mechanical plan fixes. It must not invent unknown endpoints, auth mechanisms, request/response schema, or data factories.

---

## When to Use

Use this skill when:

- `api-plan-review.json` exists.
- The API plan reviewer found auto-fixable issues.
- No human review is required.
- The orchestrator wants to re-run API plan review after applying fixes.

---

## Inputs

Required:

```text
qa/changes/<change-id>/review/api-plan-review.json
qa/changes/<change-id>/plans/api-plan.md
qa/changes/<change-id>/plans/api-test-data-plan.md
qa/changes/<change-id>/plans/api-codegen-plan.md
qa/changes/<change-id>/plans/m3-review-summary.md
```

Optional (only present when `.aa/data-knowledge.yaml` was missing during planning):

```text
qa/changes/<change-id>/plans/data-knowledge.proposal.yaml
.aa/data-knowledge.yaml
```

Recommended:

```text
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
```

## Outputs

Update only allowed plan files:

```text
qa/changes/<change-id>/plans/*.md
qa/changes/<change-id>/plans/data-knowledge.proposal.yaml
```

Write:

```text
qa/changes/<change-id>/review/api-plan-review-apply-summary.md
```

Do not edit:

```text
qa/changes/<change-id>/review/api-plan-review.json
qa/changes/<change-id>/review/api-plan-review-summary.md
tests/**
qa/changes/<change-id>/cases/**
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/known-product-issues.md
.aa/data-knowledge.yaml
```

Do not edit `known-product-issues.md` — see **Forbidden Fixes**.

---

## Gate Boundaries

This fixer must never bypass the reviewer. It is **not** a gate producer.

- Only apply findings where `auto_fix_allowed == true` and `human_review_required == false`.
- **Never** write or modify `api-plan-review.json` or any review JSON file.
- **Never** set or change `decision` to `pass` or `codegen_readiness` to `ready` — the fixer has no authority to pass the gate.
- **Never** modify `phases.api_plan_review.status` — only the reviewer updates gate status.
- **Do NOT** create or update `phases.api_plan_fixer.status` unless the workflow schema explicitly defines it.
- Fix attempt records belong under `phases.api_plan_review.fix_attempts` — same pattern as `phases.case_review.fix_attempts`.
- After applying fixes, you **must** request a fresh run of `aa-api-plan-reviewer`. The workflow only continues to `aa-api-codegen` when a **new** `api-plan-review.json` has `decision == "pass"` and `codegen_readiness in ["ready","ready_with_warnings"]`.
- A fixer's claim that it "fixed everything" does not release the gate. Only the reviewer's new JSON does.

---

## Allowed Fixes

You may apply fixes for findings where:

- `auto_fix_allowed = true`
- `human_review_required = false`
- `severity` in `["low", "medium"]`

**Explicit severity rule:** Findings with `severity = high` or `severity = critical` must **always** be escalated to human review — regardless of `auto_fix_allowed`. Never auto-fix high or critical severity findings.

Allowed operations:

- Add missing case references already present in case YAML
- Normalize headings and section structure
- Move misplaced content into the correct plan file
- Add known assertions from case YAML
- Add known preconditions from case YAML
- Add known execution command
- Add TODO markers only for already documented non-blocking warnings (see below)
- Add missing cleanup note if cleanup method is already known and preserves invariants; do not turn raw-delete cleanup for M2M, closure, or soft-delete entities into an accepted plan
- Clarify fixture reuse if present in `.aa/data-knowledge.yaml`
- Add codegen constraints such as "append only, do not overwrite existing tests"
- Add missing **Factory / Boundary Strategy** section to `api-test-data-plan.md` (entities already listed; use three-ring skeleton + per-entity table from api-test-data-plan contract)
- Replace hardcoded `BASE_URL` / credential string in plan text with reference to `tests.config.settings` (plan documentation text only, not generated code)
- Add plan note: fixtures must not use HTTP create for non-create cases (documentation only; does not authorize keeping HTTP fixtures)

**TODO marker rule:** Do not convert blockers into TODO-only warnings. Unknown endpoint path, unknown method, unknown auth mechanism, missing required fixture, unknown expected response schema, or unsafe cleanup must remain blockers or human-review items. Adding a TODO marker to a genuine blocker does not resolve it and must not change the finding's severity or status.

---

## Forbidden Fixes

Do not invent:

- HTTP method or endpoint path
- Auth mechanism or token format
- Request body schema
- Expected response body or status code
- Fixture or factory name
- Data setup or cleanup endpoint
- Business state transition
- Product behavior
- Endpoint coverage-gap documentation (`known-product-issues.md`)
- **`tests/testdata/domain/` implementations** — fixer may only document a `create-if-missing` or `reuse` shared capability; it must not write factory code
- **`tests/api/adapters/` implementations** — fixer may only repair the plan mapping/ownership text; it must not write adapter code

Do not create or modify:

```text
qa/changes/<change-id>/known-product-issues.md
```

Endpoint coverage-gap documentation requires human action and cannot be fixed by `aa-api-plan-fixer`. This aligns with `aa-api-plan-reviewer` coverage-gap rules.

Do not change:

- `codegen_readiness`
- Review decision fields
- Reviewer JSON
- Approved case content
- Source requirement interpretation

Do not hide unknowns. If something is unknown, make it explicit in the plan as a TODO or human review item — only when the reviewer already classified it as a non-blocking warning.

Do not directly update `.aa/data-knowledge.yaml`. Only propose updates in `data-knowledge.proposal.yaml` when authorized by a validated `auto_fix_plan` entry.

---

## Fix Source Rule

Only apply fixes explicitly listed in `api-plan-review.json` under `auto_fix_plan`.

Do not infer or expand fixes from the `findings` array alone.

Each `auto_fix_plan` entry must specify:

- `finding_id` — maps to a finding in `findings`
- `target_file` — exact file to edit (must be in Target File Allowlist)
- `operation` — exact operation (`edit`, `append`, `normalize`, `add_todo`, `move_section`)
- `description` — why the operation is safe

### Per-Item Validation (Hard Rule)

For **every** `auto_fix_plan` item, before applying any edit:

1. Resolve `finding_id` → matching entry in `findings[]`.
2. If no matching finding exists → **STOP** (reviewer contract error).
3. Verify referenced finding has `auto_fix_allowed == true` → if false, **STOP**.
4. Verify referenced finding has `human_review_required == false` → if true, **STOP**.
5. Verify referenced finding has `severity in ["low", "medium"]` → if `high` or `critical`, **STOP**.
6. Verify `target_file` is in Target File Allowlist → if not, **STOP**.
7. Verify `operation` is in the allowed enum → if not, **STOP**.

**Rationale:** `auto_fix_plan` is produced by the gate reviewer. If any item violates the contract, the review JSON is not trustworthy. The fixer must **STOP** before applying partial fixes — do not skip invalid items and continue with the rest.

These failures mean the reviewer JSON contract is untrustworthy. **Do not skip and continue.** Record the error in **Reviewer Contract Errors** in the apply summary, then stop.

If a finding in `findings[]` has no matching `auto_fix_plan` entry, add it to **Skipped Findings** in the apply summary (reviewer chose not to auto-fix it). Do not improvise a fix.

---

## Target File Allowlist

`auto_fix_plan.target_file` must be one of:

```text
qa/changes/<change-id>/plans/api-plan.md
qa/changes/<change-id>/plans/api-test-data-plan.md
qa/changes/<change-id>/plans/api-codegen-plan.md
qa/changes/<change-id>/plans/m3-review-summary.md
qa/changes/<change-id>/plans/data-knowledge.proposal.yaml
```

If `target_file` is outside this allowlist and appears in `auto_fix_plan`, **STOP** as reviewer contract error — do not skip and continue.

**`data-knowledge.proposal.yaml` creation rule:**

May create or modify `data-knowledge.proposal.yaml` **only when**:

- `auto_fix_plan.target_file` is exactly `qa/changes/<change-id>/plans/data-knowledge.proposal.yaml`
- The referenced finding is `severity in ["low", "medium"]` with `auto_fix_allowed == true` and `human_review_required == false`
- Content is limited to missing capability descriptions or `discovered_candidates` already evidenced in files on disk
- Do **not** create the file proactively outside an authorized `auto_fix_plan` entry

---

## Fix Strategy

1. Read `api-plan-review.json`.
2. Validate top-level gate fields (Context Contract step 4):
   - `review_type == "api-plan"`
   - `change_id == <change-id>`
   - `decision == "needs_fix"`
   - `next_action == "run_api_plan_fixer"`
   - `human_review_required == false`
   - `auto_fix_allowed == true`
   - `auto_fix_plan` non-empty
3. For each `auto_fix_plan` item, run **Per-Item Validation** (Fix Source Rule). Any failure → write apply summary with **Reviewer Contract Errors** and **STOP**.
4. Apply minimal edits to target plan files per validated `auto_fix_plan` instructions only.
5. Preserve existing style and ordering.
6. Add unknowns to plan TODO sections or `data-knowledge.proposal.yaml` — only when authorized by a validated `auto_fix_plan` entry and the unknown is already a non-blocking reviewer warning. Never downgrade blockers.
7. Write `api-plan-review-apply-summary.md`.
8. Report the state delta: append to `phases.api_plan_review.fix_attempts` (do **not** modify `phases.api_plan_review.status`) — applied to `workflow-state.yaml` per the Context Contract.
9. Tell the orchestrator to re-run `aa-api-plan-reviewer`.

---

## File-Specific Rules

### `api-plan.md`

May update:

- Scenario mapping
- Endpoint / method clarity (only when already known from case YAML or backend source)
- Request / assertion mapping
- Auth notes (only when auth details are already known from case YAML or `.aa/data-knowledge.yaml`)
- TODO markers only for items the reviewer already documented as non-blocking warnings — **must not** add TODOs for unknown endpoint / auth that are blockers, human-review items, or codegen-blocking unknowns

Do not invent executable details.

### `api-test-data-plan.md`

May update:

- Entity list
- Known states from case YAML
- Known fixture reuse
- Cleanup notes
- TODO markers only for unknown factories / states already documented by the reviewer as non-blocking warnings

Do not invent factories or cleanup endpoints. Cleanup must maintain the same invariants; do not raw-delete M2M, closure, or soft-delete entities.

### `api-codegen-plan.md`

May update:

- File target
- Append-only rule
- Existing test style notes
- Required command
- Codegen constraints
- TODO markers only for items the reviewer already documented as non-blocking warnings

### `m3-review-summary.md`

May update:

- Summary of remaining risks
- Notes that fixes were applied (add separate note: `Pending re-review after fixes.` — not as an enum value)
- Human review notes

**Readiness rules** (enum values: `ready` | `ready_with_warnings` | `not_ready` only):

- Only `aa-api-plan-reviewer` may **upgrade** readiness.
- The fixer may **preserve** existing **Plan Readiness** and **Codegen Readiness** enum values, or **downgrade** to `not_ready` if the existing value is unsafe after the edit.
- The fixer **must never** set readiness to `ready` or `ready_with_warnings`.
- Do **not** replace enum readiness values with `pending re-review` or any non-enum string.
- If `m3-review-summary.md` is updated, add a separate note (outside the enum fields): `Pending re-review after fixes.`

### `data-knowledge.proposal.yaml`

May append proposed knowledge entries **only** when authorized by a validated `auto_fix_plan` entry targeting this file (see Target File Allowlist).

Do not create or expand the file outside an authorized `auto_fix_plan` entry.

Do not promote proposals into `.aa/data-knowledge.yaml`.

---

## Apply Summary Format

Write:

```text
qa/changes/<change-id>/review/api-plan-review-apply-summary.md
```

Use this structure:

```markdown
# API Plan Review Apply Summary

## Change ID

<change-id>

## Applied Fixes

| Finding ID | File | Operation | Summary |
|---|---|---|---|

## Reviewer Contract Errors

| Item | Reason |
|---|---|

Record here when validation fails before any fix is applied — e.g. `change_id` mismatch, `next_action` mismatch, `finding_id` not found, `high`/`critical` severity in `auto_fix_plan`, invalid `target_file`, or invalid `operation`. On contract error, **STOP** — do not apply partial fixes.

## Skipped Findings

| Finding ID | Reason |
|---|---|

Use only for findings present in `findings[]` but absent from `auto_fix_plan` (reviewer chose not to auto-fix). Not for contract errors.

## Files Modified

- ...

## Remaining Unknowns

- ...

## Next Step

Re-run `aa-api-plan-reviewer`.
```

---

## Final Response

After applying fixes, respond with:

```
API plan review fixes applied.

Modified files:
- ...

Reviewer contract errors:
- ... (if any — fixer must have STOPped before applying fixes)

Skipped findings:
- ...

Remaining unknowns:
- ...

Next step:
Re-run aa-api-plan-reviewer.
```

If reviewer contract errors occurred, respond with:

```
API plan fixer STOPped — reviewer contract error.

Errors:
- ...

No plan files were modified.

Next step:
Fix api-plan-review.json or re-run aa-api-plan-reviewer.
```

If no fixes were applied, say so clearly and explain why.

Do not claim the API plan review passed. Only the reviewer can pass the gate.
