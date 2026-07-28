---
name: aa-api-plan-reviewer
description: "Review AA API planning artifacts before API code generation. Use after aa-api-plan has generated API plan files. Read-only: writes api-plan-review.json and api-plan-review-summary.md, never modifies plan files."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-api-plan-reviewer.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared domain builders belong only in `tests/testdata/domain/`; API execution glue belongs only in `tests/api/adapters/`.
- Require plan mappings to `capabilities.domain_factories` and `capabilities.adapters.api` in `.aa/data-knowledge.yaml`.
- Reject shared factories containing pytest/HTTP/event-loop bridging, API adapters placed in another layer, or a plan that rewrites an existing shared factory instead of reusing it.
- A missing shared capability may be `create-if-missing` for the first active codegen layer; conflicting creators are `severity: high` and make codegen `not_ready`.
- Treat `.aa/data-knowledge.yaml` as the only formal L1 path. Any alternate hidden-directory path for `data-knowledge.yaml` is a blocking, mechanically auto-fixable finding: replace it with `.aa/data-knowledge.yaml`, remove any alternate-path proposal, and recompute readiness from the actual `.aa/` file.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.api_plan.status == done`.
3. Read input files from disk: `plans/api-plan.md`, `plans/api-test-data-plan.md`, `plans/api-codegen-plan.md`, `plans/m3-review-summary.md`.
4. If any required file is missing, stop and report.
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/review/api-plan-review.json`
   - `qa/changes/<change-id>/review/api-plan-review-summary.md`
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.api_plan_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.api_plan_review.gate_file` = `review/api-plan-review.json`

---

# AA API Plan Reviewer

## Purpose

Review AA API planning artifacts before code generation.

Use this skill after `aa-api-plan` has generated API test plan files.

This skill is **read-mostly**. It reviews API plan files and writes structured review outputs. It must not modify plan files.

---

## When to Use

Use this skill when:

- API plan files have been generated.
- You need to verify whether the API plan is ready for code generation.
- You need a machine-readable review gate before `aa-api-codegen`.

---

## Inputs

Required:

```text
qa/changes/<change-id>/plans/api-plan.md
qa/changes/<change-id>/plans/api-test-data-plan.md
qa/changes/<change-id>/plans/api-codegen-plan.md
qa/changes/<change-id>/plans/m3-review-summary.md
qa/changes/<change-id>/cases/**/*.yaml
```

Optional (only present when `.aa/data-knowledge.yaml` was missing during planning):

```text
qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml
```

Recommended:

```text
qa/changes/<change-id>/proposal.md
.aa/data-knowledge.yaml
tests/api/**
```

**Coverage-gap acknowledgment rule:**

- If an endpoint coverage gap is detected, record it as a blocking `needs_review` item in `api-plan-review.json` with `category: coverage_gap`.
- Do **not** write `decision == pass` until a human acknowledges the workaround scope in review output or via the Issue review workflow.
- Do **not** defer acknowledgment to `aa-api-codegen` — codegen does not create canonical Issue/Problem lifecycle state.

## Outputs

Write:

```text
qa/changes/<change-id>/review/api-plan-review.json
qa/changes/<change-id>/review/api-plan-review-summary.md
```

Create the review directory if it does not exist.

---

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance to `aa-api-codegen` without the JSON file this skill writes.

- You **must** write `qa/changes/<change-id>/review/api-plan-review.json` as valid JSON with all required fields, including `codegen_readiness` and `assertion_traceability`.
- You **must** write `qa/changes/<change-id>/review/api-plan-review-summary.md`.
- A natural language conclusion in chat is **not** a substitute for the JSON file. Never end with only a textual verdict.
- User approval in chat does not release the gate; only a valid `api-plan-review.json` does. See **User Approval Handling**.
- If you cannot write the JSON file for any reason, treat the review as **failed** — the workflow must treat a missing or invalid `api-plan-review.json` as a STOP condition.

---

## User Approval Handling

User approval is not a substitute for review.

If the user says "approved", "looks good", or "continue":

- Treat it only as review context.
- Still validate every review criterion independently.
- Cross-check `m3-review-summary.md` **Plan Readiness**, **Codegen Readiness**, **Blockers**, and **Needs Review** — do not contradict plan self-assessment without explicit findings.
- Only write `decision == "pass"` when the plan satisfies all review criteria.
- If blockers or unresolved product decisions remain, write `needs_fix`, `needs_human_review`, or `reject`.

---

## Review Scope

Review the API plan for:

- Case-to-plan coverage
- API endpoint and method correctness
- Request/response assertion completeness
- Test data setup and cleanup feasibility
- Auth and fixture handling
- Codegen readiness
- Missing knowledge and unknowns
- Assertion traceability from approved cases to the executable API plan

---

## Review Criteria

### 1. Case-to-Plan Coverage

Every API case with `automation.required = true` should be mapped to one or more planned test scenarios.

Flag:

- Case missing from plan
- Plan scenario not linked to any case
- Critical assertion missing from plan

**Test function naming (mechanical, blocking):** for every row in `api-codegen-plan.md` **Test Function Mapping**, the Test Function MUST match:

```
^test_<case_id lowercase>__
```

i.e. the lowercase form of that row's Case ID as prefix, then a **double underscore** (e.g. `TC_USER_API_001` → `test_tc_user_api_001__...`). Any mismatch (missing prefix, single underscore, wrong case_id) is a **finding with `blocking: true`** and `auto_fix_allowed: true` (fix = rename in the mapping table) — decision cannot be `pass` and `codegen_readiness` cannot be `ready`/`ready_with_warnings` until fixed. This is what lets the `aa run` result parser backfill case_id; without it every executed test becomes an Unmapped Test.

### 2. API Endpoint and Method

Check whether each scenario has:

- HTTP method and path (or explicit `TBD` with Needs Review / Blockers per api-plan rules)
- Auth header strategy
- Request body structure
- Expected status code
- Expected response fields

Do not accept plans that depend on vague steps such as:

- "call the appropriate endpoint"
- "verify it works"

If Method or Path is `TBD` in **API Targets**, verify it appears in **Needs Review** or **Blockers** — not silently omitted.

### 3. Test Data Plan

Check:

- Required entities are listed
- Entity states are clear
- Fixture or factory strategy is defined
- Setup order is correct
- Cleanup strategy exists
- Data does not depend on production-like hardcoded IDs

Flag unknown factories, unknown states, and missing cleanup.

**Domain Boundary Checks:**

- **Factory-first default:** for every entity listed in **Required Data**, plan must specify Ring + method in **Factory / Boundary Strategy** → else `severity: medium` (`auto_fix_allowed: true`).
- If `tests/testdata/domain/<entity>.py` defines `make_<entity>()`, the plan must reuse it unchanged through `tests/api/adapters/` → else `severity: high`.
- If Target Files contains an API adapter but lacks the corresponding shared capability mapping and ownership (`create-if-missing` or `reuse`) → `severity: high`, `codegen_readiness = not_ready`.
- `tests/api/adapters/` must contain only pytest lifecycle and transport orchestration. Direct business defaults/invariants in an adapter → `severity: high`.
- **HTTP setup ban:** fixture or setup step uses `POST /.../create` (or helper wrapping create) for cases **not** primarily testing create → `severity: high` (`auto_fix_allowed: false`). Exception: create-focused case IDs explicitly mapped in **Data Setup Mapping** with reason.
- Anti-pattern: `tmp_<entity>` fixture = POST create + GET list resolve id → flag when `make_<entity>` exists or should exist per degradation ladder step 1.
- Cleanup must maintain the same invariants as setup; raw-delete cleanup for M2M, closure, or soft-delete entities → `severity: high` (`auto_fix_allowed: false` unless plan only needs wording clarification).
- Entities with M2M tables (Role↔menus/apis / User↔roles): plan must use shared `make_*` capabilities through the API adapter; raw single-table insertion is `severity: high`.
- Entities with closure derived tables (Dept↔DeptClosure): plan must use `make_dept()` → else `severity: high`.
- Entities with password hashing (User): plan must use `make_user()` → else `severity: high`.
- Menu / single-table domain entities: plan must use `make_menu()` when factory exists; HTTP fixture seeding forbidden except create cases → else `severity: high` (or `medium` if factory missing but step 1 documented in Blockers).
- Test config (BASE_URL / admin credentials / prefixes): plan must reference `tests.config.settings`; must not hardcode → else `severity: medium`.
- If legacy `tests/factories/` is discovered, treat it as migration evidence only. The reviewed plan must map confirmed capabilities to `tests/testdata/domain/` and API execution to `tests/api/adapters/`.
- If `api-test-data-plan.md` lacks **Factory / Boundary Strategy** section → `severity: medium` (`auto_fix_allowed: true`).

### 4. Codegen Readiness

Return one of: `ready` / `ready_with_warnings` / `not_ready`

**Hard rule — missing `.aa/data-knowledge.yaml`:**

If `.aa/data-knowledge.yaml` does not exist (including when `data-knowledge.proposal.api.yaml` was generated):

- `codegen_readiness` **MUST** be `not_ready`.
- Reason: `aa-api-codegen` requires formal `.aa/data-knowledge.yaml` regardless of whether selected API cases have minimal data needs.
- `decision` **cannot** be `pass` until the formal knowledge file exists and capabilities resolve (orchestrator also checks file existence before codegen).

**Use `ready` when:**

- `.aa/data-knowledge.yaml` exists.
- Endpoint, method, auth, request body, and assertions are all clear.
- Data setup is feasible.
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
- Endpoint, method, or auth is unknown without documented TBD handling.
- Required fixture/factory is missing.
- Plan requires product decisions.
- Codegen would likely hallucinate implementation details.
- Any condition in **Ready With Warnings Boundary** is not met.
- `m3-review-summary.md` **Codegen Readiness** is `not_ready` and plan blockers remain unresolved.

### Ready With Warnings Boundary

`ready_with_warnings` is allowed only when:

- `.aa/data-knowledge.yaml` exists.
- HTTP method and path are confirmed (not `TBD`), or workaround is explicitly documented with coverage gap **and** acknowledged in review JSON (`needs_review` resolved or `ready_with_warnings` with documented gap finding).
- Auth strategy is confirmed.
- Required fixtures / factories are available or safely optional.
- Request body and expected assertions do not require guessing schema.
- Cleanup strategy is confirmed or explicitly unnecessary.
- No blocker exists.
- Warnings are explicitly documented.
- Codegen can proceed without guessing product behavior.

If codegen would need to guess endpoint, method, auth mechanism, fixture, request schema, expected response schema, cleanup behavior, or product semantics, use `not_ready`.

### 5. Auth Handling

Acceptable:

- Auth fixture is known and mapped in `.aa/data-knowledge.yaml`.
- Invalid token cases use a clearly defined invalid string.
- No-auth cases explicitly pass no Authorization header.

Not acceptable:

- Hardcoded real tokens
- Undefined auth mechanism
- Auth guessed from endpoint path
- Marking unknown auth as TODO-only instead of blocker / needs review

### 6. Existing Test Style

If `tests/api/**` exists, check whether the plan respects existing style:

- File naming
- Fixture naming
- pytest conftest conventions
- Incremental append instead of overwrite

### 7. Missing Knowledge and Unknowns

Check that the plan does not silently depend on unknown capabilities:

- Any unknown endpoint, method, auth mechanism, or factory **must** be recorded as a blocker or needs review — not silently assumed, and not downgraded to TODO-only warnings.
- If `data-knowledge.proposal.api.yaml` was generated by `aa-api-plan` and `.aa/data-knowledge.yaml` is still missing, set `codegen_readiness = not_ready` and do not write `decision = pass`.
- If `data-knowledge.proposal.api.yaml` was generated by `aa-api-plan`, verify all proposed capabilities are either confirmed from `.aa/data-knowledge.yaml` or flagged as blockers.
- Unresolved capabilities must appear in `m3-review-summary.md` under Blockers or Needs Review.
- If the plan references fixtures or factories not present in `.aa/data-knowledge.yaml`, flag as `high` severity finding.
- Unknowns that are silently omitted from the plan are a `high` finding.

### 8. Assertion Traceability

For every API case with `automation.required = true`, compare each `assertions[]` item in the case delta to the API plan's expected status, response, and postcondition assertions. Record every assertion in `assertion_traceability` using one of:

| verdict | Meaning | Required handling |
|---|---|---|
| `mapped` | The plan assertion is semantically equivalent to the case assertion. | Pass silently; no finding required. |
| `narrowed` | The plan narrows a broader case assertion (for example, case says `4xx`, plan says observed `404`). | Allowed only when the plan marks the expectation as observed and cites source-code, fact-baseline, or advisory evidence. Add a non-blocking `traceability` finding that explains the narrowing and its evidence. If evidence is missing, escalate to blocker. |
| `contradicted` | The plan contradicts the case assertion, changes `assert_ideal` / `assert_known_bug` semantics, or turns a "non-500" expectation into an expected 500. | Blocking issue. `decision` cannot be `pass`. If the plan can be mechanically corrected from the case file, record a `blocking: true` auto-fixable finding and an `auto_fix_plan`; otherwise record a blocker and use `reject` or `needs_human_review`. |
| `missing` | The case assertion has no corresponding plan assertion. | P0/P1 cases: blocking issue and `codegen_readiness` cannot be `ready`. If mechanically restorable from the case file, record a `blocking: true` auto-fixable finding; otherwise record a blocker. P2+ cases: high finding only if the omission has an explicit non-blocking rationale. |

Do not let the plan silently replace approved case semantics with observed product behavior. Observed behavior may refine a broad assertion, but it cannot override the approved intent.

---

## Decision Rules

Return one of: `pass` / `needs_fix` / `needs_human_review` / `reject`

Derive the decision mechanically, in this order:

1. Use `reject` when required plan files are missing, the plan targets the wrong feature, or the plan contradicts approved case files in a way that cannot be safely and mechanically corrected.
2. If any blocker or `blocking: true` finding exists, do not use `pass`:
   - Use `needs_fix` only when `blockers[]` is empty and every blocking issue is represented by an auto-fixable finding with `severity in ["low", "medium"]`, `human_review_required == false`, and `auto_fix_plan` is non-empty.
   - Use `needs_human_review` when resolving the blocking issue requires a product decision, scope confirmation, endpoint/auth/fixture clarification, or acceptance of an endpoint coverage gap.
3. Use `pass` when there are only non-blocking risk warnings and all Gate Consistency Rules for `pass` are satisfied. Summarize the warnings; do not turn them into blocker-like findings.
4. Use `pass` when there are no findings, no blockers, no blocking `needs_review` items, `.aa/data-knowledge.yaml` exists, and `codegen_readiness` is `ready` or `ready_with_warnings`.

**Finding discipline:** write findings only for blockers or risk warnings with a concrete downstream failure mode. Do not write endorsement findings such as "no change required", "proceed as planned", or "per plan; not blocking" unless they describe a specific risk, affected case, and consequence. Put general approval language in `summary`, not `findings[]`.

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
| `needs_fix` | `run_api_plan_fixer` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

### Gate Consistency Rules

These combinations are **invalid** and must never appear in `api-plan-review.json`:

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
- Every referenced finding **MUST** have `severity in ["low", "medium"]`
- Every referenced finding **MUST** have `human_review_required == false`
- `aa-api-plan-fixer` **cannot** create canonical Issue/Problem records — missing coverage-gap acknowledgment **cannot** be resolved via `needs_fix`

---

## Risk Level Rules

Use: `low` / `medium` / `high` / `critical`

- **low**: Minor wording or formatting issues.
- **medium**: Some missing details, but codegen may continue with warnings.
- **high**: Missing endpoint/auth/data/fixture strategy. Missing core case coverage. Codegen likely to hallucinate.
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
- Add missing **Factory / Boundary Strategy** section skeleton (entities already listed in plan; include per-entity Ring + method table)
- Replace hardcoded `BASE_URL` / credential string in plan with reference to `tests.config.settings`
- Add note in **Fixture Mapping** that HTTP create must not be used for non-create cases (wording only when reviewer flagged HTTP setup)

Do not downgrade blockers into TODO-only warnings.

Unknown endpoint, unknown method, unknown auth mechanism, missing required fixture/factory, unknown expected response schema, unsafe cleanup, or **inventing `make_*` factory implementations** must remain blockers or `needs_human_review`.

Not auto-fixable:

- Unknown endpoint or method
- Unknown auth mechanism
- Unknown fixture or factory
- Unknown expected response body
- Product behavior ambiguity
- Missing requirement scope
- Endpoint coverage-gap acknowledgment (outside fixer allowlist; requires human action or Issue review workflow)

---

## Required Capabilities (gate input — mandatory)

Every `api-plan-review.json` **must** include a non-empty `required_capabilities: string[]` field. This list drives the pre-codegen capability gate (`api-plan-review-gate`). Omitting the field or leaving it empty is a **reviewer contract error** → gate **`stop`** (not plan-fixer, not pass).

**Hard schema check (runs before the gate):** `aa validate --change <change-id> --artifact review/api-plan-review.json` now **rejects** the file when `required_capabilities` is missing, `null`, an empty list, or contains a blank/empty item. This is not optional formatting advice — the mandatory validate step below fails until the list is populated with concrete leaf keys. Never finish the review with an unpopulated list; if you genuinely cannot resolve any leaf, set `codegen_readiness = not_ready`, file the `data-knowledge.proposal.api.yaml`, and still list the leaf keys you require so the knowledge-remediation checkpoint has targets.

**How to derive the list**

1. Read `.aa/data-knowledge.yaml` (formal L1) and every in-scope API case + plan data requirements.
2. Collect every **leaf-only** dotted key the selected cases and plan need for codegen (auth, domain factories, API adapters, cleanup).
3. Use C4 leaf paths only — examples:
   - `auth.api_admin_token`
   - `capabilities.domain_factories.api.make_user`
   - `capabilities.adapters.api.user.make_user`
   - `capabilities.cleanup.delete_user`
4. **Never** use container paths (e.g. `capabilities.adapters.api.user`) — they count as missing at gate time.

**When L1 is missing or lacks required leaves**

- Create or update `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml`.
- Use `mode: bootstrap` when `.aa/data-knowledge.yaml` is absent; `mode: delta` when L1 exists but leaves are missing.
- Include only the missing leaf candidates (plus `needs_review` / `promotion_checklist` as needed).
- Do **not** write `.aa/data-knowledge.yaml` directly — remediation goes through `aa knowledge promote`.
- Set `codegen_readiness = not_ready` until L1 leaves exist; gate routes missing leaves to the **knowledge-remediation** checkpoint (human promote, then re-run review).

**Auth leaf note:** `auth.*` leaves are valid without `symbol` when `AuthLeaf` validates (`method` required). Do not require `symbol` on auth entries.

---

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `src/schema/review.ts` (validated by `aa validate`). The example below is
> illustrative only. After writing review files you MUST run:
>
> ```
> aa validate --change <change-id> --artifact review/api-plan-review.json
> ```
>
> and resolve every reported error. Do not rely on this document for the full field list.

Write valid JSON to:

```text
qa/changes/<change-id>/review/api-plan-review.json
```

**Minimal top-level structure (illustrative — see `src/schema/review.ts` for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "api-plan",
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
  "assertion_traceability": [],
  "required_capabilities": [
    "auth.api_admin_token",
    "capabilities.domain_factories.api.make_user"
  ],
  "next_action": "continue",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

`reviewed_files` **MUST** be non-empty and include every file actually read:

- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/cases/**/*.yaml`
- `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml` (if present)
- `review/api-plan-review.json` coverage-gap findings (if read for workaround acknowledgment)

Set `blocking: true` on findings and `needs_review` items when product behavior, scope, endpoint coverage gap, or auth strategy must be confirmed before codegen. Set `blocking: false` only for non-blocking clarifications that do not affect gate safety.

**Assertion traceability rules:**

- Include at least one `assertion_traceability` item for every in-scope API case with `automation.required = true`.
- `verdict = narrowed` requires evidence and a non-blocking `traceability` finding that explains the narrowing.
- `verdict in ["contradicted", "missing"]` requires a corresponding blocker or `blocking: true` finding; P0/P1 `missing` assertions are blocking issues.
- If any item has `verdict = contradicted`, `decision` must not be `pass`.
- If any P0/P1 item has `verdict = missing`, `codegen_readiness` must not be `ready`.

---

## Summary Markdown Format

Write:

```text
qa/changes/<change-id>/review/api-plan-review-summary.md
```

Use this structure:

```markdown
# API Plan Review Summary

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

