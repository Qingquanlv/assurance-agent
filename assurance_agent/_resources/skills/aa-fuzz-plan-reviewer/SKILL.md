---
name: aa-fuzz-plan-reviewer
description: Review fuzz planning artifacts semantically before code generation. Writes the registered fuzz plan review JSON and Markdown summary without modifying plan files.
---

## Purpose

Act as the semantic gate between `aa-fuzz-plan` and `aa-fuzz-codegen`. Decide whether the plan covers approved Fuzz cases, preserves robustness intent, exposes concrete risks, and can be implemented without guessing product behavior.

Review schema source resolvability, related API case linkage, authentication semantics, seed/corpus adequacy, target paths, and robustness-only expectations. Do not reimplement deterministic plan checks or restate the registered JSON schema.

Apply non-deprecated guidance from `.aa/memory/aa-fuzz-plan-reviewer.md` when that read-only file exists. Empty scope is handled by the graph applicability operation before this skill runs.

## Inputs

Required; stop if any is missing:

- `qa/changes/<change-id>/plans/fuzz-plan.md`
- `qa/changes/<change-id>/plans/fuzz-codegen-plan.md`
- `qa/changes/<change-id>/plans/fuzz-review-summary.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

Read when present:

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`

## Outputs

Write both:

- `qa/changes/<change-id>/review/fuzz-plan-review.json`
- `qa/changes/<change-id>/review/fuzz-plan-review-summary.md`

The JSON is the registered `PlanReview` gate artifact; its schema contract is supplied by the runtime (`aa validate`). Populate the semantic verdict, concrete downstream risks, all codegen capability leaves, and an actionable next step.

Always emit `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, `risk_level`, and non-empty fully qualified `required_capabilities` for an applicable review. Use this consistency map:

| `decision` | `codegen_readiness` | `auto_fix_allowed` | `human_review_required` | `next_action` |
|---|---|---|---|---|
| `pass` | `ready` or `ready_with_warnings` | `false` | `false` | `continue` |
| `needs_fix` | `not_ready` | `false` | `false` | `human_review` |
| `needs_human_review` | `not_ready` | `false` | `true` | `human_review` |
| `reject` | `not_ready` | `false` | `true` | `stop` |

Fuzz has no automatic plan fixer. Always set `auto_fix_allowed: false` and `auto_fix_plan: []`. For `needs_fix`, use a manual/human next action — use a manual human next action only.

Always set `risk_level` to exactly `low`, `medium`, `high`, or `critical`. The downstream gate applies `policy.human_review_risk_levels`.

The graph coordinator records the task and gate outcome; this skill does not own progression.

## Boundaries

Write only the two review outputs listed in Outputs.

Do not modify plan files, case files, source code, tests, knowledge files, or memory files.

Do not write `review/fuzz-plan-checks.json`; mechanical checks are graph-owned.

Do not invent schema sources, auth, related API cases, factories, or product intent.

Do not continue into code generation. The graph gate is the progression authority.

## Domain Notes

- Schema source must be concrete (`from_asgi` import path or `from_uri` base URL).
- Every Fuzz target must link a functional API `case_id` via `related_cases`.
- Authenticated endpoints reuse known fixtures/data-knowledge; no hardcoded real tokens.
- Expectations are robustness-only (no 5xx, schema-valid input not rejected, response schema-conformant).
- Seed/corpus adequacy must be explicit when the plan relies on seeded state.
