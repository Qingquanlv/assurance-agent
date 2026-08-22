# E2E codegen

Capability-owned E2E codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed E2E plan and frozen case references into generated browser tests
plus a closed mapping. Schema truth is `assurance_generation.contracts` for
generated files and mapping, and `assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- reviewed E2E plan (`PlanResultV1`)
- frozen case references for the selected E2E cases
- baseline tree identity
- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/review/plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- `.aa/config.yaml`
- backend and frontend product source (read-only)
- `tests/e2e/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/e2e-codegen-summary.md`
- `qa/changes/<change-id>/codegen/e2e-generated-files.json`
- generated or updated test files under `tests/e2e/**`

### conditional

- `tests/testdata/domain/**` when the reviewed plan authorizes a shared builder

## Boundaries

Write only authorized `tests/e2e/**` and `tests/testdata/**` paths plus the
summary and generated-files manifest.

Do not modify product source.

Do not run the product test runner.

The graph owns phase state. Do not write an orchestration state file.

Framework is pytest-playwright. Keep Case ID → symbol → target file
traceability exact.

## Fixture Closure

Before writing the manifest, resolve every mapped test parameter to a fixture
defined or imported by the test module, an ancestor `conftest.py`, or the
installed Playwright pytest plugin. Generate any missing project fixture under
authorized `tests/e2e/**`, record changed fixture files as `support` with
`case_ids: []`, and finish only when the unresolved fixture set is empty.

For every generated or reused browser test and shared login fixture, inspect the
actual login DOM and feature-component DOM before accepting its locators. Do not
assume an accessible name or control role from visible design intent. Prefer a
stable observed placeholder, label, role, or test id. Update an existing mapped
test instead of marking it `reused` when any locator disagrees with the current
product markup.

For synchronous pytest-playwright tests, do not call `asyncio.run()` in a test,
fixture, setup, cleanup, or adapter bridge. Playwright owns an event loop in the
test thread. Use a synchronous HTTP adapter, or an existing project helper that
runs the coroutine in a separate thread when a loop is already active.

Inspect the real handler response for every fixture create operation. If success
contains no identifier, perform an exact supported list/get lookup and verify
the created record before returning it; never require `data.id`.

Resolve every fixture setup and cleanup operation by exact HTTP method/path from
the live OpenAPI document when available, otherwise from the declared router and
schema source. Route naming convention is not evidence.

## Generated-files Manifest Rules

- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file
  whose content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target.

Capability keys must be exact typed leaves. Every mapped function is named
`test_<case_id_lowercase>__<desc>`.
