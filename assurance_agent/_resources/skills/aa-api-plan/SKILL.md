---
name: aa-api-plan
description: Use when a QA Case Delta contains API automation cases and you need to generate reviewable plan files before writing any test code. Triggers on: "generate API plan", "M3 Stage 1", "api-plan.md", "review before codegen", "api planning". Only processes API cases with automation.required = true. Never generates test code.
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-api-plan.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Test Data Architecture Contract

- Shared business-valid builders live in `tests/testdata/domain/`. They contain domain defaults, invariant-preserving create/cleanup operations, and return plain snapshots. They contain no pytest, Playwright, Hypothesis, Locust, HTTP-client, subprocess, or event-loop bridge code.
- API execution glue lives in `tests/api/adapters/`. It is the only API-owned location for pytest fixtures, in-process async calls, or live-server setup transport.
- Plans must map each data need to one shared domain factory and one API adapter. A shared file is `create-if-missing` and may be created only by the first active codegen layer; later layers reuse it without rewriting it.
- `.aa/data-knowledge.yaml` must use `capabilities.domain_factories` and `capabilities.adapters.api`; legacy `capabilities.factories` is discovery-only and must be proposed for migration.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. Verify `phases.case_review.status == pass` (gate must be cleared).
3. Read **Required** input files from disk:
   - `qa/changes/<change-id>/.qa.yaml`
   - `qa/changes/<change-id>/proposal.md`
   - `qa/changes/<change-id>/cases/**/case.yaml`
   - If any required file is missing, **STOP** and report.
4. Read **Optional** input files from disk (missing = warning, do not STOP):
   - `.aa/config.yaml`
   - `.aa/data-knowledge.yaml` (if missing, proceed and generate proposal — see Data Knowledge Layer Rules)
   - Backend source files (for endpoint / schema confirmation)
   > **data-knowledge tier rules:**
   > - `aa-api-plan` stage: missing `.aa/data-knowledge.yaml` does not block planning; generate `data-knowledge.proposal.api.yaml`; Plan Readiness may be `ready_with_warnings`; **Codegen Readiness MUST be `not_ready`** (regardless of case data needs).
   > - `aa-api-codegen` stage: `.aa/data-knowledge.yaml` **must exist**. `data-knowledge.proposal.api.yaml` is reference only and cannot substitute.
5. Use files as the sole source of truth.

**After completing work:**

1. Write output files:
   - `qa/changes/<change-id>/plans/api-plan.md`
   - `qa/changes/<change-id>/plans/api-test-data-plan.md`
   - `qa/changes/<change-id>/plans/api-codegen-plan.md`
   - `qa/changes/<change-id>/plans/m3-review-summary.md`
   - `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml` (only when `.aa/data-knowledge.yaml` is missing)
