# Fuzz plan

Capability-owned fuzz plan skill. Do not select a provider, model, or adapter.

Turn the approved Fuzz portion of a reviewed case document into reviewable
implementation plans. Schema truth is `assurance_generation.contracts` for the
plan result and `assurance_intake.contracts` for reviewed cases.

## Locked Capability Selection

The result contract's `required_capabilities` enum is the sole whitelist for
the top-level result and every coverage row. Copy exact strings from that enum;
never construct a key from a namespace, helper name, or analogous layer. A
declared leaf in one namespace does not imply a same-suffix
`capabilities.adapters.fuzz.*` leaf. If the exact needed leaf is not in the
enum, describe the gap in the plan/review readiness; do not emit a virtual key.
Before returning, reject your own result unless every capability value is
byte-for-byte present in the enum.

## Inputs

Read `proposal.md` first. When its `Product Source Verification` section lists
exact product-source paths, read every listed path directly before any discovery.
A glob result of `No files found` is not evidence that product source is absent;
ignored source files remain exact-readable. Only declare source unavailable after
those exact reads and a path-scoped grep both fail.

### required

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/proposal.md`

### optional

- `qa/changes/<change-id>/review/fuzz-plan-review.json`
- `qa/changes/<change-id>/facts/fact-baseline.json`
- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- product source under the project source root (read-only)
- `tests/fuzz/**`
- `tests/testdata/domain/**`

## Outputs

### required

- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-mapping.json`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`

## Closed Codegen Mapping Contract

`fuzz-codegen-mapping.json` must use this exact JSON shape:

```json
{"schema_version":"1","layer":"fuzz","entries":[{"case_id":"TC_DEPT_FUZZ_001","symbol":"test_tc_dept_fuzz_001__behavior","target_file":"tests/fuzz/test_dept.py"}]}
```

Use `schema_version: "1"`, not `"1.0"`. The only top-level keys are
`schema_version`, `layer`, `entries`, and optional `schema_case_ids`. Each entry
has exactly `case_id`, `symbol`, and `target_file`. Do not emit `family`,
`change_id`, `mappings`, or `test_function`. Map every selected Fuzz Case ID
exactly once, and no other Case ID.

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue
into codegen. The graph owns phase state. Do not write an orchestration state
file.

## Domain Notes

When `review/fuzz-plan-review.json` exists with `decision: needs_fix`, apply
only the findings named in `auto_fix_plan` and only the artifact sections
identified by their locators. Reinspect the cited source before editing, keep
unrelated plan decisions unchanged, and return the complete updated plan
package for another review round.

Both `fuzz-plan.md` and `fuzz-codegen-plan.md` must be independently
consumable. Each file must contain the exact `## Test Function Mapping` heading
and the following four-column mapping shape:

```markdown
## Test Function Mapping

| Case ID | Test Function | Target File | Schema Acquisition |
|---|---|---|---|
| TC_DEPT_FUZZ_001 | `test_tc_dept_fuzz_001__create_payload` | `tests/fuzz/test_dept_fuzz.py` | `from_url: /openapi.json; fallback: app.openapi()` |
```

Every row must have a non-empty Schema Acquisition cell. Write both the
markdown tables and `fuzz-codegen-mapping.json`; they must name the same
Case ID → function → file relation.

Every mapped function name is `test_<case_id_lowercase>__<behavior>` and must
contain the complete Case ID.

Positive seed closure is required for every mutation-based fuzz case. Spell
out the concrete valid baseline values and derive them from the frozen
OpenAPI/fact evidence. Never assume reserved domains such as `example.test`
pass an email validator.

Derive rejection oracles from constraints that actually exist in the declared
OpenAPI/router schema. Plan cleanup for every mutation that can create state.

If the requirement freezes the project's existing response contract but the
OpenAPI operation omits a success content schema, inspect the actual response
class/envelope in source. Plan exact assertions against that source-backed
contract and record the OpenAPI documentation gap for later issue analysis;
do not invent a schema and do not require a new owner decision. Use the
source-proven application import for any `app.openapi()` fallback and validate
positive seeds against the real request model.

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```

The typed plan result must include an endpoint/property strategy. Every planned
case must have operation and risk coverage. Capability keys must be exact typed
leaves.
