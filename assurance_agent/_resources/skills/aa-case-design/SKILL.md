---
name: aa-case-design
description: "MUST use to design QA scope and generate proposal.md plus semantic case delta YAML before aa-case-reviewer. Clarifies requirements, target module, QA scope, test target selection (API/E2E/Fuzz/Performance), data needs, assertions, and coverage approach through collaborative dialogue."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-case-design.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml` if it exists, including top-level `run_context`.
   - `run_context.interaction_mode == autonomous` (default when absent under `aa-workflow`) → do not run planned clarification dialogue or require user approval; generate from Explore `test_strategy` + resolved `open_questions`, then hand off to `aa-case-reviewer`.
   - `run_context.interaction_mode == interactive` → keep the existing clarification dialogue and explicit user approval before writing files.
2. **Explore gate (Phase 1.1):** If `phases.explore` exists (written by `aa-explore`), apply gate logic from this repo's `aa-explore/SKILL.md` (Context Contract + Phase 1 gate):
   - `status == pending` → **STOP**
   - `status == done` → read `explore/advisory.json`; missing file → **STOP**
   - `mode == required` and `status in [failed, unavailable]` → **STOP**
   - `mode == advisory` and `status in [skipped, unavailable, failed]` → record warning, continue without advisory
3. Read **Required** inputs:
   - user requirement text
   - relevant backend and/or frontend product source files under the project source root
   The case designer MUST read those files directly before choosing cases and assertions.
   Explore source evidence is not a substitute for this direct read. If the relevant
   product source cannot be located or read, **STOP** with `source_unavailable` rather
   than deriving executable cases from requirements, advisory, docs, or tests alone.
4. Read **Optional** inputs (missing = warning, do **not** STOP):
   - existing `qa/cases/**`
   - existing `tests/api/**`
   - existing `tests/e2e/**`
   - existing `qa/knowledge/**`
   - existing `qa/changes/**`
   If optional QA directories are missing, record a warning and continue as a **new QA asset initialization** path. Do **not** stop solely because `qa/cases/`, `tests/`, or `qa/knowledge/` does not exist.
5. If `workflow-state.yaml` exists and `phases.skill_registry_check.status == fail` → **STOP**.
6. Use files as the sole source of truth.
7. **Review re-entry:** If `review/case-review.json` exists with `decision: needs_fix`,
   apply only findings that carry a `locator`. Do not rewrite cases or proposal
   sections the findings do not name.

**After completing work:**

1. Write all required output files:
   - `qa/changes/<change-id>/.qa.yaml`
   - `qa/changes/<change-id>/proposal.md`
   - `qa/changes/<change-id>/cases/<module>/case.yaml`
2. Handle `qa/changes/<change-id>/workflow-state.yaml` by execution context:
   - **Dispatched phase subagent (aa-workflow subagent-dispatch mode):** never create or write `workflow-state.yaml` — the orchestrator owns it and created it in Phase 1.1. Report the state delta in your final message instead:
     - `phases.case_design.status = done`
     - `phases.case_design.outputs` = all output files
   - **Standalone / inline invocation (primary agent):** create or update the file directly:
     - If the file does **not** exist, create it with the full base schema (see `aa-workflow` `workflow-state.yaml` schema), including:
       - `execution_mode: inline`
       - `subagent_skill_inheritance: disabled`
       - `phases.skill_registry_check.status: skipped`
       - `phases.skill_registry_check.reason: standalone aa-case-design invocation; full registry check is owned by aa-workflow`
       - `phases.skill_registry_check.checked_skills: [aa-case-design]`
       - `agent_warnings` with `OPENCODE-SKILL-RESOLUTION-001`
     - Do **not** set `phases.skill_registry_check.status: pass` from this skill — full registry verification belongs to `aa-workflow` Phase 0.
     - Do **not** create a partial file containing only `phases.case_design`. Always write the full schema.
     - Set `phases.case_design.status = done`
     - List all output files under `phases.case_design.outputs`
3. Record any warnings or known issues explicitly (in `workflow-state.yaml` when inline/standalone; in the reported state delta when dispatched).

---

# Brainstorming Requirements Into QA Coverage

Help turn requirements and change descriptions into a well-scoped QA coverage plan. In `interactive` mode, do this through collaborative dialogue. In `autonomous` mode, derive the plan from file evidence, Explore `test_strategy`, and resolved `open_questions`, then generate the proposal and semantic case delta YAML directly in this skill.

Start by deriving the change ID and exploring the existing QA context. Branch by `run_context.interaction_mode`:

- `interactive`: ask clarifying questions **one at a time**. Once you understand the scope, propose 2–3 coverage approaches and get user approval. After approval, write `proposal.md`, then write the semantic case delta YAML and self-review it before handing off to aa-case-reviewer.
- `autonomous`: do not ask planned clarification questions and do not wait for user approval. Use Explore `test_strategy` as the macro plan proposal, consume `open_questions` with `status=answered` / `answered_via=auto_default`, use neutral wording for `deferred` or `undecided`, write `proposal.md` with `generation_mode: autonomous`, then write case delta YAML and self-review it before handing off to aa-case-reviewer.

<HARD-GATE>
Do NOT generate case delta, plan, test code, execution result, review result, or archive output until the mode-specific gate below is satisfied.

`interactive` gate:
1. QA context has been explored,
2. clarifying questions have been answered,
3. 2–3 QA coverage approaches have been proposed,
4. the user has approved the QA coverage approach.

`autonomous` gate:
1. QA context has been explored,
2. Explore advisory (when present) has no `status=unanswered` open question,
3. `test_strategy` / propagated `assertion_intent` / available QA context have been reconciled into an internal coverage decision,
4. `proposal.md` records `generation_mode: autonomous` and describes which defaults were used.

After the mode-specific gate passes, this skill MUST:
1. Write `qa/changes/<change-id>/proposal.md`
2. Write `qa/changes/<change-id>/cases/<module>/case.yaml`

After the case YAML is written and self-reviewed, the ONLY next workflow is aa-case-reviewer.

Do NOT invoke aa-api-plan, aa-e2e-plan, aa-api-codegen, aa-e2e-codegen, or aa-archive directly from this skill.
</HARD-GATE>

## Anti-Pattern: "This Is Too Simple To Need Brainstorming"

Every `interactive` QA change goes through this process. A one-line fix, a small new field, a configuration toggle — all of them. Unexamined assumptions about what to test, what data to set up, and what constitutes a pass are where most QA effort is wasted. The brainstorm can be short for simple changes, but in `interactive` mode you MUST complete it and get approval before generating any file. In `autonomous` mode, the explicit approval is replaced by transparent defaults plus the mandatory `aa-case-reviewer` gate.

## Checklist

Maintain a visible checklist for each item, or use the available task/todo tool if the environment supports it. Complete them in order:

0. **Explore gate** — read `phases.explore`; if `done`, read `explore/advisory.json` (internal; no dump to user); collect `open_questions_for_case_design[]`:
   - `status=answered` (or legacy `answer != null`) resolves the assertion intent for **that specific `pitfall_ref` only** (do not re-ask that exact pitfall in interactive mode), but do **not** mark the whole category satisfied.
   - `status=deferred` or `assertion_intent=undecided` means use neutral wording and do not assert the pitfall as known-accepted behavior.
   - `status=unanswered` is allowed only in `interactive` mode before Step 4; in `autonomous` mode it is a STOP because Explore should have applied `auto_default`.
   Also read propagated `assertion_intent` on `case_design_guidance.priority_hints[]`, `watchlist[]`, and `suggested_scenarios[]` — when present, treat the hint/scenario text as the authoritative test directive (do not reinterpret neutral pitfall wording); also read `test_strategy` (if present) as a **macro plan proposal** for scope/data/layer/approach.
1. **Derive change ID** — format `<TICKET-ID>-<short-kebab-description>`
2. **Explore QA context** — check `qa/cases/`, `tests/`, `qa/knowledge/`, `qa/changes/`
3. **Identify target module** — ask one module confirmation question at a time; decompose if multiple independent modules are involved
   - If `phases.explore.status == done`: after module confirm, show **3–5 bullet Explore 摘要** (confidence tags; path to `advisory.json`; no full dump)
4. **Mode branch**:
   - `interactive`: ask clarifying questions one at a time — cover 8 categories; **prioritize watchlist / `exception_scenarios`** when advisory exists; lead macro categories with the `test_strategy` proposal (confirm/override) when present; treat advisory answered OQs as already-resolved for that specific pitfall; surface unresolved OQs as part of their category's question.
   - `autonomous`: skip planned user questions. Adopt `test_strategy` where supported by evidence, choose conservative defaults for missing macro categories, and keep `deferred` / `undecided` pitfalls neutral.
5. **Reconcile internally** — map advisory to `adopted[]`, `override[]` (with reason), `gap[]`; **do NOT dump risk report to user**; in `interactive` mode, gap-only follow-up max 1–2 questions; in `autonomous` mode, record unresolved gaps in `proposal.md` instead of asking.
6. **Propose 2–3 QA coverage approaches** — `interactive` only; each must cite adopted / override / gap from Reconcile; the option list MUST include an explicit Fuzz/Performance opt-in entry (Category 3 hard rule).
7. **Get user approval** — `interactive` only; wait for explicit confirmation. `autonomous` skips the user wait and relies on `aa-case-reviewer` as the quality gate — but still MUST persist approval metadata (step 8).
8. **Write `.qa.yaml` approval metadata** — **required in both modes** before / when writing cases. `case-design-gate` reads `.qa.yaml.approval` and **STOP**s when the block is missing (`missing_field`). Never strip a pre-seeded `approval` block when rewriting `.qa.yaml`.
   - `interactive`:
     ```yaml
     approval:
       mode: interactive
       approved_by: user
       approved_approach: <stable-option-id>
       approved_at: <ISO timestamp>
     ```
   - `autonomous` (mandatory — user wait is skipped, gate is not):
     ```yaml
     approval:
       mode: autonomous
       approved_by: aa-workflow
       approved_approach: <chosen coverage approach summary>
       approved_at: <ISO timestamp>
     ```
   `aa status` enforces this through `case-design-gate`; `cases/` existing on disk is not enough for case-design to be `done`.
9. **Write `proposal.md`** — must include `## Explore Input` when advisory `done`; `_skipped` placeholder otherwise; plus `## Test Types Considered` (all four layers, selected/declined + reason) and `## Layer Rationale`; include `generation_mode: autonomous` when no user approval was requested. It must also contain exactly one product-source attestation section in this form:
   ```markdown
   ## Product Source Verification

   - independently_read: true
   - reviewed_source_files:
     - `project/relative/product/source.py`
   ```
   List only product source files read directly during this invocation. Do not cite requirements, Explore artifacts, QA cases, tests, plans, generated files, or documentation as product source. Omitting the section, repeating it, setting `independently_read` to false, or providing an empty/non-product path list makes the proposal invalid.
10. **Write case delta YAML** — to `qa/changes/<change-id>/cases/<module>/case.yaml`
11. **Self-review case delta YAML** — validate schema; **case.yaml MUST NOT contain advisory metadata** (see below)
12. **Hand off** — report completion; orchestrator invokes `aa-case-reviewer`

### case.yaml boundary (Explore)

**MUST NOT** persist in `case.yaml`:

- advisory IDs (`WL-*`, `PH-*`, `KPI-*` as trace fields)
- `evidence_ids[]`, `adopted[]`, `override[]`, `gap[]`
- `explore`, `advisory_input`, or Reconcile blocks
- `test_strategy`, `pitfall_ref`, `assertion_intent`, or any `OQ-*` id

**Only** user-approved case business fields (priority, type, assertions, automation, etc.).

Explore disposition lives **only** in `proposal.md` (`## Explore Input`).

**Readiness:** generated `case.yaml` must not contain Explore Input / evidence_ids / WL-* / PH-* metadata keys.

---

## Clarifying Question Strategy

The 8 categories below are a **coverage checklist**, not a questionnaire. Do not ask them in a batch.

**Rules:**

- Ask **one question at a time**, in the order that best reduces uncertainty.
- If a category can be inferred from project context, state the inference and ask for confirmation instead of asking an open-ended question.
- Only move to the next question when the current one is answered.
- Prefer multiple-choice questions when possible.

**open_questions deduplication (when advisory `done`):**

Before starting Step 4, partition `open_questions_for_case_design[]` from the advisory:

- `answered` = items where `status = "answered"` (legacy: `answer != null` and `answered_via = "explore"`) → treat as already-established context for that specific `pitfall_ref`; do not re-ask that exact pitfall. **Do NOT mark the whole category pre-satisfied** — each item only resolves the assertion intent for one pitfall, not the entire `success_assertions` / `exception_scenarios` / `out_of_scope` category. In `interactive` mode, still ask the category's broader question in Step 4, citing the already-resolved pitfalls as established context rather than re-deciding them.
- `deferred` = items where `status = "deferred"` → do not assert this pitfall as accepted behavior; use neutral language and list it as unresolved in `proposal.md`.
- `pending` = items where `status = "unanswered"` or legacy `answer == null` → in `interactive` mode, merge these into the category queue and ask them when their category comes up; in `autonomous` mode, STOP because `aa-explore` should have resolved them via `auto_default`.

Do NOT repeat an advisory question (same `pitfall_ref`) that was already answered in `aa-explore`. The user has already answered it; use it silently as clarification context when covering the broader category.

**Assertion intent from propagated guidance (when advisory `done`):**

When a `priority_hint`, `watchlist` item, or `suggested_scenario` carries `assertion_intent` + `open_question_ref`, that pair is the **authoritative test directive** for that pitfall — prefer it over neutral pitfall descriptions or your own inference:

| `assertion_intent` | Case-design action |
|---|---|
| `assert_ideal` | Design cases that assert the **ideal** behavior (expected rejection / correct constraint) — do NOT write cases that merely reproduce the bug as acceptable |
| `assert_known_bug` | Design cases that assert the **current** buggy behavior (document as known limitation) |
| `ignore` | Do not generate assertions for this pitfall; if a `priority_hint` was correctly removed in explore reconciliation, do not reintroduce coverage via `suggested_scenarios` |
| absent / `undecided` | Fall back to open_question answer if present; otherwise ask during the matching clarifying category |

If `priority_hint.hint` text still reads like neutral pitfall reproduction but `assertion_intent` is set, **trust `assertion_intent`** and rewrite case expectations accordingly — flag in `proposal.md` Explore Input if hint and intent appear contradictory (reviewer signal).

**`priority_hints` vs `suggested_scenarios` (how to consume each):** `priority_hints` tell you **测什么方向** (which pitfalls to cover + assertion direction); `suggested_scenarios` are **怎么测** drafts (case-ready scenarios, each carrying `priority_hint_ref`). Use a PH to decide coverage/direction, and turn each SS into one concrete `case.yaml` entry — a single PH may fan out into several SS along distinct input/assertion dimensions. If an SS only restates its PH with no extra execution detail (no concrete input value, no specific asserted status/field), treat it as a thin pointer and derive the case's concrete steps/assertions yourself from the PH + module contract; do not blindly copy the SS description into the case.

**`test_strategy` as a macro-plan starting point (when advisory `done`):**

`aa-explore` may populate `advisory.json.test_strategy` (scope / data_focus / layer_recommendation / approach) as an evidence-derived proposal. This skill still owns the macro categories (`module_confirmation`, `change_type`, `test_types`, `data_needs`, `target_selection_depth`, `out_of_scope`) — `test_strategy` does not pre-answer them. Instead, when asking each of those categories, lead with the relevant `test_strategy` slice as a recommendation to confirm or override, rather than asking open-ended:

> "Explore 基于代码证据建议覆盖 API + E2E，并对 UserCreate/UserUpdate schema 增加 Fuzz（依据：发现用户输入 schema SC-SCHEMA-002/004）。是否采用此方案，还是需要调整？"

The user's confirm/override response is what gets recorded as `adopted[]` / `override[]` in Step 5 (Reconcile) — same mechanism as priority_hint/watchlist adoption. If `test_strategy` is absent or empty, ask the category normally with no proposal to lead with.

**The 8 categories must be covered before case delta generation — but must NOT be asked as a batch:**

| # | Category | What to Establish |
|---|---|---|
| 1 | Module confirmation | Confirmed owner module path |
| 2 | Change type | ADDED / MODIFIED / REMOVED |
| 3 | Test types | API / E2E / Fuzz / Performance |
| 4 | Data needs | Required entities and their states |
| 5 | Success assertions | Observable outcomes for each test type |
| 6 | Exception scenarios | Edge cases and error paths to include |
| 7 | Target selection + depth | Which test types (`type`) to generate? For Fuzz: which endpoints? For Performance: which capability + what P95 / error-rate thresholds? |
| 8 | Out of scope | Explicit exclusions |

> When the user selects **Fuzz**, ask which input endpoints need robustness testing. When the user selects **Performance**, ask which capability is high-frequency/core and what the acceptable P95 latency and error-rate thresholds are (no defaults — thresholds must be user-confirmed).

**Category 3 hard rule — all four layers must be offered, not just recommended ones:**

- The coverage-approach options presented to the user MUST include an explicit Fuzz/Performance opt-in entry (like option E in the example below), **even when Explore does not recommend those layers**. Presenting only API/E2E depth variants (e.g. 方案 A/B/C all within API+E2E) without a Fuzz/Performance entry is a process deviation.
- When the user declines a layer, record the decision — silence is not a decision. `proposal.md` `## Test Types Considered` captures all four layers with `selected | declined + reason`.
- An Explore `test_strategy.layer_recommendation` with `recommended: false` is a lead-in for the question ("Explore 不建议 Fuzz，理由是 X — 是否同意？"), never a reason to skip asking.

### Test Layer Decision Tree

For every case, apply this rule before assigning its `type`:

```
Behavior to verify:
├─ Single HTTP request + response assertion          → type: API         (tests/api/)
│    status codes / response body / permission codes / field validation / error-code matrix
├─ Multi-step browser interaction + UI feedback      → type: E2E         (tests/e2e/)
│    form submission triggers live list refresh / confirmation dialog interaction
├─ Complex input / schema / boundary / parser        → type: Fuzz        (tests/fuzz/)
│    robustness of a user-input endpoint (no crash, no 5xx, schema-valid input not wrongly rejected)
└─ High-frequency / core / heavy query / key path    → type: Performance (tests/perf/)
     absolute thresholds (P95 / error_rate), no historical baseline
```

Hard rules:
- If API can cover a functional assertion, do NOT design it as E2E.
- Error-code matrices go to API.
- E2E keeps 1 happy-path case + at most 2–3 critical exception flows.
- The same assertion point MUST NOT appear across cases of different `type`.
- **Fuzz does not replace functional assertions**: every Fuzz case MUST set `related_cases` pointing at the corresponding functional (API) case.
- **Performance MUST carry execution identity and thresholds**: a Performance case must set non-empty `automation.performance.scenario.capability` and `automation.performance.scenario.endpoint`, plus `automation.performance.scenario.thresholds`; omission is invalid (reviewer blocker).
- **Fuzz / Performance are additive cases** — they do not cancel the API/E2E functional coverage of the same endpoint.

**Good question example:**

> "For this change, which test types should we cover?
>
> A. E2E only
> B. API only
> C. API + E2E
> D. API + E2E + exception cases
> E. Add Fuzz (input robustness) and/or Performance (high-frequency endpoints)"

**Forbidden:**

> "Please answer these 8 questions: (1) change type, (2) test types, (3) data needs..."

---

## Process Flow

```dot
digraph brainstorming_for_qa {
    "Requirement / Ticket" [shape=doublecircle];
    "Derive change ID" [shape=box];
    "Explore qa/cases\ntests, qa/knowledge" [shape=box];
    "Identify target module" [shape=box];
    "Ask clarifying questions\n(one at a time)" [shape=box];
    "All 8 categories covered?" [shape=diamond];
    "Internal risk analysis" [shape=box];
    "Visual companion useful?" [shape=diamond];
    "Offer Visual Companion\n(own message, no other content)" [shape=box];
    "Propose 2-3 coverage approaches" [shape=box];
    "User approves approach?" [shape=diamond];
    "Write proposal.md" [shape=box];
    "Write case delta YAML" [shape=box];
    "Case delta self-review" [shape=box];
    "Review passed?" [shape=diamond];
    "Fix inline" [shape=box];
    "Hand off to aa-case-reviewer" [shape=doublecircle];

    "Requirement / Ticket" -> "Derive change ID";
    "Derive change ID" -> "Explore qa/cases\ntests, qa/knowledge";
    "Explore qa/cases\ntests, qa/knowledge" -> "Identify target module";
    "Identify target module" -> "Ask clarifying questions\n(one at a time)";
    "Ask clarifying questions\n(one at a time)" -> "All 8 categories covered?";
    "All 8 categories covered?" -> "Ask clarifying questions\n(one at a time)" [label="no, keep asking"];
    "All 8 categories covered?" -> "Internal risk analysis" [label="yes"];
    "Internal risk analysis" -> "Visual companion useful?";
    "Visual companion useful?" -> "Offer Visual Companion\n(own message, no other content)" [label="yes"];
    "Visual companion useful?" -> "Propose 2-3 coverage approaches" [label="no"];
    "Offer Visual Companion\n(own message, no other content)" -> "Propose 2-3 coverage approaches";
    "Propose 2-3 coverage approaches" -> "User approves approach?";
    "User approves approach?" -> "Propose 2-3 coverage approaches" [label="no, revise"];
    "User approves approach?" -> "Write proposal.md" [label="yes"];
    "Write proposal.md" -> "Write case delta YAML";
    "Write case delta YAML" -> "Case delta self-review";
    "Case delta self-review" -> "Review passed?";
    "Review passed?" -> "Fix inline" [label="no"];
    "Fix inline" -> "Case delta self-review";
    "Review passed?" -> "Hand off to aa-case-reviewer" [label="yes"];
}
```

**The terminal state is handoff to the orchestrator or user with:**
- `change_id`
- case file path
- next recommended skill: `aa-case-reviewer`

Do NOT invoke `aa-case-reviewer` directly when running under `aa-workflow`. Do NOT invoke aa-api-plan, aa-e2e-plan, aa-api-codegen, aa-e2e-codegen, or aa-archive from aa-case-design.

---

## The Process

### Step 1: Derive Change ID

Every brainstorm must be tied to a change ID. This ID is the directory name under `qa/changes/`.

**Format:**

```
<TICKET-ID>-<short-kebab-description>
```

**Examples:**

| Input | Change ID |
|---|---|
| "REQ-001 add order receive" | `REQ-001-order-receive` |
| "BUG-234 fix empty state on warehouse list" | `BUG-234-empty-state` |
| "TASK-89 support bulk import for items" | `TASK-89-bulk-import` |

**If no ticket ID is provided**, ask the user once:

> "What ticket or requirement ID should I use for this change? (e.g. REQ-001, BUG-234, TASK-89) Or I can generate a date-based ID like `QA-20260601-order-receive`."

Do not proceed until you have an ID.

---

### Step 2: Explore QA Context

Before asking any questions, explore the existing QA state. This prevents proposing cases that already exist and ensures you understand current module ownership and automation coverage.

**Check in this order** (all paths are optional on first-time QA setup — record a warning and continue if missing):

```
qa/cases/                         → module structure and ownership
qa/cases/<likely-module>/case.yaml → existing case IDs, style, naming conventions
tests/api/                        → current API automation coverage
tests/e2e/                        → current E2E automation coverage
qa/knowledge/                     → data factory patterns, natural step conventions
qa/changes/                       → in-progress or recently archived changes (overlap check)
```

**What to derive:**

- Existing module ownership — where does this requirement likely live?
- Existing case IDs — what is the highest existing ID to avoid collision?
- Possible duplicate cases — are there overlapping cases already in `qa/cases/`?
- Current API / E2E automation coverage — what is already automated?
- Domain-specific data factories or natural step templates in `qa/knowledge/`
- Related in-progress changes — any conflicts or dependencies?

**If the requirement spans multiple independent modules:**

> "This requirement seems to span `<module-A>` and `<module-B>`. These are independent modules. I recommend scoping this brainstorm to one module at a time. Which should we start with?"

---

## Knowledge Source Boundary

| Source | Purpose | aa-case-design may |
|---|---|---|
| `qa/knowledge/` | Project documentation: business terms, page flows, natural step conventions, historical QA notes | Read only |
| `.aa/data-knowledge.yaml` | Formal data capability registry used by planning/codegen gates **and** the closed MRC key set | Read only; must NOT create or modify |

If missing data capabilities are discovered during brainstorming, record them in:

- `proposal.md` under **Data Needs**
- case YAML `test_data` as natural language needs

Formal promotion into `.aa/data-knowledge.yaml` is handled outside `aa-case-design` (by the user or a dedicated data knowledge update process).

### MRC closed-key discipline (mandatory)

Minimum Required Coverage keys are a **closed set**. Do **not** freely invent MRC keys that are absent from data-knowledge / declared journey sets.

| MRC category | Allowed key sources |
|---|---|
| `data_integrity` / `negative` | `entities.<entity>.constraints.<key>` from `.aa/data-knowledge.yaml`, or `auth.*` / `auth_matrix.*` |
| `e2e` / `e2e_if_enabled` | Declared journey keys already present in advisory MRC / project journey set (A4 denominator) — do not invent new journey names |
| `api` | Free-form operation names (not a DataKnowledge leaf) |

**When a needed `data_integrity` / `negative` / auth / journey key is missing from the closed set:**

1. Do **not** write the invented key into `trace/minimum-coverage-matrix.yaml` or `trace.minimum_required_coverage` as if it were already known.
2. Emit a knowledge proposal under `qa/changes/<change-id>/plans/data-knowledge.proposal.<layer>.yaml` with the missing key listed in `discovered_candidates` (and describe it under `proposal.md` Data Needs).
3. Leave the MRC matrix row unmapped / out of scope until a human promotes the proposal into L1 — never bypass the proposal path to score “full coverage”.

Use the complete runtime envelope below for either authorized layer, changing only
source-backed values and the output suffix (`api` or `e2e`):

### Canonical missing-key proposal

```yaml
schema_version: "1"
based_on_l1_version: 1
mode: delta
accounts: {}
auth: {}
entities: {}
auth_matrix: {}
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
discovered_candidates:
  - id: DK-CASE-USER-001
    knowledge_key: entities.user.constraints.password_reset_requires_current_password
    evidence: "app/api/v1/user/user.py:reset_password"
needs_review:
  - Confirm the source-backed constraint before promotion into L1.
promotion_checklist:
  - "Run aa knowledge validate --change <change-id> before promotion."
```

For a delta, set `based_on_l1_version` to the current L1 `version`. For a bootstrap
proposal, set it to `null` and set `mode: bootstrap`. Validate the entire document
against `DataKnowledgeProposal`; do not emit only `discovered_candidates` or an inner
entity fragment.

Keep every entity entry structurally meaningful: an `entities.<name>` mapping must
contain at least one non-empty `constraints` or `required_fields` entry. If no such
source-backed fact exists, omit that entity entirely and leave `entities: {}`; never
emit placeholders such as `entities: {role: {constraints: {}}}`.

Self-review must fail if any `data_integrity` / `negative` / journey MRC key was invented without a corresponding knowledge proposal.

---

### Step 3: Identify Target Module

Ask **one module confirmation question at a time**.

If the requirement appears to span multiple independent modules, ask the user to choose the **first module to scope**. Do not force all modules into one case delta.

Example:

> "This looks like it belongs under `qa/cases/warehouse/inbound/`. Is that correct, or should another module own these cases?"

Wait for confirmation before continuing.

---

### Step 4: Ask Clarifying Questions One at a Time

Follow the strategy in **Clarifying Question Strategy** above.

Cover all 8 categories before proposing coverage approaches. One question per message.

---

### Step 5: Internal Risk Analysis

After clarifying questions, analyze risks internally. Do NOT dump this full analysis to the user. Use it to inform the coverage approaches you propose.

**Consider:**

- Business state transitions and side effects
- Required data state (entities that must exist, statuses, ownership)
- Auth and permission boundaries
- Repeated actions and idempotency
- Invalid input and boundary conditions
- External dependencies (third-party services, async jobs)
- Async UI behavior (loading states, polling, delayed updates)
- API / UI consistency (are the same business rules enforced at both layers?)
- Regression risk (what existing behavior could this break?)
- Flaky automation risk (timing issues, dynamic data, environment-sensitive steps)

---

### Step 6: Optional Visual Companion

Consider offering the visual companion **only** when the upcoming question involves genuinely visual content.

**Use the browser for:**

- UI flow comparison (multiple E2E paths side by side)
- E2E path diagram (step-by-step user flow visualization)
- Test coverage matrix (module × test type grid)
- Data flow diagram (how data moves between API, DB, and UI)
- Page interaction mockup (which elements are under test)

**Use the terminal for:**

- Module confirmation
- Change type confirmation (ADDED / MODIFIED / REMOVED)
- Data needs clarification
- Success assertion text
- Exception scope decisions
- Automation target yes/no

**Offering the companion** (this offer MUST be its own message):

> "Some of what we're working on might be easier to explain if I can show it in a browser — for example, an E2E path diagram or a coverage matrix. Want to try it? (Requires opening a local URL)"

If they agree, read: `skills/aa-case-design/visual-companion.md`

**Visual companion is optional and must never block case design.** If the browser, local URL, or companion tooling is unavailable, continue with text-only coverage approaches. Do not wait for visual setup before writing `proposal.md` or case YAML.

---

### Step 7: Propose 2–3 QA Coverage Approaches (`interactive` only)

Present 2–3 options with trade-offs and your recommendation. Lead with your recommendation. Wait for explicit approval before writing any file.

In `autonomous` mode, skip this section: select one conservative approach from `test_strategy` + evidence, record the rationale and `generation_mode: autonomous` in `proposal.md`, and continue directly to writing outputs.

The option list MUST include an explicit Fuzz/Performance opt-in option even if the recommended approach does not include them. If Explore recommends `recommended: false` for Fuzz or Performance, cite that rationale and ask the user to confirm or override it.

**Example:**

> **A. E2E only**
> - Covers the user workflow end-to-end.
> - Fast to create and easy to maintain.
> - Does not verify backend state or business rules independently.
>
> **B. API + E2E** ← Recommended
> - API verifies backend state transitions and business rules.
> - E2E verifies the user-visible path and UI consistency.
> - Recommended for state-changing workflows where both layers matter.
>
> **C. API + E2E + exception cases**
> - Best regression coverage for high-risk features.
> - Requires more data setup and more cases.
> - Appropriate when this feature is critical path or has a history of regressions.
>
> **D. Add Fuzz and/or Performance**
> - Fuzz hardens user-input endpoints such as create/update schemas.
> - Performance validates high-frequency or heavy-query paths with user-confirmed P95/error-rate thresholds.
> - Add only when these non-functional targets are in scope for this change.
>
> Recommendation: **B**, because this is a state-changing workflow.

---

### Step 8: Write proposal.md

After the user approves the coverage approach, write:

```
qa/changes/<change-id>/proposal.md
```

`proposal.md` records the approved QA proposal. It is a **process asset**, not a main asset. It must not be merged into `qa/cases/`. It will be archived with the change.

**Required format:**

```markdown
# Proposal: <change-id>

## Why

Why this QA change is needed.

## Test Basis

- Requirement ID: <requirement-id>
- Feature Name: <feature-name>
- Source: GitLab issue / product requirement / OpenSpec requirement / user story
- Target Module: <module>
- Target Case File: `qa/cases/<module>/case.yaml`

## What to Test

What behavior, module, business flow, or interface capability this change covers.

## Product Source Verification

- independently_read: true
- reviewed_source_files:
  - `<project-relative product source path>`

| Claim | Source file | Evidence checked |
|---|---|---|
| `<behavior used to design a case>` | `<project-relative path>` | `<route/handler/schema/component symbol or line-level observation>` |

This section records aa-case-design's own source read. Do not copy Explore's
`source_code_read` flag or advisory evidence into this section as proof.

## Out of Scope

What is explicitly excluded from this change.

## Confirmed Coverage Approach

The selected coverage approach and rationale.

Example:
> 选择 API-only 覆盖，因为当前需求只验证 logout 接口最小响应级行为，不扩展 token 吊销、多设备登出和审计链路。

## Test Conditions

| Condition ID | Condition | Source | Priority | Risk Level | Design Technique |
|---|---|---|---|---|---|
| COND-<MODULE>-001 | <test condition description> | <requirement-id> | P0 | high | use_case |
| COND-<MODULE>-002 | <test condition description> | <requirement-id> | P1 | medium | negative |

## Quality Risks

| Risk | Likelihood | Impact | Level | Mitigation |
|---|---|---|---|---|
| <risk description> | 1–5 | 1–5 | low/medium/high/critical | <mitigation> |

## Test Types

- API
- E2E

## Test Types Considered

All four layers must appear — this table is the evidence that Fuzz/Performance were offered to the user.

| Layer | Decision | Reason |
|---|---|---|
| API | selected | <why> |
| E2E | selected | <why> |
| Fuzz | declined | <user's reason, e.g. 输入 schema 简单，本期不做健壮性测试> |
| Performance | declined | <user's reason, e.g. 无高频路径，无性能指标要求> |

## Layer Rationale

For each case in this change, record the layer assignment and rationale.
Format is fixed — reviewer uses `case_id` to cross-check each case's `type`.

- TC_MENU_001: API
  - reason: <why API is sufficient, e.g. validates status code / response body directly>

- TC_MENU_E2E_001: E2E
  - reason: <why E2E is required, e.g. validates browser interaction and live UI update>

- TC_MENU_FUZZ_001: Fuzz
  - reason: <endpoint accepts user-input schema, needs robustness; relates TC_MENU_001>

- TC_MENU_PERF_001: Performance
  - reason: <high-frequency query endpoint, P95 < 200ms; relates TC_MENU_002>

## Explore Input

<!-- Include this section ONLY when phases.explore.status == done. Otherwise write: _skipped (no advisory)_ -->

- advisory: `explore/advisory.json` (status: done | degraded)
- source_code_read: true | false
- test_strategy: adopted as-is | adopted with overrides | not provided
  - proposal: <test_strategy.approach, if present>
  - override: [<layer/scope item>: reason — <rationale for overriding this part of the proposal>]
- open_questions (assertion-intent, per pitfall):
  - answered in explore: OQ-001 (pitfall_ref: SC-RBAC-001, success_assertions) — assert_known_bug, "<answer>"
  - pending (answered here): OQ-002 (pitfall_ref: SC-SCHEMA-005, exception_scenarios) — assert_ideal, "<answer given in this session>"
  - skipped: OQ-004 (pitfall_ref: SC-SCHEMA-004, out_of_scope) — left unanswered
- adopted: [PH-001 → TC_MODULE_001, WL-002 → TC_MODULE_003]
- override: [PH-002: reason — <rationale for overriding this priority hint>]
- gap: [macro categories clarified here beyond what test_strategy covered]

## Data Needs

Required data factories, inputs, and outputs.

## Success Assertions

Natural language expected outcomes.

## Exception Scenarios

Approved exception cases, or explicitly excluded ones.

## Entry Criteria

- Requirements are clear enough to derive test conditions.
- Target module is confirmed.
- Target case file path is confirmed.
- Required fixtures / factories can be created or reused.

## Exit Criteria for This Phase

- `proposal.md` is written.
- `cases/<module>/case.yaml` is generated and self-reviewed.
- Each case is traceable to `requirement_id` and `test_condition_id`.
- Test targets (`type`) are identified and confirmed by the user (`automation.confirmed_by`).
- Ready for `aa-case-reviewer`.

## Downstream Exit Criteria

- After case review passes, `plans/*.md` can be generated by downstream plan skills (`aa-api-plan`, `aa-e2e-plan`).
- After codegen and execution, traceability can be updated by downstream execution/archive phases (`aa-run`, `aa-archive`).
```

`proposal.md` is a **process asset**. It must NOT be merged into `qa/cases/`. It will be archived with the change. It prevents aa-api-plan, aa-e2e-plan, and aa-api-codegen/aa-e2e-codegen from drifting from the approved scope.

---

### Step 9: Write Case Delta YAML

After writing `proposal.md`, generate and write the semantic case delta YAML.

**Output path:**

```
qa/changes/<change-id>/cases/<module>/case.yaml
```

**Target stable case file (for archive merge):**

```
qa/cases/<module>/case.yaml
```

This YAML is the **source of truth** for this change. It is a delta file even though it is named `case.yaml`.

- Do NOT generate Markdown case files as the source of truth.
- The case YAML describes **what to test**, not how to implement the test code.

Follow the **Case YAML Output Contract** section exactly. Apply **Delta Operation Rules** to decide `added` / `modified` / `removed`.

---

### Step 10: Case Delta Self-Review

After writing the YAML, self-review it using the **Case Delta Readiness Check** below.

If issues are found: fix them inline and re-check. Do not ask the user for review until the YAML passes all checks.

Once the YAML passes, show the user a brief summary:

```
Files written:
  qa/changes/<change-id>/proposal.md
  qa/changes/<change-id>/cases/<module>/case.yaml

Case delta:
  ADDED: N cases
  MODIFIED: M cases
  REMOVED: K cases

Coverage approach: <A/B/C> — <rationale>
```

Then report:

> "Case design is complete. Next recommended step: `aa-case-reviewer`."

---

### Step 11: Hand off to orchestrator

After the case YAML is written and self-reviewed, **do not invoke `aa-case-reviewer` directly**. Instead, report completion to the caller (orchestrator or user) and indicate that `aa-case-reviewer` should be run next.

If operating standalone (no orchestrator), the user may explicitly ask to invoke `aa-case-reviewer`. In orchestrated runs, the orchestrator manages the review lifecycle and will invoke the reviewer as Phase 2.

Pass the change ID and case YAML path:

```
change-id: <change-id>
case file: qa/changes/<change-id>/cases/<module>/case.yaml
```

Do NOT generate plans in this skill. Planning is done by aa-api-plan and aa-e2e-plan after case review passes.

---

## QA Change Directory Protocol

The full directory structure for a QA change:

```
qa/
├── cases/
│   └── <module>/
│       └── case.yaml               ← stable main asset (merged after archive)
│
├── changes/
│   └── <change-id>/
│       ├── .qa.yaml                ← change metadata and workflow state
│       ├── proposal.md             ← why / what / coverage approach (process asset)
│       ├── cases/
│       │   └── <module>/
│       │       └── case.yaml       ← case delta (this change's added/modified/removed)
│       ├── plans/                  ← created by aa-api-plan / aa-e2e-plan, NOT by aa-case-design
│       │   ├── api-plan.md         ← API test implementation plan (Markdown)
│       │   └── e2e-plan.md         ← E2E test implementation plan (Markdown)
│       ├── execution/
│       │   ├── api-result.json
│       │   ├── e2e-result.json
│       │   ├── summary.md
│       │   └── execution-manifest.yaml
│       ├── review/
│       │   ├── case-review.json
│       │   ├── case-review-summary.md
│       │   ├── api-plan-review.json
│       │   ├── api-plan-review-summary.md
│       │   ├── plan-review.json
│       │   └── plan-review-summary.md
│       └── trace/
│           └── traceability-matrix.yaml
│
└── archive/
```

**Mapping to OpenSpec concepts:**

| Superpowers for QA | OpenSpec |
|---|---|
| `qa/changes/<id>/.qa.yaml` | `openspec/changes/<id>/.openspec.yaml` |
| `qa/changes/<id>/proposal.md` | `openspec/changes/<id>/proposal.md` |
| `qa/changes/<id>/plans/*.md` | `openspec/changes/<id>/tasks.md` |
| `qa/changes/<id>/cases/<module>/case.yaml` | `openspec/changes/<id>/specs/<module>/spec.md` |
| `qa/cases/<module>/case.yaml` | `openspec/specs/<module>/spec.md` |
| `added / modified / removed` | `ADDED / MODIFIED / REMOVED Requirements` |
| `aa-archive` | `openspec archive` |

**Asset types:**

| Asset | Type | Merged on archive? |
|---|---|---|
| `qa/cases/<module>/case.yaml` | Main asset | — (target of merge) |
| `qa/changes/<id>/cases/<module>/case.yaml` | Delta (process) | Yes → merged into main |
| `qa/changes/<id>/proposal.md` | Process asset | No → archived only |
| `qa/changes/<id>/plans/*.md` | Process asset | No → archived only |
| `qa/changes/<id>/execution/` | Process asset | No → archived only |
| `qa/changes/<id>/review/` | Process asset | No → archived only |
| `qa/changes/<id>/trace/` | Process asset | No → archived only |

---

## .qa.yaml Template

`.qa.yaml` stores **stable change metadata** (change id, targets, feature name). `workflow-state.yaml` stores **mutable runtime phase state** and is the gate source for orchestration.

The optional `workflow` section below is **informational only** — do **not** use `.qa.yaml.workflow` as a gate source.

```yaml
schema_version: "1.0"
schema: case-driven
created_at: "YYYY-MM-DDTHH:mm:ssZ"

change:
  change_id: REQ-001-order-receive
  requirement_id: REQ-001
  feature_name: order-receive
  status: draft

targets:
  cases:
    - module: warehouse.inbound
      change_case_file: qa/changes/REQ-001-order-receive/cases/warehouse/inbound/case.yaml
      target_case_file: qa/cases/warehouse/inbound/case.yaml

workflow:   # informational only — not a gate source
  current_step: aa-case-design
  next_step: aa-case-reviewer
```

---

## Case YAML Output Contract

> **Schema source of truth:** the complete, enforced field contract for `case.yaml`
> lives in `assurance_agent/artifacts/models/cases.py`. Runtime `finalize` validates
> authored files against that model. The example below is illustrative only.

The generated case delta MUST be YAML. It contains natural language QA cases.

`change` metadata lives in `.qa.yaml`. `proposal` (coverage approach) lives in `proposal.md`. The case file itself contains only cases.

**Required path:**

```
qa/changes/<change-id>/cases/<module>/case.yaml
```

**Target stable case file:**

```
qa/cases/<module>/case.yaml
```

**Top-level structure (case.yaml contains ONLY these keys):**

```yaml
schema_version: "1.0"

added: []
modified: []
removed: []
```

Every case under `added` or `modified` must satisfy the field contract in `assurance_agent/artifacts/models/cases.py`. There is no `automation.target` field — top-level `type` is the single source of truth for the test target.

`trace` is required on every case. When `risk-advisory/advisory.json` or `explore/advisory.json` contains `minimum_required_coverage`, every required MRC item must be mapped to at least one case via `trace.minimum_required_coverage`; do not rely on title/name inference. Downstream archive / execution may enrich other trace fields.

MRC keys must obey **MRC closed-key discipline** above: never invent closed-category keys; unknown keys require a knowledge proposal (`discovered_candidates`), not a silent matrix entry.

Also write `qa/changes/<change-id>/trace/minimum-coverage-matrix.yaml`:

```yaml
- mrc_id: MRC-API-006
  key: refresh_api
  required: true
  covered_by_cases: [TC_API_012, TC_API_013]
  status: covered | skipped_by_scope
  skip_reason: null
```

`mrc_id and key must each be unique across the entire matrix`. When two
categories describe the same obligation, keep one row and map all relevant
cases to it; do not duplicate the key under a second `mrc_id`.

**`type` → `framework` → output directory (must match):**

| `type` | `automation.framework` | output dir |
|--------|------------------------|------------|
| API | pytest | `tests/api/` |
| E2E | pytest-playwright | `tests/e2e/` |
| Fuzz | schemathesis | `tests/fuzz/` |
| Performance | locust | `tests/perf/` |

One case has exactly one `type`. Fuzz and Performance are **independent cases** (`type: Fuzz` / `type: Performance`) that link the functional case they harden via `related_cases`. They do **not** replace the functional API/E2E coverage of that endpoint.

**Minimal example (illustrative — see `assurance_agent/artifacts/models/cases.py` for the full contract):**

```yaml
modified:
  - case_id: TC_USER_AUTH_002
    title: 有效登录用户可以成功登出
    status: draft
    priority: P0
    severity: blocker
    type: API
    module: user.auth
    requirement_id: REQ-002
    feature_name: user-logout
    # remaining fields: assurance_agent/artifacts/models/cases.py
```

**Every case under `removed` MUST include:**

```yaml
case_id: <existing-case-id>
reason: <why this case is no longer needed>
```

---

## Delta Operation Rules

When generating `qa/changes/<change-id>/cases/<module>/case.yaml`, decide the operation at generation time.

- Use `added` when the case does not exist in the target case file (`qa/cases/<module>/case.yaml`).
- Use `modified` when the case already exists and this change updates its content.
- Use `removed` when the case already exists but is no longer applicable.

**Do not leave this decision to archive.**

**Before writing the delta:**

1. Read `qa/cases/<module>/case.yaml` if it exists.
2. Compare by `case_id`.
3. Put each case into exactly one of `added`, `modified`, or `removed`.

**Validation rules:**

- `added.case_id` MUST NOT already exist in the target case file.
- `modified.case_id` MUST already exist in the target case file.
- `removed.case_id` MUST already exist in the target case file.

If the target case file does not exist yet, all cases must be `added`.

---

## Natural Language Case Rules

The case YAML contains **user-readable QA cases** optimized for Web display and human review. All fields must be written in natural language.

Case YAML describes what to test. API mapping belongs to `api-plan.md`. E2E locator mapping belongs to `e2e-plan.md` and `tests/e2e/`.

**YAML scalar safety (mandatory):**

- Natural-language list items under `preconditions`, `test_data`, `steps`, `assertions`, `postconditions`, and `edge_cases` MUST be YAML strings.
- If a list item contains a colon followed by a space (for example `name: "qa_dept_dup"` or `部门 1: "x"`), quote the entire item:
  - Good: `- '第一次 name: "qa_dept_dup"'`
  - Good: `- '部门 1: "qa_dept_filter_match"'`
  - Bad: `- 第一次 name: "qa_dept_dup"`
- Do not rely on YAML parsing `- name: "x"` as an object inside natural-language fields. These fields are string arrays, not maps.
- When in doubt, quote the entire scalar with single quotes and escape inner single quotes by doubling them.

**Allowed:**

```yaml
objective: 验证已登录用户调用登出接口时，系统返回登出成功。

summary: >
  已登录用户携带有效 access_token 调用登出接口时，系统应返回登出成功。

preconditions:
  - 系统中存在一个已激活用户

test_data:
  - 使用 active_user_fixture 创建或获取已激活用户

steps:
  - 携带有效 access_token 调用登出接口
  - 查看接口响应结果

assertions:
  - 接口返回 HTTP 200
  - 响应业务码为 200

postconditions:
  - 本 case 不验证 token 吊销状态

edge_cases:
  - 未携带 Token 调用登出接口

related_cases:
  - TC_USER_AUTH_003

automation:
  required: true
  target: API
  framework: pytest
  suggested_file: tests/api/test_auth_api.py
  status: planned

regression:
  candidate: true
  tier: smoke
  rationale: P0 核心认证链路。
```

**Forbidden in case YAML — these belong in `plans/api-plan.md` or `tests/`:**

- `method`, `path`, `headers` mappings (`POST /api/v1/base/logout`)
- `Authorization` header values (`Bearer ${token}`, `Bearer invalid.token.string`)
- Concrete request URLs with environment host (`https://prod.example.com/api/...`)
- Hard-coded auth tokens, real credentials, or secrets
- `pytest` test code or fixtures (`def test_`, `assert`, `@pytest.fixture`)
- `httpx` / `requests` call code
- Playwright test code (`page.locator(...)`, `await page.click(...)`)
- CSS selectors (`.btn-receive`, `#order-form`)
- XPath (`//button[@class='receive']`)
- `data-testid` attributes
- Raw SQL data setup
- Execution history or test run results
- Attachments, screenshots, or videos

**Where execution details belong:**

```
plans/api-plan.md    → method / path / auth / API mapping / pytest commands
plans/e2e-plan.md    → natural steps / environment / Playwright commands
tests/api/           → actual pytest implementation
tests/e2e/           → actual Playwright implementation
execution/*.yaml     → test run results
```

---

## Case Priority and Severity Rules

Every generated case MUST include both `priority` and `severity`.

**Priority** — execution and release priority:

| Level | Meaning |
|---|---|
| P0 | Smoke / critical path — must always pass before release |
| P1 | Core business logic — high confidence required |
| P2 | Important but not blocking release |
| P3 | Edge case / low risk |

**Severity** — business impact if this behavior fails:

| Level | Meaning |
|---|---|
| `blocker` | Completely prevents core user flow |
| `critical` | Major feature broken, no workaround |
| `major` | Feature degraded, workaround exists |
| `minor` | Minor UX or edge case issue |

**Default mapping:**

| Priority | Severity | Typical use |
|---|---|---|
| P0 | blocker / critical | Release-blocking core flow |
| P1 | critical / major | Important regression or high-value feature |
| P2 | major / minor | Normal coverage |
| P3 | minor | Optional or low-risk edge case |

If uncertain, choose the lower priority and explain the assumption in `proposal.md` or in the case notes.

---

## Risk-based Regression Rules

Every case under `added` or `modified` MUST include a `regression` block (field contract: `assurance_agent/artifacts/models/cases.py`).

**Tier definitions:**

| Tier | When to use |
|---|---|
| `smoke` | Release-blocking core happy paths (P0). Must pass before any deployment. |
| `sanity` | Focused validation after small changes. Key scenarios only. |
| `regression` | Normal recurring coverage. Standard regression suite. |
| `full` | Broad end-to-end coverage. Run periodically or before major releases. |
| `none` | One-off, low-value, or deprecated case. Not included in any suite. |

**`selection_reason` values** (use one or more):

| Value | Meaning |
|---|---|
| `critical_user_journey` | Core user workflow |
| `security_related` | Auth, permissions, sensitive data |
| `high_risk` | Risk level is high or critical |
| `regression_prone` | History of regressions in this area |
| `newly_added_feature` | New behavior not yet stable |
| `contract_boundary` | API contract or integration boundary |

**`maintenance_rule` values:**

| Value | When to use |
|---|---|
| `keep_until_feature_deprecated` | Keep as long as the feature is active |
| `keep_for_one_release` | Remove after one stable release cycle |
| `review_after_refactor` | Re-evaluate when implementation changes |
| `remove_if_automated` | Remove manual case once automated |

**Rules:**

- P0 cases SHOULD be `smoke` or `sanity`.
- P1 cases SHOULD be `regression` or `smoke`.
- P2/P3 cases MAY be `regression`, `full`, or `none`.
- `rationale` must explain the tier selection. Do not leave it blank.
- `candidate: false` means the case is intentionally excluded from regression suites. Always provide a `rationale`.
- `selection_reason` supports risk-based suite selection for Web dashboards and CI pipelines.

**Security rule:** Never include real credentials, real tokens, hardcoded secrets, or environment-specific credentials in any case field. Test data references must use fixture names or generic descriptions.

---

## Test Conditions Rule

Before generating the case delta YAML, derive test conditions from the test basis (requirements, user stories, feature descriptions).

**Test conditions establish the mapping:**

```text
requirement_id → test_condition_id → case_id → plan → test code → execution result
```

**What is a test condition?**

A test condition is a testable statement derived from a requirement. It is higher-level than a test case and links requirements to cases.

Example:

| Condition ID | Condition | Source | Priority | Risk Level | Design Technique |
|---|---|---|---|---|---|
| COND-USER-AUTH-001 | 验证有效登录用户可以成功登出 | REQ-002 | P0 | high | use_case |
| COND-USER-AUTH-002 | 验证未认证登出请求会被拒绝 | REQ-002 | P1 | medium | negative |

**Rules:**

- Derive test conditions internally during brainstorming (Step 5: Internal Risk Analysis).
- Write the final test conditions table in `proposal.md`.
- Every generated case MUST reference `test_condition_id`.
- If `test_condition_id` cannot be derived, use `TBD` and explain why in `proposal.md`.
- One test condition can map to multiple cases (e.g. happy path + negative cases).

---

## Test Design Technique Rule

Every generated case MUST include a `design_technique` field (enum values: `assurance_agent/artifacts/models/cases.py`).

**Technique selection guide:**

| Technique | Use when |
|---|---|
| `use_case` | Testing a user workflow or business flow end-to-end |
| `equivalence_partitioning` | Testing valid vs. invalid input classes |
| `boundary_value_analysis` | Testing numeric limits, min/max values, or range edges |
| `decision_table` | Testing combinations of business rules (if A and B, then C) |
| `state_transition` | Testing lifecycle or status changes (draft → active → closed) |
| `exploratory` | Intentionally open-ended investigation without predefined steps |
| `negative` | Testing auth errors, validation rejections, and invalid states |
| `checklist` | Light-weight coverage of a known list of conditions |

**Rules:**

- Choose ONE primary technique per case.
- The technique should justify the design of `steps` and `assertions`.
- `negative` cases should reference the corresponding happy-path case in `related_cases`.
- `exploratory` cases SHOULD have time-boxed `steps` and a focused `objective`.

---

## Risk-based Testing Rule

Every generated case MUST include a `risk` block (field contract: `assurance_agent/artifacts/models/cases.py`).

**Risk level mapping:**

| likelihood × impact | level |
|---|---|
| ≥ 15 | `critical` |
| 8 – 14 | `high` |
| 4 – 7 | `medium` |
| 1 – 3 | `low` |

**Use risk to justify:**

- `priority` — high-risk cases should be P0 or P1
- `severity` — critical/high impact maps to `blocker` or `critical`
- `automation.required` — true for high/critical risk cases
- `regression.tier` — high-risk cases should be in `smoke` or `regression`

**Rules:**

- Risk assessment is done internally. Do NOT ask the user to score risks.
- If uncertain, default to `likelihood: 3, impact: 3, level: medium`.
- `rationale` must explain the scoring. One sentence is sufficient.
- Cross-check: if `priority: P0` but `risk.level: low`, document the mismatch in `proposal.md`.

---

## Downstream Plans

`aa-case-design` does **NOT** generate plans.

After case review passes:

- `aa-api-plan` generates `qa/changes/<change-id>/plans/api-plan.md` (+ related M3 plan files)
- `aa-e2e-plan` generates `qa/changes/<change-id>/plans/e2e-plan.md` (+ related M4 plan files)

**Ownership boundaries:**

| Concern | Owner skill / artifact |
|---|---|
| API method / path / auth mapping | `aa-api-plan` → `plans/api-plan.md` |
| E2E route / selector / data setup mapping | `aa-e2e-plan` → `plans/e2e-plan.md` |
| Test code | `aa-api-codegen` / `aa-e2e-codegen` |
| Execution results | `aa-run` → `execution/*` |

Do **not** embed pytest execution, Playwright execution, or `execution/*-result.json` expectations in case YAML or in this skill. Plans describe implementation intent; they do **not** execute tests or write execution results.

---

## Case Delta Readiness Check

Before invoking aa-case-reviewer, verify that ALL of these are true. Fix any issue inline before proceeding.

**YAML structure:**

1. YAML is valid and parseable.
   - Must be validated with a real YAML parser after writing, not only visually inspected.
   - Pay special attention to natural-language list items containing `: `, quotes, parentheses, or HTTP/status fragments.
2. `schema_version` exists.
3. `added`, `modified`, and `removed` are arrays (not null).
4. case.yaml does NOT contain a top-level `change:` block (belongs in `.qa.yaml`).
5. case.yaml does NOT contain a top-level `proposal:` block (belongs in `proposal.md`).

**Delta operation correctness:**

6. `added.case_id` — does NOT exist in target case file.
7. `modified.case_id` — DOES exist in target case file.
8. `removed.case_id` — DOES exist in target case file.

**Per-case fields (every added / modified case):**

9. `case_id` — exists and matches `TC_[A-Z0-9]+(_[A-Z0-9]+)*_[0-9]{3}` format (underscore-only; hyphens NOT allowed; e.g. `TC_USER_001`, `TC_API_V2_001`, `TC_M4_E2E_001`, `TC_3D_ASSET_001`).
10. `case_id` — unique within this delta.
11. `title` — exists and is not empty.
12. `status` — one of draft, active, deprecated.
13. `priority` — one of P0, P1, P2, P3.
14. `severity` — one of blocker, critical, major, minor.
15. `type` — exists and is one of `API`, `E2E`, `Fuzz`, `Performance`.
16. `module` — exists.
17. `requirement_id` — exists.
18. `feature_name` — exists.
19. `test_condition_id` — exists (TBD is acceptable if documented in proposal.md).
20. `design_technique` — exists and is a valid enum value.
21. `objective` — exists and is not empty.
22. `summary` — exists and is not empty.
23. `risk.likelihood`, `risk.impact`, `risk.level` — all present.
24. `risk.rationale` — present and non-empty.
25. `preconditions` — exists (may be empty list).
26. `test_data` — exists (may be empty list).
27. `steps` — not empty.
28. `assertions` — not empty.
29. `postconditions` — exists (may be empty list, but must be present).
30. `edge_cases` — exists (may be empty list, but must be present).
31. `related_cases` — exists (may be empty list, but must be present).
32. `automation` — has `required`, `framework`, `status` (there is **no** `automation.target` — `type` is the single source of truth).
    - `automation.framework` — one of `pytest`, `pytest-playwright`, `schemathesis`, `locust`, `null`, and MUST match `type`: API→pytest, E2E→pytest-playwright, Fuzz→schemathesis, Performance→locust.
    - `automation.status` — one of `not_automated`, `planned`, `automated`, `flaky`, `deprecated`.
    - When `type == Fuzz`: `automation.fuzz.endpoints` is present and non-empty, and `related_cases` points at ≥1 non-Fuzz case.
    - When `type == Performance`: `automation.performance.scenario.capability` and `automation.performance.scenario.endpoint` are non-empty strings, and `automation.performance.scenario.thresholds` has non-empty `p95_ms` and `error_rate_max` (no placeholders).
33. `regression` — has `candidate`, `tier`, `rationale`.
34. `regression.selection_reason` — present (may be empty list).
35. `regression.maintenance_rule` — present and non-empty.
36. `trace` — exists (may be `{}` or partially filled; see **trace** schema in Case YAML Output Contract).

**Natural language rules:**

37. `steps` contains no CSS selectors, XPath, `page.locator()`, or `data-testid`.
38. `steps` contains no `method/path/headers/Authorization` mappings.
39. `steps` contains no pytest/httpx/requests/Playwright code.
40. `assertions` contains no raw auth tokens or environment-specific URLs.
41. No execution history is embedded in any case field.
42. No attachments, screenshots, or video references in any case field.
43. No real credentials, real tokens, or hardcoded secrets in any field.
44. No `semantic_steps`, `e2e_natural_steps`, `api_intent`, or `automation_targets` fields appear.

**ISTQB traceability rules:**

45. `priority` is consistent with `risk.level` (P0/P1 should have high/critical risk, or mismatch is documented in proposal.md).
46. `automation.required: true` for all cases with `risk.level: high` or `risk.level: critical`.
47. `regression.tier` is consistent with `priority` (P0 → smoke/sanity, P1 → regression/smoke).

**Coverage:**

48. At least one happy path case (P0 or P1) exists in `added` or `modified`.
49. `proposal.md` exists at `qa/changes/<change-id>/proposal.md`.
50. `.qa.yaml` exists at `qa/changes/<change-id>/.qa.yaml`.
51. MRC closed keys: every `data_integrity` / `negative` / journey key in
    `trace/minimum-coverage-matrix.yaml` cites `.aa/data-knowledge.yaml` or the
    declared journey set; any unknown key has a matching
    `plans/data-knowledge.proposal.*.yaml` `discovered_candidates` entry (never
    invent MRC keys into the matrix alone).
52. MRC row identity: reject any duplicate mrc_id or key across the entire
    `trace/minimum-coverage-matrix.yaml` document.

---

## Next Workflow

After writing and self-reviewing the case YAML:

- **If running under `aa-workflow` (orchestrated):** do **NOT** invoke `aa-case-reviewer` directly. Report completion to the orchestrator. The orchestrator will invoke `aa-case-reviewer` as Phase 2.
- **If running standalone and the user explicitly asks to continue:** load `aa-case-reviewer` next.

`aa-case-reviewer` reads:

```
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/<module>/case.yaml
```

After case review passes, the orchestrator routes to:
- `aa-api-plan` (if test_types includes api)
- `aa-e2e-plan` (if test_types includes e2e)

Do NOT generate plans in this skill.

---

## Traceability Matrix Template

The traceability matrix is initialized or updated by downstream execution and archive phases (`aa-run` / `aa-archive`), not by `aa-case-design`. It lives at:

```
qa/changes/<change-id>/trace/traceability-matrix.yaml
```

It implements the full ISTQB traceability chain:

```text
requirement_id → test_condition_id → case_id → plan → test code → execution result
```

**Template:**

```yaml
schema_version: "1.0"

change_id: <change-id>

links:
  - requirement_id: REQ-002
    test_condition_id: COND-USER-AUTH-001
    case_id: TC_USER_AUTH_002
    plan:
      api: qa/changes/<change-id>/plans/api-plan.md
      e2e: null
    test_code:
      api: tests/api/test_auth_api.py
      e2e: null
    execution_result:
      api: qa/changes/<change-id>/execution/api-result.json
      e2e: null
    status: planned | implemented | passed | failed | waived
```

**Status values:**

| Status | Meaning |
|---|---|
| `planned` | Case exists, plan written, test code not yet generated |
| `implemented` | Test code exists, not yet run |
| `passed` | Last execution passed |
| `failed` | Last execution failed |
| `waived` | Execution skipped with documented reason |

**Rules:**

- `aa-case-design` does NOT write `traceability-matrix.yaml`. It is updated by `aa-run` / `aa-archive` after plans and test code exist.
- Each `case_id` in the delta SHOULD appear in the traceability matrix after execution.
- `test_condition_id` in the matrix MUST match the `test_condition_id` in `case.yaml`.
- `requirement_id` in the matrix MUST match the `requirement_id` in `case.yaml`.

---

## Red Flags

Stop and address these before continuing:

- No target module can be identified from the codebase
- Requirement spans multiple independent modules — decompose first
- Success assertions are vague ("works correctly", "looks good")
- Required data state is unknown or assumed
- User asks to generate test code before confirming scope
- E2E natural steps reference locators or automation API
- Automation target is unclear
- Request mixes product design decisions with QA case generation
- Proposed cases conflict with existing IDs in `qa/cases/`
- Coverage approach hasn't been approved before writing any file
- `modified` or `removed` references a case ID that doesn't exist in target case file

---

## Rules

- Brainstorm is a conversation — complete it fully before writing any file.
- Ask one clarifying question at a time. Never batch questions.
- Prefer multiple-choice questions when possible.
- Explore current project QA context before proposing coverage.
- If the requirement spans multiple modules, decompose it first.
- Always propose 2–3 QA coverage approaches before final confirmation.
- Always get user confirmation before generating any file.
- Write `proposal.md` before writing the case YAML.
- Case YAML MUST be at `qa/changes/<id>/cases/<module>/case.yaml` — never at `qa/cases/` directly.
- case.yaml must NOT contain `change:` or `proposal:` top-level blocks.
- Cases must be natural language — `objective`, `summary`, `preconditions`, `test_data`, `steps`, `assertions`, `postconditions`, `edge_cases`, `related_cases`.
- NEVER write `method/path/headers`, auth tokens, pytest code, Playwright code, locators, execution history, or secrets in case YAML.
- Apply Delta Operation Rules: read target case file before writing, classify each case correctly.
- Self-review the YAML before invoking aa-case-reviewer.
- Do NOT generate plans in this skill. Plans are generated by aa-api-plan and aa-e2e-plan.
- Do NOT generate or modify `tests/api` or `tests/e2e` in this skill.
- Do NOT write review or execution result in this skill.
- Do NOT archive anything in this skill.
- If the user says "skip brainstorm" while `interaction_mode == interactive`, do **NOT** skip the hard gate. Instead, offer a shortened brainstorm mode:
  > "You've asked to skip brainstorming. I cannot skip the hard gate — QA scope must be confirmed before writing any file. I can run a shortened mode: I'll confirm module, change type, test types, data needs, success assertions, exception scope, automation target, and out-of-scope in fewer questions. Shall I proceed with the shortened mode?"
  In shortened mode, still cover all 8 clarifying categories. Still propose 2–3 coverage approaches. Still require user approval before writing any file.

---

## Key Principles

- **One question at a time** — Don't overwhelm with multiple questions
- **Multiple choice preferred** — Easier to answer than open-ended when possible
- **YAGNI ruthlessly** — Remove out-of-scope cases from all coverage proposals
- **Explore before proposing** — Always check existing `qa/cases/` and `tests/` before recommending coverage
- **Propose alternatives** — Always offer 2–3 coverage approaches before settling
- **Approve before writing (`interactive`)** — Get coverage approach approval before generating any file
- **Transparent defaults (`autonomous`)** — Skip the user wait, but still write `.qa.yaml` `approval.mode: autonomous` (gate-required) and mark `proposal.md` with `generation_mode: autonomous` plus defaults / unresolved gaps
- **Never strip `.qa.yaml` approval** — Rewriting `.qa.yaml` must keep or rewrite a complete `approval` block; omitting it makes `case-design-gate` STOP
- **proposal.md first** — Write the proposal before writing cases
- **Natural language cases** — Describe what to test in plain language; leave method/path/auth/code to aa-api-plan; include objective, postconditions, edge_cases, related_cases, automation.status, regression
- **Delta operations are explicit** — Decide added/modified/removed at write time, not at archive time
- **Validate before invoking** — Self-review the case YAML before handing off to aa-case-reviewer
- **No execution details in cases** — `method/path/headers` belong in `plans/api-plan.md`
