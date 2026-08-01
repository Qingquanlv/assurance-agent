# aa-e2e-codegen (fixture structural contract)

## Inputs

### required

- `change:plans/e2e-plan.md`
- `change:plans/e2e-test-data-plan.md`
- `change:plans/e2e-codegen-plan.md`
- `change:plans/m4-review-summary.md`
- `change:cases/**/case.yaml`
- `change:review/plan-review.json`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/e2e/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/e2e-codegen-summary.md`
- `change:codegen/e2e-generated-files.json`

### conditional

- `repo:tests/e2e/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
