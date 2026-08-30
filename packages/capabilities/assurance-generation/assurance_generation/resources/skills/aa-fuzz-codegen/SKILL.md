# Fuzz codegen

Capability-owned fuzz codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed fuzz plan and frozen case references into generated property
tests plus a closed mapping. Schema truth is `assurance_generation.contracts`
for generated files and mapping, and `assurance_intake.contracts` for reviewed
cases.

## Inputs

### required

- reviewed fuzz plan (`PlanResultV1`) including endpoint/property strategy
- frozen case references for the selected fuzz cases
- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-mapping.json`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`
- `qa/changes/<change-id>/review/fuzz-plan-review.json`
- `qa/changes/<change-id>/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `qa/changes/<change-id>/review/fuzz-plan-checks.json`
- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/fuzz/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/codegen/fuzz-codegen-summary.md`
- `qa/changes/<change-id>/codegen/fuzz-generated-files.json`
- generated or updated test files under `qa/changes/<change-id>/generated/fuzz/files/tests/fuzz/**`

### conditional

- `qa/changes/<change-id>/generated/fuzz/files/tests/testdata/domain/**` when the
  reviewed plan authorizes a shared builder

The generated-files manifest and mapping keep `target_path="tests/..."`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only authorized staged files under
`qa/changes/<change-id>/generated/fuzz/files/` plus the summary and
generated-files manifest. Manifest `repo_path` / mapping `target_file` remain
the logical `tests/fuzz/**` or `tests/testdata/**` target.

Do not modify product source.

The graph owns phase state. Do not write an orchestration state file.

Framework is schemathesis / Hypothesis. Keep Case ID → symbol → target file
traceability exact. There is no fuzz codegen-fix handler in this phase.

## Frozen Inputs and Completion Check

Plan, case, and review inputs are immutable. Read them as approved evidence;
never rewrite, repair, or supersede them during codegen.

Every closed-mapping target must appear in `files` as a `test_entry` with the
exact mapped Case IDs. Reopen every target before returning and verify that the
mapped symbol exists in that file. Do not list plan, case, or review inputs in
`files`.

## Local Module Closure

Before writing the manifest, verify every top-level `tests.*` import and every
dynamic `*_MODULE = "tests...."` reference resolves to either a frozen
repository file or a file generated in this candidate. A data-knowledge symbol
is not proof that its Python module exists. Generate the authorized support
module or use an existing resolvable implementation.

## Generated-files Manifest Rules

- Include exactly `schema_version`, `change_id`, `layer`, `files`, `mapping`,
  and `required_capabilities`. The manifest `mapping` and
  `required_capabilities` must exactly match the final structured result.

Only `test_entry` manifest entries may claim mapped Case IDs, and their
`case_ids` must exactly match the codegen mapping for that path. Support and
shared-builder entries always use `case_ids: []`.

Use `generated` only for a newly added file and `updated` only for a file whose
content this invocation changed. `reused` is legal only for an unchanged,
selected private-root `test_entry` that is itself a codegen-mapping target.

When Hypothesis tests also accept pytest fixtures, bind generated values by name
(`@given(field=...)`), never positionally. Subtract those named `@given(...)`
parameters from the function signature and resolve every remaining parameter to
a fixture. `client` and `admin_token` are not implicit.

When `QA_FUZZ_SCHEMA_MODE=uri` or `FUZZ_SCHEMA_MODE=uri`, acquire the OpenAPI
document from the live product with the configured HTTP client. In this mode
never import an application module, request an `app` fixture, or use an
in-process ASGI client.

Treat that OpenAPI document as a generation input. Resolve every target
operation by exact `(METHOD, PATH)` membership and derive the request-body
schema from the resolved operation. Never hard-code a guessed operation.

Validate each positive seed before applying fuzz mutations. Do not use the
reserved `example.test` domain for a valid email unless the product is proven to
accept it; prefer a proven value or `example.com`.

Implement every mapped test with the exact canonical symbol
`test_<case_id_lowercase>__<behavior>`. Capability keys must be exact typed
leaves.

Do not bind different imports to the same module-level name. Alias Hypothesis
configuration when the test also imports repository settings.
