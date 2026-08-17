---
name: aa-case-reviewer
description: "Review AA QA case design artifacts and produce a structured review result. Use after aa-case-design has generated or updated QA case artifacts. Read-only: writes case-review.json and case-review-summary.md, never modifies case files."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.json`.
2. Verify `phases.case_design.status == done`.
3. Read input files from disk: `.qa.yaml`, `proposal.md`, `cases/<module>/case.yaml`.
4. Independently read the relevant **product source code** for every product fact used in the verdict. At minimum inspect the implementation entry point plus the controller/service/schema/model or frontend component needed to verify the proposed scenarios. Do not treat proposal, case, Explore advisory, requirements, docs, or tests as product-fact evidence.
5. If any required artifact is missing, or relevant product source cannot be read, **STOP** before a normal pass/needs-fix verdict. Do not invent product behavior. If the orchestrator requires a JSON artifact for diagnostics, write `needs_human_review` with the missing evidence identified.
6. Use files read in this invocation as the sole source of truth; do not rely on the case author's conclusions or prior conversation.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/review/case-review.json`
   - `qa/changes/<change-id>/review/case-review-summary.md`
2. Report the `workflow-state.json` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.json` — report the values in your final message and the orchestrator applies them):
   - `phases.case_review.status` = `pass | needs_fix | needs_human_review | reject`
   - `phases.case_review.gate_file` = `review/case-review.json`

---

# AA Case Reviewer

## Purpose

Review AA QA case design artifacts and produce a structured review result.

Use this skill after `aa-case-design` has generated or updated QA case artifacts.

This skill is **read-mostly**. It should review the case proposal and case YAML files, then write review outputs under the change review directory.

It must not modify `proposal.md` or case YAML files.

---

## When to Use

Use this skill when:

- A new QA case delta has been generated.
- Existing case files were modified.
- You need to verify whether case artifacts are complete, testable, and ready for API/E2E planning.
- You need a machine-readable review gate before moving to planning.

---

## Inputs

The user or orchestrator should provide:

- `change_id`

Expected input files:

```text
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
qa/changes/<change-id>/.qa.yaml
```

Required independent evidence:

```text
<product source files relevant to the requirement>
```

Source verification is mandatory even when Explore or Case Design already read the same files. The reviewer must repeat the read independently and must not copy `source_code_evidence` from `explore/advisory.json` as a substitute.

Optional reference files:

```text
qa/cases/**/*.yaml
.aa/data-knowledge.yaml
docs/**/*.md
```

## Outputs

Write the following files:

```text
qa/changes/<change-id>/review/case-review.json
qa/changes/<change-id>/review/case-review-summary.md
```

Create the review directory if it does not exist.

---

## Mandatory Output Contract

This skill is a **gate producer**. The workflow cannot advance past case review without the JSON file this skill writes.

- You **must** write `qa/changes/<change-id>/review/case-review.json` as valid JSON with all required fields.
- You **must** write `qa/changes/<change-id>/review/case-review-summary.md`.
- A natural language conclusion in chat is **not** a substitute for the JSON file. Never end with only a textual verdict.
- User approval in chat does not release the gate; only a valid `case-review.json` does. If the user says "approved", "looks good", or "continue", treat it only as review context — still validate every review criterion independently. Only write `decision == "pass"` when the artifacts satisfy all review criteria.
- If you cannot write the JSON file for any reason, treat the review as **failed** and report it — the workflow must treat a missing or invalid `case-review.json` as a STOP condition.
- `case-review.json` must contain a `source_verification` object with exactly these required fields: `independent: true`, a non-empty `reviewed_source_files` list of product source paths, and a non-empty `verified_claims` list. Every `verified_claims[]` item must contain `claim` and a non-empty `evidence_files` list drawn from `reviewed_source_files`. Paths under `qa/`, `.aa/`, `.opencode/`, `docs/`, `requirements/`, and `tests/` do not count as product source.

---

## Review Scope

Review the generated case artifacts for:

- Requirement coverage
- Case clarity
- Case YAML schema consistency
- Testability
- API/E2E automation readiness
- Preconditions and test data
- Assertions
- Traceability
- Minimum Required Coverage (MRC) mapping from advisory to cases
- Risk and ambiguity
- Duplicate or conflicting cases
- Test layer assignments (API vs E2E per the decision tree in `aa-case-design`)

### Explore soft check (Phase 0.5 — warning only, not blocker)

When `explore/advisory.json` exists and `phases.explore.status == done`:

```
FOR EACH watchlist item WHERE confidence == high:
  proposal.md ## Explore Input MUST list WL-* as disposition in [adopted, override]
  IF override → reason required in table
