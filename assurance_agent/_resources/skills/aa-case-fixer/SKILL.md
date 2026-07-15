---
name: aa-case-fixer
description: "Apply safe, automatic fixes to AA case artifacts based on case-review.json. Use after aa-case-reviewer returns decision=needs_fix with auto_fix_allowed=true and human_review_required=false. Never invents product behavior."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Read `events.jsonl` and determine the execution mode before validating the old review fields:
   - **automatic mode**: `phases.case_review.status == needs_fix`.
   - **human-approved mode**: the latest `human_decision` for checkpoint `case-review-gate` (or legacy checkpoint `case-review`) has action `fix_and_proceed`, and its `review_sha256` matches the current `review/case-review.json`.
   - Otherwise stop.
3. Read input files from disk: `review/case-review.json`, `cases/<module>/case.yaml`, `proposal.md`.
4. Always verify `review_type == "case"` and `change_id == <change-id>`. In human-approved mode, apply only changes explicitly resolved by the decision reason. **human-approved mode bypasses the default auto-fix gate-field requirements** for `decision`, `next_action`, `human_review_required`, `auto_fix_allowed`, and non-empty `auto_fix_plan`.
5. In automatic mode, verify the remaining `case-review.json` gate fields in order — stop on first failure:
   - `decision == "needs_fix"`.
   - `human_review_required == false` — if not, stop **unless** human_approved exception applies (see below).
   - `auto_fix_allowed == true`.
   - `auto_fix_plan` is non-empty.
   - `next_action == "run_case_fixer"`.
   - No `auto_fix_plan` item references a `high` or `critical` severity finding **unless** human_approved exception applies.

**human-approved exception**: In human-approved mode, allow fixing blocker/high severity findings explicitly resolved by the decision reason; record them in apply-summary `human_approved_fixes[]`. Never write review JSON.

6. Use files as the sole source of truth.

**After completing work:**

