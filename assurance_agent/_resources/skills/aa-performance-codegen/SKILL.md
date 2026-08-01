---
name: aa-performance-codegen
description: Generate performance tests and a strict generated-files manifest after the plan gate passes.
---

## Purpose

Generate performance tests under `tests/perf/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. The graph gate is the progression authority.

## Inputs

### required

- `change:plans/performance-plan.md`
- `change:plans/performance-codegen-plan.md`
- `change:plans/performance-review-summary.md`
- `change:review/performance-plan-review.json`
- `change:review/performance-plan-checks.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/perf/**`
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

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source.
