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

Consume the same Task Mapping uses `Case ID | Task Method | Target File`. structure emitted by the planner. Emit PlanReview fields consumed by the graph gate.
