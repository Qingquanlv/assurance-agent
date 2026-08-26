# Performance plan review

Capability-owned performance plan review skill. Do not select a provider,
model, or adapter.

Review performance plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["PERF-PLAN-001", "PERF-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

## Inputs

### required

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
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

Write only review outputs. Authorizing bounded planner re-entry does not permit
the reviewer to edit plan files. Each automatic finding must point to an exact
plan artifact and bounded key/section that the planner can revise from observed
source. A missing `performance-plan-checks.json` is not a stop condition; when
present, consume it as deterministic evidence. The graph owns phase state. Do
not write an orchestration state file.

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
- When an existing method/path and response shape prove a corrected lookup,
  route the exact affected plan sections through bounded `needs_fix`; do not
  require a human merely because the defect is blocking.
- Require scenario identity and numeric thresholds on the typed plan result.
