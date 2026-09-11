# Fuzz codegen

Capability-owned fuzz codegen skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn a reviewed fuzz plan and frozen case references into generated property
tests plus a closed mapping. Schema truth is `assurance_generation.contracts`
for generated files and mapping, and `assurance_intake.contracts` for reviewed
cases.

## Inputs

Read every exact product-source path cited by the approved plan before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Use path-scoped grep only after the
exact reads, and never replace source-backed plan facts with guesses from naming.

### required

- reviewed fuzz plan (`PlanResultV1`) including endpoint/property strategy
- frozen case references for the selected fuzz cases
- `qa/results/plans/fuzz-plan.md`
- `qa/results/plans/fuzz-codegen-plan.md`
- `qa/results/plans/fuzz-codegen-mapping.json`
- `qa/results/plans/fuzz-review-summary.md`
- `qa/results/review/fuzz-plan-review.json`
- `qa/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `qa/results/review/fuzz-plan-checks.json`
- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/fuzz/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/results/codegen/fuzz-codegen-summary.md`
- `qa/results/codegen/fuzz-generated-files.json`
- generated or updated test files under `qa/tests/fuzz/**`

### conditional

- `qa/tests/testdata/domain/**` when the
  reviewed plan authorizes a shared builder

The generated-files manifest and mapping keep `target_file` under `qa/tests/`. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only authorized files under
`qa/tests/` plus the summary and
generated-files manifest. Manifest `repo_path` / mapping `target_file` are
the logical and physical `qa/tests/fuzz/**` or `qa/tests/testdata/**` path.

Do not modify product source.

The graph owns phase state. Do not write an orchestration state file.

Framework is schemathesis / Hypothesis. Keep Case ID → symbol → target file
traceability exact.

Use the installed Schemathesis v4 API exactly: construct an in-memory schema as
`schemathesis.openapi.from_dict(document)`. Never pass `base_url` to
`schemathesis.openapi.from_dict`; when a generated Schemathesis case performs
the request, pass `base_url` to `case.call` or `case.call_and_validate` instead.
Schemathesis `case.call(..., session=...)` accepts only a `requests.Session`;
never pass an `httpx.Client` as the Schemathesis `session`. When the inherited
runtime client is HTTPX, omit `session` from `case.call` so Schemathesis uses
its own transport, and keep the inherited HTTPX client for lifecycle discovery
and cleanup. When that generated request is authenticated, pass the
authenticated headers or cookies explicitly to `case.call`; Schemathesis's own
transport does not inherit authentication state from the HTTPX lifecycle client.
If the generated test uses Schemathesis only to select or inspect an operation,
no base URL is needed on the schema object.
For a hand-authored payload, construct an explicit case with
`operation.Case(...)`; `operation.make_case(...)` does not exist in
Schemathesis v4. When generation should come from the operation schema, use
`operation.as_strategy()` and draw or parameterize the resulting Case objects.

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

## Local Module Closure

Before writing the manifest, verify every top-level `tests.*` import and every
dynamic `*_MODULE = "tests...."` reference resolves to either a frozen
repository file or a file generated in this candidate. A data-knowledge symbol
is not proof that its Python module exists. Use an existing resolvable
implementation, an explicitly listed support output, or define the required
helper in the authorized mapped target.

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

Never pass a function-scoped fixture to an `@given` test. Do not suppress
`HealthCheck.function_scoped_fixture`: doing so would reuse one mutable fixture
across examples instead of resetting it. Reuse only immutable, longer-lived
infrastructure fixtures such as an HTTP client or credential, and perform
mutable setup and cleanup inside each generated example (or through ordinary
helpers called by the test) so every example owns its state lifecycle.

Before defining any fixture in a mapped test module, exact-read every ancestor
`conftest.py` from the mapped file's directory to the test root. Never shadow an
existing fixture in the mapped test module. Use the discovered fixture directly
and reuse its response-envelope handling instead of duplicating login or token
extraction code.

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
