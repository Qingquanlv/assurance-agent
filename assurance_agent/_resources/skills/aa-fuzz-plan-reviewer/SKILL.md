---
name: aa-fuzz-plan-reviewer
description: Human-only fuzz plan review emitting PlanReview with auto_fix_allowed false.
---

## Purpose

Review fuzz plans and emit a PlanReview document. This layer is human-only: `auto_fix_allowed: false` and `auto_fix_plan: []`. Require `required_capabilities` and `human_review` when facts are missing.

## Inputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-review-summary.md`
- `change:review/fuzz-plan-checks.json`
- `change:cases/**/case.yaml`

### optional

- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/fuzz-plan-review.json`
- `change:review/fuzz-plan-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only review outputs. Do not authorize automatic fixers.

## Domain Notes

Independently inspect both `fuzz-plan.md` and `fuzz-codegen-plan.md`. Each must
contain the exact `## Test Function Mapping` heading with a four-column
`Case ID | Test Function | Target File | Schema Acquisition` table. Every row
must have a non-empty Schema Acquisition cell. Do not infer the codegen-plan
mapping from `fuzz-plan.md`, a separate prose/code-block procedure, or prose
such as `Test Function Spec`. Return a non-pass review when either file lacks
the independently parseable mapping or when the two relations differ.

Every mapped function must use `test_<case_id_lowercase>__<behavior>` with the
complete Case ID. Return a non-pass review when a symbol is shortened or cannot
be mapped back to its Case ID. Emit PlanReview fields consumed by the graph gate.
For an approved, codegen-ready plan, emit the exact JSON value
`"decision": "pass"`; never emit the legacy compatibility value `"approved"`.