ELSE emit warning: RISK-ADVISORY-WATCHLIST-UNADDRESSED
```

Finding example:

```json
{
  "id": "RISK-ADVISORY-WATCHLIST-UNADDRESSED",
  "severity": "low",
  "human_review_required": false,
  "message": "High-confidence watchlist item WL-001 not addressed in proposal.md Explore Input"
}
```

Also **warn** (non-blocker) if `case.yaml` contains advisory trace metadata (`evidence_ids`, `explore`, `risk_advisory` (legacy), `WL-*` / `PH-*` / `HS-*` (legacy) as dedicated trace fields).

### Test Types Considered check (warning only, not blocker)

`proposal.md` MUST contain a `## Test Types Considered` section listing **all four layers** (API / E2E / Fuzz / Performance), each marked `selected` or `declined` with a reason. This is the evidence that Fuzz/Performance were offered to the user during clarification (Category 3 hard rule in `aa-case-design`).

```text
IF proposal.md has no "## Test Types Considered" section
   OR any of the four layers is missing from it
   OR a declined layer has no reason
→ emit warning: TEST-TYPES-NOT-CONSIDERED
```

Finding example:

```json
{
  "id": "TEST-TYPES-NOT-CONSIDERED",
  "severity": "low",
  "human_review_required": false,
  "message": "proposal.md Test Types Considered is missing or does not cover all four layers (API/E2E/Fuzz/Performance) with selected/declined decisions"
}
```

### Minimum Required Coverage gate (hard)

When `risk-advisory/advisory.json` or `explore/advisory.json` contains `minimum_required_coverage`:

```text
FOR EACH required MRC item:
  It MUST appear in at least one case trace.minimum_required_coverage
  It MUST appear in trace/minimum-coverage-matrix.json covered_by_cases
  The covering case type/layer MUST match the MRC layer (api/e2e/both)
  Closed-category keys (data_integrity / negative / auth / e2e_if_enabled journey)
  MUST cite the DataKnowledge / journey closed set — not freely invented names
```

Violations:

- required MRC item has no covering case → `needs_fix`
- matrix and case.yaml trace disagree → `needs_fix`
- e2e_if_enabled item is skipped without explicit skipped_by_scope + reason → `needs_human_review`
- closed-category MRC key absent from `.aa/data-knowledge.yaml` / declared journey set **and** no matching `plans/data-knowledge.proposal.*.yaml` `discovered_candidates` entry → `needs_fix` (case-design must not invent MRC keys; unknown keys require a knowledge proposal)

`case-review.json` should include:

```json
"minimum_coverage": {
  "total_required": 24,
  "covered": 24,
  "skipped_by_scope": 0,
  "missing": []
}
```

These four fields are an exact projection of the frozen matrix, not an estimate:

- count only rows whose `required` value is true;
- `covered` is the number of required rows with `status: covered`;
- `skipped_by_scope` is the number of required rows with that status;
- `missing` lists every required `skipped_by_scope` row's `key`, in matrix order.

The runtime recomputes this projection from `trace/minimum-coverage-matrix.json`
and rejects the review when any count or key differs.

---

## Review Criteria

### 1. Requirement Coverage

Check whether the cases cover the intended requirement.

Look for:

- Missing core business scenarios
- Missing positive path
- Missing negative path when relevant
- Missing permission / role scenarios when relevant
- Missing boundary conditions
- Missing regression impact
- Out-of-scope scenarios mixed into the case

If a requirement is ambiguous, do not invent product behavior. Mark it as human review required.

### 2. Case Clarity

Each case should have clear:

- `case_id`
- `title`
- `summary`
- `module`
- `requirement_id`
- `feature_name`
- `priority`
- `severity`
- `type`
- `status`
- `preconditions`
- `test_data`
- `steps`
- `assertions`
- `automation` block (see YAML structure below)

The case should be understandable by a QA engineer without reading hidden context.

### 3. YAML Structure

Check that case files are valid YAML and follow the delta format produced by `aa-case-design`.

**Change delta files** (`qa/changes/<change-id>/cases/**/case.yaml`) must use the delta top-level structure:

```yaml
schema_version: "1.0"

added:
  - case_id: "TC_USER_AUTH_001"
    title: "..."
    # ... all required case fields
modified:
  - case_id: "TC_USER_AUTH_002"
    title: "..."
    # ... all required case fields
removed:
  - case_id: "TC_USER_AUTH_003"
    reason: "..."
```

Review every case under `added` and `modified`. Review every entry under `removed`.

Do not expect a top-level `case_id` in the change delta file — cases are always nested inside `added`, `modified`, or `removed`.

**Required fields per case under `added` / `modified`:**

```yaml
case_id: "TC_[A-Z0-9]+(_[A-Z0-9]+)*_[0-9]{3}"  # underscore-only; e.g. TC_USER_001, TC_USER_AUTH_002 — hyphens are NOT allowed
title: "..."
status: draft|active|deprecated
priority: P0|P1|P2|P3
severity: blocker|critical|major|minor
type: API|E2E|Fuzz|Performance
module: "..."
requirement_id: "..."
feature_name: "..."
test_condition_id: "..."               # required (TBD acceptable with rationale)
design_technique: use_case|equivalence_partitioning|boundary_value_analysis|...
objective: "..."
tags: []
summary: "..."
preconditions: []
test_data: []
steps: []
assertions: []
postconditions: []
edge_cases: []
related_cases: []
risk:
  likelihood: 1–5
  impact: 1–5
  level: low|medium|high|critical
  rationale: "..."
regression:
  candidate: true|false
  tier: smoke|sanity|regression|full|none
  selection_reason: []
  maintenance_rule: "..."
  rationale: "..."
automation:
  required: true|false
  # no `target` field — top-level `type` (API|E2E|Fuzz|Performance) is the single source of truth
  framework: pytest|pytest-playwright|schemathesis|locust|null
  status: not_automated|planned|automated|flaky|deprecated
  confirmed_by: user|null
  confirmed_at: <ISO-8601|null>
  fuzz: { endpoints: [...], expectations: [...] }          # only when type: Fuzz
  performance: { scenario: { capability, endpoint, thresholds, load } }   # only when type: Performance
trace: {}
```

**`automation_targets` field:** For change delta files produced by `aa-case-design`, `automation_targets` is **forbidden** and must be flagged as a blocker. Legacy stable case files in `qa/cases/**` may use it for reference, but the change delta under `qa/changes/<change-id>/cases/**/case.yaml` must use the `automation` block.

### 4. Testability

Flag cases that cannot be implemented because:

- Steps are too vague
- Assertions are not observable
- Preconditions are missing
- Required data state is undefined
- Auth state is unknown
- Page route is unknown
- API endpoint is unknown
- Selector or UI behavior is unknown
- Expected result is subjective or not verifiable

### 5. Automation Readiness

For E2E cases, check:

- Natural language steps are executable
- Page entry point is clear
- User role is clear
- Test data is available or constructible
- Assertions can be checked with Playwright or pytest
- No hidden manual-only step is required

For API cases, check:

- The case describes the scenario clearly (not necessarily the exact endpoint — endpoint detail belongs in the API plan, not the case)
- Preconditions and auth requirements are stated at a conceptual level
- Expected behaviour / response outcome is clear
- Test data requirements are described
- Do **not** flag a case as not automation-ready just because it omits method/path — that detail is added during API planning, not case design

### 6. Data and Preconditions

Check whether `test_data` and `preconditions` are enough.

Flag:

- Unknown fixture
- Unknown factory
- Missing cleanup strategy
- Conflicting data state
- Environment-specific assumptions
- Hardcoded production-like IDs
- Data dependencies that require manual preparation

### 7. Assertions

Assertions should be specific and observable.

