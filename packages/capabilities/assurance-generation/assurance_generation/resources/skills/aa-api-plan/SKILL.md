# API plan

Capability-owned API plan skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

Turn the approved API portion of a reviewed case document into reviewable
implementation plans. Schema truth is `assurance_generation.contracts` for the
plan result and `assurance_intake.contracts` for reviewed cases.

## Locked Capability Selection

The result contract's `required_capabilities` enum is the sole whitelist for
the top-level result and every coverage row. Copy exact strings from that enum;
never construct a key from a namespace, helper name, or analogous layer. In
particular, a declared `capabilities.domain_factories.*` leaf does not imply a
same-suffix `capabilities.adapters.api.*` leaf. If a genuinely consumed helper
or fixture leaf is not in the enum, describe the gap in the plan/review
readiness; do not emit a virtual key. Before returning, reject your own result
unless every capability value is byte-for-byte present in the enum.

The SUT endpoint being tested is not itself a capability dependency. A mapped
API test may issue a source-proven HTTP request directly with its declared auth
fixture and existing target-local request helpers. Do not invent a missing API
adapter requirement merely because the test performs create, list, get,
update, or delete operations. Require an adapter leaf only when the plan
actually consumes that cataloged reusable adapter symbol.

## Inputs

Read `proposal.md` first. When its `Product Source Verification` section lists
exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

### required

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/.qa.yaml`
- `qa/changes/<change-id>/proposal.md`

### optional

- `qa/changes/<change-id>/review/api-plan-review.json`
- `qa/changes/<change-id>/facts/fact-baseline.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only contract evidence)
- `tests/testdata/domain/**`
- `tests/api/adapters/**`
- `tests/config.py`
- `tests/conftest.py`

## Outputs

### required

- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-mapping.json`
- `qa/changes/<change-id>/plans/api-execution-bindings.json`
- `qa/changes/<change-id>/plans/m3-review-summary.md`

`api-execution-bindings.json` is a candidate locator document with exactly
`schema_version: "1"`, one reviewed `case_id`, and a `bindings` object keyed by
obligation ID. Each value describes only where the runtime obtains an actual
value. Use the installed HTTP User-create and SQLite User binding IDs and
versions supplied by the task inputs. Never emit SQL, expected values,
comparators, `required`, counts, readiness, pass/fail, or completion claims in
this file. Map every reviewed assertion ID plus `initial.user_absent`,
`action.finished`, and `oracle.executed`; when the frozen profile requires
Trace, also map `trace.http`, `trace.user_write`, `trace.user_completed`, and
`trace.drained`.

The host finalizer exclusively writes
`qa/changes/<change-id>/plans/api-case-execution-plan.json`. Do not write or
list a digest for that formal artifact in the structured result. The host
derives its expected references, comparators, required set, completion rules,
readiness, canonical bytes, and digest from authenticated inputs.

## Closed Codegen Mapping Contract

`api-codegen-mapping.json` must use this exact JSON shape:

```json
{"schema_version":"1","layer":"api","entries":[{"case_id":"TC_DEPT_API_001","symbol":"test_tc_dept_api_001__behavior","target_file":"tests/api/test_dept.py"}]}
```

Use `schema_version: "1"`, not `"1.0"`. The only top-level keys are
`schema_version`, `layer`, `entries`, and optional `schema_case_ids`. Each entry
has exactly `case_id`, `symbol`, and `target_file`. Do not emit `family`,
`change_id`, `mappings`, or `test_function`. Map every selected API Case ID
exactly once, and no other Case ID.

### conditional

- `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml`

When a proposal is required, write the complete data-knowledge proposal envelope.
For a delta, `based_on_l1_version` is the current L1 `version`; for a bootstrap
proposal it is `null` and `mode` is `bootstrap`. Top-level `version`,
`change_id`, `proposal_kind`, `target`, `proposed_leaves`, and layer-specific
wrapper objects are forbidden. Validate the whole envelope against the runtime
data-knowledge proposal contract.

## Boundaries

Write only the plan artifacts listed in Outputs.

Do not write `.aa/data-knowledge.yaml` or `.aa/memory/**`.

Do not modify case files, proposal files, or application source.

Do not write tests, factories, adapters, helpers, or execution results.

Do not run the product test runner or execute data setup during planning.

Do not silently guess endpoints, methods, auth, schemas, fixtures, cleanup, or
product behavior.

Before naming an endpoint, payload, response field, or reusable helper, inspect
the declared product source and test inputs. A plan must describe the observed
contract, including create operations whose success response has no identifier
and therefore requires a supported follow-up lookup.

When the frozen Explore advisory and reviewed case set
`assertion_intent: assert_ideal`, keep that ideal oracle even when current source
implements the opposite behavior. The source mismatch is the defect the later
test and issue-analysis flow must expose; do not turn it into an unresolved
authorization decision. Map exact L1-declared fixture/helper symbols to bounded
codegen targets when their implementations are not on disk yet.

Do not use `removed` cases as plan scope.

Do not continue past planning into codegen.

The graph owns phase state. Do not write an orchestration state file.

## Domain Notes

Select only `added` and `modified` entries whose type is API and whose
automation is required. Planning establishes case coverage, endpoint and
assertion intent, data setup and cleanup, factory/adapter ownership, and
separate Plan Readiness and Codegen Readiness. Unknown product facts remain
explicit review items or blockers; they are never guessed.

When `review/api-plan-review.json` exists with `decision: needs_fix`, apply only
the findings named in `auto_fix_plan` and only their `locator` targets. Apply
every listed finding in the same planner re-entry; do not return after repairing
only the first finding. Treat each locator as authorizing exactly its named
artifact and key/section, and do not infer permission to edit a second artifact
from prose in another finding. Do not rewrite unmentioned plan sections or
mapping rows. Keep
`plans/api-codegen-mapping.json` as the closed Case ID → symbol → target file
contract; the markdown plan is narrative only.

Shared business-valid factories belong in `tests/testdata/domain/`. They own
domain defaults and invariant-preserving create/cleanup behavior, return plain
snapshots, and contain no HTTP client, browser, property, or load-test glue.

Reusable cross-test API lifecycle and transport glue belongs in
`tests/api/adapters/`. A plan maps each external reusable data need to a shared
domain factory or API adapter when it actually consumes one. Requests that are
the behavior under test, and existing helpers private to the closed-mapping
target, stay in that mapped API test and do not require an adapter capability.
The first active codegen layer may own a shared module that is absent from L1 as
`create-if-missing`; every L1-declared symbol and every later-layer reference
is `reuse`.

Authoring tables (keep column names exact):

- Scope uses `Case ID | Title`.
- API Targets uses `Case ID | Scenario | Method | Path | Expected`.
- Auth Strategy uses `Case ID | Auth`.
- Request Strategy uses `Case ID | Headers | Body | Params`.
- Assertion Strategy uses `Case ID | Assertions`.
- Mock Strategy uses `Case ID | Dependency | Approach`.
- Cleanup Strategy uses `Case ID | Cleanup`.
- Output File Candidates uses `Case ID | Target File`.
- Required Data uses `Entity | State | Capability`.
- Capability Mapping uses `Need | Capability | Source | Status (found/missing/warning)`.
- Factory / Boundary Strategy uses `Entity | Ring | Preferred method | Notes`.
- No Data Required Cases uses `Case ID | Rationale`.
- Target Files uses `File | Purpose`.
- Test Function Mapping uses `Case ID | Test Function | Target File`. Every
  function is named `test_<case_id_lowercase>__<desc>`; the full case_id and
  double underscore are mandatory.
- Factory Mapping uses `Entity | Shared Module | Function | Ownership | Required By`.
  `Shared Module` names `tests/testdata/domain/<entity>.py`, and Ownership is
  `reuse` for every symbol already declared by L1 knowledge. Use
  `create-if-missing` only when L1 does not declare that shared symbol.
- Adapter Mapping uses `Entity | API Adapter | Transport | Cleanup`.
- Fixture Mapping uses `Fixture | Source Factory | Wrapper Only (yes/no) | Required By`.
- Helper Mapping uses `Helper | Purpose | Required By`.
- Import Strategy uses `Target File | Imports`.
- Assertion Mapping uses `Case ID | Assertions`.
- Data Setup Mapping uses `Case ID | Setup | Capability`.
- Cleanup Mapping uses `Case ID | Cleanup | Capability`.
- Run Guidance uses `Target | Pytest Args | Markers | Environment`.

Every planned case must have operation and risk coverage. Capability keys must
be exact typed leaves. Plan output paths must stay under the declared write
roots.
