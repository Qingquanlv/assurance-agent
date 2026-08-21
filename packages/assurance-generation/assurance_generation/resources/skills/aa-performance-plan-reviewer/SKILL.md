# Performance plan review

Capability-owned performance plan review skill. Do not select a provider,
model, or adapter.

Review performance plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. This layer is human-only:
`auto_fix_allowed: false` and `auto_fix_plan: []`.

## Inputs

### required

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/review/performance-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`

### optional

- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/perf/**` and `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/review/performance-plan-review-summary.md`

## Boundaries

Write only review outputs. Do not authorize automatic fixers. The graph owns
phase state. Do not write an orchestration state file.

## Domain Notes

Consume the same Task Mapping structure emitted by the planner:

- Validate the explicit `## Task Mapping` table independently in both
  `performance-plan.md` and `performance-codegen-plan.md`.
- Headers are `Case ID | Task Method | Target File`.
- Require exactly one row for every selected Performance Case ID.
- Reject a Case ID that appears more than once.
- Require the mapped method to be the primary executable load-test task under
  `tests/perf/**`.

For an approved, codegen-ready plan, emit `"decision": "pass"`. Never emit
`"approved"`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.

Runtime contract closure:

- Verify setup, measured, and cleanup operations by exact method/path against
  live OpenAPI when available, otherwise against router/schema source.
- Reject a hard-coded load-test host when the declared runtime provides
  `BASE_URL` or `API_BASE_URL`.
- Verify that seed identifier extraction matches the real create response, or
  that the plan names a supported lookup. Reject assumed `data.id` response
  shapes as `not_ready`.
- Require scenario identity and numeric thresholds on the typed plan result.
