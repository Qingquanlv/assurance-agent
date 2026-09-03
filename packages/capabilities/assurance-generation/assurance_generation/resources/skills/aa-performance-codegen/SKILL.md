# Performance codegen

Capability-owned performance codegen skill. Do not select a provider, model, or
adapter. Do not look up a global skill catalog.

Turn a reviewed performance plan and frozen case references into generated load
tests plus a closed mapping. Schema truth is `assurance_generation.contracts`
for generated files and mapping, and `assurance_intake.contracts` for reviewed
cases.

## Inputs

Read every exact product-source path cited by the approved plan before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Use path-scoped grep only after the
exact reads, and never replace source-backed plan facts with guesses from naming.

Never read `.env`, `*.env`, or credential-bearing benchmark environment files.
Use environment variable names and non-secret defaults only; implement the approved
configuration contract without inspecting credential values.

### required

- reviewed performance plan (`PlanResultV1`) including scenario identity and
  numeric thresholds
- frozen case references for the selected performance cases
- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-mapping.json`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `qa/changes/<change-id>/review/performance-plan-checks.json`
- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/perf/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/performance-codegen-summary.md`
- `qa/changes/<change-id>/codegen/performance-generated-files.json`
- generated or updated test files under `qa/changes/<change-id>/generated/performance/files/tests/perf/**`

### conditional

- `qa/changes/<change-id>/generated/performance/files/tests/testdata/domain/**`
  when the reviewed plan authorizes a shared builder

The generated-files manifest and mapping keep `target_path="tests/..."`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only authorized staged files under
`qa/changes/<change-id>/generated/performance/files/` plus the summary and
generated-files manifest. Manifest `repo_path` / mapping `target_file` remain
the logical `tests/perf/**` or `tests/testdata/**` target.

Do not modify product source.

The graph owns phase state. Do not write an orchestration state file.

Framework is Locust. Keep Case ID → symbol → target file traceability exact.

## Frozen Inputs and Completion Check

Plan, case, and review inputs are immutable. Read them as approved evidence;
never rewrite, repair, or supersede them during codegen.

The runtime `allowed_outputs` list is the exact write whitelist and overrides
the wildcard-shaped output descriptions above. If a fixture or helper path is
not listed, keep the fixture or helper inside an authorized mapped target.
Never attempt or declare an unlisted support file, and never return a manifest
entry for a write that the tool rejected or that you did not reopen.

Every closed-mapping target must appear in `files` as a `test_entry` with the
exact mapped Case IDs. Reopen every target before returning and verify that the
mapped symbol exists in that file. Do not list plan, case, or review inputs in
`files`.
There is no performance codegen-fix handler in this phase.

## Mapping Rules

- Include exactly `schema_version`, `change_id`, `layer`, `files`, `mapping`,
  and `required_capabilities` in the generated-files manifest. The manifest
  `mapping` and `required_capabilities` must exactly match the final structured
  result.

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
