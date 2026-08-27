# Fuzz plan review

Capability-owned fuzz plan review skill. Do not select a provider, model, or
adapter.

Review fuzz plans and emit a `PlanReviewAuthoring` document from
`assurance_generation.contracts`.

Evidence-proven, bounded defects use `needs_fix`: set `auto_fix_allowed: true`,
`human_review_required: false`, `codegen_readiness: not_ready`, and list every
bounded finding ID in `auto_fix_plan`. Severity alone does not require human
review. Use `needs_human_review` only when correction requires a missing
product, policy, authorization, or safety decision; then prohibit automatic
repair and leave `auto_fix_plan` empty. A codegen-ready `pass` never requires
human review.

`auto_fix_plan` has one exact JSON shape: an array of non-empty finding ID
strings, for example `"auto_fix_plan": ["FUZZ-PLAN-001", "FUZZ-PLAN-002"]`.
Never put objects, `finding_id`/`action` pairs, prose, or locator data in this
array. Put repair detail in the matching finding's `message` and `locator`, and
in `next_action`. Use `"auto_fix_plan": []` for `pass` and
`needs_human_review`.

## Inputs

### required

- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-mapping.json`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`
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

Write only review outputs. Authorizing bounded planner re-entry does not permit
the reviewer to edit plan files. Each automatic finding must point to an exact
plan artifact and bounded key/section that the planner can revise from observed
source. A missing `fuzz-plan-checks.json` is not a stop condition; when present,
consume it as deterministic evidence. The graph owns phase state. Do not write
an orchestration state file.

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
- When an existing method/path, schema, or application constraint proves a
  corrected bounded strategy, use `needs_fix`; do not require a human merely
  because the defect is blocking.
- An incorrect application import, router export, schema loader, or fallback
  acquisition path is a bounded automatic finding when repository source proves
  the exact replacement. Put its finding ID in `auto_fix_plan`; never escalate
  that source-backed correction to human review.
- When the requirement owner has already frozen "the project's existing
  response contract" and source defines a concrete response class or envelope,
  use that source-backed envelope as contract evidence. A missing OpenAPI
  `response_model`/success content schema is a reportable contract-documentation
  gap, not a new product decision. Route a plan that relies only on the missing
  OpenAPI response schema to bounded `needs_fix`: revise its oracle to the exact
  source-backed envelope and let execution/issue analysis expose any mismatch.
- Escalate a success-response oracle only when both the frozen requirement and
  inspected source leave the accepted shape genuinely ambiguous. Do not ask a
  human to restate an acceptance criterion already present in the requirement.
- Require an endpoint/property strategy on the typed plan result.