Good assertions:

- Response status equals expected value
- Response body contains expected field/value
- UI displays expected text/state
- Database or state transition is verifiable if project supports it

Bad assertions:

- "System works correctly"
- "Page should be normal"
- "User experience is good"
- "No bug appears"

### 8. Traceability

Check whether the case links back to:

- Requirement ID
- Feature name
- Module
- Source document or user request if available
- Related existing case if modified

### 9. Forbidden Content

Case YAML must NOT contain any of the following. Flag as a blocker if found:

- HTTP method or endpoint path (e.g. `POST /api/v1/...`, `GET /api/...`)
- `Authorization` header values (e.g. `Bearer ${token}`)
- Concrete request URLs with environment host (e.g. `https://prod.example.com/...`)
- Hard-coded auth tokens, real credentials, or secrets
- pytest code (`def test_`, `assert`, `@pytest.fixture`, `httpx`, `requests`)
- Playwright code (`page.locator(...)`, `await page.click(...)`)
- CSS selectors (`.btn`, `#form`, `.class-name`)
- XPath (`//button[@class='receive']`)
- `data-testid` attributes
- Raw SQL data setup
- Execution history or test run results embedded in the case body

These details belong in API plan, test code, or data-knowledge.yaml — not in case.yaml.

### 10. Delta Operation Correctness

For every change delta file (`qa/changes/<change-id>/cases/**/case.yaml`), verify:

- `added[].case_id` **MUST NOT** already exist in the target stable case file (`qa/cases/<module>/case.yaml`). Violation = blocker.
- `modified[].case_id` **MUST** already exist in the target stable case file. Violation = blocker.
- `removed[].case_id` **MUST** already exist in the target stable case file. Violation = blocker.
- A `case_id` **MUST** appear in exactly one of `added`, `modified`, or `removed`. Duplication across lists = blocker.
- If the target stable case file does not exist yet, all cases **MUST** be under `added`. Any `modified` or `removed` entries are blockers.

If the target stable case file cannot be found, record as a warning (not a blocker) — it may not yet exist for a new module.

### 11. Duplicate and Conflicting Cases

- Check for cases with the same functional scenario (different IDs but identical intent).
- Check for cases whose assertions contradict each other.
- Check for cases that overlap with existing stable cases in `qa/cases/<module>/case.yaml`.
- Flag duplicates as auto-fixable (rename/merge) if safe; flag contradictions as `needs_human_review`.

### 12. Risk and Ambiguity

- Check whether `risk.level` is consistent with `priority` (P0/P1 → high/critical).
- Check whether high/critical risk cases have `automation.required = true`.
- Check whether `risk.rationale` is meaningful (not a generic placeholder).
- Flag vague risk rationale as low/medium finding. Flag missing risk block as blocker.

### 13. Layering Review (four-layer)

Compare `proposal.md` `## Layer Rationale` entries against each case's `type` (the single source of truth; there is no `automation.target`).

Flag these anti-patterns:

**API / E2E (functional layering):**
- A case verifiable by a single HTTP request is assigned `E2E` instead of `API` → **Type 1**
- Error-code / field-validation matrix is in E2E → **Type 1**
- Full CRUD flow uses E2E for every scenario (no API cases) → **Type 1** per scenario, or **Type 2** if assertions must migrate
- The same assertion point appears in cases of two different `type` → **Type 2** (assertion migration)

**Fuzz selection:**
- A `type: Fuzz` case has no `related_cases` pointing at a functional (API) case, i.e. Fuzz is used to replace functional assertions → **Type 2** (`human_review_required`, return to `aa-case-design`)
- `type: Fuzz` assigned to a pure read endpoint with no user input → **Type 1**, `fix_scope: ["type", "automation.fuzz"]`

**Performance selection:**
- `type: Performance` assigned to a low-frequency / non-core endpoint → **Type 1**, `fix_scope: ["type", "automation.performance"]`
- `type: Performance` case missing `automation.performance.scenario.thresholds` (p95_ms / error_rate_max) or using placeholder thresholds → **Type 2** blocker (thresholds must be user-confirmed)

**Classification:**

