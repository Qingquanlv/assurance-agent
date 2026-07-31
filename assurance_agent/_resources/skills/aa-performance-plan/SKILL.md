---
name: aa-performance-plan
description: Use when a QA Case Delta contains Performance automation cases and you need reviewable performance plan files before test code is written. Processes added or modified Performance cases with automation.required = true and never generates tests.
---

## Purpose

Turn the approved Performance portion of a QA Case Delta into reviewable implementation plans for `aa-performance-plan-reviewer` and, after that gate passes, `aa-performance-codegen`.

Read the change from disk; do not rely on conversation history. Apply non-deprecated guidance from `.aa/memory/aa-performance-plan.md` when that read-only file exists. Select only `added` and `modified` entries whose type is Performance and whose automation is required; retain `removed` entries only as context. If none remain, stop and report — do not invent empty plans.

Performance planning establishes absolute thresholds, load shape, scenario coverage, and statistical interpretation. Unknown product facts remain explicit review items or blockers. ## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/cases/**/case.yaml`
- `qa/changes/<change-id>/proposal.md`

Optional; warn if absent:

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- `qa/changes/<change-id>/facts/fact-baseline.json`
- existing `tests/perf/**`

## Outputs

Write only below `qa/changes/<change-id>/plans/`:

| Path | Required structure |
|---|---|
| `performance-plan.md` | Scope; scenarios; absolute thresholds; load shape; statistical interpretation; Needs Review; Blockers |
| `performance-codegen-plan.md` | Target Files; Factory Mapping; Scenario Mapping; Threshold Mapping; Codegen Preconditions |
| `performance-review-summary.md` | change and loaded cases; generated files; knowledge status; Blockers; Needs Review; Plan Readiness; Codegen Readiness; next reviewer |

Keep every column name and order exact for mechanically parsed tables.

- At least one table in `performance-plan.md` must contain `Case ID`, and every in-scope case must appear as its full case_id.
- `performance-codegen-plan.md` must include a canonical Factory Mapping section:

```markdown
## Factory Mapping

| Shared Module | Function | Ownership |
|---|---|---|
| tests/factories/account.py | make_account | reuse |
```

Domain-specific sections may remain, but they do not replace Factory Mapping.

## Boundaries

Write only the plan artifacts listed in Outputs.

Do not write `.aa/data-knowledge.yaml`, `.aa/memory/**`, review JSON, checks JSON, or test code.

Do not invent thresholds, load shapes, or product intent.

Do not continue into code generation. The graph gate is the progression authority.
