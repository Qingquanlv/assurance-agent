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

Consume the same Test Function Mapping uses `Case ID | Test Function | Target File` plus Schema Acquisition fields. structure emitted by the planner. Emit PlanReview fields consumed by the graph gate.
