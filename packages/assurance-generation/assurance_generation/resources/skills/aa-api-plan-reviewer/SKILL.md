# API plan review

Capability-owned API plan review skill. Do not select a provider, model, or
adapter.

Review the API plan package and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. Mechanical plan-check facts arrive as inputs;
do not infer or apply a policy action from them — the downstream gate owns
routing.

## Inputs

### required

- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/review/api-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`

### optional

- `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/api/**` and `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/review/api-plan-review.json`
- `qa/changes/<change-id>/review/api-plan-review-summary.md`

## Boundaries

Write only the review outputs listed above.

Do not write plan Markdown, tests, or knowledge files.

The graph owns phase state. Do not write an orchestration state file.

## Domain Notes

Emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and
`risk_level`. Use decision `pass`, never `approved`. Each finding must include
`id`, `severity`, `category`, `message`, and `locator` (`artifact` plus optional
`case_id` / `key`). Point locators at `plans/api-codegen-mapping.json` when the
defect is a mapping row.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent a constraint or adapter suffix. If a needed leaf is absent, use
  `needs_human_review` with `not_ready` instead of `pass` with a virtual key.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly.

Runtime contract closure:

- Independently compare every method/path named by the API plan and codegen
  plan with the live OpenAPI document when available, otherwise with the actual
  router source. Naming convention is not evidence.
- Compare seed and mutation payloads with the registered request schema and
  compare identifier extraction with the real response shape.
- A missing operation, wrong HTTP method, unverified payload field, or assumed
  response identifier is a non-pass finding with `not_ready`.
- Inspect the declared API and shared test-data trees before claiming a mapped
  test, fixture, or helper is absent.
