# Performance codegen

Capability-owned performance codegen skill. Do not select a provider, model, or
adapter. Do not look up a global skill catalog.

Turn a host-built performance codegen scope and frozen case references into generated load
tests plus a closed mapping. Schema truth is `assurance_generation.contracts`
for generated files and mapping, and `assurance_intake.contracts` for reviewed
cases.

## Inputs

Read every exact product-source path cited by the host scope and reviewed cases before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Use path-scoped grep only after the
exact reads, and never replace source-backed case facts with guesses from naming.

Before any glob or directory discovery, convert every declared Python symbol
for setup, cleanup, authentication, and shared support into its exact module
path and read that path directly. The write whitelist does not limit imports or
reads: an existing helper outside `allowed_outputs` remains reusable and must
not be copied or inlined into the mapped target.

Never read `.env`, `*.env`, or credential-bearing benchmark environment files.
Use environment variable names and non-secret defaults only; implement the approved
configuration contract without inspecting credential values.

### required

- host-built performance codegen scope (`CodegenScopeV1`)
- frozen case references for the selected performance cases
- `qa/cases/**/case.yaml`
- `.aa/data-knowledge.yaml`

### optional

- baseline tree identity when the graph provides one
- `qa/results/review/performance-plan-checks.json`
- `.aa/config.yaml`
- product source under the project source root (read-only contract evidence)
- `qa/tests/perf/**`
- `qa/tests/testdata/domain/**`

## Outputs

### required

- `qa/results/codegen/performance-codegen-summary.md`
- `qa/results/codegen/performance-generated-files.json`
- generated or updated test files listed in the host `locked_outputs`

### conditional

- the locked testdata file when the host `locked_outputs` include it

Write only host `locked_outputs`. `target_file` must equal the locked test file
for that case; testdata must be the locked testdata file. Do not
write generated tests into the original `tests/**` tree.

## Boundaries

Write only host `locked_outputs`. Manifest `repo_path` / mapping `target_file`
must equal the locked test file for that case; testdata must be the locked
testdata file.

Do not modify product source.

The graph owns phase state. Do not write an orchestration state file.

Framework is Locust. Keep Case ID → symbol → target file traceability exact.
Use Locust's real task API: `@task` marks the method. Never pass `name=` to the
`@task` decorator; put the stable statistics label on
`self.client.get(..., name=...)` (or the corresponding request method) instead.
When reading the resulting entry, use
`environment.stats.get(stable_name, method)`: the statistics name is the first
argument and the HTTP method is the second. Reversing them silently selects an
empty entry even though the named requests ran.
Import the generated Locust module in the configured runtime before returning
so decorator and class-definition errors fail during codegen rather than the
later execution Attempt.

Resolve every approved setup, cleanup, authentication, or other support
capability symbol against the exact repository path before implementing it. If
the symbol resolves, import and call that exact symbol instead of reimplementing
it inside the Locust file. `create-if-missing` is permission to create an absent
helper, not evidence that it is absent. Only create or inline a replacement
after an exact read proves the declared module or symbol is absent and the
runtime write whitelist authorizes its target.

For authenticated scenarios, controlled execution does not promise inherited
token environment variables. When the plan declares an authentication helper,
call the declared authentication capability with the runtime HTTP client and
use its returned headers. Do not replace that call with a direct token lookup.

## Frozen Inputs and Completion Check

The host-built scope and reviewed cases are immutable. Read them as approved evidence;
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


## Mapping Rules

- Include exactly `schema_version`, `change_id`, `layer`, `files`, `mapping`,
  and `required_capabilities` in the generated-files manifest. The manifest
  `mapping` and `required_capabilities` must exactly match the final structured
  result.
- The final structured result and generated-files manifest must each be exactly
  equal the reviewed plan's `required_capabilities`; never drop authentication,
  setup, or cleanup leaves merely because their helpers were reused unchanged.

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

Capability keys must be exact typed leaves. The closed Task Mapping owns the
mapped Locust class and method name; preserve it exactly.
