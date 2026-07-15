---
name: aa-intake
description: "Run two-stage AA intake mode: interactive test-infra bootstrap (Phase 0) + explore + case design + case review/fix only. Stops after case review passes; after user confirmation call workflow_start (prefer) or aa-execute fallback."
---

# AA Intake

`aa-intake` is the **two-stage / interactive** orchestration entrypoint for design-time clarification.

> **Preferred handoff (M3+):** after intake completes and the user confirms, call the OpenCode
> `workflow_start` tool (`scope: execute`) so the TS driver owns post-intake phases.
> Fallback when the tool/driver is unavailable: tell the user to run `aa-execute` or
> `aa workflow run --change <id> --scope execute`.

It owns the intake scope plus **Phase 0 test-infra bootstrap** (runs at startup, before explore):

```text
test-infra-bootstrap (Phase 0) -> explore -> case-design -> case-review -> case-fix loop
```

It does not run fact-baseline, plan, codegen, execution, inspect, healing, report, or archive phases.

## What is inline here vs referenced

The **ordered intake runbook is inline in this file** (see **Intake Runbook** below) — Phase 0 bootstrap, the interactive explore/case-design behaviour, the case-review gate check, and the case-fix loop are written here as a gap-free checklist.

Still **referenced** from `skills/aa-workflow/FALLBACK-RUNBOOK.md` when the TS driver is
unavailable (shared fallback; single source of truth to avoid drift):

- **Shared orchestration mechanics**: Scheme E dispatch + state-hash rules, Phase Completion Rule, Skill Load Gate, Phase → Skill Path Map, `workflow-state.yaml` schema, Subagent Usage Disambiguation.
- **Deep per-phase contracts**: `aa-test-infra-bootstrap` (per-file contract), `aa-explore`, `aa-case-design`, `aa-case-reviewer`, `aa-case-fixer`. Load the phase skill when dispatched; the runbook only sequences them.

Preferred post-intake handoff remains **`workflow_start`** / `aa workflow run --scope execute`
(see Completion below) — not loading the full fallback runbook.

## Startup

1. Resolve `<change-id>` and runtime params exactly as `aa-workflow` does.
2. Ensure `workflow-state.yaml.params.run_mode` is one of `full`, `case-only`, or `review-case`.

### Phase 0 — Test Infra Bootstrap (interactive; runs before explore)

Run **inline** in the primary agent before any intake phase dispatch. This is the **only** place in two-stage mode where bootstrap may ask the user for config (URLs, credentials, ports) when defaults cannot be verified from `run.py` / `web/.env` / `app/core/init_app.py`.

```
Load aa-test-infra-bootstrap
  → check tests/config.py, tests/conftest.py, tests/schema_validation.py against per-file contract
  → CREATE missing conformant files; KEEP existing conformant files untouched
  → if a file exists but fails contract → STOP, ask user (never silently overwrite)
  → if defaults cannot be verified → ask user before writing
  → apply phases.test_infra_bootstrap state delta to workflow-state.yaml
```

Gate (same as `aa-workflow` Phase 0):

- `phases.test_infra_bootstrap.status == done` and all 3 files present → continue to Phase 1.1.
- `status == pending` or contract mismatch → **STOP** until bootstrap completes or user explicitly sets `skipped`.

3. Run the same registry / execution-mode setup as `aa-workflow` Phase 1.1 for intake-phase skills:
   - `aa-test-infra-bootstrap` (Phase 0 — already run above; mark `skill_loaded: true`)
   - `aa-explore`
   - `aa-case-design`
   - `aa-case-reviewer`
   - `aa-case-fixer`
4. Stamp the derived run context:

```bash
aa state configure --change <change-id> --orchestrator aa-intake
```

This writes:

```yaml
run_context:
  orchestrator_skill: aa-intake
  interaction_mode: interactive
  active_scope: intake
```

Do not hand-write `run_context`.

## Orchestration Loop

Use the `aa-workflow` Scheme E / inline orchestration loop unchanged, with the current `run_context.active_scope = intake`. `aa status --change <id> --next --json` drives phase selection and only dispatches intake-scope phases. The **Intake Runbook** below is the ordered checklist the loop walks.