## Assertion Traceability

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

The **Needs Review** section must mirror `needs_review` from `api-plan-review.json` — especially when `decision == needs_human_review`. Do not leave it empty when blocking `needs_review` items exist.

---

## Endpoint Coverage Integrity

If a case targets a specific endpoint, the implementation plan should exercise that endpoint directly.

If the plan uses a different endpoint because the target endpoint is broken or unavailable, this is a **coverage gap** — not equivalent direct endpoint coverage. Mark the finding with `category: coverage_gap`.

### Hard Rules

**If an endpoint coverage gap exists and workaround scope is not yet acknowledged:**

- `decision` **MUST** be `needs_human_review` (scope / workaround acknowledgment required; fixer cannot create Issue lifecycle state)
- `codegen_readiness` **MUST** be `not_ready`
- `human_review_required` **MUST** be `true`
- `auto_fix_allowed` **MUST** be `false`
- `next_action` **MUST** be `human_review` — **MUST NOT** be `continue`
- Add a blocking `needs_review` item documenting the required human acknowledgment
- Do **not** write `decision == needs_fix` for missing coverage-gap acknowledgment — `aa-api-plan-fixer` cannot resolve it

**If coverage gap requires product / scope acknowledgment** (workaround not yet accepted):

- `decision` **MUST** be `needs_human_review`
- `codegen_readiness` **MUST** be `not_ready`
- High-severity coverage findings **MUST** have `human_review_required: true` and **cannot** enter `needs_fix`

