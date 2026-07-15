---
name: aa-retro
description: Use after aa retro has generated qa/retro/<retro-id>/context.json to produce evidence-backed improvement proposals. Proposal-only; never directly modify skills, schema, memory, or project files.
---

# AA Retro Proposal Skill

Use this skill when asked to analyze `qa/retro/<retro-id>/context.json` and propose improvements for the Assurance Agent system.

## Inputs

- Required: `qa/retro/<retro-id>/context.json`
- Optional read-only context:
  - recent `qa/retro/*/promotions.json` files (for rejected / needs_rework history)
  - `qa/retro/<retro-id>/evidence/<change-id>/` snapshots for `evidence_source: "unarchived"`
  - `.aa/memory/**`
  - `schemas/workflow-schema.yaml`
  - relevant `skills/*/SKILL.md`

## Hard Rules

- Do not modify SKILL.md, workflow schema, `.aa/memory/**`, or project source files.
- Write only:
  - `qa/retro/<retro-id>/proposals.json`
  - `qa/retro/<retro-id>/retro-summary.md`
- Every proposal must cite evidence_ids that already exist in context.json.
- If evidence is weak or missing, do not create a proposal — **except** skill-execution drift (see below), which is mandatory when the signal is present.
- Generate proposals with `status: "proposed"` only.
- Do not repeat proposals that are equivalent to recent `rejected` proposals.
- For observations similar to recent `needs_rework` proposals, incorporate the `rework_note` and materially revise the proposal instead of resubmitting the same text.

## Required Analysis Order

1. **Skill execution drift** (`signals.skill_execution`) — evaluate first; see next section.
2. Failure distribution, gate pushback, healing efficiency, human overrides, reclassifications, eval trend.
3. Draft proposals; write `proposals.json` + `retro-summary.md`.

## Skill Execution Drift (mandatory)

`signals.skill_execution` is produced when a workflow phase finished with `skill_loaded: false` (phase ran without loading its skill contract). `true` and `n/a` are not drift.

**If `signals.skill_execution` is non-empty, you MUST create at least one proposal per drifted phase** (unless an equivalent proposal was recently `rejected`, or you are revising a recent `needs_rework` with a material rewrite). Do not bury drift only under "rejected observations" or skip it because other failure signals look more interesting.

For each drifted phase entry:

- Cite that entry's `evidence_ids` (e.g. `RET-…#workflow-state:report`).
- Prefer `layer: "agent"`, `apply_kind: "memory_append"`.
- Target the phase's skill memory file; if the root cause is orchestrator not enforcing load-before-work, also consider a second proposal targeting `.aa/memory/aa-workflow.md`.
- `problem` must name the phase and state it is skill-execution drift (`skill_loaded=false`).
- `proposed_change` must append a concrete rule: read that phase's `SKILL.md` before doing phase work, and only then set / report `skill_loaded: true`.

Phase → memory target → eval_suite:

| phase (examples) | target | eval_suite |
|---|---|---|
| `case_design` / case review | `.aa/memory/aa-case-design.md` (or reviewer skill memory) | `workflow-case` |
| `api_plan` / `api_codegen` / related | matching `.aa/memory/aa-api-*.md` | `workflow-api-codegen` |
| `e2e_plan` / `e2e_codegen` / related | matching `.aa/memory/aa-e2e-*.md` | `workflow-e2e-codegen` |
| `execution` / `healing` / `healing_rerun` | `.aa/memory/aa-run.md` or `.aa/memory/aa-execute.md` | `workflow-run` |
| `inspect` / `healing_reinspect` | `.aa/memory/aa-inspect.md` | `workflow-full` |
| `report` | `.aa/memory/aa-report-generator.md` | `workflow-full` |
| unknown / orchestrator-wide | `.aa/memory/aa-workflow.md` | `workflow-full` |

Single-change drift still gets a proposal (nightly may auto-tag `needs_rework` when unique change evidence `< 2`; that is driver policy, not a reason to omit the proposal).

## Proposal Layers

- `agent`: per-skill memory rule, `apply_kind: "memory_append"`, target `.aa/memory/aa-<skill>.md`
- `interaction`: contract change across producer/consumer skills, `apply_kind: "contract_field"`
- `team`: workflow schema change, `apply_kind: "schema_param"` or `"schema_structure"`

## Evidence Source Handling

`context.json` may include evidence from both archived and unarchived changes.
Treat both as valid if their evidence_ids exist in context.json. When explaining or
summarizing evidence:

- `evidence_source: "archive"` means the stable evidence is under `qa/archive/<change-id>/`.
- `evidence_source: "unarchived"` means the stable evidence snapshot is under
  `qa/retro/<retro-id>/evidence/<change-id>/`; do not rely on live `qa/changes/`
  because benchmark cleanup may remove it before review.

## Eval Suite Selection

Use an existing eval suite only:

| Target | eval_suite |
|---|---|
| `.aa/memory/aa-api-codegen.md` | `workflow-api-codegen` |
| `.aa/memory/aa-e2e-codegen.md` | `workflow-e2e-codegen` |
| `.aa/memory/aa-fuzz-codegen.md` | `workflow-fuzz-codegen` |
| `.aa/memory/aa-performance-codegen.md` | `workflow-performance-codegen` |
| `.aa/memory/aa-case-design.md` | `workflow-case` |
| run/healing execution memory | `workflow-run` |
| Other valid `.aa/memory/aa-*.md` targets | `workflow-full` |

Never invent suite names such as `workflow-inspect-codegen`.

## Output: proposals.json

```json
{
  "retro_id": "retro-20260708",
  "proposals": [
    {
      "id": "RETRO-001",
      "layer": "agent",
      "target": ".aa/memory/aa-api-codegen.md",
      "problem": "Repeated test data failures for department name length",
      "evidence_ids": ["RET-a#fail-1"],
      "proposed_change": "Append a rule to keep generated department names within ORM max_length constraints.",
      "apply_kind": "memory_append",
      "eval_suite": "workflow-api-codegen",
      "risk": "low",
      "confidence": "high",
      "status": "proposed"
    }
  ]
}
```

## Output: retro-summary.md

Summarize:

- evidence window and change count
- **skill execution drift**: for each `signals.skill_execution` entry, state phase / count / whether a proposal was filed (or why skipped: recent reject / rework revision only)
- top repeated failures
- proposed changes grouped by layer
- rejected observations with insufficient evidence
- eval suite required for each proposal
