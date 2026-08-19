---
name: aa-fuzz-plan
description: Produce reviewable fuzz plan files before test code is written.
---

## Purpose

Turn the approved Fuzz portion of a QA Case Delta into reviewable implementation plans for `aa-fuzz-plan-reviewer` and, after that gate passes, `aa-fuzz-codegen`.

## Inputs

### required

- `change:cases/**/case.yaml`
- `change:proposal.md`

### optional

- `change:facts/fact-baseline.json`
- `repo:.aa/config.yaml`
- `repo:.aa/data-knowledge.yaml`
- `repo:app/**` (read-only SUT contract evidence)
- `repo:tests/fuzz/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-codegen-mapping.json`
- `change:plans/fuzz-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue into codegen. The graph gate is the progression authority.

## Domain Notes

Both `fuzz-plan.md` and `fuzz-codegen-plan.md` must be independently consumable.
Each file must contain the exact `## Test Function Mapping` heading and the
following four-column mapping shape. Copy it
once into each output and replace the example values; do not split Schema
Acquisition into prose or a code block:

```markdown
## Test Function Mapping

| Case ID | Test Function | Target File | Schema Acquisition |
|---|---|---|---|
| TC_DEPT_FUZZ_001 | `test_tc_dept_fuzz_001__create_payload` | `tests/fuzz/test_dept_fuzz.py` | `from_url: /openapi.json; fallback: app.openapi()` |
```

Do not rename the heading to `Test Function Spec`, `Files`, or prose that merely
describes the mapping. Every row must have a non-empty Schema Acquisition cell.
The codegen precommit validator prefers `fuzz-codegen-mapping.json` and falls
back to this table in `fuzz-codegen-plan.md`. Write both; they must name the
same Case ID → function → file relation. A missing or mismatched mapping fails closed.

Every mapped function name is `test_<case_id_lowercase>__<behavior>` and must
contain the complete Case ID. For example, `TC_USER_FUZZ_001` maps to a name
starting with `test_tc_user_fuzz_001__`; shortened names such as
`test_user_create_fuzz` are invalid because coverage cannot bind them to a case.

Positive seed closure is required for every mutation-based fuzz case. Spell out
the concrete valid baseline values and derive them from the frozen OpenAPI/fact
evidence, including format and persistence-model length validators. Include
fixed prefixes and candidate markers when calculating the final field length.
Never assume reserved domains such as
`example.test` pass an email validator; use a runtime-proven value or a generally
valid domain such as `example.com`. An invalid positive seed makes the plan not
codegen-ready because it tests setup failure instead of the intended mutation.
Use an observed optional/default association value when the schema permits it;
do not require a pre-existing related record merely because the field is present.

Derive rejection oracles from constraints that actually exist in the declared
OpenAPI/router schema. An unconstrained long string is a robustness input, not a
schema-invalid input: require no 5xx, but do not require rejection unless a real
maximum length or application validator proves it invalid. Plan cleanup for every
mutation that can create state, including an unexpectedly accepted candidate,
before reusing any unique field in a later seed.

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```
