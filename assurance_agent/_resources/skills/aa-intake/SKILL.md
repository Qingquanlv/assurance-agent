---
name: aa-intake
description: "Run two-stage AA intake mode: interactive explore + case design + case review/fix only. Prefer `aa workflow run --entrypoint case|intake` / workflow_start. Stops after case review passes; after user confirmation hand off via workflow_start (entrypoint: execute)."
---

# AA Intake

`aa-intake` is the **two-stage / interactive** orchestration entrypoint for design-time clarification.

> **Preferred entry:** `aa workflow run --change <id> --entrypoint case` (or `intake`) /
> OpenCode `workflow_start` with the matching entrypoint. GraphRuntime is the only
> progression writer — do not hand-edit `workflow-state.yaml` or invent phase completion.
>
> **Preferred handoff:** after intake completes and the user confirms, call
> `workflow_start` (`entrypoint: execute`) or
> `aa workflow run --change <id> --entrypoint execute`.

It owns the intake scope:

```text
explore -> case-design -> case-review -> case-fix loop
```

It does not run fact-baseline, plan, codegen, execution, inspect, healing, report, or archive phases.

## What is inline here vs referenced

The **ordered intake runbook is inline in this file** (see **Intake Runbook** below) — the interactive explore/case-design behaviour, the case-review gate check, and the case-fix loop are written here as a gap-free checklist for the interactive agent.

Operator recovery (expired lease, interrupt, digest drift) lives in
`skills/aa-workflow/FALLBACK-RUNBOOK.md` — inspect / resume / import only. There is no
hand-edited phase fallback and no agent orchestration loop that writes progression.

Deep per-phase contracts: load `aa-explore`, `aa-case-design`,
`aa-case-reviewer` when dispatched; this skill only sequences them.

## Startup

1. Resolve `<change-id>` and runtime params. Prefer starting via
   `aa workflow run --entrypoint case|intake` / `workflow_start` so GraphRuntime owns the ledger.
2. Ensure params `run_mode` is one of `full`, `case-only`, or `review-case`.
3. Intake-phase skills expected by the registry:
   - `aa-explore`
   - `aa-case-design`
   - `aa-case-reviewer`
4. Prefer driver-stamped run context via `aa workflow run` / `workflow_start`. Do not hand-write `run_context`.

## Orchestration

Prefer GraphRuntime (`aa workflow run` / `aa status` / `aa workflow resume`).
`aa status --change <id> --next --json` projects next work; only intake-scope nodes are
dispatched under `entrypoint: case|intake`. The **Intake Runbook** below is the ordered
checklist the interactive agent walks when clarifying with the user — artifact production
is not completion; the ledger is.

## Intake Runbook (ordered, inline)

**Phase Completion Rule.** Producing artifacts on disk is NOT completion. GraphRuntime
commits node outcomes to the ledger; operators never hand-edit progression. Prefer
`aa workflow run --entrypoint case` / `workflow_start`.

```
Phase 1.2 — Explore              (INLINE in primary agent when interactive)
  → load aa-explore; interaction_mode: interactive → ask per-pitfall open questions
  → write explore/advisory.json (open_questions answered_via: aa-intake, with user confirmation)
  → aa risk validate-advisory: FAILS interactive intake if OQs were
    answered via explore / auto_default or lack user-confirmation metadata
  → ledger: GraphRuntime commits explore — then aa status

Phase 2.1 — Case Design
  → dispatch aa-case-design; interaction_mode: interactive → clarify + get EXPLICIT user approval
    before writing files
  → outputs .qa.yaml (incl. approval.approved_by: user / approved_approach / approved_at),
    proposal.md, cases/<module>/case.yaml
  → case-design-gate will NOT mark cases done without .qa.yaml.approval
  → ledger: GraphRuntime commits case-design — then aa status

Phase 2.2 — Case Review (initial)
  → dispatch aa-case-reviewer → review/case-review.json   (reviewer JSON is the release gate)
  → aa gate check --phase case-review --change <id> --json      ← REQUIRED for local evidence
  → ledger: GraphRuntime commits case-review + gate — then aa status

Phase 2.3 — Case Fix loop (if gate needs_fix)
  → for each attempt (max = max_case_fix_attempts):
      aa-case-design → aa-case-reviewer → aa gate check
      pass → exit loop ; reject / human_review_required → see Human Review below ; exhausted → STOP
  → ledger: GraphRuntime commits each re-review — then aa status
```

### Human Review (intake scope) — no hand-written pass

If the case-review gate returns `needs_human_review` or `reject`: **STOP** and ask the user. Never edit `review/case-review.json` to `decision: pass`. To proceed after a human decision, use `aa workflow resume --change <id> --interrupt <interrupt-id> --action fix_and_proceed --reason "<user decision>"`.

## Completion Condition

`aa-intake` is complete only when:

1. `explore/advisory.json` exists and has no `open_questions_for_case_design[].status == "unanswered"`;
2. `qa/changes/<change-id>/.qa.yaml` contains `approval.approved_by: user`, `approval.approved_approach`, and `approval.approved_at` for interactive intake;
3. `qa/changes/<change-id>/cases/` contains generated case delta YAML and `case-design-gate` passes;
4. `review/case-review.json` exists with `decision == "pass"`;
5. `aa status --change <id> --next --json` reports terminal `completed` for `entrypoint: case|intake`.

When complete, stop. Do not automatically continue into execute scope.

### Handoff (after user confirmation)

1. Ask the user to confirm starting autonomous execute for `<change-id>`.
2. **Preferred:** call the `workflow_start` tool with `change_id` and `entrypoint: execute`.
   Progress continues in nested sessions / QA panel; do not run execute phases yourself.
3. **Fallback** (tool missing or driver unavailable):

```text
Intake complete. To continue autonomous execution:
  - Prefer: workflow_start tool (entrypoint: execute), or
  - CLI: aa workflow run --change <change-id> --entrypoint execute
  - Legacy skill: aa-execute
```
