# API codegen

Capability-owned API codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed API plan and frozen case references into generated tests plus a
closed mapping. Schema truth is `assurance_generation.contracts` for generated
files and mapping, and `assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- reviewed API plan (`PlanResultV1`)
- frozen case references for the selected API cases
- baseline tree identity
- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-mapping.json`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/review/api-plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/api/**`
- `tests/api/adapters/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/api-codegen-summary.md`
- `qa/changes/<change-id>/codegen/api-generated-files.json`
- generated or updated test files under `tests/api/**`

### conditional

- `tests/testdata/domain/**` when the reviewed plan authorizes a shared builder

## Boundaries

Write only authorized `tests/api/**` and `tests/testdata/**` paths plus the
summary and generated-files manifest.

Do not modify product source.

Do not run the product test runner or invent collection evidence.

The graph owns phase state. Do not write an orchestration state file.

Framework is pytest. Keep Case ID → symbol → target file traceability exact.

## Fixture Closure

Before writing the manifest:

1. List every mapped test function parameter that is not supplied by
   `pytest.mark.parametrize` or Hypothesis `@given(...)`.
2. Resolve each parameter to a fixture defined or imported by the test module,
   an ancestor `conftest.py`, or an installed pytest plugin.
3. For every unresolved project fixture, generate or update an authorized
   `tests/api/**/conftest.py` or support module and list that changed file in the
   manifest with `role: support` and `case_ids: []`.
4. Finish only when the unresolved fixture set is empty.

Names such as `client` and `admin_token` are not implicit fixtures. Their
provider must exist in the candidate tree.

## Runtime Contract Closure

Before writing any HTTP request or the generated-files manifest, inspect the
live product OpenAPI document when `BASE_URL` is configured; otherwise inspect
the actual router and request-schema source. Build an explicit set of
`(METHOD, PATH)` operations and verify every generated request against it.

- Do not derive conventional routes such as `/detail` or choose `PUT` for an
  update merely from its name. Use the exact registered method and path.
- Build request bodies from the registered request schema, including field
  types and required fields. Do not infer payload keys from a UI form.
- Inspect the real response schema or handler. A successful create response
  that contains no identifier must be followed by a supported lookup; never
  assume `data.id` exists.
- If any operation, payload, or response fact cannot be verified, do not emit a
  runnable test for it or claim readiness in the summary.

This verification is read-only and is not test execution.

## Generated-files Manifest Rules

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
