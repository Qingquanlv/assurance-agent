---
name: aa-e2e-plan-reviewer
description: "Review AA E2E planning artifacts before code generation. Use after aa-e2e-plan has generated test planning files. Read-only: writes plan-review.json and plan-review-summary.md, never modifies plan files."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-e2e-plan-reviewer.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared domain builders belong only in `tests/testdata/domain/`; E2E transport belongs only in `tests/e2e/adapters/`.
- Require `capabilities.domain_factories` plus `capabilities.adapters.e2e`; reject API fixtures/adapters reused as E2E execution glue.
- Reject direct async-domain imports from sync Playwright, any in-thread event-loop bridge, or subprocess setup without timeout, JSON/plain-data I/O, exit-code checking, and symmetric cleanup.
- When `review/e2e-plan-checks.json` is present, treat `shared_factory` and other mechanical check outcomes as immutable `PlanCheckDocument` facts. Do not re-adjudicate severity, codegen readiness, or gate routing from those findings — policy belongs to the downstream gate only.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.e2e_plan.status == done`.
3. Read input files from disk: `plans/e2e-plan.md`, `plans/e2e-test-data-plan.md`, `plans/e2e-codegen-plan.md`, `plans/m4-review-summary.md`.
4. If any required file is missing, **STOP** before normal review — see **Missing Input Handling**.
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/review/plan-review.json`
   - `qa/changes/<change-id>/review/plan-review-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.e2e_plan_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.e2e_plan_review.gate_file` = `review/plan-review.json`

---

# AA E2E Plan Reviewer

## Purpose

Review AA E2E planning artifacts before code generation.

Use this skill after `aa-e2e-plan` has generated test planning files.

This skill is **read-mostly**. It reviews plan files and writes structured review outputs. It must not modify plan files.

---

## When to Use

Use this skill when:

- E2E plan files have been generated.
- You need to verify whether the plan is ready for code generation.
- You need a machine-readable review gate before `aa-e2e-codegen`.

---

## Inputs

Required:

```text
qa/changes/<change-id>/plans/e2e-plan.md
qa/changes/<change-id>/plans/e2e-test-data-plan.md
qa/changes/<change-id>/plans/e2e-codegen-plan.md
qa/changes/<change-id>/plans/m4-review-summary.md
```

Optional (only present when `.aa/data-knowledge.yaml` was missing during planning):

```text
qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml
```

Recommended:

```text
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
.aa/data-knowledge.yaml
tests/e2e/**
qa/changes/<change-id>/review/e2e-plan-checks.json
```

When present, treat `review/e2e-plan-checks.json` strictly as an immutable `PlanCheckDocument`: its `status`, `checks`, `findings`, and `refs` are facts. Do not recompute them or infer severity from them. Do not infer or apply a policy action. Policy adjudication belongs only to the downstream gate and policy is not a reviewer input. Never write or modify `review/e2e-plan-checks.json`; the graph mechanical-check operation owns that artifact.

## Outputs

Write:

```text
qa/changes/<change-id>/review/plan-review.json
qa/changes/<change-id>/review/plan-review-summary.md
```

Create the review directory if it does not exist.

---

## Missing Input Handling

If required input files are missing:

- **STOP** before normal review.
- Report every missing required file.
- If a diagnostic JSON must be written, write `plan-review.json` with:
  - `decision = "reject"`
  - `risk_level = "critical"`
  - `codegen_readiness = "not_ready"`
  - `human_review_required = true`
  - `auto_fix_allowed = false`
  - `next_action = "stop"`
  - `blockers` includes every missing required file

Do not write `decision == "pass"` when required plan files are absent.

---

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance to `aa-e2e-codegen` without the JSON file this skill writes. The JSON is the registered gate artifact; its schema contract is supplied by the runtime. Populate the semantic verdict, concrete downstream risks, case-level assertion traceability, all codegen capability leaves, and an actionable next step.

Always emit the gate-consumed fields `codegen_readiness`, `auto_fix_allowed`, `human_review_required`, and `risk_level`, even though compatibility models make them optional. Missing any one sends `missing_field_is` to `stop`. Use this exact consistency map:

| `decision` | `codegen_readiness` | `auto_fix_allowed` | `human_review_required` | `next_action` |
|---|---|---|---|---|
| `pass` | `ready` or `ready_with_warnings` | `false` | `false` | `continue` |
| `needs_fix` | `not_ready` | `true` | `false` | `run_e2e_plan_fixer` |
| `needs_human_review` | `not_ready` | `false` | `true` | `human_review` |
| `reject` | `not_ready` | `false` | `true` | `stop` |

