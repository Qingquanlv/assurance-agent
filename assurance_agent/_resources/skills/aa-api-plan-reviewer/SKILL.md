---
name: aa-api-plan-reviewer
description: Review API plan artifacts and emit api-plan-review.json for the graph-owned plan gate.
---

## Purpose

Review the API plan package and emit a runtime PlanReview document. Mechanical PlanCheckDocument facts arrive as inputs; do not infer or apply a policy action from them — the downstream gate owns routing.

## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/api-codegen-mapping.json`
- `change:plans/m3-review-summary.md`
- `change:review/api-plan-checks.json`
- `change:cases/**/case.yaml`

### optional

- `change:plans/data-knowledge.proposal.api.yaml`
- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:review/api-plan-review.json`
- `change:review/api-plan-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the review outputs listed above.

Do not write plan Markdown, tests, or knowledge files.

Semantic responsibilities remain: assertion traceability, coverage gap, required capabilities, and Test Function Mapping review.

## Domain Notes

The schema contract is supplied by the runtime. Emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and `risk_level`. The reject routing example is `` `reject` | `not_ready` | `false` | `true` | `stop` ``. Gate policy keys such as `policy.human_review_risk_levels` are consumed by the graph coordinator / ledger, not rewritten here.

Each finding must include `id`, `severity`, `category`, `message`, and `locator`
(`artifact` plus optional `case_id` / `key`). Point locators at
`change:plans/api-codegen-mapping.json` when the defect is a mapping row.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `repo:.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready` instead of `pass` with a virtual key.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly in L1.