2. Report the `workflow-state.yaml` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.yaml` — report the values in your final message and the orchestrator applies them):
   - `phases.api_plan.status = done`
   - `phases.api_plan.outputs` = **all actually generated** output files (include `data-knowledge.proposal.api.yaml` if generated)
   - **Do NOT** set `phases.api_plan_review.status` — that gate belongs to `aa-api-plan-reviewer`

---

# API Planning for QA

Name: API Test Plan Generation

Description: "QA superpower: MUST use before generating API test code. Produces reviewable api-plan.md, api-test-data-plan.md, api-codegen-plan.md, and m3-review-summary.md from case.yaml, proposal.md, and the data knowledge layer. This skill does NOT generate test code or run pytest."

Converts a QA Case Delta into a reviewable API test implementation plan.

This skill is AA M3 Stage 1. It reads `qa/changes/<change-id>/cases/**/case.yaml`, filters API automation cases, and generates API test plan, test data plan, and codegen plan. After completion, proceed to `aa-api-plan-reviewer`; the orchestrator may enter `aa-api-codegen` only when `api-plan-review.json` satisfies the gate.

## When to Use

Use when the user asks to:

- Generate M3 API plan
- Generate api-plan from cases
- Generate api-test-data-plan
- Generate api-codegen-plan
- Plan API automation tests

## Do Not Use When

Do NOT use this skill when:

- The user already asked to generate test code from the plan
- The user asks to run pytest
- The user asks to generate `/tests/api/*.py`
- The user asks to generate execution results
- case.yaml has not been generated yet
- `case-review.json` has `decision != "pass"` (structured gate not cleared)

## Inputs

**Required** (STOP if missing):

- `qa/changes/<change-id>/.qa.yaml`
- `qa/changes/<change-id>/proposal.md`
- `qa/changes/<change-id>/cases/**/case.yaml`

**Optional** (warning if missing, do not STOP):

- `.aa/config.yaml`
- `.aa/data-knowledge.yaml`
- Backend source files (for endpoint / schema confirmation)

If `.aa/config.yaml` is missing, record a warning but do not block.

If `.aa/data-knowledge.yaml` is missing, **do not block the plan stage**. Do not write directly to the project-level knowledge base. You MUST generate:

`qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml`

Record a warning in `m3-review-summary.md` and provide **Plan Readiness** and **Codegen Readiness** separately (see decision rules).

> **Plan vs Codegen gate (data-knowledge):**
> - **aa-api-plan**: missing `.aa/data-knowledge.yaml` does not block planning; generate `plans/data-knowledge.proposal.api.yaml`; Plan Readiness may be `ready_with_warnings`; **Codegen Readiness MUST be `not_ready`**.
> - **aa-api-codegen**: missing `.aa/data-knowledge.yaml` is a **BLOCKER**; `data-knowledge.proposal.api.yaml` is reference only and cannot substitute the formal knowledge base.
> - **Reason**: `aa-api-codegen` requires formal `.aa/data-knowledge.yaml` before start regardless of whether selected cases have minimal data needs.

## Outputs

May generate ONLY:

- `qa/changes/<change-id>/plans/api-plan.md`
- `qa/changes/<change-id>/plans/api-test-data-plan.md`
- `qa/changes/<change-id>/plans/api-codegen-plan.md`
- `qa/changes/<change-id>/plans/m3-review-summary.md`
- `qa/changes/<change-id>/plans/data-knowledge.proposal.api.yaml` (only when `.aa/data-knowledge.yaml` does not exist)

Must NOT generate:

- `.aa/data-knowledge.yaml` (write proposal only, not project-level knowledge base)
- `/tests/api/*.py`
- `/tests/testdata/domain/<entity>.py` (shared; create-if-missing or reuse)
- `/tests/api/adapters/*.py` (API-owned pytest/transport wrappers)
- `/tests/helpers/*.py`
- `qa/changes/<change-id>/execution/*`

## Workflow

1. Read `.aa/config.yaml`; if missing, record warning.
2. Read `qa/changes/<change-id>/.qa.yaml`.
3. Read `qa/changes/<change-id>/proposal.md`.
4. Locate `qa/changes/<change-id>/cases/**/case.yaml`.
5. Parse `schema_version`, `added`, `modified`, `removed`.
6. Filter cases with `type = API` and `automation.required = true` **from `added` and `modified` only**. If none match, stop and report.
   - **Must NOT** generate API plans for cases under `removed`.
   - `removed` entries (case_id / reason only) are for context and archive merge validation, not plan selection.
7. Read `.aa/data-knowledge.yaml`. If missing, generate `plans/data-knowledge.proposal.api.yaml` (must NOT write `.aa/data-knowledge.yaml`) and record warning in `m3-review-summary.md`.
8. Generate `plans/api-plan.md` (create `plans/` if missing).
9. Generate `plans/api-test-data-plan.md`.
10. Generate `plans/api-codegen-plan.md`.
11. Generate `plans/m3-review-summary.md`.
12. Stop; do not generate test code.
13. Remind next step is `aa-api-plan-reviewer`; orchestrator may enter `aa-api-codegen` only when ALL are true:
    - `api-plan-review.json` `decision == "pass"`
    - `codegen_readiness in ["ready", "ready_with_warnings"]`
    - `.aa/data-knowledge.yaml` exists
    User verbal approval cannot substitute the gate.

## Checklist

Complete in order:

- [ ] Find change-id
- [ ] Read `.qa.yaml`
- [ ] Read `proposal.md`
- [ ] Read `cases/**/case.yaml`
- [ ] Confirm `schema_version: "1.0"`
- [ ] Filter API automation cases from `added` / `modified` only (`type: API` + `automation.required: true`); skip `removed`
- [ ] Read `.aa/data-knowledge.yaml` (if missing, write `plans/data-knowledge.proposal.api.yaml`; must NOT write `.aa/data-knowledge.yaml` directly)
- [ ] Generate `api-plan.md`
- [ ] Generate `api-test-data-plan.md`
- [ ] Generate `api-codegen-plan.md`
- [ ] Generate `m3-review-summary.md`
- [ ] Confirm no test code was generated
- [ ] Report the `phases.api_plan.status = done` state delta (applied to `workflow-state.yaml` per the Context Contract)
- [ ] Output next-step hint (point to `aa-api-plan-reviewer`, not directly to `aa-api-codegen`)

## Output Contract

### api-plan.md

Path: `qa/changes/<change-id>/plans/api-plan.md`

Must include:

- **Source** — case source file path(s)
- **Scope** — Case ID and title list (from `added` + `modified` only; exclude `removed`)
- **API Targets** — table: Case ID \| Scenario \| Method \| Path \| Expected
- **Auth Strategy** — auth requirements per case
- **Request Strategy** — headers, body, params (method / path / headers go in plan only; **must NOT** write back to `case.yaml`)
- **Assertion Strategy** — assertion list per case
- **Mock Strategy** — dependencies to mock (or None)
- **Cleanup Strategy** — post-test state cleanup
- **Output File Candidates** — suggested test file paths
- **Needs Review** — items requiring human confirmation
- **Blockers** — items blocking codegen (or None)

**API Targets TBD rules** (when Method or Path cannot be confirmed):

- Use `TBD` in Method / Path columns and explain in the same row or under **Needs Review**.
- Never silently guess endpoint or HTTP method.
- If only non-critical details are missing (e.g. optional query param), put under **Needs Review**; Plan Readiness may be `ready_with_warnings`.
- If Method or Path is completely unknown and cannot be inferred from backend source, put under **Blockers**; Plan Readiness and Codegen Readiness MUST be `not_ready`.
- If Method is known but Path is uncertain (or vice versa), at minimum put under **Needs Review**; if it blocks codegen mapping, also put under **Blockers**.

### api-test-data-plan.md

Path: `qa/changes/<change-id>/plans/api-test-data-plan.md`

Must include:

- **Scope** — cases requiring data setup
- **Required Data** — table: Entity \| State \| Capability
- **Preconditions** — from `case.yaml:preconditions`
- **Capability Mapping** — table: Need \| Capability \| Source \| Status (found/missing/warning)
- **Factory / Boundary Strategy** — per-entity seeding approach (see three-ring rules below)
- **Setup Strategy** — ordered data setup steps
- **Runtime Verify Strategy** — data verification assertions before API calls
- **Cleanup Strategy** — post-test cleanup
- **No Data Required Cases** — cases needing no data setup
- **Blockers / Assumptions** — unresolved data dependencies
- **Review Checklist** — items for reviewer confirmation

#### Factory / Boundary Strategy — Three-Ring Rules

Seeding method is determined by **aggregate boundary**; do not mix layers:

| Ring | Seeding method | Typical scenario |
|------|----------------|------------------|
| **Within domain (same aggregate)** | Call shared factory functions from `tests/testdata/domain/`; functions are named `make_*` and call service/repository/controller code internally | `make_role()` / `make_user()` / `make_dept()` / `make_menu()` |
| **Cross domain (cross-aggregate reference)** | Seed a valid entity in the other domain first, then pass its id as reference | Bind Role to User: `make_role()` first, then pass id to `make_user(role_ids=[...])` |
| **External domain (external systems)** | mock / contract stub (no external deps in this project; reserved) | third-party auth, message queues, etc. |

**Per-entity seeding table (plan MUST document every entity under test):**

**Factory module naming convention:** shared capabilities live under `tests/testdata/domain/<entity>.py`; API execution wrappers live under `tests/api/adapters/<module>.py`.

**Factory usage contract (must appear in plan when fixtures/factories are involved):**

1. Setup uses factory; test body uses HTTP.
2. Factory modules call service/controller code; do not copy controller code into tests.
3. Factories must not leak ORM objects to test functions; return plain data snapshots or convert ORM objects to snapshots inside fixtures.
4. Shared factories never own sync/async bridging. In-process async API adapters may await them; live-server adapters use a confirmed HTTP transport or bounded isolated worker. If no safe boundary can be identified, codegen readiness is `not_ready`.
5. Create API cases must exercise HTTP create; do not replace create behavior assertions with the same create factory path.
6. Cleanup must maintain the same invariants; do not raw-delete M2M, closure, or soft-delete entities.
7. Shared factory modules live under `tests/testdata/domain/<entity>.py`; API adapter modules live under `tests/api/adapters/`.

| Entity | Ring | Preferred method | Notes |
|--------|------|------------------|-------|
| Role | Within domain | `make_role()` | M2M menus/apis |
| User | Within domain | `make_user()` | M2M roles + password hash |
| Dept | Within domain | `make_dept()` | DeptClosure |
| Menu | Within domain | `make_menu()` when present in `tests/testdata/domain/menu.py`; else propose in `data-knowledge.proposal.api.yaml` | Single table + `parent_id`; factory encapsulates resolve |
| AuditLog | Within domain (leaf) | `make_audit_log()` or direct insert | No derived tables |

**HTTP API seeding boundary (hard rule for plan + codegen):**

- **Forbidden** for fixture/setup/teardown: calling `POST /.../create` (or equivalent) to seed data for cases whose **primary assertion is NOT the create endpoint itself**.
- **Allowed** only when:
  - The case explicitly tests create/update/delete HTTP behavior (e.g. TC_*_create happy path, validation on create body), **or**
  - No `make_*` / service path exists **and** plan documents degradation step + reviewer accepts (see ladder below).
- **Forbidden** pattern to flag in plan review: an API fixture that POSTs create + GET list to resolve id without an approved adapter mapping; use the shared `make_<entity>()` capability through `tests/api/adapters/`.

**Mandatory rules (codegen must follow):**

- **Default (within domain):** setup/teardown uses `tests/testdata/domain/` capabilities through `tests/api/adapters/` when authorized in `.aa/data-knowledge.yaml`.
- Entities with M2M tables (Role↔menus/apis, User↔roles): **must** use shared `make_*` functions through the API adapter; must not raw-insert a single table.
- Entities with closure derived tables (Dept↔DeptClosure): **must** use `make_dept()`; **must not** raw-insert `Dept`; **must not** HTTP-create in fixtures.
- Entities with password hashing (User): **must** use `make_user()`; **must not** raw-insert plaintext password; **must not** HTTP-create in fixtures.
- Menu (and other single-table domain entities): **must** use `make_menu()` when factory exists; **must not** HTTP-create in fixtures except create-focused cases.
- Leaf tables with no derived relations (e.g. AuditLog): `make_audit_log()` or `Model.create` direct insert allowed; still **must not** use HTTP-create in fixtures unless case tests create API.
- Test config (BASE_URL / admin credentials / prefixes): **must** reference `tests.config.settings`; **must not** hardcode or `os.environ.get`.

**Degradation ladder when no factory / service / API exists (priority order):**

1. Propose `make_<entity>()` in `tests/testdata/domain/<entity>.py` and its API adapter mapping (preferred; codegen is blocked until authorized).
2. Indirect seeding via byproducts or composed operations on real code paths (service/controller).
3. Reuse app internal helpers to maintain invariants.
4. Copy invariants on test side + add contract guard tests (prevent drift).
5. Add env-flag-guarded test seed entry in app.
6. HTTP create for setup **only** with explicit plan + reviewer approval and documented in **Needs Review** (not default).
7. Pure leaf table with no invariants → direct insert (last resort).

### api-codegen-plan.md

Path: `qa/changes/<change-id>/plans/api-codegen-plan.md`

This file describes how to generate test code; it must NOT contain test code.

Must include:

- **Target Files** — table: File \| Purpose
  - If setup creates or cleans domain data, Target Files must include `tests/testdata/domain/<entity>.py` as `create-if-missing` or `reuse`, plus `tests/api/adapters/<module>.py`.
  - API pytest wrappers may appear only under `tests/api/adapters/`; they must not duplicate domain creation rules.
  - Live-server setup must name a confirmed HTTP or isolated-worker boundary. Do not plan a generic shared ORM runtime or an in-thread event-loop bridge.
- **Test Function Mapping** — table: Case ID \| Test Function \| Target File
  - **Naming (hard rule):** every Test Function MUST be `test_<case_id lowercase>__<description>` — the lowercase case_id prefix followed by a **double underscore**. Example: `TC_USER_API_001` → `test_tc_user_api_001__list_users_happy_path`. The `aa run` result parser backfills case_id from this prefix; names without it become Unmapped Tests, break MRC/inspect/healing traceability, and cap the quality gate at PASS_WITH_WARNINGS. A plan whose mapping violates this rule is **not codegen-ready**.
- **Factory Mapping** — table: Entity \| Shared Module (`tests/testdata/domain/<entity>.py`) \| Function (`make_*`) \| Ownership (`create-if-missing`/`reuse`) \| Required By
- **Adapter Mapping** — table: Entity \| API Adapter (`tests/api/adapters/<module>.py`) \| Transport (`in_process_async`/`http`/`isolated_worker`) \| Cleanup
- **Fixture Mapping** — table: Fixture \| Source Factory \| Wrapper Only (yes/no) \| Required By
- **Helper Mapping** — shared helper description
- **Import Strategy** — import modules per file
- **Assertion Mapping** — table: Case ID \| Assertions
- **Data Setup Mapping** — data setup steps per case
- **Cleanup Mapping** — cleanup steps per case
- **Run Guidance** — run hints for `aa-run` (Phase 8) (pytest args, markers, env vars); not executed by aa-api-codegen, input for execution layer only
- **Codegen Preconditions** — conditions before codegen starts:
  - `api-plan-review.json` `decision == "pass"`
  - `codegen_readiness in ["ready", "ready_with_warnings"]` (from reviewer, not plan self-assessment)
  - **Formal** `.aa/data-knowledge.yaml` **must exist** (hard gate; `data-knowledge.proposal.api.yaml` cannot substitute; if missing at plan stage Codegen Readiness MUST be `not_ready` until knowledge base is filled)

### m3-review-summary.md

Path: `qa/changes/<change-id>/plans/m3-review-summary.md`

Must include:

- **Change** — change-id
- **Loaded Case Files** — processed case.yaml path list
- **API Cases** — total (`added` + `modified` only), plan-ready count, codegen readiness status
- **Removed Cases** — case_id list from `removed` (record only, not in plan)
- **Generated Plan Files** — four required plan file paths; if `data-knowledge.proposal.api.yaml` was generated, list separately as optional artifact
- **Data Knowledge Layer Status** — OK / Warning and capability resolution results
- **Blockers** — items blocking codegen (or None)
- **Needs Review** — items requiring human confirmation
- **Plan Readiness** — `ready` / `ready_with_warnings` / `not_ready` (plan document complete and reviewable)
- **Codegen Readiness** — `ready` / `ready_with_warnings` / `not_ready` (orchestrator may enter `aa-api-codegen` after review pass)
- **Next Step** — direct reviewer to run `aa-api-plan-reviewer`; codegen waits for `api-plan-review.json` gate

### Plan Readiness Decision

Assess whether the plan document itself is complete and reviewable (**not** the same as codegen-ready):

| Value | Condition |
|-------|-----------|
| `ready` | No blockers; Needs Review empty or all non-blocking; all selected API cases mapped |
| `ready_with_warnings` | No blockers; non-blocking Needs Review exists; or capability `confidence: low` but endpoint mapping not blocked |
| `not_ready` | Blockers exist; or API Target Method/Path is `TBD` and cannot be mapped |

### Codegen Readiness Decision

Assess whether orchestrator may enter `aa-api-codegen` after review pass (**stricter** than Plan Readiness):

| Value | Condition |
|-------|-----------|
| `ready` | Plan Readiness = `ready`; `.aa/data-knowledge.yaml` exists and selected case capabilities resolve OK |
| `ready_with_warnings` | Plan Readiness = `ready` or `ready_with_warnings`; `.aa/data-knowledge.yaml` **exists**; non-blocking Needs Review; required capabilities confirmed or safely optional |
| `not_ready` | Plan Readiness = `not_ready`; **or** `.aa/data-knowledge.yaml` missing (**regardless** of selected case data/auth/cleanup needs); **or** missing data capability with no reasonable proposal |

**Tier rules when `.aa/data-knowledge.yaml` is missing (hard rule):**

- Plan stage does not block → may generate `data-knowledge.proposal.api.yaml`; **Plan Readiness** may be `ready_with_warnings`.
- **Codegen Readiness MUST be `not_ready`** — regardless of whether selected API cases have minimal data needs.
- **Reason**: `aa-api-codegen` requires formal `.aa/data-knowledge.yaml` before start; `data-knowledge.proposal.api.yaml` is a remediation artifact only.
- Orchestrator must fill `.aa/data-knowledge.yaml` and re-review before codegen.

## Data Knowledge Layer Rules

Must attempt to read: `.aa/data-knowledge.yaml` (missing does not block plan stage).

**Stage gate (plan vs codegen):**

| Stage | When `.aa/data-knowledge.yaml` is missing |
|-------|---------------------------------------------|
| **aa-api-plan** | Do not block planning; generate `plans/data-knowledge.proposal.api.yaml`; Plan Readiness may be `ready_with_warnings`; **Codegen Readiness MUST be `not_ready`** |
| **aa-api-codegen** | **BLOCKER**; `data-knowledge.proposal.api.yaml` is reference only and cannot substitute formal knowledge base |

If the file does not exist:

1. Generate `plans/data-knowledge.proposal.api.yaml` — use the **empty proposal template** below.
2. **Must NOT** generate `.aa/data-knowledge.yaml`.
3. **Must NOT** invent specific capability names in the template (fixture names, auth methods, cleanup methods).
4. If `tests/` exists, scan and fill `discovered_candidates` (`confidence: low`). Priority scan targets:
   - `tests/testdata/domain/` — shared domain-aware factory modules
   - `tests/api/adapters/` — API-only pytest and transport orchestration
   - `tests/config.py` — unified settings (`base_url` / `admin_username` / `admin_password` / `*_prefix`)
   - `tests/conftest.py` — shared fixtures (`api_client` / `auth_headers` / `assert_schema`)
   - `tests/schema_validation.py` — `assert_matches_schema` / `_LOCAL_SCHEMAS`
5. If `tests/` is missing or unreadable, **must NOT** fabricate candidates; record warning in `m3-review-summary.md` and add items to `needs_review`.
6. Record missing capabilities in `needs_review`.

**`tests/config.py` write / update gate:**

If `tests/config.py` does not exist, or if planning reveals that a new field or default value would be needed:

1. **STOP** before writing or proposing any default value.
2. **Ask the user** to confirm each runtime fact. Required questions:

   | Field | Question to ask |
   |-------|-----------------|
   | `base_url` | "What is the backend base URL? — please confirm from `run.py` or your start command." |
   | `frontend_url` | "What is the frontend dev server URL? — please confirm from `package.json` or `vite.config.ts`." |
   | `admin_username` | "What admin username does QA use? — please confirm from seed config or README." |
   | `admin_password` | "What admin password does QA use? — please confirm from seed config or README." |
   | new `*_prefix` | "What prefix should `<entity>` test data use? Must be a value production data will never have." |

3. Do **not** propose a default value based on convention or prior generated tests.
4. If the user cannot confirm a value → add it to `needs_review` in `m3-review-summary.md` and set `codegen_readiness = not_ready`.
5. If `tests/config.py` already exists and covers the needed fields → scan and use existing values; do not modify the file.

```yaml
capabilities:
  fixtures: {}
  auth: {}
  cleanup: {}