Always set `risk_level` to exactly `low`, `medium`, `high`, or `critical`; `risk_level` is a fact, not a routing instruction. The downstream gate applies `policy.human_review_risk_levels`; the reviewer must not hard-code that policy.

- You **must** write `qa/changes/<change-id>/review/plan-review.json` as valid JSON with all required fields, including `codegen_readiness`.
- You **must** write `qa/changes/<change-id>/review/plan-review-summary.md`.
- A natural language conclusion in chat is **not** a substitute for the JSON file. Never end with only a textual verdict.
- User approval in chat does not release the gate; only a valid `plan-review.json` does. See **User Approval Handling**.
- If you cannot write the JSON file for any reason, treat the review as **failed** — the workflow must treat a missing or invalid `plan-review.json` as a STOP condition.

---

## User Approval Handling

User approval is not a substitute for review.

If the user says "approved", "looks good", or "continue":

- Treat it only as review context.
- Still validate every review criterion independently.
- Cross-check `m4-review-summary.md` **Plan Readiness** (if present), **Codegen Readiness**, **Blockers**, and **Needs Review** — do not contradict plan self-assessment without explicit findings.
- Only write `decision == "pass"` when the plan satisfies all review criteria.
- If blockers or unresolved product decisions remain, write `needs_fix`, `needs_human_review`, or `reject`.

---

## Review Scope

Review the plan for:

- Case-to-plan coverage
- E2E flow correctness
- Test data setup feasibility
- Factory Mapping and Adapter Mapping semantic consistency with `.aa/data-knowledge.yaml`
- Codegen readiness
- Selector and route confidence
- Auth and role handling
- Fixture reuse
- Incremental codegen safety
- Execution command correctness
- Missing knowledge and unknowns
- assertion traceability from approved cases through Assertion Mapping and Test Function Mapping
- required capabilities as fully qualified C4 leaf keys for codegen

---

## Review Criteria

### 1. Case-to-Plan Coverage

Every E2E case under `added` and `modified` should be mapped to one or more planned test scenarios.

Do **not** expect plan coverage for cases under `removed` — verify they appear in `m4-review-summary.md` **Removed Cases** only.

Flag:

- Case missing from plan
- Plan scenario not linked to any case
- Critical assertion missing from plan
- Manual-only step included without automation strategy

**Test function naming (mechanical, blocking):** for every row in `e2e-codegen-plan.md` **Test Function Mapping**, the Test Function MUST match:

```
^test_<case_id lowercase>__
```

i.e. the lowercase form of that row's Case ID as prefix, then a **double underscore** (e.g. `TC_USER_E2E_001` → `test_tc_user_e2e_001__...`). Any mismatch is a **finding with `blocking: true`** and `auto_fix_allowed: true` (fix = rename in the mapping table) — decision cannot be `pass` and `codegen_readiness` cannot be `ready`/`ready_with_warnings` until fixed. The `aa run` parser backfills case_id from this prefix; without it every executed test becomes an Unmapped Test and the gate is capped at PASS_WITH_WARNINGS.

### 2. E2E Flow Correctness

Check whether each flow has:

- Start route or navigation strategy
- User role
- Authentication state
- Preconditions
- Test data
- Steps
- Expected assertions
- Cleanup strategy if data is mutated

Do not accept plans that depend on vague steps such as:

- "go to the correct page"
- "click the proper button"
- "verify it works"

### 3. Test Data Plan

Check:

- Required entities are listed
- Entity states are clear
- Fixture or factory strategy is defined
- Setup order is correct
- Cleanup strategy exists
- Shared data does not create flaky tests
- Data does not depend on production-like hardcoded IDs

Flag unknown factories, unknown states, and missing cleanup.

### 4. Codegen Readiness

Return one of: `ready` / `ready_with_warnings` / `not_ready`

### Hard rule — missing `.aa/data-knowledge.yaml`

If `.aa/data-knowledge.yaml` does not exist (including when `data-knowledge.proposal.e2e.yaml` was generated):

- `codegen_readiness` **MUST** be `not_ready`.
- `decision` **MUST NOT** be `pass`.
- Reason: `aa-e2e-codegen` requires formal `.aa/data-knowledge.yaml`; `data-knowledge.proposal.e2e.yaml` cannot substitute it.
- If `data-knowledge.proposal.e2e.yaml` exists, treat it only as remediation input — verify proposed capabilities are either confirmed from `.aa/data-knowledge.yaml` or flagged as blockers.

