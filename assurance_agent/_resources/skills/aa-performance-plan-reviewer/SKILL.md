---
name: aa-performance-plan-reviewer
description: Human-only performance plan review emitting PlanReview with auto_fix_allowed false.
---

## Purpose

Review performance plans and emit a PlanReview document. This layer is human-only: `auto_fix_allowed: false` and `auto_fix_plan: []`. Require `required_capabilities` and `human_review` when facts are missing.

## Inputs

### required

- `change:plans/performance-plan.md`
- `change:plans/performance-codegen-plan.md`
- `change:plans/performance-codegen-mapping.json`
- `change:plans/performance-review-summary.md`
- `change:review/performance-plan-checks.json`
- `change:cases/**/case.yaml`

### optional

- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/performance-plan-review.json`
- `change:review/performance-plan-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only review outputs. Do not authorize automatic fixers.

## Domain Notes

Consume the same Task Mapping structure emitted by the planner:

- Validate the explicit `## Task Mapping` table independently in both
  `performance-plan.md` and `performance-codegen-plan.md`. A review must not pass
  when either artifact lacks the table, even if equivalent facts appear in prose
  or another section.
`Case ID | Task Method | Target File`.

Treat Task Mapping as a strict one-to-one execution-entry relation. Before passing:

- Require exactly one row for every selected Performance Case ID.
- Reject a Case ID that appears more than once, even when its task method or target
  file differs.
- Require the mapped method to be the primary executable Locust task under `tests/perf/**`.
- Require setup, cleanup, seed helpers, factories, adapters, and other support
  functions to be documented outside Task Mapping.

Emit the PlanReview fields consumed by the graph gate.
For an approved, codegen-ready plan, emit the exact JSON value
`"decision": "pass"`. Never emit `"approved"`: that legacy value remains
readable for historical artifacts but intentionally does not release the gate.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `repo:.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready` instead of `pass` with a virtual key.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly in L1.