**If the coverage gap is explicitly acknowledged in review output** (workaround accepted, gap recorded in `needs_review` / findings):

- `decision` **MAY** be `pass`
- `codegen_readiness` **MUST** be `ready_with_warnings` (never `ready`)
- `risk_level` **MUST** be at least `medium`
- `blockers` **MUST** be empty
- Downstream execution / archive **MUST NOT** be treated as clean PASS
- Reviewer **MUST** verify acknowledgment before writing `decision == pass` — do **not** defer to codegen

### Example — gap not yet documented

Target endpoint from case:

```text
GET /api/v1/menu/get
```

Planned implementation uses alternate endpoint:

```text
GET /api/v1/menu/list
```

The review finding must include:

```json
{
  "id": "API-PLAN-FINDING-COV-001",
  "severity": "high",
  "category": "coverage_gap",
  "file": "qa/changes/<change-id>/plans/api-codegen-plan.md",
  "message": "Target endpoint GET /api/v1/menu/get is replaced by GET /api/v1/menu/list due to a product bug. Direct endpoint coverage remains open.",
  "suggestion": "Human must acknowledge the coverage gap and accepted workaround scope in review output before pass.",
  "auto_fix_allowed": false,
  "human_review_required": true
}
```

Required review output when coverage-gap acknowledgment is missing:

```json
{
  "decision": "needs_human_review",
  "codegen_readiness": "not_ready",
  "human_review_required": true,
  "auto_fix_allowed": false,
  "next_action": "human_review",
  "risk_level": "medium"
}
```

### Example — gap acknowledged, pass allowed

After human acknowledges the coverage gap in review output (resolved `needs_review` item or explicit finding):

- `decision: pass`
- `codegen_readiness: ready_with_warnings`
- `risk_level: medium` (minimum)
- `blockers: []`
- Include the acknowledgment finding in `reviewed_files`

---

## Final Response

After writing the files, respond with:

```
API plan review completed.

Decision: <decision>
Risk: <risk_level>
Codegen readiness: <ready|ready_with_warnings|not_ready>
Human review required: <true|false>
Auto fix allowed: <true|false>

Files:
- qa/changes/<change-id>/review/api-plan-review.json
- qa/changes/<change-id>/review/api-plan-review-summary.md
```

Do not claim the plan is ready unless `codegen_readiness` is `ready` or `ready_with_warnings`.

Do not modify plan files in this skill.