discovered_candidates:
  - name: <candidate-fixture-or-factory-name>
    source: <file-path-where-found>
    confidence: low | medium | high

needs_review:
  - <missing capability description>
```

Rules:

- Do not freely invent fixtures, factories, auth, or cleanup.
- `discovered_candidates` records candidates only; not confirmed capabilities.
- If capability is missing or `confidence: low`, record as blocker or needs_review and update **Plan Readiness** and **Codegen Readiness** (see decision rules).
- If `.aa/data-knowledge.yaml` is missing, **Codegen Readiness MUST be `not_ready`** (regardless of case data needs).
- If capability exists but verify is incomplete, record as needs_review.
- Stage 1 is static validation only; do not execute real seeding.
- `data-knowledge.proposal.api.yaml` must NOT be used by codegen as a substitute for `.aa/data-knowledge.yaml`.

## Hard Rules

- Always generate plans before codegen.
- This Skill must not generate test code.
- This Skill must not generate execution results.
- Always read `qa/changes/<change-id>/cases/**/case.yaml`.
- Only process API cases under `added` and `modified`.
- Do NOT generate API plans for cases under `removed`. `removed` entries are read only for context and archive merge validation.
- Only process API cases with `automation.required = true`.
- Always generate api-plan.md.
- Always generate api-test-data-plan.md.
- Always generate api-codegen-plan.md.
- Always generate m3-review-summary.md.
- Never silently guess endpoint paths.
- Never invent fixtures, factories, auth helpers, or cleanup helpers.
- Missing data capability must be recorded as blocker or needs_review.
- PRD is not read from a fixed directory; requirements come from case.yaml and proposal.md.
- Test code generation requires `api-plan-review.json` `decision == "pass"`. User approval may be input to `aa-api-plan-reviewer`, but is not itself a gate artifact.
- Never write method, path, or headers back to `case.yaml`.
- Do not set `phases.api_plan_review.status` — that belongs to `aa-api-plan-reviewer`.

## Stop Conditions

Must stop immediately after generating these four required plan files:

- `api-plan.md`
- `api-test-data-plan.md`
- `api-codegen-plan.md`
- `m3-review-summary.md`

If `.aa/data-knowledge.yaml` is missing, must also generate `data-knowledge.proposal.api.yaml` (fifth optional artifact).

Before stopping, report the `workflow-state.yaml` state delta (see Context Contract for who applies it):

- `phases.api_plan.status = done`
- `phases.api_plan.outputs` = **all actually generated** output paths (include `data-knowledge.proposal.api.yaml` if generated)
- **Must NOT** set `phases.api_plan_review.status`

After stopping, output:

```
M3 Stage 1 completed. Next: aa-api-plan-reviewer.
Orchestrator may invoke aa-api-codegen only if:
- api-plan-review.json has decision == "pass"
- codegen_readiness in ["ready", "ready_with_warnings"]
- .aa/data-knowledge.yaml exists
User verbal approval cannot substitute api-plan-review.json.
```

Must NOT continue generating:

- `/tests/api/*.py`
- `/tests/testdata/domain/<entity>.py`
- `/tests/api/adapters/*.py`
- `/tests/helpers/*.py`
- `qa/changes/<change-id>/execution/*`

## Anti-patterns

Forbidden:

- "This case is simple, generate test code directly"
- Generating `/tests/api` without a plan
- Silently guessing endpoints without marking for review
- Freely inventing fixtures or auth helpers
- Running pytest during plan stage
- Writing execution results during plan stage
- Writing method/path/header back to case.yaml

## Next Workflow

After completion, next workflow is:

**aa-api-plan-reviewer**

Flow:
1. Hand plan files to `aa-api-plan-reviewer` to produce `api-plan-review.json`.
2. Orchestrator may enter `aa-api-codegen` only when **all** are true:
   - `decision == "pass"`
   - `codegen_readiness in ["ready", "ready_with_warnings"]`
   - `.aa/data-knowledge.yaml` exists
3. If `.aa/data-knowledge.yaml` was missing at plan stage, **Codegen Readiness in m3-review-summary.md MUST be `not_ready`**; fill formal knowledge base and re-review before codegen — do not jump to codegen on `ready_with_warnings` alone.

User verbal approval ("looks good", "continue") **cannot substitute** `api-plan-review.json` gate; user approval may be input to `aa-api-plan-reviewer` but is not a gate artifact itself.
