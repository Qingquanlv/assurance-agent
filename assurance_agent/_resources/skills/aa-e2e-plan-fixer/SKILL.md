---
name: aa-e2e-plan-fixer
description: "Apply safe, automatic fixes to AA E2E plan artifacts based on plan-review.json. Use after aa-e2e-plan-reviewer returns decision=needs_fix with auto_fix_allowed=true and human_review_required=false. Never invents routes, selectors, auth, or data factories."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read the bound `## Runtime Context` prompt block (`platform:plan-fixer-runtime-context/v1`). Use its `mode` and `source_review_*` fields as the sole authority for why this fixer was invoked:
   - **automatic_healing**: proceed with automatic-mode gate validation below.
   - **human_approved**: require `resume_action == fix_and_proceed`, non-empty `human_reason`, and `source_review_sha256` matching the current review bytes. Do not inspect the host coordinator ledger.
   - Otherwise stop.
2. Read input files from disk: `review/plan-review.json`, `plans/e2e-plan.md`, `plans/e2e-test-data-plan.md`, `plans/e2e-codegen-plan.md`, `plans/m4-review-summary.md`.
3. Always verify `review_type == "e2e-plan"` and `change_id == <change-id>`. In human_approved mode, apply only changes explicitly resolved by the decision reason. **human_approved mode bypasses the default auto-fix gate-field requirements** for `decision`, `next_action`, `human_review_required`, `auto_fix_allowed`, and non-empty `auto_fix_plan`.
4. In automatic_healing mode, verify the remaining gate fields in order — **STOP** on first failure:
   - `decision == "needs_fix"` — if not, stop.
   - `next_action == "run_e2e_plan_fixer"` — if not, stop as reviewer contract error.
   - `human_review_required == false` — if not, stop **unless** human_approved exception applies (see below).
   - `auto_fix_allowed == true` — if not, stop.
   - `auto_fix_plan` exists and is non-empty — if missing or empty, stop as reviewer contract error.

**human_approved exception**:

> In human_approved mode, allow fixing blocker / high severity items explicitly resolved by the decision reason, using `findings[]` and files on disk as evidence. Record each item in `plan-review-apply-summary.md` under `human_approved_fixes[]`. **Still never write review JSON or change gate status** — re-run `aa-e2e-plan-reviewer` after fixes.

5. In automatic_healing mode, resolve every `auto_fix_plan` item before applying fixes. In human_approved mode, validate each approved change against the decision reason, matching finding, and target-file allowlist.
6. Use files as the sole source of truth.

**After completing work:**

