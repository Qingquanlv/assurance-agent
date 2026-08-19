---
name: aa-e2e-codegen
description: Generate E2E tests and a strict generated-files manifest after the E2E plan gate passes.
---

## Purpose

Generate E2E tests under `tests/e2e/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. Do not run pytest collect or claim Traceability Verification evidence.

## Inputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/e2e-codegen-mapping.json`
- `change:plans/m4-review-summary.md`
- `change:review/plan-review.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:app/**` and `repo:web/**` (read-only SUT and DOM evidence)
- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/e2e-codegen-summary.md`
- `change:codegen/e2e-generated-files.json`

### conditional

- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source. Do not run pytest.

## Fixture Closure

Before writing the manifest, resolve every mapped test parameter to a fixture
defined or imported by the test module, an ancestor `conftest.py`, or the
installed Playwright pytest plugin. Generate any missing project fixture under
authorized `tests/e2e/**`, record changed fixture files as `support` with
`case_ids: []`, and finish only when the unresolved fixture set is empty. The
Graph precommit validator checks this candidate-tree contract without executing
the fixtures.

For every generated or reused browser test and shared login fixture, inspect the
actual login DOM and feature-component DOM before accepting its locators. Do not
assume an accessible name or control role from visible design intent: an input
with only a placeholder has no such name, and a checkbox group is not a
combobox. Prefer a stable observed placeholder, label, role, or test id. Update
an existing mapped test instead of marking it `reused` when any locator disagrees
with the current SUT markup.

For synchronous pytest-playwright tests, do not call `asyncio.run()` in a test,
fixture, setup, cleanup, or adapter bridge. Playwright owns an event loop in the
test thread, so nested `asyncio.run()` fails before browser assertions execute.
Use a synchronous HTTP adapter, or an existing project helper that runs the
coroutine in a separate thread when a loop is already active. Inspect reused
fixtures as well as newly generated files for this incompatibility.

Inspect the real handler response for every fixture create operation. If success
contains no identifier, perform an exact supported list/get lookup and verify the
created record before returning it; never require `data.id`. Reused shared
test-data helpers are part of this closure and must be updated when their response
mapping contradicts the current SUT.

Resolve every fixture setup and cleanup operation by exact HTTP method/path from
the live OpenAPI document when available, otherwise from the declared router and
schema source. Route naming convention is not evidence.

## Generated-files Manifest Rules

- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping (`plans/e2e-codegen-mapping.json`) for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file whose
  content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target. Never list an unchanged adapter, helper,
  fixture, or shared builder as `reused`; omit unchanged support dependencies
  from the manifest.
