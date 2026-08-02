---
name: aa-fuzz-codegen
description: Generate fuzz tests and a strict generated-files manifest after the plan gate passes.
---

## Purpose

Generate fuzz tests under `tests/fuzz/**`, shared builders under `tests/testdata/**` when authorized, a human summary, and a strict generated-files manifest. The graph gate is the progression authority.

## Inputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-review-summary.md`
- `change:review/fuzz-plan-review.json`
- `change:review/fuzz-plan-checks.json`
- `change:cases/**/case.yaml`
- `repo:.aa/data-knowledge.yaml`

### optional

- `repo:.aa/config.yaml`
- `repo:tests/fuzz/**`
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

## Boundaries

Write only authorized test/testdata paths plus the summary and manifest. Do not modify product source.
