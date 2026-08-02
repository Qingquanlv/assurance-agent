# aa-performance-codegen (fixture structural contract)

## Inputs

### required

- `change:plans/performance-plan.md`
- `change:plans/performance-codegen-plan.md`
- `change:plans/performance-review-summary.md`
- `change:cases/**/case.yaml`
- `change:review/performance-plan-review.json`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/perf/adapters/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:codegen/performance-codegen-summary.md`
- `change:codegen/performance-generated-files.json`

### conditional

- `repo:tests/perf/**`
- `repo:tests/testdata/domain/**`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
