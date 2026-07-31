---
name: aa-performance-plan-reviewer
description: Review performance planning artifacts semantically before code generation. Writes the registered performance plan review JSON and Markdown summary without modifying plan files.
---

## Purpose

Act as the semantic gate between `aa-performance-plan` and `aa-performance-codegen`. Decide whether the plan covers approved Performance cases, preserves measurable intent, exposes concrete risks, and can be implemented without guessing product behavior.

Review absolute thresholds, load shape, scenario coverage, and statistical interpretation. Do not reimplement deterministic plan checks or restate the registered JSON schema.

Apply non-deprecated guidance from `.aa/memory/aa-performance-plan-reviewer.md` when that read-only file exists. Empty scope is handled by the graph applicability operation before this skill runs.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/plans/performance-plan.md`
- `qa/changes/<change-id>/plans/performance-codegen-plan.md`
- `qa/changes/<change-id>/plans/performance-review-summary.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

Read when present:

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`

## Outputs

Write both:

- `qa/changes/<change-id>/review/performance-plan-review.json`
- `qa/changes/<change-id>/review/performance-plan-review-summary.md`

The JSON is the registered `PlanReview` gate artifact; its schema contract is supplied by the runtime (`aa validate`). Populate the semantic verdict, concrete downstream risks, all codegen capability leaves, and an actionable next step.

Always emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, `risk_level`, and non-empty fully qualified `required_capabilities` for an applicable review. Use this consistency map:

| `decision` | `codegen_readiness` | `auto_fix_allowed` | `human_review_required` | `next_action` |
|---|---|---|---|---|
| `pass` | `ready` or `ready_with_warnings` | `false` | `false` | `continue` |
| `needs_fix` | `not_ready` | `false` | `false` | `human_review` |
| `needs_human_review` | `not_ready` | `false` | `true` | `human_review` |
| `reject` | `not_ready` | `false` | `true` | `stop` |

Performance has no automatic plan fixer. Always set `auto_fix_allowed: false` and `auto_fix_plan: []`. For `needs_fix`, use a manual/human next action — use a manual human next action only.

Always set `risk_level` to exactly `low`, `medium`, `high`, or `critical`. The downstream gate applies `policy.human_review_risk_levels`.

The graph coordinator records the task and gate outcome; this skill does not own progression.

## Boundaries

Write only the two review outputs listed in Outputs.

Do not modify plan files, case files, source code, tests, knowledge files, or memory files.

Do not write `review/performance-plan-checks.json`; mechanical checks are graph-owned.

Do not invent thresholds, load shapes, factories, or product intent.

Do not continue into code generation. The graph gate is the progression authority.

## Domain Notes

- Absolute thresholds must be numeric and comparable (latency, error rate, throughput).
- Load shape must specify concurrency/ramp/duration without ambiguity.
- Scenario coverage must map every in-scope Performance case.
- Statistical interpretation must state how results are aggregated and judged.
