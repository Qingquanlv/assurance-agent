# Performance plan

Capability-owned performance plan skill. Do not select a provider, model, or
adapter.

Turn the approved Performance portion of a reviewed case document into
reviewable implementation plans. Schema truth is `assurance_generation.contracts`
for the plan result and `assurance_intake.contracts` for reviewed cases.

## Locked Capability Selection

The result contract's `required_capabilities` enum is the sole whitelist for
the top-level result, every coverage row, and every performance scenario's
`capability`. Copy exact strings from that enum; never construct a key from a
namespace, helper name, or analogous layer. A declared leaf in one namespace
does not imply a same-suffix `capabilities.adapters.performance.*` leaf. If the
exact needed leaf is not in the enum, describe the gap in the plan/review
readiness; do not emit a virtual key. Before returning, reject your own result
unless every capability value is byte-for-byte present in the enum.

Resolve declared support symbols by exact module-path reads before describing
their implementation status. A helper outside the codegen write whitelist can
still be imported and reused. If that exact helper already implements the
reviewed lifecycle, the plan must not instruct codegen to update it; only the
mapped writable test target is generated. Prefer a declared callable
performance authentication adapter over an ambient token environment variable.

## Prepared source observations

The JSON instruction includes `planning_facts`: bounded static observations with
file digests, exact symbol names/signature shapes, fixture names, and environment
variable names. Use the indexed paths for direct reads instead of rediscovering
them. `unknown` and `uninspected_paths` never prove absence; imports, plugins,
dynamic registrations and transitive environment dependencies may be unresolved.
Environment names expose no values and do not establish availability or necessity.
Check original source for behavior, auth semantics and oracle claims. Keep owner
requirements and frozen assertion intent distinct from observed implementation;
a source defect must not weaken the expected test behavior.

Use exact indexed identifiers when applicable. Before returning, reconcile the
closed mapping with every displayed Test Function Mapping, validate table capability
keys, and distinguish an existing helper amendment from create-if-missing. On repair,
check the whole package for consistency while editing only authorized locators;
if another required edit is outside them, report the scope gap without broadening it.

## Inputs

Read `proposal.md` first. When its `Product Source Verification` section lists
exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

Never read `.env`, `*.env`, or credential-bearing benchmark environment files.
Use environment variable names and non-secret defaults only; derive the required
variable names from approved plans, test configuration source, and the locked
benchmark requirement without inspecting secret values.

### required

- `qa/cases/**/case.yaml`
- `qa/proposal.md`

### optional

- `qa/results/review/performance-plan-review.json`
- `qa/results/facts/fact-baseline.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/perf/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/results/plans/performance-plan.md`
- `qa/results/plans/performance-codegen-plan.md`
- `qa/results/plans/performance-codegen-mapping.json`
- `qa/results/plans/performance-review-summary.md`

## Closed Codegen Mapping Contract

`performance-codegen-mapping.json` must use this exact JSON shape:

```json
{"schema_version":"1","layer":"performance","entries":[{"case_id":"TC_DEPT_PERF_001","symbol":"DeptUser.read_department","target_file":"qa/tests/perf/locustfile_dept.py"}]}
```

Use `schema_version: "1"`, not `"1.0"`. The only top-level keys are
`schema_version`, `layer`, `entries`, and optional `schema_case_ids`. Each entry
has exactly `case_id`, `symbol`, and `target_file`. Do not emit `family`,
`change_id`, `mappings`, or `test_function`. Map every selected Performance
Case ID exactly once, and no other Case ID.

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue
into codegen. The graph owns phase state. Do not write an orchestration state
file.

## Domain Notes

On planner re-entry, the final JSON instruction's `plan_repair_review` is the
authoritative current review. Do not glob for or read any other plan-review
file; apply only the findings named in `auto_fix_plan` and only the artifact
sections identified by their locators. Reinspect the cited source before editing, keep
unrelated plan decisions unchanged, and return the complete updated plan
package for another review round.

On every return, `output_files` is the complete plan-package manifest. List all
required output paths above, including required files that were unchanged in
this repair. Do not return only the files edited in the current repair.

Task Mapping uses `Case ID | Task Method | Target File`.

Task Mapping is a strict one-to-one execution-entry relation:

- Both `performance-plan.md` and `performance-codegen-plan.md` must each contain
  an explicit `## Task Mapping` table with those exact headers.
- Emit exactly one Task Mapping row for each selected Performance Case ID.
- Map that row to the primary executable load-test task method under
  `tests/perf/**`.
- Never add separate Task Mapping rows for setup, cleanup, seed helpers,
  factories, adapters, or support functions.
- A Case ID repeated in Task Mapping is invalid even when the method or target
  file differs.

Every setup lifecycle must document a concrete positive seed and how each field
was validated against the frozen OpenAPI/fact evidence. Never assume a reserved
domain such as `example.test` satisfies an email validator.

For every string seed with a source-declared maximum length, include a literal character budget
for the complete rendered value, including prefixes, uniqueness fragments, and hierarchy suffixes.
Before returning, count every concrete root, child, and grandchild seed name separately; each must
fit the product limit. Do not merely state that a value is within the limit. For a 20-character
department-name limit, a length-safe pattern is `p<uuid8>r`, `p<uuid8>c`, and `p<uuid8>g` (10
characters each). Never use a pattern such as `perfdept<uuid10>root`, whose rendered length is 22.

Inspect the declared router response construction and existing load-test
support code before specifying measured assertions. Preserve the observed
envelope level.

### Seed identifier closure

Before writing a seed lifecycle, inspect the exact create response in router
source or frozen OpenAPI evidence. The plan must not assume `data.id` or
`data.dept_id` merely because an existing helper expects one.

If create does not return an identifier, the plan must name a source-backed
identifier lookup that can resolve the record from a run-unique seed value. It
must state the exact lookup operation, response envelope, match key, identifier
field, ambiguity handling, and how the resolved ID is used for descendants and
child-first cleanup. A list/tree lookup by a run-unique name is valid only when
the inspected router/schema source proves that operation returns both the name
and identifier. If neither the create response nor a supported lookup yields an
ID, do not invent one or claim codegen readiness.

### Runtime host closure

Inspect the existing load-test target and declared runtime configuration. When
the runtime provides `API_BASE_URL` or `BASE_URL`, the performance codegen plan
must explicitly replace hard-coded hosts with those environment variables and
state the precedence. Do not preserve or introduce a localhost fallback unless
the declared runtime contract explicitly requires it.

Valid example:

```markdown
## Task Mapping

| Case ID | Task Method | Target File |
|---|---|---|
| TC_ACCOUNT_PERF_001 | AccountListUser.list_accounts | tests/perf/locustfile_account.py |

## Seed Lifecycle

| Setup | Cleanup | Support Module |
|---|---|---|
| setup_account | cleanup_account | tests/perf/adapters/account_seed.py |
```

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```

The typed plan result must include scenario identity and numeric thresholds
(`p95_ms`, `error_rate_max`). Every planned case must have operation and risk
coverage. Capability keys must be exact typed leaves.

Before returning, verify all of the following:

- every created seed ID comes from the observed create envelope or a named,
  source-backed identifier lookup;
- every concrete seed string has an explicit literal character budget and fits each source-declared
  minimum/maximum length after all uniqueness and hierarchy suffixes are included;
- cleanup uses only IDs established by that lifecycle;
- the codegen plan tells codegen to replace hard-coded hosts whenever the
  runtime exposes `API_BASE_URL` or `BASE_URL`;
- no response field or runtime fallback was inferred from an existing helper
  without corroborating product/runtime evidence.
