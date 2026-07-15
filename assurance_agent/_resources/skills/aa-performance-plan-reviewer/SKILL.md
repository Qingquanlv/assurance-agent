---
name: aa-performance-plan-reviewer
description: "Review AA performance planning artifacts before performance code generation. Use after aa-performance-plan has generated performance plan files. Read-only: writes performance-plan-review.json and performance-plan-review-summary.md, never modifies plan files."
---

## Test Data Architecture Contract

- Require shared builders in `tests/testdata/domain/` and bulk seed/cleanup adapters in `tests/perf/adapters/`.
- Require mappings to `capabilities.domain_factories` and `capabilities.adapters.performance`.
- Reject Locust code coupled to pytest fixtures, API/E2E adapters, per-user ORM initialization, or a cleanup plan without a generated-data manifest.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.performance_plan.status == done`.
3. Read input files from disk: `plans/performance-plan.md`, `plans/performance-codegen-plan.md`, `plans/performance-review-summary.md`, and the selected `cases/**/case.yaml` (type == Performance).
4. If any required file is missing, stop and report.
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/review/performance-plan-review.json`
   - `qa/changes/<change-id>/review/performance-plan-review-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.performance_plan_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.performance_plan_review.gate_file = review/performance-plan-review.json`

---

# AA Performance Plan Reviewer

## Purpose

Review the performance plan before code generation and produce a machine-readable gate. Read-only; must not modify plan files.

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance to `aa-performance-codegen` without a valid `performance-plan-review.json`.

- You **must** write valid JSON with all required fields, including `codegen_readiness`.
- A natural-language verdict is **not** a substitute for the JSON gate.
- User approval does not release the gate.
- If you cannot write the JSON file, treat the review as failed (STOP condition).

## Review Criteria

1. **Case-to-plan coverage** — every selected `type: Performance` case maps to a planned scenario.
2. **Thresholds present and absolute** — each scenario has non-empty `p95_ms` and `error_rate_max`. Missing or placeholder thresholds → **blocker** (Performance must be a pass/fail against confirmed absolute thresholds, not a baseline comparison).
3. **Load profile concrete** — `users`, `spawn_rate`, `run_time_s` are set.
4. **Target environment resolvable** — base URL comes from `.aa/config.yaml` (or explicitly provided); not vague.
5. **Auth strategy** — Locust user auth reuses known fixtures/data-knowledge; no hardcoded real tokens.
6. **Target file path** — outputs map to `tests/perf/locustfile_<module>.py` (required for runner discovery).

## Decision Rules

Return one of: `pass` / `needs_fix` / `needs_human_review` / `reject`.

- **pass**: `codegen_readiness in [ready, ready_with_warnings]`, `blockers` empty, no blocking `needs_review`.
- **needs_fix**: mechanically fixable plan issues only; `auto_fix_plan` non-empty; all referenced findings `severity in [low, medium]`.
- **needs_human_review**: thresholds missing/unconfirmed, base URL unknown, or auth unknown.
- **reject**: wrong feature, required files missing, plan contradicts case files, or Performance used as a baseline/regression comparison instead of absolute thresholds.

### Gate Consistency Rules

- If `blockers` non-empty → `decision != pass`, `codegen_readiness = not_ready`, `next_action != continue`, `auto_fix_allowed = false`.
- If `codegen_readiness == not_ready` → `decision != pass`, `next_action != continue`.
- If `decision == pass` → `codegen_readiness in [ready, ready_with_warnings]`, `blockers` empty, `human_review_required = false`, `auto_fix_allowed = false`, `next_action = continue`.
- If any `needs_review` item `blocking == true` → `decision = needs_human_review`, `human_review_required = true`, `next_action = human_review`.
- Missing/placeholder thresholds → MUST be a blocker; `decision` MUST NOT be `pass`.

### next_action Mapping

| decision | next_action |
|---|---|
| `pass` | `continue` |
| `needs_fix` | `run_performance_plan_fixer` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `src/schema/review.ts` (validated by `aa validate`). The example below is
> illustrative only. After writing review files you MUST run:
>
> ```
> aa validate --change <change-id> --artifact review/performance-plan-review.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

Write valid JSON to:

```text
qa/changes/<change-id>/review/performance-plan-review.json
```

**Minimal top-level structure (illustrative — see `src/schema/review.ts` for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "performance-plan",
  "change_id": "<change-id>",
  "decision": "pass",
  "risk_level": "low",
  "codegen_readiness": "ready",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "summary": "Short review summary.",
  "reviewed_files": [],
  "blockers": [],
  "findings": [],
  "needs_review": [],
  "auto_fix_plan": [],
  "next_action": "continue",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

`reviewed_files` MUST be non-empty and include `plans/performance-plan.md`, `plans/performance-codegen-plan.md`, `plans/performance-review-summary.md`.

## Hard Rules

- Do not modify plan files.
- Do not write `decision == pass` when any scenario lacks confirmed absolute thresholds.
- Do not accept plans whose base URL or auth strategy is unresolved.
- Performance is a pass/fail against absolute thresholds — reject baseline/regression framing.
