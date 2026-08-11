# aa-api-codegen (fixture structural contract)

## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/api-test-data-plan.md`
- `change:plans/api-codegen-plan.md`
- `change:plans/m3-review-summary.md`
- `change:cases/**/case.yaml`
- `change:review/api-plan-review.json`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/api/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`
- `change:codegen/api-generated-files.json`

### conditional

- `repo:tests/api/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
