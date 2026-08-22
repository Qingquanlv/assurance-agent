# Performance codegen

Capability-owned performance codegen skill. Do not select a provider, model, or
adapter. Do not look up a global skill catalog.

Turn a reviewed performance plan and frozen case references into generated load
tests plus a closed mapping. Schema truth is `assurance_generation.contracts`
for generated files and mapping, and `assurance_intake.contracts` for reviewed
cases.

## Inputs

### required

- reviewed performance plan (`PlanResultV1`) including scenario identity and
  numeric thresholds
- frozen case references for the selected performance cases
- baseline tree identity
- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/review/performance-plan-checks.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/perf/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/performance-codegen-summary.md`
- `qa/changes/<change-id>/codegen/performance-generated-files.json`
- generated or updated test files under `tests/perf/**`

### conditional

- `tests/testdata/domain/**` when the reviewed plan authorizes a shared builder

## Boundaries

Write only authorized `tests/perf/**` and `tests/testdata/**` paths plus the
summary and generated-files manifest.

Do not modify product source.

The graph owns phase state. Do not write an orchestration state file.

Framework is Locust. Keep Case ID → symbol → target file traceability exact.
There is no performance codegen-fix handler in this phase.

## Mapping Rules

- Consume Task Mapping as a strict one-row-per-Case-ID relation. Do not
  reinterpret setup, cleanup, seed helpers, factories, or adapters as additional
  mapped test entries.
- Assign each Case ID only to its executable `test_entry`. Support and
  `shared_builder` files must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file
  whose content this invocation changed. `reused` is legal only for an
  unchanged, selected private-root `test_entry` that is itself a Task Mapping
  target.
- If Task Mapping itself repeats a Case ID, the plan is invalid and must not be
  represented as a different relation.

## Runtime Contract Closure

Before writing a Locust task or seed adapter, inspect the live OpenAPI document
when `BASE_URL` is configured; otherwise inspect the actual router and schema
source. Verify every setup, measured, and cleanup request by exact method/path
and request schema.

Resolve the Locust host from the declared benchmark/runtime environment. Do not
hard-code a local port when `BASE_URL` or `API_BASE_URL` is available.

Inspect real response shapes. Do not require a create response to return an
identifier unless the handler actually does. Seed setup must fail once with the
response status and a redacted diagnostic.

Validate measured assertions against the complete response object before
unwrapping payload data. Pagination fields may be siblings of a list-valued
`data`.

Validate every positive seed against the real request schema and runtime
validators before starting Locust. Do not use `example.test` as a valid email
unless the product is proven to accept it; prefer a proven value or
`example.com`.

Capability keys must be exact typed leaves. Every mapped function is named
`test_<case_id_lowercase>__<desc>`.
