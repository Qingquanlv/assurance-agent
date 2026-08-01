---
name: aa-performance-plan
description: Produce reviewable performance plan files before test code is written.
---

## Purpose

Turn the approved Performance portion of a QA Case Delta into reviewable implementation plans for `aa-performance-plan-reviewer` and, after that gate passes, `aa-performance-codegen`.

## Inputs

### required

- `change:cases/**/case.yaml`
- `change:proposal.md`

### optional

- `change:facts/fact-baseline.json`
- `repo:.aa/config.yaml`
- `repo:.aa/data-knowledge.yaml`
- `repo:tests/perf/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:plans/performance-plan.md`
- `change:plans/performance-codegen-plan.md`
- `change:plans/performance-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue into codegen. The graph gate is the progression authority.

## Domain Notes

Task Mapping uses `Case ID | Task Method | Target File`.

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```
