# E2E codegen

Capability-owned E2E codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed E2E plan and frozen case references into generated browser tests
plus a closed mapping. Schema truth is `assurance_generation.contracts` for
generated files and mapping, and `assurance_intake.contracts` for reviewed cases.

## Inputs

Read every exact product-source path cited by the approved plan before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Use path-scoped grep only after the
exact reads, and never replace source-backed plan facts with guesses from naming.

### required

- reviewed E2E plan (`PlanResultV1`)
- frozen case references for the selected E2E cases
- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/review/e2e-plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `.aa/config.yaml`
- backend and frontend product source (read-only)
- `tests/e2e/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/e2e-codegen-summary.md`
- `qa/changes/<change-id>/codegen/e2e-generated-files.json`
- generated or updated test files under `qa/changes/<change-id>/generated/e2e/files/tests/e2e/**`

### conditional

- `qa/changes/<change-id>/generated/e2e/files/tests/testdata/domain/**` when the
  reviewed plan authorizes a shared builder

The generated-files manifest and mapping keep `target_path="tests/..."`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only authorized staged files under
`qa/changes/<change-id>/generated/e2e/files/` plus the summary and
generated-files manifest. Manifest `repo_path` / mapping `target_file` remain
the logical `tests/e2e/**` or `tests/testdata/**` target.

Do not modify product source.

Do not run the product test runner.

The graph owns phase state. Do not write an orchestration state file.

Framework is pytest-playwright. Keep Case ID → symbol → target file
traceability exact.

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

## Fixture Closure

Before writing the manifest, resolve every mapped test parameter to a fixture
defined or imported by the test module, an ancestor `conftest.py`, or the
installed Playwright pytest plugin. Use an explicitly listed support output
when one exists; otherwise define each missing fixture in the authorized mapped
test module. Record a support file only when its exact path is in
`allowed_outputs`, and finish only when the unresolved fixture set is empty.

`conftest.py` is pytest discovery configuration, not an importable support
module. Never generate `from conftest import ...` or otherwise import a
`conftest.py` helper from a test. Keep fixture-only code in `conftest.py`; move
helpers that a test imports into a regular module under `tests/e2e/**`, import
that module by its package path, and record a newly changed helper as `support`
with `case_ids: []`.

For every generated or reused browser test and shared login fixture, inspect the
actual login DOM and feature-component DOM before accepting its locators. Do not
assume an accessible name or control role from visible design intent. Prefer a
stable observed placeholder, label, role, or test id. Update an existing mapped
test instead of marking it `reused` when any locator disagrees with the current
product markup.

Before accepting a locator, prove it is strict-mode unique in the state where
it is used. In particular, scope form-validation assertions to the current
visible form or dialog. Never use an unscoped page-wide text locator when the
same text can label inputs, serve as placeholder text, or appear in another
form item.

When a component library supplies localized default action labels, do not guess
the visible or accessible name from the action's meaning. Read the component's
configured locale and resolve the active locale's exact default text, or inspect
the rendered DOM, before using an exact text or role locator.

Do not infer an ARIA role from the component name: a source-level number, select,
or dialog component is not proof of its rendered role. When an optional control
already satisfies the case through its initialized default, omit that redundant
interaction unless a verified rendered locator is available.

For Playwright URL assertions, a Python string is an exact expected URL, not a
regular expression. When the assertion intentionally describes a URL pattern,
pass a compiled `re.Pattern` (for example `re.compile(r".*/system/dept$")`) to
`expect(page).to_have_url(...)`; otherwise assert the exact resolved URL.

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

- Include exactly `schema_version`, `change_id`, `layer`, `files`, `mapping`,
  and `required_capabilities`. The manifest `mapping` and
  `required_capabilities` must exactly match the final structured result.
- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file
  whose content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target.

Capability keys must be exact typed leaves. Every mapped function is named
`test_<case_id_lowercase>__<desc>`.
