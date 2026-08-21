# Fuzz plan

Capability-owned fuzz plan skill. Do not select a provider, model, or adapter.

Turn the approved Fuzz portion of a reviewed case document into reviewable
implementation plans. Schema truth is `assurance_generation.contracts` for the
plan result and `assurance_intake.contracts` for reviewed cases.

## Inputs

### required

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/proposal.md`

### optional

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

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue
into codegen. The graph owns phase state. Do not write an orchestration state
file.

## Domain Notes

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
