# aa-fuzz-codegen (fixture structural contract)

## Inputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-review-summary.md`
- `change:cases/**/case.yaml`
- `change:review/fuzz-plan-review.json`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/fuzz/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/fuzz-codegen-summary.md`
- `change:codegen/fuzz-generated-files.json`

### conditional

- `repo:tests/fuzz/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
