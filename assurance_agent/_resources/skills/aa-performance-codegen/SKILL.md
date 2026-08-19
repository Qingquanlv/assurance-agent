---
name: aa-performance-codegen
description: Generate performance tests and a strict generated-files manifest after the plan gate passes.
---

## Purpose

Generate performance tests under `tests/perf/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. The graph gate is the progression authority.

## Inputs

### required

- `change:plans/performance-plan.md`
- `change:plans/performance-codegen-plan.md`
- `change:plans/performance-codegen-mapping.json`
- `change:plans/performance-review-summary.md`
- `change:review/performance-plan-review.json`
- `change:review/performance-plan-checks.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:app/**` (read-only SUT contract evidence)
- `repo:tests/perf/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/performance-codegen-summary.md`
- `change:codegen/performance-generated-files.json`

### conditional

- `repo:tests/perf/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source.

## Mapping Rules

- Consume Task Mapping as a strict one-row-per-Case-ID relation. Do not reinterpret
  setup, cleanup, seed helpers, factories, or adapters as additional mapped test
  entries.
- In `performance-generated-files.json`, assign each Case ID only to its executable
  `test_entry`. Support and `shared_builder` files must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file whose
  content this invocation changed. `reused` is legal only for an unchanged,
  selected private-root `test_entry` that is itself a Task Mapping target. Never
  list unchanged setup, cleanup, adapter, fixture, or shared-builder dependencies
  as `reused`; omit unchanged support dependencies from the manifest.
- If Task Mapping itself repeats a Case ID, do not try to compensate in generated
  files; the plan is invalid and must not be represented as a different relation.

## Runtime Contract Closure

Before writing a Locust task or seed adapter, inspect the live OpenAPI document
when `BASE_URL` is configured; otherwise inspect the actual router and schema
source. Verify every setup, measured, and cleanup request by exact method/path
and request schema.
Resolve the Locust host from the declared benchmark/runtime environment. Do not
hard-code a local port when `BASE_URL` or `API_BASE_URL` is available.

Inspect real response shapes as well. In particular, do not require a create
response to return an identifier unless the handler actually does; when it does
not, use a supported lookup to obtain the manifest-owned identifier. Seed setup
must fail once with the response status and a redacted diagnostic, rather than
letting every Locust user repeatedly execute a scenario with missing seed data.

Validate measured assertions against the complete response object before
unwrapping payload data. Pagination fields may be siblings of a list-valued
`data`; never unwrap that list and then require the sibling fields inside it.

Validate every positive seed against the real request schema and runtime
validators before starting Locust. Do not use `example.test` as a valid email
unless the SUT is proven to accept it; prefer a proven value or `example.com`.