**Use `ready` when:**

- `.aa/data-knowledge.yaml` exists.
- Test flows are clear.
- Data setup is feasible.
- Assertions are observable.
- Required capabilities resolve.
- Codegen can proceed without guessing.

**Use `ready_with_warnings` when:**

- `.aa/data-knowledge.yaml` exists.
- All conditions in **Ready With Warnings Boundary** are met.
- Minor, non-blocking assumptions exist.
- Warnings are explicitly documented.
- Codegen can proceed without guessing product behavior.

**Use `not_ready` when:**

- `.aa/data-knowledge.yaml` is missing (hard rule above).
- Route/auth/data/selector behavior is unknown.
- Required case mapping is missing.
- Plan requires product decisions.
- Codegen would likely hallucinate implementation details.
- Any condition in **Ready With Warnings Boundary** is not met.
- `m4-review-summary.md` **Codegen Readiness** is `not_ready` and plan blockers remain unresolved.

### Ready With Warnings Boundary

`ready_with_warnings` is allowed only when:

- `.aa/data-knowledge.yaml` exists.
- Routes are confirmed.
- Auth or storageState strategy is confirmed.
- Required setup scripts / fixtures are available or safely optional.
- Key UI interactions have a locator strategy that does not require guessing.
- Cleanup strategy is confirmed or explicitly unnecessary.
- No blocker exists.
- Warnings are explicitly documented.
- Codegen can proceed without guessing product behavior.

If codegen would need to guess route, selector, auth state, fixture, setup script, expected UI copy, cleanup behavior, or product semantics, use `not_ready`.

### 5. Selector and Route Confidence

Check whether the plan avoids unsupported assumptions.

For **Route Mapping** and **Locator Candidate** tables (or equivalent sections in `e2e-plan.md` / `e2e-codegen-plan.md`):

- Any `Confidence = unknown` **must** appear in Blockers or Needs Review.
- Any key interaction with `Source` missing **must** be flagged.
- Any locator generated without source evidence **must** be `high` severity.
- `data-testid` **must not** be invented; if absent from source, it cannot be used as a confirmed locator.

Acceptable:

- Route is known from existing project files.
- Selector strategy uses accessible role/text/testid when available.
- Residual non-blocking warnings are explicitly documented.

Not acceptable:

- Inventing `data-testid`
- Inventing route path
- Inventing button text
- Inventing auth storage state
- Inventing API setup endpoints
- Marking unknown route, selector, auth state, or required fixture as TODO-only instead of blocker / needs review

### 6. Existing Test Style

If `tests/e2e/**` exists, check whether the plan respects existing style:

- File naming
- Fixture naming
- Auth setup
- Page helper conventions
- pytest or Playwright style
- Incremental append instead of overwrite

### 7. Execution Requirements

If the project requires headed execution, verify the plan includes:

```bash
uv run pytest tests/e2e/ -v --headed
```

If the project uses another runner, verify the command matches the project convention.

### 8. Missing Knowledge and Unknowns

Check that the plan does not silently depend on unknown capabilities:

- Any unknown route, selector, auth state, or factory **must** be recorded as a blocker or needs review — not silently assumed, and not downgraded to TODO-only warnings.
- If `data-knowledge.proposal.e2e.yaml` was generated by `aa-e2e-plan` and `.aa/data-knowledge.yaml` is still missing, set `codegen_readiness = not_ready` and do **not** write `decision = pass` (see **Hard rule — missing `.aa/data-knowledge.yaml`**).
- If `data-knowledge.proposal.e2e.yaml` was generated by `aa-e2e-plan`, verify all proposed capabilities are either confirmed from `.aa/data-knowledge.yaml` or flagged as blockers.
- Unresolved capabilities must appear in `m4-review-summary.md` under Blockers or Needs Review.
- If the plan references fixtures, setup scripts, auth, or cleanup not present in `.aa/data-knowledge.yaml`, flag as `high` severity finding.
- Unknowns that are silently omitted from the plan are a `high` finding.

---

## Decision Rules

Return one of: `pass` / `needs_fix` / `needs_human_review` / `reject`

**Use `pass` when:**

- `codegen_readiness` is `ready` or `ready_with_warnings`.
- `.aa/data-knowledge.yaml` exists.
- No blocker exists.
- Codegen can continue safely.

