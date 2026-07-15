---
name: aa-execute
description: "Fallback for two-stage execute mode. Prefer workflow_start / `aa workflow run --scope execute`. When the driver is unavailable, stamp execute run_context and follow aa-workflow/FALLBACK-RUNBOOK.md for post-intake phases only."
---

# AA Execute

## Preferred entry (driver)

After `aa-intake` completes and the user confirms:

1. Call OpenCode tool **`workflow_start`** with `scope: "execute"`, **or**
2. CLI: `aa workflow run --change <change-id> --scope execute`

Do not re-run intake phases. The driver enforces execute-scope preflight (case-review pass,
cases present, test infra, etc.).

## Fallback (driver unavailable)

1. Resolve `<change-id>` and params (`run_mode` ∈ `full` | `api-only` | `e2e-only` | `plan-only` | `codegen-only` | `review-plan`).
2. Stamp run context (do not hand-write `run_context`):

```bash
aa state configure --change <change-id> --orchestrator aa-execute
```

3. **Preflight** (STOP if unmet — execute never asks interactive bootstrap questions):
   - `review/case-review.json` with `decision == "pass"`
   - `qa/changes/<id>/cases/` present
   - no unanswered explore open questions
   - `tests/config.py`, `tests/conftest.py`, `tests/schema_validation.py` conformant
   - execute-scope skill registry + derive `gates.healing_available` (see fallback runbook Phase 1.1 execute subset)
4. Open **`skills/aa-workflow/FALLBACK-RUNBOOK.md`** and run only **execute-scope** phases
   (`fact-baseline` → plan/review/fix → codegen → execution → inspect → healing → report →
   archive-eligibility) with `run_context.active_scope = execute`.
5. Explore / case-design / case-review / case-fix stay out of scope.

## Completion

Terminal when `aa status --change <id> --next --json` reports `completed` for
`active_scope: execute` (or driver exit 0 / paused 30 for human review).

## Human review

Never hand-edit review JSON to `pass`. Use `aa decide` after the user decides, then
resume via `workflow_start` / `aa workflow run` (or continue the fallback loop).