**Type 1 — Mechanical fix** (target label is wrong; assertions unchanged):
- Examples: error-code matrix assigned `E2E`; Fuzz on a no-input read endpoint
- `severity: low | medium`
- `auto_fix_allowed: true`
- `fix_scope: ["type"]` (plus `["automation.fuzz"]` / `["automation.performance"]` when the target sub-block must change)
- Reviewer MUST include `fix_scope` for every layering finding with `auto_fix_allowed: true`

**Type 2 — Design-level issue** (case must be split / refactored / assertions migrated, or Fuzz/Performance misused):
- Examples: one E2E scenario wraps both API logic and UI interaction; Fuzz replacing functional assertions; Performance without thresholds
- `severity: high`
- `auto_fix_allowed: false`
- `human_review_required: true` (workflow: return to `aa-case-design` for redesign)

Finding example (Type 1):

```json
{
  "id": "CASE-FINDING-LAYER-001",
  "severity": "medium",
  "category": "layering",
  "file": "qa/changes/<change-id>/cases/<module>/case.yaml",
  "message": "TC_MENU_001 validation is verifiable via single API request; set type to API not E2E.",
  "suggestion": "Change type from E2E to API.",
  "auto_fix_allowed": true,
  "fix_scope": ["type"],
  "human_review_required": false
}
```

---

## Decision Rules

Return one of these decisions:

- `pass`
- `needs_fix`
- `needs_human_review`
- `reject`

**Use `pass` when:**

- Case artifacts are complete enough.
- No blocker exists.
- Planning/codegen can continue safely.

**Use `needs_fix` when:**

- Issues are clear and can be automatically fixed.
- No product decision is required.
- No high-risk ambiguity exists.

**Use `needs_human_review` when:**

- Product behavior is ambiguous.
- Test scope needs confirmation.
- Auth/role/data behavior is unknown.
- A missing requirement cannot be inferred from files.
- Risk level is high or critical.

**Use `reject` when:**

- Case artifacts are fundamentally wrong.
- The cases test the wrong feature.
- YAML is invalid and cannot be safely repaired.
- Generated cases conflict with explicit requirement.

**Missing Inputs (special case):**

If required input files are missing, STOP before normal review — do not produce a normal review verdict. If the orchestrator requires a JSON artifact for diagnostics, write a minimal `reject` record:

```json
{
  "decision": "reject",
  "risk_level": "critical",
  "human_review_required": true,
  "auto_fix_allowed": false,
  "next_action": "stop",
  "blockers": [{ "id": "CASE-BLOCKER-MISSING", "severity": "critical", "message": "Required file missing: <path>", "required_action": "Provide the missing file and re-run aa-case-reviewer." }]
}
```

Default behaviour is STOP; the JSON artifact is written only to enable orchestrator diagnostics, not to trigger a fix loop.

### Decision ↔ JSON Field Constraints

These field values **must always be consistent**. Violating them creates gate bypass risk:

| decision | human_review_required | auto_fix_allowed |
|---|---|---|
| `pass` | `false` | `false` |
| `needs_fix` | `false` | `true` |
| `needs_human_review` | `true` | `false` (MUST be false) |
| `reject` | `true` | `false` (MUST be false) |

**Rule**: `human_review_required` MUST be `true` whenever `decision` is `needs_human_review` or `reject`.
**Rule**: `auto_fix_allowed` MUST be `false` whenever `human_review_required` is `true`.
**Rule**: `next_action` MUST match `decision`:

| decision | next_action |
|---|---|
| `pass` | `continue` |
| `needs_fix` | `run_case_design` |
| `needs_human_review` | `human_review` |
| `reject` | `stop` |

A reviewer that writes `decision = "needs_human_review"` with `human_review_required: false` produces an invalid review that enables fixer gate bypass.
A reviewer that writes `decision = "needs_fix"` with `next_action = "continue"` produces a contradictory gate that would skip required fixes.

### needs_fix Hard Rules

When `decision == "needs_fix"`, ALL of the following MUST hold — violating any is an invalid review:

- `auto_fix_allowed` MUST be `true`.
- `human_review_required` MUST be `false`.
- `auto_fix_plan` MUST contain at least one item.
- Every `auto_fix_plan[].finding_id` MUST reference an existing entry in `findings`.
- Every referenced finding MUST have `auto_fix_allowed == true`.
- Every referenced finding MUST have `human_review_required == false`.
- Findings with `severity in ["high", "critical"]` MUST NOT be referenced in `auto_fix_plan`.

---

## Risk Level Rules

Use: `low` / `medium` / `high` / `critical`

- **low**: Minor clarity or formatting issues.
- **medium**: Missing optional edge case. Some assertions could be stronger. Minor data setup uncertainty.
- **high**: Missing core scenario. Product behavior ambiguous. Auth/role/data setup unknown. Codegen likely to generate invalid tests.
- **critical**: Wrong feature. Invalid or missing required files. Case contradicts requirement. Dangerous destructive behavior is proposed.

---

## Auto Fix Rules

Set `auto_fix_allowed = true` only when all fixable findings can be applied mechanically.

Examples of auto-fixable findings:

- Missing tag
- Inconsistent status value
- Weak but obvious wording improvement
- Missing trace field when source is known
- YAML formatting issue
- Duplicate title that can be renamed safely
- Missing assertion that is directly implied by a step

Set `human_review_required = true` when:

- Product behavior must be decided
- Unknown route/auth/role/data must be confirmed
- Test scope is disputed
- A requirement interpretation is uncertain
- The fix would require inventing business behavior

---

## Required JSON Format

> **Schema source of truth:** the complete, enforced field contract for review gate JSON
> lives in `assurance_agent/artifacts/models/review.py`. Runtime `finalize` validates
> authored files against that model. The example below is illustrative only.

Write valid JSON to:

```text
qa/changes/<change-id>/review/case-review.json
```

**Minimal top-level structure (illustrative — see `assurance_agent/artifacts/models/review.py` for the full contract):**

```json
{
  "schema_version": "1.0",
  "review_type": "case",
  "change_id": "<change-id>",
  "decision": "pass",
  "risk_level": "low",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "summary": "Short review summary.",
  "reviewed_files": [],
  "blockers": [],
  "findings": [
    {
      "id": "CR-001",
      "severity": "high",
      "category": "coverage",
      "message": "required case is missing a locator-backed assertion",
      "locator": {"artifact": "change:cases/module/case.yaml", "case_id": "TC_001"}
    }
  ],
  "needs_review": [],
  "auto_fix_plan": [],
  "next_action": "continue",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

`reviewed_files` MUST include:
- `qa/changes/<change-id>/proposal.md`
- `qa/changes/<change-id>/.qa.yaml`
- every `qa/changes/<change-id>/cases/**/case.yaml` reviewed

`reviewed_files` MUST NOT be empty.

`fix_scope` is REQUIRED when a finding has `category == "layering"` and `auto_fix_allowed == true`.
It lists the exact case fields the fixer is permitted to touch.
For all other categories, `fix_scope` is omitted.

**`needs_review` hard rules:**

- If `needs_review` is non-empty and any item has `blocking == true`:
  - `decision` MUST be `needs_human_review`.
  - `human_review_required` MUST be `true`.
  - `auto_fix_allowed` MUST be `false`.
  - `next_action` MUST be `human_review`.
- If all items have `blocking == false`, a `pass` or `needs_fix` decision is still allowed, but the items must remain in `needs_review` so the reviewer record is transparent.

---

## Summary Markdown Format

Write a human-readable summary to:

```text
qa/changes/<change-id>/review/case-review-summary.md
```

Use this structure:

```markdown
# Case Review Summary

## Decision

- Decision:
- Risk:
- Auto Fix Allowed:
- Human Review Required:

## Summary

...

## Blockers

...

## Findings

...

## Auto Fix Plan

...

## Next Action

...
```

---

## Final Response

After writing the files, respond with:

```
Case review completed.

Decision: <decision>
Risk: <risk_level>
Human review required: <true|false>
Auto fix allowed: <true|false>

Files:
- qa/changes/<change-id>/review/case-review.json
- qa/changes/<change-id>/review/case-review-summary.md
```

Do not claim the review passed unless `decision = pass`.

Do not modify case files in this skill.