**Use `needs_fix` when:**

- Plan issues are fixable without product decisions.
- Auto-fix can make the plan codegen-ready.
- `auto_fix_plan` is non-empty and every entry maps to a finding with `severity in ["low", "medium"]`.

**Use `needs_human_review` when:**

- Product behavior is unclear.
- Route/auth/selector/data state is unknown.
- Test scope must be confirmed.
- Codegen would require guessing.

**Use `reject` when:**

- Plan is for the wrong feature.
- Required plan files are missing.
- Plan contradicts case files.
- Codegen would be unsafe or misleading.

### Decision ↔ JSON Field Constraints

| decision | human_review_required | auto_fix_allowed |
|---|---|---|
| `pass` | `false` | `false` |
| `needs_fix` | `false` | `true` (only low/medium severity findings) |
| `needs_human_review` | `true` | `false` |
| `reject` | `true` | `false` |

**Never** set `human_review_required = false` when `decision = needs_human_review`.
**Never** set `auto_fix_allowed = true` when `decision = pass` or `decision = reject`.
**Never** set `decision = needs_fix` with an empty `auto_fix_plan`.

### next_action Mapping

| decision | next_action |
|---|---|
| `pass` | `continue` |
| `needs_fix` | `run_e2e_plan_fixer` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

### Gate Consistency Rules

These combinations are **invalid** and must never appear in `plan-review.json`:

**Blockers**

If `blockers` is non-empty:

- `decision` **MUST NOT** be `pass`
- `codegen_readiness` **MUST** be `not_ready`
- `next_action` **MUST NOT** be `continue`
- `auto_fix_allowed` **MUST** be `false`

**Codegen readiness**

If `codegen_readiness == "not_ready"`:

- `decision` **MUST NOT** be `pass`
- `next_action` **MUST NOT** be `continue`

If `decision == "pass"`:

- `codegen_readiness` **MUST** be `ready` or `ready_with_warnings`
- `.aa/data-knowledge.yaml` **MUST** exist
- `blockers` **MUST** be empty
- No `needs_review` item with `blocking == true`
- `human_review_required` **MUST** be `false`
- `auto_fix_allowed` **MUST** be `false`
- `next_action` **MUST** be `continue`

**Blocking needs_review items**

If `needs_review` contains any item with `blocking == true`:

- `decision` **MUST** be `needs_human_review`
- `human_review_required` **MUST** be `true`
- `auto_fix_allowed` **MUST** be `false`
- `next_action` **MUST** be `human_review`

**needs_fix severity**

If `decision == "needs_fix"`:

- `auto_fix_allowed` **MUST** be `true`
- `auto_fix_plan` **MUST** be non-empty
- Every `auto_fix_plan` item **MUST** map to a finding with `severity in ["low", "medium"]`
- Every referenced finding **MUST** have `human_review_required == false`
- `high` / `critical` findings **MUST NOT** enter `auto_fix_plan`

---

## Risk Level Rules

Use: `low` / `medium` / `high` / `critical`

- **low**: Minor wording or formatting issues.
- **medium**: Some missing details, but codegen may continue with warnings.
- **high**: Missing route/auth/data/selector strategy. Missing core case coverage. Codegen likely to hallucinate.
- **critical**: Wrong feature. Required files missing. Plan contradicts approved case. Destructive or unsafe test behavior.

---

## Auto Fix Rules

Set `auto_fix_allowed = true` only if all required fixes are safe and mechanical.

Auto-fixable examples:

- Add missing case reference
- Normalize headings
- Move content into correct plan section
- Add explicit assertion already present in case YAML
- Add known execution command
- Add TODO markers only for already documented non-blocking warnings
- Clarify fixture reuse if known from `.aa/data-knowledge.yaml`

Do not downgrade blockers into TODO-only warnings.

Unknown route, unknown selector, unknown auth state, missing required fixture/factory, unknown expected UI copy, or unsafe cleanup must remain blockers or `needs_human_review`.

Not auto-fixable:

- Unknown route
- Unknown selector
- Unknown auth state
- Unknown fixture/factory
- Unknown expected UI copy
- Product behavior ambiguity
- Missing requirement scope

---

## Required Capabilities (gate input — mandatory)

Every `plan-review.json` **must** include a non-empty `required_capabilities: string[]` field. This list drives the pre-codegen capability gate (`e2e-plan-review-gate`). Omitting the field or leaving it empty is a **reviewer contract error** → gate **`stop`**.

