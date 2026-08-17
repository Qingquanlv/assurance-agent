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
- `change:plans/performance-codegen-mapping.json`
- `change:plans/performance-review-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`

## Boundaries

Write only the plan artifacts listed in Outputs. Do not write tests or continue into codegen. The graph gate is the progression authority.

## Domain Notes

Task Mapping uses `Case ID | Task Method | Target File`.

Task Mapping is a strict one-to-one execution-entry relation:

- Both `performance-plan.md` and `performance-codegen-plan.md` must each contain
  an explicit `## Task Mapping` table with the exact headers shown below. The
  codegen validator reads the frozen codegen plan directly; prose such as
  "Task Mapping target" or a mapping embedded in another table is not a substitute.
- Emit exactly one Task Mapping row for each selected Performance Case ID.
- Map that row to the primary executable Locust task method under `tests/perf/**`.
- Never add separate Task Mapping rows for setup, cleanup, seed helpers, factories,
  adapters, or support functions. Describe those dependencies under Factory Mapping
  or a separate Seed Lifecycle section instead.
- A Case ID repeated in Task Mapping is invalid even when the method or target file
  differs.

Valid example:

```markdown
## Task Mapping

| Case ID | Task Method | Target File |
|---|---|---|
| TC_ACCOUNT_PERF_001 | AccountListUser.list_accounts | tests/perf/locustfile_account.py |

## Seed Lifecycle

| Setup | Cleanup | Support Module |
|---|---|---|
| setup_account | cleanup_account | tests/perf/adapters/account_seed.py |
```

Factory Mapping section (required):

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/testdata/domain/account.py | make_account | reuse |
```
