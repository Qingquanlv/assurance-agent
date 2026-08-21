# Fuzz plan review

Capability-owned fuzz plan review skill. Do not select a provider, model, or
adapter.

Review fuzz plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`. This layer is human-only:
`auto_fix_allowed: false` and `auto_fix_plan: []`.

## Inputs

### required

- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-mapping.json`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`
- `qa/changes/<change-id>/review/fuzz-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`

### optional

- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/fuzz/**` and `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/review/fuzz-plan-review.json`
- `qa/changes/<change-id>/review/fuzz-plan-review-summary.md`

## Boundaries

Write only review outputs. Do not authorize automatic fixers. The graph owns
phase state. Do not write an orchestration state file.

## Domain Notes

Independently inspect both `fuzz-plan.md` and `fuzz-codegen-plan.md`. Each must
contain the exact `## Test Function Mapping` heading with a four-column
`Case ID | Test Function | Target File | Schema Acquisition` table. Return a
non-pass review when either file lacks the independently parseable mapping or
when the two relations differ.

Every mapped function must use `test_<case_id_lowercase>__<behavior>` with the
complete Case ID. For an approved, codegen-ready plan, emit `"decision": "pass"`;
never emit `"approved"`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent constraint suffixes. If a needed leaf is absent, use
  `needs_human_review` with `not_ready`.

Runtime contract closure:

- For URI schema acquisition, resolve each fuzz target by exact `(METHOD, PATH)`
  membership in the live OpenAPI document before passing the plan.
- Validate every positive seed against the real request schema. Reject unproven
  reserved email domains such as `example.test`.
- Reject a rejection oracle for inputs that do not violate an observed schema
  or application constraint.
- Require an endpoint/property strategy on the typed plan result.
