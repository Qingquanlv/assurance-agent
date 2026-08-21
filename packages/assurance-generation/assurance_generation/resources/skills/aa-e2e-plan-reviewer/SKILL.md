# E2E plan review

Capability-owned E2E plan review skill. Do not select a provider, model, or
adapter.

Review the E2E plan package and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. Mechanical plan-check facts arrive as inputs;
do not infer or apply a policy action from them.

## Inputs

### required

- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/review/e2e-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`

### optional

- `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`
- `.aa/data-knowledge.yaml`
- backend and frontend product source (read-only)
- `tests/e2e/**` and `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/review/plan-review.json`
- `qa/changes/<change-id>/review/plan-review-summary.md`

## Boundaries

Write only the review outputs listed above. Keep semantic factory mapping
review. Never write `e2e-plan-checks.json`. The graph owns phase state. Do not
write an orchestration state file.

## Domain Notes

Use decision `pass`, never `approved`. Emit `codegen_readiness`,
`auto_fix_allowed`, `human_review_required`, and `risk_level`. Each finding
must include `id`, `severity`, `category`, `message`, and `locator`. Point
mapping defects at `plans/e2e-codegen-mapping.json`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly.

Runtime contract closure:

- Independently inspect the declared backend/frontend sources and existing
  E2E/test-data trees.
- Compare setup and cleanup payloads, identifier extraction, and browser
  locators with the real handlers and DOM. A create response without an
  identifier requires an exact supported lookup; assumed `data.id` is
  `not_ready`.
- Verify each setup and cleanup HTTP method/path against live OpenAPI when
  available, otherwise against the declared router/schema source.
