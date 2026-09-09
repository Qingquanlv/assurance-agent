# API codegen

Capability-owned API codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed API plan and frozen case references into generated tests plus a
closed mapping. Schema truth is `assurance_generation.contracts` for generated
files and mapping, and `assurance_intake.contracts` for reviewed cases.

## Inputs

Read every exact product-source path cited by the approved plan before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Use path-scoped grep only after the
exact reads, and never replace source-backed plan facts with guesses from naming.

### required

- reviewed API plan (`PlanResultV1`)
- frozen case references for the selected API cases
- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/review/api-plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/api/**`
- `tests/api/adapters/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/api-codegen-summary.md`
- `qa/changes/<change-id>/codegen/api-generated-files.json`
- generated or updated test files under `qa/changes/<change-id>/generated/api/files/tests/api/**`

When `verified_codegen.validation_profile` is present, every mapped test is a
thin entrypoint containing only the exact installed bridge import and one
`execute_case("<mapped-case-id>")` call. Copy every field of the supplied
`verified_codegen` identity into the mapping and add `case_spec_digests` from
the authenticated machine plan. Do not issue HTTP requests, open SQLite,
implement Trace/oracle comparisons, embed expected values, paths, credentials,
execution IDs or runtime verdicts, or add Python assertions. The parent host
owns all runtime facts and verdict inputs.

### conditional

- `qa/changes/<change-id>/generated/api/files/tests/testdata/domain/**` when the
  reviewed plan authorizes a shared builder

The generated-files manifest and mapping keep `target_path="tests/..."`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only authorized staged files under
`qa/changes/<change-id>/generated/api/files/` plus the summary and
generated-files manifest. Manifest `repo_path` / mapping `target_file` remain
the logical `tests/api/**` or `tests/testdata/**` target.

Do not modify product source.

Do not run the product test runner or invent collection evidence.

The graph owns phase state. Do not write an orchestration state file.

Framework is pytest. Keep Case ID → symbol → target file traceability exact.

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

Before writing the manifest:

1. List every mapped test function parameter that is not supplied by
   `pytest.mark.parametrize` or Hypothesis `@given(...)`.
2. Resolve each parameter to a fixture defined or imported by the test module,
   an ancestor `conftest.py`, or an installed pytest plugin.
3. For every unresolved project fixture, use an explicitly listed support
   output when one exists; otherwise define it in the authorized mapped test
   module. List a support file only when its exact path is in `allowed_outputs`.
4. Finish only when the unresolved fixture set is empty.

Names such as `client` and `admin_token` are not implicit fixtures. Their
provider must exist in the candidate tree.

Read each fixture's yielded client type and each consumed helper implementation.
A test being `async def` does not make its fixtures asynchronous. With a
synchronous `httpx.Client`, use `response = api_client.get(...)` without `await`,
including inside async tests. Await requests only when the actual client is
`httpx.AsyncClient` or its source-backed wrapper returns an awaitable.
`response.json()` is synchronous for both clients. An async cleanup helper may
still require `await cleanup_dept(...)` in the same test; mark that async test
for the installed pytest async runner. Decide awaitability per call, not per
test or from a fixture's name.

## Runtime Contract Closure

Before writing any HTTP request or the generated-files manifest, inspect the
live product OpenAPI document when `BASE_URL` is configured; otherwise inspect
the actual router and request-schema source. Build an explicit set of
`(METHOD, PATH)` operations and verify every generated request against it.

- Do not derive conventional routes such as `/detail` or choose `PUT` for an
  update merely from its name. Use the exact registered method and path.
- Build request bodies from the registered request schema, including field
  types and required fields. Do not infer payload keys from a UI form.
- Apply those constraints to every request value, including fixture setup and
  unique-name prefixes. Count the complete runtime value after prefixes,
  suffixes, and random tokens, and fit every generated value within the
  source-backed schema constraints unless that specific case is intentionally
  testing an invalid boundary.
- Inspect the real response schema or handler. A successful create response
  that contains no identifier must be followed by a supported lookup; never
  assume `data.id` exists.
- If any operation, payload, or response fact cannot be verified, do not emit a
  runnable test for it or claim readiness in the summary.

This verification is read-only and is not test execution.

## Generated-files Manifest Rules

- Include exactly `schema_version`, `change_id`, `layer`, `files`, `mapping`,
  and `required_capabilities`. In verified mode the mapping also carries the
  supplied frozen identity and per-case spec digests. The manifest `mapping` and
  `required_capabilities` must exactly match the final structured result.
- Only `test_entry` entries may claim mapped Case IDs, and their `case_ids` must
  exactly match the codegen mapping for that path.
- Every `support` and `shared_builder` entry must use `case_ids: []`.
- Use `generated` only for a newly added file and `updated` only for a file
  whose content this invocation changed.
- `reused` is legal only for an unchanged, selected private-root `test_entry`
  that is itself a codegen-mapping target. Never list an unchanged adapter,
  helper, fixture, or shared builder as `reused`; omit unchanged support
  dependencies from the manifest.

When selected API cases or private-root targets exist, an empty `files` array
is invalid. Generate or update every selected mapping target and list it as a
`test_entry` with the exact mapped Case IDs.

Implement every mapped test with the exact canonical symbol
`test_<case_id_lowercase>__<behavior>` so the complete Case ID is recoverable.
Capability keys must be exact typed leaves.
