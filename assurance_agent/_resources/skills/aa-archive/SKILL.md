---
name: aa-archive
description: Use after all applicable review gates pass and execution is PASS or PASS_WITH_WARNINGS. Merges case delta into qa/cases/<module>/case.yaml, confirms test code exists in tests/, archives process artifacts to qa/archive/<change-id>/. Requires valid review JSON with decision=pass for every applicable gate.
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify all required gates have passed (see **Pre-condition** and **Archive Gate** below).
3. Read input files from disk:
   - `review/case-review.json` (required)
   - `review/api-plan-review.json` (if API plan exists)
   - `review/plan-review.json` (if E2E plan exists)
   - `execution/api-result.json`, `execution/e2e-result.json`, `execution/summary.md` (if present)
   - `execution/execution-manifest.yaml` (if present)
   - `inspect/failure-analysis.json`, `inspect/failure-summary.md` (if present)
   - `qa/changes/<change-id>/known-product-issues.md` (source of truth, if present)
   - `execution/known-product-issues.md`, `inspect/known-product-issues.md` (snapshots, if present)
4. If any required gate file is missing or has `decision != "pass"`, stop and report.
5. Use files as the sole source of truth.

**After completing work:**

1. Write archived artifacts to `qa/archive/<change-id>/`.
2. Merge case delta into `qa/cases/<module>/case.yaml`.
3. Archive status is committed by the **archive graph node** (GraphRuntime). Never hand-edit
   `workflow-state.yaml`, never run `aa workflow status`, and never write ledger/checkpoint files.
   Report the intended archive outcome in your final message; the graph node records it.

---

## Purpose

Complete the change cycle: merge primary assets and archive process artifacts.

## When to Use

- User says "archive change `<change-id>`"
- User says "merge and close this change"
- Orchestrator Phase 14 with `auto_archive == true` and all archive gates satisfied

## Archive Gate

ALL of the following must be true before archiving:

**Review gates**

- `qa/changes/<change-id>/review/case-review.json` exists, is valid JSON, and `decision == "pass"`
- If `qa/changes/<change-id>/plans/api-plan.md` exists → `review/api-plan-review.json` must exist and `decision == "pass"`
- If `qa/changes/<change-id>/plans/e2e-plan.md` exists → `review/plan-review.json` must exist and `decision == "pass"`

**Execution gates**

- `phases.execution.status` in `[PASS, PASS_WITH_WARNINGS]` — **NEVER archive when FAIL or SKIPPED-only without explicit user override** (default: STOP on FAIL)
- `phases.healing.status` in `[not_needed, resolved, skipped]` — **NEVER archive when applied/exhausted/failed** (`applied` means fixers ran but re-inspect has not confirmed success yet)
- No unresolved `fix_proposal_eligible` failures in `inspect/failure-analysis.json`

**Inspect gates (when warnings exist)**

- If `phases.execution.status == PASS_WITH_WARNINGS` or known product issues exist:
  - `phases.inspect.status` must be in `[done, partial]` — if not, STOP and run `aa-inspect` first
  - Known product issues must be explicitly acknowledged in archive summary

**Assets**

- `qa/changes/<change-id>/cases/<module>/case.yaml` exists (the case delta)
- `qa/cases/<module>/case.yaml` is writable (the stable main asset)
- Required test files referenced in codegen plans exist under `tests/`

**Pragmatic archive without review JSON is forbidden.** Never archive based on natural language approval, an agent's judgment, or "the tests look done". The only authority to archive is present, valid review JSON with `decision == "pass"` for every applicable gate.

## Output

After archiving:

```
qa/cases/<module>/case.yaml       ← merged with case delta (ADDED / MODIFIED / REMOVED)
tests/api/**                      ← retained as-is (primary asset)
tests/e2e/**                      ← retained as-is (primary asset)
qa/archive/<change-id>/           ← all process artifacts
qa/archive/<change-id>/archive-summary.md
```

## Process

### Step 1: Check Review Gate

**1a. Check case-review.json**

Read `qa/changes/<change-id>/review/case-review.json`.

If missing or invalid JSON → **STOP**.

If `decision != "pass"` → **STOP** immediately. Do not proceed.

**1b. Check API plan review (if API plan exists)**

If `qa/changes/<change-id>/plans/api-plan.md` exists:

- Read `qa/changes/<change-id>/review/api-plan-review.json`.
- If missing or invalid → **STOP**.
- If `decision != "pass"` → **STOP**. API plan review must pass before archiving.

**1c. Check E2E plan review (if E2E plan exists)**

If `qa/changes/<change-id>/plans/e2e-plan.md` exists:

- Read `qa/changes/<change-id>/review/plan-review.json`.
- If missing or invalid → **STOP**.
- If `decision != "pass"` → **STOP**. E2E plan review must pass before archiving.

### Step 1d: Check Execution and Healing Gates

Read `workflow-state.yaml`:

- If `phases.execution.status == FAIL` → **STOP** (unless user explicitly overrides — default workflow forbids archive on FAIL)
- If `phases.execution.status == SKIPPED` and tests were expected → **STOP** or record `not_run` explicitly in summary
- If `phases.healing.status in [exhausted, failed]` → **STOP**
- If `inspect/failure-analysis.json` contains unresolved `fix_proposal_eligible` failures → **STOP**

If `phases.execution.status == PASS_WITH_WARNINGS` or known product issues exist:

- Require `phases.inspect.status in [done, partial]` — otherwise **STOP**, run `aa-inspect` first

### Step 2: Merge Case Delta

Source delta file:

```
qa/changes/<change-id>/cases/<module>/case.yaml
```

Target stable file:

```
qa/cases/<module>/case.yaml
```

For each case in the delta:

- **ADDED**: append new case to target file. Verify `case_id` does NOT already exist.
- **MODIFIED**: find matching `case_id` in target and replace the entire case block.
- **REMOVED**: find matching `case_id` in target and delete the entire case block.

After merge: verify no duplicate `case_id` values, no orphaned `related_cases` references.

If `qa/cases/<module>/case.yaml` does not exist yet, create it with the added cases.

### Step 3: Confirm Test Code

Verify that test files referenced in `api-codegen-plan.md` and `e2e-codegen-plan.md` exist under `tests/api/` and `tests/e2e/`.

- For each `tests/api/test_*.py` referenced in `api-codegen-plan.md`: verify the file exists.
- For each `tests/e2e/test_*.py` referenced in `e2e-codegen-plan.md`: verify the file exists.

If required test files are missing: **STOP**. Test code must exist before archiving.

### Step 3a: Check Known Product Issues

Before writing the archive summary, check for known product issue files:

```text
qa/changes/<change-id>/known-product-issues.md          ← source of truth (written by codegen)
qa/changes/<change-id>/execution/known-product-issues.md ← execution snapshot
qa/changes/<change-id>/inspect/known-product-issues.md   ← inspect snapshot (if present)
qa/changes/<change-id>/inspect/failure-analysis.json
```

If any known product issue record exists:

- Archive is still allowed, provided all review gates pass and execution is `PASS` or `PASS_WITH_WARNINGS`.
- Archive summary **must** use `archive_status: archived_with_warnings`.
- Do **not** write `clean`, `no risk`, or `fully passed without issues` anywhere in the archive summary.

### Step 3b: Record Execution Status

Check whether execution summaries exist:

- `qa/changes/<change-id>/execution/summary.md` (latest run; also at `execution/runs/<batch-id>/summary.md`)
- `qa/changes/<change-id>/execution/api-result.json`
- `qa/changes/<change-id>/execution/e2e-result.json`
- `qa/changes/<change-id>/execution/execution-manifest.yaml`

Use unified execution status values: `PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED | not_run`

Read `final_status` from result JSON when present; otherwise derive from `workflow-state.yaml` `phases.execution.status`.

**Execution failure (`FAIL`) blocks archive by default.** Record status in `archive-summary.md` only when archive is explicitly allowed by workflow policy.

### Step 4: Archive Process Artifacts

> **Order matters:** produce archive artifacts first; the **archive graph node**
> commits archive status to the ledger. Never run
> `aa workflow status --phase archive` (removed) and never hand-edit
> `workflow-state.yaml` to mark archive done before copying evidence.

Copy (do not move) the following to `qa/archive/<change-id>/`:

| Source | Destination |
|--------|-------------|
| `qa/changes/<change-id>/plans/` | `qa/archive/<change-id>/plans/` |
| `qa/changes/<change-id>/review/` | `qa/archive/<change-id>/review/` |
| `qa/changes/<change-id>/execution/` | `qa/archive/<change-id>/execution/` |
| `qa/changes/<change-id>/inspect/` | `qa/archive/<change-id>/inspect/` |
| `qa/changes/<change-id>/trace/` | `qa/archive/<change-id>/trace/` |
| `qa/changes/<change-id>/proposal.md` | `qa/archive/<change-id>/proposal.md` |
| `qa/changes/<change-id>/.qa.yaml` | `qa/archive/<change-id>/.qa.yaml` |
| `qa/changes/<change-id>/known-product-issues.md` | `qa/archive/<change-id>/known-product-issues.md` (if present) |
| `qa/changes/<change-id>/workflow-state.yaml` | `qa/archive/<change-id>/workflow-state.yaml` |

Additional retro evidence requirements:

- Copy `qa/changes/<change-id>/events.jsonl` to `qa/archive/<change-id>/events.jsonl` if it exists. This is required for retro evidence IDs such as `<change-id>#seq<N>`.
- Copy the full `qa/changes/<change-id>/healing/` directory to `qa/archive/<change-id>/healing/` if it exists, including `fix-proposal.json`, `fix-proposal.md`, and all `*-apply-summary.{json,md}` files. Final `inspect/failure-analysis.json` may no longer contain failures that successful healing fixed; retro needs the healing artifacts to recover those learning signals.

Do **not** delete `qa/changes/<change-id>/` after archiving. The change directory is preserved as a reference. Deletion, if desired, must be performed manually by a human.

### Step 5: Write Archive Summary

Write to: `qa/archive/<change-id>/archive-summary.md`

Must include:

- `change_id`
- `archived_at` ISO timestamp (top-level metadata; consumed by `aa retro --since`)
- Cases added / modified / removed (count + IDs)
- Target case file: `qa/cases/<module>/case.yaml`
- Test files created or updated
- `case-review.json` decision and risk level
- API plan review decision and risk level (if applicable, from `api-plan-review.json`)
- E2E plan review decision and risk level (if applicable, from `plan-review.json`)
- Execution status section:
  - API execution: `PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED | not_run` + counts
  - E2E execution: `PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED | not_run` + counts
  - Workflow execution: `phases.execution.status`
  - Note: `FAIL` blocks archive by default; `PASS_WITH_WARNINGS` requires inspect acknowledgment
- Archive status: `archived` | `archived_with_warnings`
- Known product issues section (if applicable):

```yaml
archive_status: archived_with_warnings
known_product_issues:
  - id: KPI-001
    endpoint: GET /api/v1/menu/get
    severity: major
    status: open
    workaround: validated through GET /api/v1/menu/list
    coverage_gap: direct GET /api/v1/menu/get remains open
    follow_up: fix backend serialization in get_menu
```

## What NEVER Gets Merged Into qa/cases/

- Plan files (`plans/*.md`) — process artifacts
- Review files (`review/*.json`, `review/*.md`) — process artifacts
- Execution results (`execution/*.json`) — historical records
- Inspect results (`inspect/*`) — historical records
- `proposal.md` — process asset

## File Path Reference

| Old (do not use) | Correct |
|---|---|
| `qa/changes/<id>/case-delta/` | `qa/changes/<id>/cases/<module>/case.yaml` |
| `qa/cases/<module>.md` | `qa/cases/<module>/case.yaml` |
| `case.review.yaml` | `review/case-review.json` |
| `code.review.yaml` | `review/plan-review.json` or `review/api-plan-review.json` |
| `execution.review.yaml` | `execution/e2e-result.json` or `execution/api-result.json` |
| `passed` / `passed_with_known_issues` | `PASS` / `PASS_WITH_WARNINGS` |
| `archived_with_known_issues` | `archived_with_warnings` |

## Red Flags

| Thought | Reality |
|---------|---------|
| "I'll merge plan files into qa/cases/" | Plans are NEVER merged into cases. |
| "I'll archive without checking case-review.json" | Case review gate is mandatory — must be `decision: pass`. |
| "User approved in chat, I'll archive without the JSON" | Pragmatic archive without review JSON is forbidden. Only a valid `decision: pass` JSON releases the gate. |
| "Tests failed so I'll skip archiving" | Correct — `FAIL` blocks archive by default. Record status and STOP. |
| "API plan review is not pass but I'll archive anyway" | API/E2E plan reviews must also pass if the corresponding plan files exist. |
| "The test code is already there, skip step 3" | Confirm explicitly — step 3 is a safety check. |
| "I'll skip the archive summary" | Archive summary is the audit trail. |
| "Tests passed so archive is clean" | If `known-product-issues.md` exists or execution is `PASS_WITH_WARNINGS`, archive must use `archived_with_warnings`. |
| "I'll write to qa/cases/<module>.md" | Cases are YAML, not Markdown: `qa/cases/<module>/case.yaml`. |
| "Execution FAIL but I'll archive anyway" | Default workflow forbids archive on FAIL. |
| "PASS_WITH_WARNINGS without inspect is fine" | Run `aa-inspect` first; require `phases.inspect.status in [done, partial]`. |

## Post-Archive State

Change cycle is complete:

- `qa/cases/<module>/case.yaml` reflects new/modified/removed cases
- `tests/` contains test code (primary assets)
- `qa/archive/<change-id>/` holds all process artifacts for audit
- `qa/changes/<change-id>/` is preserved (not deleted — manual cleanup only)
- `phases.archive.status` is `archived` or `archived_with_warnings`
