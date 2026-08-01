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
- `repo:tests/fuzz/**`
- `repo:tests/testdata/domain/**`

## Outputs

### required

- `change:plans/fuzz-plan.md`
- `change:plans/fuzz-codegen-plan.md`
- `change:plans/fuzz-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue into codegen. The graph gate is the progression authority.

## Domain Notes

Test Function Mapping uses `Case ID | Test Function | Target File` plus Schema Acquisition fields.

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```
