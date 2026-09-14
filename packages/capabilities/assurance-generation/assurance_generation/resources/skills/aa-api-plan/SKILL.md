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

## Boundary Construction Proof

Before authoring or repairing the plan package, inventory every selected case
whose input, output, count, ordering, or payload has a source-proven numeric or
length boundary. For each boundary, carry one proof with these fields through
the relevant plan tables and notes: source constraint, target value,
construction expression, arithmetic proof, runtime assertion, and expected
response. Reconcile the proof across the case, API plan, test-data plan, and
codegen plan before returning.

An upper bound on a helper result does not prove its exact length. Normalize a
variable-length helper result to the required length instead of adding a suffix
to an assumed base length. For example, when a non-empty unique base is at most
20 characters and the required invalid value is exactly 21 characters, use an
explicit construction such as `candidate = (base + "x" * 21)[:21]`, record the
proof `len(candidate) = 21`, and require codegen to check
`len(candidate) == target_length` before issuing the request. Apply the same
proof discipline to minimums, maximums, off-by-one cases, collection sizes,
timeouts, and other numeric boundaries.

## Durable mapping and the execution view

Closed mapping `target_file` values must stay under `qa/tests/`. The
execution view remaps `qa/tests/<rest>` to `tests/<rest>` for pytest
collection. Fixtures and support modules live under `qa/tests/`.
Treat a missing `qa/tests/**/conftest.py` as fixture unavailability.
Do not look up fixtures under the SUT `tests/` tree.
Do not retarget mapping rows to `tests/`.

## Inputs

Read `proposal.md` first. When its `Product Source Verification` section lists
exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

Before authoring the first plan, close a complete support/runtime inventory for
the whole API package. For every mapped target, exact-read every candidate
support module and read every ancestor `conftest.py` from the target directory
through the test root. Resolve each consumed dotted symbol to its implementation,
compare each helper's real signature with the planned data need, and trace its
environment, credential, and persistence requirements. Never assign `missing`
or `create-if-missing` from glob or grep output when an exact declared path can
be read. If an existing helper cannot express a lifecycle step such as nested
entity creation, keep the helper only for the operations its signature supports
and map the remaining step to a source-proven test-local boundary. For every
negative request, retain the ideal non-creation assertion and plan bounded
finally-safe cleanup in case the defective product unexpectedly persists data.

### required

- `qa/cases/**/case.yaml`
- `qa/.qa.yaml`
- `qa/proposal.md`
- `qa/results/facts/fact-baseline.json`

### optional

- `qa/results/review/api-plan-review.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only contract evidence)
- `qa/tests/testdata/domain/**`
- `qa/tests/api/adapters/**`
- `qa/tests/config.py`
- `qa/tests/conftest.py`

## Initial administrator credentials

When a selected API case requires administrator authentication, derive the exact
`admin_username` and `admin_password` from the product's startup initialization or seed source.
Use the fact baseline to locate the evidence, then exact-read the cited product source and verify
that both values are the ones used to create the initial administrator. Record the verified pair
in the Auth Strategy and Run Guidance sections consumed by codegen.

Never use a test-runtime credential default or infer a password from a conventional value. Do not
read `.env` or `*.env` files. If both values cannot be resolved from deterministic source, mark
codegen not ready and state the unresolved source instead of supplying a fallback.

## Outputs

### required

- `qa/results/plans/api-plan.md`
- `qa/results/plans/api-test-data-plan.md`
- `qa/results/plans/api-codegen-plan.md`
- `qa/results/plans/api-codegen-mapping.json`
- `qa/results/plans/m3-review-summary.md`

## Closed Codegen Mapping Contract

`api-codegen-mapping.json` must use this exact JSON shape:

```json
{"schema_version":"1","layer":"api","entries":[{"case_id":"TC_DEPT_API_001","symbol":"test_tc_dept_api_001__behavior","target_file":"qa/tests/api/test_dept.py"}]}
```

Use `schema_version: "1"`, not `"1.0"`. The only top-level keys are
`schema_version`, `layer`, `entries`, and optional `schema_case_ids`. Each entry
has exactly `case_id`, `symbol`, and `target_file`. Do not emit `family`,
`change_id`, `mappings`, or `test_function`. Map every selected API Case ID
exactly once, and no other Case ID.

### conditional

- `qa/results/plans/data-knowledge.proposal.api.yaml`

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

When a negative request omits the field that would identify a created entity,
prove non-creation with a bounded before/after collection or tree snapshot (or
an equivalent source-backed invariant). Never invent a sentinel value that is
absent from the request and then query for that value; it cannot identify any
side effect of the request.

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

On planner re-entry, the final JSON instruction's `plan_repair_review` is the
authoritative current review. Do not glob for or read any other plan-review
file; apply only the findings named in `auto_fix_plan` and only their `locator`
targets. Apply
every listed finding in the same planner re-entry; do not return after repairing
only the first finding. Treat each locator as authorizing exactly its named
artifact and key/section, and do not infer permission to edit a second artifact
from prose in another finding. Do not rewrite unmentioned plan sections or
mapping rows. Keep
`plans/api-codegen-mapping.json` as the closed Case ID → symbol → target file
contract; the markdown plan is narrative only.

Before editing an authorized section, exact-read every fixture or helper
implementation named by a current finding. Resolve dotted Python symbols to
their `.py` modules, read ancestor `conftest.py` files for mapped pytest
targets, and inspect any named configuration or environment boundary. Do not
preserve an `absent`, `missing`, or `create-if-missing` claim after the exact
referenced file opens and proves the symbol exists. After the edits, exact-read
all required plan outputs and verify every authorized finding is no longer
contradicted in its located section before returning.

For a repair finding about a boundary value, rewrite the located construction
and its arithmetic proof first, then re-read every cross-artifact occurrence of
that case. Do not treat a narrative label such as "21-character value" as proof
when the construction expression produces a different length.

On every return, `output_files` is the complete plan-package manifest. List all
required output paths above, including required files that were unchanged in
this repair. Do not return only the files edited in the current repair.

Shared business-valid factories belong in `qa/tests/testdata/domain/`. They own
domain defaults and invariant-preserving create/cleanup behavior, return plain
snapshots, and contain no HTTP client, browser, property, or load-test glue.

Reusable cross-test API lifecycle and transport glue belongs in
`qa/tests/api/adapters/`. A plan maps each external reusable data need to a shared
domain factory or API adapter when it actually consumes one. Requests that are
the behavior under test, and existing helpers private to the closed-mapping
target, stay in that mapped API test and do not require an adapter capability.
L1 declaration identifies the contract; on-disk inspection determines
implementation availability. When an exact L1-declared symbol has no on-disk
implementation, the first selected codegen layer may mark its bounded target
`create-if-missing`. After that owner is selected, later selected layers must
reuse that implementation. Never treat the mere absence of a declared module
as a reason to block codegen.

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
  `Shared Module` names `qa/tests/testdata/domain/<entity>.py`. Ownership is
  `reuse` when the implementation exists or an earlier selected layer owns its
  creation; otherwise the first selected layer uses `create-if-missing` for the
  exact bounded L1-declared symbol.
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