**Hard schema check (runs before the gate):** `aa validate --change <change-id> --artifact review/plan-review.json` now **rejects** the file when `required_capabilities` is missing, `null`, an empty list, or contains a blank/empty item. This is not optional formatting advice — the mandatory validate step below fails until the list is populated with concrete leaf keys. Never finish the review with an unpopulated list; if you genuinely cannot resolve any leaf, set `codegen_readiness = not_ready`, file the `data-knowledge.proposal.e2e.yaml`, and still list the leaf keys you require so the knowledge-remediation checkpoint has targets.

**How to derive the list**

1. Read `.aa/data-knowledge.yaml` and every in-scope E2E case + plan data requirements.
2. Collect every **leaf-only** dotted key needed for E2E codegen (auth, domain factories, E2E adapters, cleanup).
3. Use C4 leaf paths only — examples:
   - `auth.api_admin_token`
   - `capabilities.domain_factories.menu.make_menu`
   - `capabilities.adapters.e2e.menu.navigate_menu_admin`
   - `capabilities.cleanup.delete_menu`
4. **Never** use container paths — they count as missing at gate time.

**When L1 is missing or lacks required leaves**

- Create or update `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml`.
- Use `mode: bootstrap` when `.aa/data-knowledge.yaml` is absent; `mode: delta` when L1 exists but leaves are missing.
- Include only missing leaf candidates; do **not** write `.aa/data-knowledge.yaml` directly.
- Set `codegen_readiness = not_ready` until L1 leaves exist; missing leaves route to the **knowledge-remediation** checkpoint (`aa knowledge promote`, then re-run review).

---

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `src/schema/review.ts` (validated by `aa validate`). The example below is
> illustrative only. After writing review files you MUST run:
>
> ```
> aa validate --change <change-id> --artifact review/plan-review.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

Write valid JSON to:

```text
qa/changes/<change-id>/review/plan-review.json
```

**Minimal top-level structure (illustrative — see `src/schema/review.ts` for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "e2e-plan",
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
  "required_capabilities": [
    "auth.api_admin_token",
    "capabilities.adapters.e2e.menu.navigate_menu_admin"
  ],
  "next_action": "continue",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

Field constraints (documented here — do **not** embed as JSON comments):

- `auto_fix_allowed`: `true` only when `decision == "needs_fix"` and every referenced finding has `severity in ["low", "medium"]`
- `human_review_required`: `true` when `decision == "needs_human_review"` or `decision == "reject"`

`reviewed_files` **MUST** be non-empty and include every file actually read:

- `qa/changes/<change-id>/plans/e2e-plan.md`
- `qa/changes/<change-id>/plans/e2e-test-data-plan.md`
- `qa/changes/<change-id>/plans/e2e-codegen-plan.md`
- `qa/changes/<change-id>/plans/m4-review-summary.md`
- `qa/changes/<change-id>/plans/data-knowledge.proposal.e2e.yaml` (if present)
- `.aa/data-knowledge.yaml` (if read for capability validation)

Set `blocking: true` when route, selector, auth state, setup capability, cleanup, expected UI copy, or scope must be confirmed before codegen. Set `blocking: false` only for non-blocking clarifications that do not affect gate safety.

---

## Summary Markdown Format

Write:

```text
qa/changes/<change-id>/review/plan-review-summary.md
```

Use this structure:

```markdown
# Plan Review Summary

## Decision

- Decision:
- Risk:
- Codegen Readiness:
- Auto Fix Allowed:
- Human Review Required:

## Summary

...

## Coverage

...

## Codegen Readiness

...

## Blockers

...

## Needs Review

...

## Findings

...

## Auto Fix Plan

...

## Next Action

...
```

The **Needs Review** section must mirror `needs_review` from `plan-review.json` — especially when `decision == needs_human_review`. Do not leave it empty when blocking `needs_review` items exist.

---

## Final Response

After writing the files, respond with:

```
Plan review completed.

Decision: <decision>
Risk: <risk_level>
Codegen readiness: <ready|ready_with_warnings|not_ready>
Human review required: <true|false>
Auto fix allowed: <true|false>

Files:
- qa/changes/<change-id>/review/plan-review.json
- qa/changes/<change-id>/review/plan-review-summary.md
```

Do not claim the plan is ready unless `codegen_readiness` is `ready` or `ready_with_warnings`.

Do not modify plan files in this skill.