1. Write updated files (only those actually changed):
   - `qa/changes/<change-id>/cases/<module>/case.yaml` (fixed version)
   - `qa/changes/<change-id>/proposal.md` (if fix requires proposal update)
   - `qa/changes/<change-id>/review/case-review-apply-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - Append to `phases.case_review.fix_attempts`: `{ attempt: N, files_modified: [...], timestamp: <now> }`
   - Do NOT change `phases.case_review.status` — only the reviewer can update the gate status

---

# AA Case Fixer

## Purpose

Apply safe, automatic fixes to AA case artifacts based on `case-review.json`.

Use this skill after `aa-case-reviewer` returns:

```text
decision = needs_fix
auto_fix_allowed = true
human_review_required = false
```

This skill must only apply safe, mechanical fixes. It must not invent product behavior.

---

## When to Use

Use this skill when:

- `case-review.json` exists.
- Review findings are auto-fixable.
- No human review is required.
- The orchestrator wants to apply case review suggestions before re-running case review.

---

## Inputs

Required:

```text
qa/changes/<change-id>/review/case-review.json
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
```

Optional:

```text
qa/changes/<change-id>/.qa.yaml
qa/cases/**/*.yaml
.aa/data-knowledge.yaml
```

## Outputs

Update only the allowed files:

```text
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
qa/changes/<change-id>/trace/minimum-coverage-matrix.yaml
```

Write an apply summary:

```text
qa/changes/<change-id>/review/case-review-apply-summary.md
```

Do not edit:

```text
qa/changes/<change-id>/review/case-review.json
qa/changes/<change-id>/review/case-review-summary.md
qa/changes/<change-id>/plans/**
tests/**
```

The orchestrator may delete or regenerate `case-review.json` after this skill completes.

---

## Target File Allowlist

Only modify files under these paths:

```text
qa/changes/<change-id>/cases/**/*.yaml
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/trace/minimum-coverage-matrix.yaml
```

**Rules:**

- Reject or skip any `auto_fix_plan` item whose `target_file` is outside these paths.
- `trace/minimum-coverage-matrix.yaml` is the Minimum Required Coverage (MRC) matrix that `aa-case-reviewer` hard-gates on. You may only make **mechanical** edits here: add/adjust a covered_by_cases entry when a case's `trace.minimum_required_coverage` already lists that MRC id, or align a matrix row with an existing case trace. Do NOT invent coverage, and do NOT author a `skipped_by_scope` decision — deciding that an MRC item is out of scope is a human/reviewer judgement (the reviewer routes those to `needs_human_review`, not `needs_fix`).
- Never modify `qa/changes/<change-id>/trace/traceability-matrix.yaml` — that file is owned by `aa-run` / `aa-archive`, not the case layer.
- Never modify `qa/cases/**` — these are stable main assets; only `aa-archive` may merge into them.
- Never modify `tests/**`, `plans/**`, `src/**`, or any path outside `qa/changes/<change-id>/`.

---

## Gate Boundaries

This fixer must never bypass the reviewer. It is **not** a gate producer.

- Only apply findings where `auto_fix_allowed == true` and `human_review_required == false`.
- **Never** write or modify `case-review.json` or any review JSON file.
- **Never** set or change `decision` to `pass` — the fixer has no authority to pass the gate.
- After applying fixes, you **must** request a fresh run of `aa-case-reviewer`. The workflow only continues when a **new** `case-review.json` has `decision == "pass"`.
- A fixer's claim that it "fixed everything" does not release the gate. Only the reviewer's new JSON does.

---

## Allowed Fixes

You may apply fixes for findings where **all** of the following are true:

- `auto_fix_allowed = true`
- `human_review_required = false`

For `severity in ["high", "critical"]`: **STOP the entire fixer immediately** — do not skip individual items, do not apply any other fixes. A valid `case-review.json` produced by `aa-case-reviewer` must never reference `high` or `critical` findings in `auto_fix_plan`. If one does, it is a reviewer safety error; require `aa-case-reviewer` to regenerate a valid JSON before retrying.

Allowed operations:

- Normalize YAML formatting
- Add missing non-controversial metadata
- Add or normalize tags
- Normalize `status` / `priority` / `severity` values
- Improve wording clarity
- Remove duplicate wording
- Add trace fields when source is explicit
- Split overly long steps into clear smaller steps
- Strengthen assertions when the expected result is explicitly stated (must not introduce forbidden content: no API paths, selectors, tokens, credentials, code, or environment-specific values)
- Add missing cleanup note if the cleanup method is already known

**Layering fixes** (`category: layering`, `auto_fix_allowed: true`):

- REQUIRED: the finding MUST have a `fix_scope` field. If `fix_scope` is absent, STOP the entire fixer immediately — this is a reviewer error; do not attempt to guess the fix scope, do not apply any other fixes.
- The ONLY permitted action is adjusting `automation_targets` on the specific case identified in the finding (e.g. `[e2e] → [api]`).
- Scope is strictly **single-case**: do not split cases, merge cases, move assertions, or change any field other than `automation_targets`.
- Do not change `title`, `steps`, `assertions`, `summary`, or any semantic content.

---

## Forbidden Fixes

Do not apply fixes that require guessing:

- Product behavior
- User role permission
- API endpoint
- Page route
- UI selector
- Auth mechanism
- Data factory
- Business state transition
- Expected response body
- Database side effects
- Error message text
- Internationalization copy
- Environment-specific values

Do not create new scenarios that were not requested.

Do not delete cases unless the review explicitly marks them as duplicate and auto-fixable.

Do not overwrite existing case files wholesale. Prefer minimal diffs.

For `layering` findings specifically:
- Do not use a `layering` finding to split, merge, or restructure cases — that is a Type 2 design issue requiring human review.
- Do not change any field other than `automation_targets` when applying a `layering` fix.

---

## Fix Strategy

1. Read `case-review.json`.
2. Check all gate fields in this order — **STOP on the first failed condition**:
   - `review_type == "case"` — if not, stop.
   - `case-review.json.change_id == <change-id>` — if mismatch, STOP (wrong review JSON for this change).
   - `decision == "needs_fix"` — if not (`pass`, `needs_human_review`, `reject`), stop.
   - `human_review_required == false` — if not, stop.
   - `auto_fix_allowed == true` — if not, stop.
   - `auto_fix_plan` exists and is non-empty — if empty, stop (no safe plan to execute).
   - `next_action == "run_case_fixer"` — if not, stop (inconsistent reviewer output).
3. Pre-flight severity check — **STOP immediately if any `auto_fix_plan` item references a finding with `severity in ["high", "critical"]`**:
   - Do not apply any fixes.
   - Record the reviewer error in `case-review-apply-summary.md` if possible.
   - Require `aa-case-reviewer` to regenerate a valid `case-review.json` without high/critical findings in `auto_fix_plan`.
4. Collect and validate fixable items — **only from `case-review.json`.`auto_fix_plan`**:
   - Only apply fixes that have an explicit entry in `auto_fix_plan`.
   - Findings without an `auto_fix_plan` entry must **not** be changed, even if `auto_fix_allowed == true`.
   - Do **not** infer fixes from vague `findings` messages.
   - For every `auto_fix_plan` item, perform these validation steps before applying — skip and record in **Skipped Findings** if any individual check fails:
     a. Resolve `finding_id` to an existing finding in `findings[]`. If no match, skip.
     b. Verify the referenced finding has `auto_fix_allowed == true`. If not, skip.
     c. Verify the referenced finding has `human_review_required == false`. If not, skip.
     d. Verify the referenced finding has `severity in ["low", "medium"]`. (Severity `high`/`critical` triggers a full STOP at step 3; this is a safety-net re-check.)
     e. Verify `target_file` is under an allowed path (see **Target File Allowlist**). If not, skip.
     f. Verify `operation` is one of `edit`, `append`, `normalize`, `rename`. If unsupported, skip and record the reason.
   - If all `auto_fix_plan` items are invalid or skipped, apply no changes and stop.
5. Apply minimal edits to target files.
6. Preserve existing style and ordering where possible.
7. Write `case-review-apply-summary.md`.
8. Report the state delta (applied to `workflow-state.yaml` per the Context Contract):
   - Append fix attempt record: `{ attempt: N, files_modified: [...] }`
   - Do NOT modify `phases.case_review.status` — gate status is reviewer-only.
9. Tell the orchestrator to re-run `aa-case-reviewer`.

---

## Apply Summary Format

Write:

```text
qa/changes/<change-id>/review/case-review-apply-summary.md
```

Use this structure:

```markdown
# Case Review Apply Summary

## Change ID

<change-id>

## Applied Fixes

| Finding ID | File | Operation | Summary |
|---|---|---|---|

## Skipped Findings

| Finding ID | Reason |
|---|---|

## Files Modified

- ...

## Next Step

Re-run `aa-case-reviewer`.
```

---

## Final Response

After applying fixes, respond with:

```
Case review fixes applied.

Modified files:
- ...

Skipped findings:
- ...

Next step:
Re-run aa-case-reviewer.
```

If no fixes were applied, say so clearly and explain why.

Do not claim the case review passed. Only the reviewer can pass the gate.