## Intake Runbook (ordered, inline)

**Phase Completion Rule (inline — do not skip).** Producing artifacts on disk is NOT phase completion. After every phase you MUST hand-update `phases.<x>.status` (+ produces/gate refs) in `workflow-state.yaml`, then run `aa status`. Intake has no CLI `aa state apply` phases — every state write here is hand-written by the orchestrator (except `run_context`, which is stamped by CLI). Each line ends with its obligation as `⟶ state: …`.

```
Phase 0 — Test Infra Bootstrap   (see Startup above; interactive; before explore)
  → gate: phases.test_infra_bootstrap.status == done + 3 files present, else STOP
  → ⟶ state: hand-update phases.test_infra_bootstrap.status = done (done in Startup)

Phase 1.2 — Explore              (INLINE in primary agent; never dispatched)
  → load aa-explore; interaction_mode: interactive → ask per-pitfall open questions
  → write explore/advisory.json (open_questions answered_via: aa-intake, with user confirmation)
  → aa risk validate-advisory: reads run_context; FAILS interactive intake if OQs were
    answered via explore / auto_default or lack user-confirmation metadata
  → ⟶ state: hand-update phases.explore.status = done (+ produces ref); then aa status

Phase 2.1 — Case Design
  → dispatch aa-case-design; interaction_mode: interactive → clarify + get EXPLICIT user approval
    before writing files
  → outputs .qa.yaml (incl. approval.approved_by: user / approved_approach / approved_at),
    proposal.md, cases/<module>/case.yaml
  → case-design-gate will NOT mark cases/ done without .qa.yaml.approval
  → ⟶ state: hand-update phases.case_design.status = done (+ produces ref); then aa status

Phase 2.2 — Case Review (initial)
  → dispatch aa-case-reviewer → review/case-review.json   (reviewer JSON is the release gate)
  → aa gate check --phase case-review --change <id> --json      ← REQUIRED
  → ⟶ state: hand-update phases.case_review.status + gate verdict ref; then aa status

Phase 2.3 — Case Fix loop (if gate needs_fix)
  → for each attempt (max = max_case_fix_attempts):
      aa-case-fixer → aa-case-reviewer → aa gate check
      pass → exit loop ; reject / human_review_required → see Human Review below ; exhausted → STOP
  → ⟶ state: hand-update phases.case_review.status after each re-review; then aa status
```

### Human Review (intake scope) — no hand-written pass

If the case-review gate returns `needs_human_review` or `reject`: **STOP** and ask the user. Never edit `review/case-review.json` to `decision: pass`. To proceed after a human decision, use `aa decide --change <id> --at case-review --action fix_and_proceed --reason "<user decision>"`, then `aa-case-fixer` (human-approved mode) → `aa-case-reviewer` re-review → `aa gate check`.

## Completion Condition

`aa-intake` is complete only when:

1. `explore/advisory.json` exists and has no `open_questions_for_case_design[].status == "unanswered"`;
2. `qa/changes/<change-id>/.qa.yaml` contains `approval.approved_by: user`, `approval.approved_approach`, and `approval.approved_at` for interactive intake;
3. `qa/changes/<change-id>/cases/` contains generated case delta YAML and `case-design-gate` passes;
4. `review/case-review.json` exists with `decision == "pass"`;
5. `aa status --change <id> --next --json` reports terminal `completed` for `active_scope: intake`.

When complete, stop. Do not automatically continue into execute scope.

### Handoff (after user confirmation)

1. Ask the user to confirm starting autonomous execute for `<change-id>`.
2. **Preferred:** call the `workflow_start` tool with `change_id` and `scope: "execute"`.
   Progress continues in nested sessions / QA panel; do not run execute phases yourself.
3. **Fallback** (tool missing or driver unavailable):

```text
Intake complete. To continue autonomous execution:
  - Prefer: workflow_start tool (scope=execute), or
  - CLI: aa workflow run --change <change-id> --scope execute
  - Legacy skill: aa-execute
```
