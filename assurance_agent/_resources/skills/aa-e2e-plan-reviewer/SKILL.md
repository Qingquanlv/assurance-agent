---
name: aa-e2e-plan-reviewer
description: Review E2E plan artifacts and emit plan-review.json for the graph-owned plan gate.
---

## Purpose

Review the E2E plan package and emit a runtime PlanReview document. Mechanical PlanCheckDocument facts in `review/e2e-plan-checks.json` arrive as inputs; do not infer or apply a policy action from them — the downstream gate owns routing.

## Inputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/e2e-codegen-mapping.json`
- `change:plans/m4-review-summary.md`
- `change:review/e2e-plan-checks.json`
- `change:cases/**/case.yaml`

### optional

- `change:plans/data-knowledge.proposal.e2e.yaml`
- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/plan-review.json`
- `change:review/plan-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Semantic responsibilities: Factory Mapping, Adapter Mapping, assertion traceability, required capabilities, and Test Function Mapping.


Write only the review outputs listed above. Keep semantic factory mapping review. Never write `e2e-plan-checks.json`.

## Domain Notes

The schema contract is supplied by the runtime. Emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and `risk_level`. The reject routing example is `` `reject` | `not_ready` | `false` | `true` | `stop` ``. Gate policy keys such as `policy.human_review_risk_levels` are consumed by the graph coordinator / ledger.

Each finding must include `id`, `severity`, `category`, `message`, and `locator`.
Point mapping defects at `change:plans/e2e-codegen-mapping.json`.
