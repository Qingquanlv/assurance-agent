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
- `change:plans/fuzz-codegen-mapping.json`
- `change:plans/fuzz-review-summary.md`
- `change:review/fuzz-plan-checks.json`
- `change:cases/**/case.yaml`

### optional

- `repo:.aa/data-knowledge.yaml`
- `repo:app/**` (read-only SUT contract evidence)
- `repo:tests/fuzz/**` and `repo:tests/testdata/domain/**` (existing implementation evidence)

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

Independently inspect both `fuzz-plan.md` and `fuzz-codegen-plan.md`. Each must
contain the exact `## Test Function Mapping` heading with a four-column
`Case ID | Test Function | Target File | Schema Acquisition` table. Every row
must have a non-empty Schema Acquisition cell. Do not infer the codegen-plan
mapping from `fuzz-plan.md`, a separate prose/code-block procedure, or prose
such as `Test Function Spec`. Return a non-pass review when either file lacks
the independently parseable mapping or when the two relations differ.

Every mapped function must use `test_<case_id_lowercase>__<behavior>` with the
complete Case ID. Return a non-pass review when a symbol is shortened or cannot
be mapped back to its Case ID. Emit PlanReview fields consumed by the graph gate.
For an approved, codegen-ready plan, emit the exact JSON value
`"decision": "pass"`; never emit the legacy compatibility value `"approved"`.

Required capability closure:

- Copy every `required_capabilities` key verbatim from a typed leaf that exists
  in `repo:.aa/data-knowledge.yaml`; a valid-looking prefix is not evidence.
- Never invent constraint suffixes such as `entities.<name>.constraints.*`. If
  a needed leaf is absent, use `needs_human_review` with `not_ready` instead of
  `pass` with a virtual key.
- Emit `pass` with `ready` / `ready_with_warnings` only when every required key
  resolves exactly in L1.

Runtime contract closure:

- The review input list is not an evidence boundary. When repository files or
  the live OpenAPI document are readable, you must perform the read-only
  inspection yourself. Do not return `not_ready` merely because the plan text
  does not embed the proof that you can independently verify.
- For URI schema acquisition, resolve each fuzz target by exact `(METHOD,
  PATH)` membership in the live OpenAPI document before passing the plan.
- Reject plans that merely propose a conventional route or defer existence to
  a runtime assertion. A missing path, wrong method, or absent request-body
  schema is `not_ready`.
- Validate every positive seed against the real request schema and application
  validators, including persistence-model length limits, before passing. Count
  fixed prefixes and candidate markers in the final length. A plausible-looking value is not evidence; reject
  unproven reserved email domains such as `example.test` unless the SUT is
  demonstrated to accept them.
- Reject setup that requires a related record when the observed schema explicitly
  permits an optional/default association value and a fresh SUT may contain none.
- Reject a rejection oracle for inputs that do not violate an observed schema or
  application constraint. In particular, an unconstrained long string may carry
  a no-5xx oracle but not an automatic `code != 200` oracle.
- Inspect the declared fuzz and shared test-data trees before claiming an
  implementation or cleanup helper is absent.