1. Write updated files (only those actually modified):
   - `qa/changes/<change-id>/plans/e2e-plan.md`
   - `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
   - `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
   - `qa/changes/<change-id>/plans/m4-review-summary.md`
   - `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml` (only if changed per authorized `auto_fix_plan` entry — see **data-knowledge.proposal.e2e.yaml** rules)
   - `qa/changes/<change-id>/review/plan-review-apply-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - Append to `phases.e2e_plan_review.fix_attempts` — on **success** or **reviewer contract error** (always record the attempt; orchestrator must not rely on chat context alone):
     ```yaml
     - attempt: <n>
       source_review: review/plan-review.json
       apply_summary: review/plan-review-apply-summary.md
       fixed: [<finding-id>, ...]          # [] on contract error
       skipped: [<finding-id>, ...]        # [] on contract error
       contract_errors: [<error>, ...]     # non-empty on contract error
       files_modified: [<path>, ...]       # [] on contract error — no plan files modified
       timestamp: <now>
     ```
   - **Do NOT** modify `phases.e2e_plan_review.status` — only `aa-e2e-plan-reviewer` updates gate status.
   - **Do NOT** create or update `phases.e2e_plan_fixer.status` unless the workflow schema explicitly defines it.

On reviewer contract error: write `plan-review-apply-summary.md` with **Reviewer Contract Errors**, set `fixed: []`, `skipped: []`, `files_modified: []`, populate `contract_errors`, then **STOP** — do not modify plan files.

---

# AA E2E Plan Fixer

## Purpose

Apply safe, automatic fixes to AA E2E plan artifacts based on `plan-review.json`.

Use this skill after `aa-e2e-plan-reviewer` returns:

```text
decision = needs_fix
auto_fix_allowed = true
human_review_required = false
next_action = run_e2e_plan_fixer
```

This skill must only apply safe, mechanical plan fixes. It must not invent unknown product behavior, routes, auth, selectors, or data factories.

---

## When to Use

Use this skill when:

- `plan-review.json` exists.
- The E2E plan reviewer found auto-fixable issues.
- No human review is required.
- The orchestrator wants to re-run plan review after applying fixes.

---

## Inputs

Required:

```text
qa/changes/<change-id>/review/plan-review.json
qa/changes/<change-id>/plans/e2e-plan.md
qa/changes/<change-id>/plans/e2e-test-data-plan.md
qa/changes/<change-id>/plans/e2e-codegen-plan.md
qa/changes/<change-id>/plans/m4-review-summary.md
```

Optional (only present when `.aa/data-knowledge.yaml` was missing during planning):

```text
qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml
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
qa/changes/<change-id>/plans/e2e-plan.md
qa/changes/<change-id>/plans/e2e-test-data-plan.md
qa/changes/<change-id>/plans/e2e-codegen-plan.md
qa/changes/<change-id>/plans/m4-review-summary.md
qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml
```

Write:

```text
qa/changes/<change-id>/review/plan-review-apply-summary.md
```

Do not edit:

```text
qa/changes/<change-id>/review/plan-review.json
qa/changes/<change-id>/review/plan-review-summary.md
qa/changes/<change-id>/review/e2e-plan-checks.json
tests/**
qa/changes/<change-id>/cases/**
qa/changes/<change-id>/proposal.md
.aa/data-knowledge.yaml
```

Do not directly update `.aa/data-knowledge.yaml`. Only propose updates in `data-knowledge.proposal.e2e.yaml` when authorized by a validated `auto_fix_plan` entry. Never promote proposal content into L1 during fixer execution.

---

## Gate Boundaries

This fixer must never bypass the reviewer. It is **not** a gate producer.

- Only apply findings where `auto_fix_allowed == true` and `human_review_required == false`.
- **Never** write or modify `plan-review.json` or any review JSON file.
- **Never** set or change `decision` to `pass` or `codegen_readiness` to `ready` — the fixer has no authority to pass the gate.
- **Never** modify `phases.e2e_plan_review.status` — only the reviewer updates gate status.
- **Do NOT** create or update `phases.e2e_plan_fixer.status` unless the workflow schema explicitly defines it.
- Fix attempt records belong under `phases.e2e_plan_review.fix_attempts` — same pattern as `phases.case_review.fix_attempts`.
- After applying fixes, you **must** request a fresh run of `aa-e2e-plan-reviewer`. The workflow only continues to `aa-e2e-codegen` when a **new** `plan-review.json` has `decision == "pass"` and `codegen_readiness in ["ready","ready_with_warnings"]`.
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
- Repair or restore `e2e-codegen-plan.md` **Factory Mapping** / **Adapter Mapping** rows only when an authorized `auto_fix_plan` entry or validated finding targets that table; copy symbols from `.aa/data-knowledge.yaml` or existing plan evidence — never invent L1 leaves or promote proposal YAML into `.aa/data-knowledge.yaml`
- Add TODO markers only for already documented non-blocking warnings (see below)
- Add missing cleanup note if cleanup method is already known
- Clarify fixture reuse if present in `.aa/data-knowledge.yaml`
- Add codegen constraints such as "append only, do not overwrite existing tests"

**TODO marker rule:** Do not convert blockers into TODO-only warnings. Unknown route, unknown selector, unknown auth state, missing required fixture/factory, unknown expected UI copy, or unsafe cleanup must remain blockers or human-review items. Adding a TODO marker to a genuine blocker does not resolve it and must not change the finding's severity or status.

---

## Forbidden Fixes

Do not invent:

- Route path
- UI selector
- `data-testid`
- Button text
- Page title
- Auth mechanism
- Test credential
- Fixture name
- Factory name
- API endpoint
- Request/response schema
- Business state transition
- Cleanup endpoint
- Product behavior

Do not change:

- `codegen_readiness`
- Review decision fields
- Reviewer JSON
- Approved case content
- Source requirement interpretation

Do not hide unknowns. If something is unknown, make it explicit in the plan as a TODO or human review item — only when the reviewer already classified it as a non-blocking warning.

Do not directly update `.aa/data-knowledge.yaml`. Only propose updates in `data-knowledge.proposal.e2e.yaml` when authorized by a validated `auto_fix_plan` entry. Never promote proposal content into L1 during fixer execution.

---

## Fix Source Rule

Only apply fixes explicitly listed in `plan-review.json` under `auto_fix_plan`.

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

These failures mean the reviewer JSON contract is untrustworthy. **Do not skip and continue.** Record the error in **Reviewer Contract Errors** in the apply summary, append a `fix_attempts` entry with `files_modified: []`, then stop.

If a finding in `findings[]` has no matching `auto_fix_plan` entry, add it to **Skipped Findings** in the apply summary (reviewer chose not to auto-fix it). Do not improvise a fix.

---

## Target File Allowlist

`auto_fix_plan.target_file` must be one of:

```text
qa/changes/<change-id>/plans/e2e-plan.md
qa/changes/<change-id>/plans/e2e-test-data-plan.md
qa/changes/<change-id>/plans/e2e-codegen-plan.md
qa/changes/<change-id>/plans/m4-review-summary.md
qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml
```

If `target_file` is outside this allowlist and appears in `auto_fix_plan`, **STOP** as reviewer contract error — do not skip and continue.

**`data-knowledge.proposal.e2e.yaml` creation rule:**

May create or modify `data-knowledge.proposal.e2e.yaml` **only when**:

- `auto_fix_plan.target_file` is exactly `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`
- The referenced finding is `severity in ["low", "medium"]` with `auto_fix_allowed == true` and `human_review_required == false`
- Content is limited to missing capability descriptions or `discovered_candidates` already evidenced in files on disk
- Do **not** write confirmed capabilities — proposals only
- Do **not** create the file proactively outside an authorized `auto_fix_plan` entry

---

## Fix Strategy

1. Read `plan-review.json`.
2. Validate top-level gate fields (Context Contract step 4):
   - `review_type == "e2e-plan"`
   - `change_id == <change-id>`
   - `decision == "needs_fix"`
   - `next_action == "run_e2e_plan_fixer"`
   - `human_review_required == false`
   - `auto_fix_allowed == true`
   - `auto_fix_plan` non-empty
3. For each `auto_fix_plan` item, run **Per-Item Validation** (Fix Source Rule). Any failure → write apply summary with **Reviewer Contract Errors**, append `fix_attempts` entry with `files_modified: []`, and **STOP**.
4. Apply minimal edits to target plan files per validated `auto_fix_plan` instructions only.
5. Preserve existing style and ordering.
6. Add unknowns to plan TODO sections or `data-knowledge.proposal.e2e.yaml` — only when authorized by a validated `auto_fix_plan` entry and the unknown is already a non-blocking reviewer warning. Never downgrade blockers.
7. Write `plan-review-apply-summary.md`.
8. Report the state delta: append to `phases.e2e_plan_review.fix_attempts` (do **not** modify `phases.e2e_plan_review.status`) — applied to `workflow-state.yaml` per the Context Contract.
9. Tell the orchestrator to re-run `aa-e2e-plan-reviewer`.

---

## File-Specific Rules

### `e2e-plan.md`

May update:

- Scenario mapping
- Step clarity
- Assertion mapping
- Flow ordering
- Known route notes (only when route is already known from case YAML or frontend source)
- TODO markers only for items the reviewer already documented as non-blocking warnings — **must not** add TODOs for unknown route, selector, auth state, setup capability, cleanup, or expected UI copy when they are blockers, human-review items, or codegen-blocking unknowns

Do not invent executable details.

### `e2e-test-data-plan.md`

May update:

- Entity list
- Known states from case YAML
- Known fixture reuse
- Cleanup notes
- TODO markers only for unknown factories / states already documented by the reviewer as non-blocking warnings

Do not invent factories or cleanup endpoints.

### `e2e-codegen-plan.md`

May update:

- File target
- Append-only rule
- Existing test style notes
- Required command
- Codegen constraints
- TODO markers only for items the reviewer already documented as non-blocking warnings

### `m4-review-summary.md`

May update:

- Summary of remaining risks
- Notes that fixes were applied (add separate note: `Pending re-review after fixes.` — not as an enum value)
- Human review notes

**Readiness rules** (enum values: `ready` | `ready_with_warnings` | `not_ready` only):

- Only `aa-e2e-plan-reviewer` may **upgrade** readiness.
- The fixer **must never upgrade** readiness.

**Allowed:**

- Preserve existing **Plan Readiness** and **Codegen Readiness** enum values
- Downgrade readiness to `not_ready` if the existing value is unsafe after the edit

**Forbidden:**

- Change `not_ready` → `ready_with_warnings`
- Change `not_ready` → `ready`
- Change `ready_with_warnings` → `ready`

- Do **not** replace enum readiness values with `pending re-review` or any non-enum string.
- If `m4-review-summary.md` is updated, add a separate note (outside the enum fields): `Pending re-review after fixes.`

### `data-knowledge.proposal.e2e.yaml`

May append proposed knowledge entries **only** when authorized by a validated `auto_fix_plan` entry targeting this file (see Target File Allowlist).

Do not create or expand the file outside an authorized `auto_fix_plan` entry.

Do not promote proposals into `.aa/data-knowledge.yaml`.

Do not write confirmed capabilities — proposals only.

---

## Apply Summary Format

Write:

```text
qa/changes/<change-id>/review/plan-review-apply-summary.md
```

Use this structure:

```markdown
# Plan Review Apply Summary

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

Re-run `aa-e2e-plan-reviewer`.
```

---

## Final Response

After applying fixes, respond with:

```
Plan review fixes applied.

Modified files:
- ...

Reviewer contract errors:
- ... (if any — fixer must have STOPped before applying fixes)

Skipped findings:
- ...

Remaining unknowns:
- ...

Next step:
Re-run aa-e2e-plan-reviewer.
```

If reviewer contract errors occurred, respond with:

```
Plan fixer STOPped — reviewer contract error.

Errors:
- ...

No plan files were modified.

Next step:
Fix plan-review.json or re-run aa-e2e-plan-reviewer.
```

If no fixes were applied, say so clearly and explain why.

Do not claim the plan review passed. Only the reviewer can pass the gate.
