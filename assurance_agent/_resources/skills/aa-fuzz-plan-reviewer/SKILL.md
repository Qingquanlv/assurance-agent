---
name: aa-fuzz-plan-reviewer
description: "Review AA fuzz planning artifacts before fuzz code generation. Use after aa-fuzz-plan has generated fuzz plan files. Read-only: writes fuzz-plan-review.json and fuzz-plan-review-summary.md, never modifies plan files."
---

## Test Data Architecture Contract

- Require shared builders in `tests/testdata/domain/`, fuzz adapters in `tests/fuzz/adapters/`, and value strategies in `tests/fuzz/strategies/`.
- Require mappings to `capabilities.domain_factories` and `capabilities.adapters.fuzz`.
- Reject strategies that create persistent state, adapters imported from API/E2E, or conflicting ownership of a shared factory.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.fuzz_plan.status == done`.
3. Read input files from disk: `plans/fuzz-plan.md`, `plans/fuzz-codegen-plan.md`, `plans/fuzz-review-summary.md`, and the selected `cases/**/case.yaml` (type == Fuzz).
4. If any required file is missing, stop and report.
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/review/fuzz-plan-review.json`
   - `qa/changes/<change-id>/review/fuzz-plan-review-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.fuzz_plan_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.fuzz_plan_review.gate_file = review/fuzz-plan-review.json`

---

# AA Fuzz Plan Reviewer

## Purpose

Review the fuzz plan before code generation and produce a machine-readable gate. This skill is read-only; it must not modify plan files.

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance to `aa-fuzz-codegen` without a valid `fuzz-plan-review.json`.

- You **must** write valid JSON with all required fields, including `codegen_readiness`.
- A natural-language verdict in chat is **not** a substitute for the JSON gate.
- User approval does not release the gate; only a valid `fuzz-plan-review.json` does.
- If you cannot write the JSON file, treat the review as failed (STOP condition).

## Review Criteria

1. **Case-to-plan coverage** — every selected `type: Fuzz` case maps to a planned fuzz target.
2. **Schema source resolvable** — `from_asgi` app import path or `from_uri` base URL is concrete, not vague.
3. **Related functional case present** — every Fuzz target links a functional API `case_id` via `related_cases`. Missing link → blocker (Fuzz must not replace functional assertions).
4. **Auth strategy** — authenticated endpoints reuse known fixtures/data-knowledge; no hardcoded real tokens.
5. **Target file path** — outputs map to `tests/fuzz/test_<module>_fuzz.py` (required for runner discovery).
6. **Expectations are robustness-only** — no 5xx, schema-valid input not rejected, response schema-conformant. Reject plans that assert business outcomes via Fuzz (that belongs in API).

## Decision Rules

Return one of: `pass` / `needs_fix` / `needs_human_review` / `reject`.

- **pass**: `codegen_readiness in [ready, ready_with_warnings]`, `blockers` empty, no blocking `needs_review`.
- **needs_fix**: mechanically fixable plan issues only; `auto_fix_plan` non-empty; all referenced findings `severity in [low, medium]`.
- **needs_human_review**: schema source unknown, auth unknown, or a Fuzz case lacks a related functional case.
- **reject**: wrong feature, required files missing, plan contradicts case files, or Fuzz used to replace functional assertions.

### Gate Consistency Rules

- If `blockers` non-empty → `decision != pass`, `codegen_readiness = not_ready`, `next_action != continue`, `auto_fix_allowed = false`.
- If `codegen_readiness == not_ready` → `decision != pass`, `next_action != continue`.
- If `decision == pass` → `codegen_readiness in [ready, ready_with_warnings]`, `blockers` empty, `human_review_required = false`, `auto_fix_allowed = false`, `next_action = continue`.
- If any `needs_review` item `blocking == true` → `decision = needs_human_review`, `human_review_required = true`, `next_action = human_review`.

### next_action Mapping

| decision | next_action |
|---|---|
| `pass` | `continue` |
| `needs_fix` | `run_fuzz_plan_fixer` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `src/schema/review.ts` (validated by `aa validate`). The example below is
> illustrative only. After writing review files you MUST run:
>
> ```
> aa validate --change <change-id> --artifact review/fuzz-plan-review.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

Write valid JSON to:

```text
qa/changes/<change-id>/review/fuzz-plan-review.json
```

**Minimal top-level structure (illustrative — see `src/schema/review.ts` for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "fuzz-plan",
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

`reviewed_files` MUST be non-empty and include `plans/fuzz-plan.md`, `plans/fuzz-codegen-plan.md`, `plans/fuzz-review-summary.md`.

## Hard Rules

- Do not modify plan files.
- Do not write `decision == pass` when a Fuzz case lacks a related functional case.
- Do not accept plans whose schema source or auth strategy is unresolved.
