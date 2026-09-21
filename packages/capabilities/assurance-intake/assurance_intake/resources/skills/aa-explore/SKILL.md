# Explore

Capability-owned explore skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read graph-owned run context and `phases.explore` status when the graph provides them.
   - `run_context.interaction_mode == autonomous` (default when absent) → do not ask planned clarification questions; resolve OQs with transparent `auto_default` decisions.
   - `run_context.interaction_mode == interactive` → enable Step 5's one-question-at-a-time pitfall dialogue.
2. Derive `<project-root>` (directory containing `.aa/config.yaml`; default = cwd).
3. Read requirement text (user message, file path, or PRD text provided in context).
4. Optional: read `.aa/data-knowledge.yaml`.

**Pre-flight checks — STOP before running CLI if any condition is true:**

| Condition | Action |
|-----------|--------|
| `phases.case_design.status == done` | **STOP** — explore must run before case design. Tell the user: "当前 change 的 case 已设计完成，Explore（风险研判）应在下一个 change 开始时使用，不应事后补跑。" |
| `phases.explore.status == done` | **STOP** — explore already complete. Tell the user the advisory path and suggest reading it. |

**After completing work:**

1. Write `qa/results/explore/exploration-draft.json` **and** `qa/results/explore/impact-inventory.json`
   - Use the native `write` tool directly. Do not call `aa risk`, `aa artifact`,
     `base64`, or any shell command; the explorer agent has no shell write authority.
   - Write the complete advisory in one operation. A placeholder or reduced object is
     invalid even when it contains `schema_version`, `change_id`, `watchlist`, and
     `open_questions_for_case_design`.
   - Autonomous, degraded, and no-source runs MUST still write a complete valid
     `exploration-draft.json`. Weak or absent evidence changes the evidence fields and
     confidence, never the required output file.
2. Immediately read both files back. This is a hard
   completion condition: if the read reports missing/error, continue writing and do
   not return. Native `write` is mandatory in this execution profile; there is no CLI
   or shell fallback. Never use Python, `tee`, a heredoc, `base64`, `aa risk`,
   `aa artifact`, `ast_grep_replace`, or a shell command suffix to create this artifact.
3. Do **not** write `qa/results/explore/advisory.md`
4. After read-back, verify every field listed under `exploration-draft.json schema (MVP)` and
   `test_strategy` is present and populated. The product finalizer performs the
   authoritative validation after this agent returns.
5. On a read-back or completeness failure, continue correcting the artifact; do not
   claim success or return a reduced object.
6. On success → report state delta `phases.explore.status = done` with counts and outputs (see Step 7). The graph owns phase state; report the delta and do not write an orchestration state file.

---

# Skill: aa-explore

## Purpose

Produce the **Explore** artifacts (Phase 0.5) from deterministic historical facts + requirement text, before `aa-case-design` begins. The Skill owns the full Explore pipeline internally.

**Does not** write `case.yaml`, modify `context.json`, or simulate `aa-case-design`.

**Graph binding:** This skill is an intake-owned explore capability. Use the locked execution binding from the prepare request. Do not select a provider, model, or adapter. Do not look up a global skill catalog.

---

## Step 1 — Read the graph-materialized Explore context

The deterministic prepare node has already written
`qa/results/explore/context.json`. Read that file directly with the native
`read` tool. Do not run a command to recreate, validate, or replace it.
Do not use glob to check either Explore path (`context.json` or `exploration.json`).
Change-local files may be ignored by repository search even though an exact native
read can access them. A glob result of `No files found` is not a missing-file result:
perform the exact read. Likewise, verify the output with an exact read after writing.

`context.json` now carries a typed `impact` projection: `impact.seeds[]` (`CF-*`, the
files/symbols the change touched — from a sealed diff when `impact.diff_base ==
"change-evidence"`, otherwise from explicit paths in the requirement),
`impact.candidate_cases[]` (`CS-*` with `case_id`), `impact.historical_problems[]`
(`HI-*` with `problem_id`), `impact.factory_leafs[]`, and `impact.unobserved_hints[]`.
These ids, together with your own `SC-*` ids, are the only ids you may cite anywhere.

- Missing or unreadable context → fail the task. Do not return structured success,
  especially not `{"output_files":[]}`; without authenticated context there is no
  valid `exploration.json` to receipt.
- Read succeeds → continue to Step 2.

---

## Step 2 — Weak-data gate

Check `context.degraded` and record `phases.explore.weak_data_treat_as` as context. It
does not waive the required artifact:

| `degraded` | `weak_data_treat_as` | `degraded_reasons` | Action |
|------------|----------------------|--------------------|--------|
| false | any | — | Continue to Step 3 (source read) |
| true | `done` | partial (not all 3) | Continue to Step 3; cap confidence at `medium` |
| true | `unavailable` | any | Continue to Step 3; describe the evidence limitation in the artifact |
| true | any | all 3 present | Continue to Step 3 and attempt source code read |

**Hard rule:** If ALL THREE of `no_diff`, `no_cases`, `no_history` are present in
`degraded_reasons`, proceed to Step 3. If source code is also empty, this is a
no-source degraded run: continue to Step 4 and write the complete artifact with honest
empty evidence-backed signals. Never convert weak evidence into a successful empty
receipt.

---

## Step 3 — Shallow source code read (新增)

Read source code structure to supplement or replace missing historical evidence. This
step is always executed when authenticated context is available.

### What to read

Use Read/Grep tools. Do NOT read test source files or business logic implementations.

**Backend (find and read):**
- Route definition files: search for `routes/`, `router.ts`, `*router*.ts`, `*.routes.ts`, `*controller*.ts` under the target module directory.
  - Extract: HTTP method, path pattern, handler name per route.
- Model/entity definition files: search for `models/`, `entities/`, `*model*.ts`, `*entity*.ts`, `*schema*.ts` under the target module directory.
  - Extract: field names, types, key constraints (required, unique, FK/relation).
- RBAC/permission configuration: search for `rbac`, `permission`, `guard`, `role` files.
  - Extract: role names or permission enum values relevant to the module.

**Frontend (directory structure only — do NOT read component internals):**
- List the target module's directory hierarchy and file names (e.g. `src/views/<module>/`).
- Do NOT read component/page source code.

**Not read (intentionally out of scope):**
- Existing test source files (`tests/`, `test/`, `*.spec.ts`, `*.test.ts`) — these belong to aa-test-author.
- Business logic implementations (service layer, use-case implementations).
- Configuration/environment files.

### Evidence produced from source code

Each structural fact found becomes an evidence entry:

```json
{
  "id": "SC-001",
  "source": "source_code",
  "type": "api_route | model_field | rbac_role | frontend_structure",
  "description": "POST /api/v1/users — createUser handler",
  "parse_confidence_cap": "medium"
}
```

All `source: "source_code"` evidence has `parse_confidence_cap: "medium"` — code structure proves interface shape, not failure history.

### Empty source gate

After reading, if **no** routes, models, or frontend structure were found (project directory is empty or unreadable):
- AND historical data was also empty (all 3 degraded_reasons present)
- THEN → continue to Step 4 as a no-source degraded run. Keep evidence-backed signal
  arrays empty, explain the evidence limit, and still write the complete valid
  `exploration.json`.

Otherwise continue to Step 4.

### evidence_inventory update

Move actually-read items from `not_inspected` to `available`:
- If routes were read → `api_routes` moves to `available`.
- If model files were read → `rbac_<module>_model` moves to `available`.
- If frontend structure was listed → add `frontend_structure` to `available`.
- Test source files always remain in `not_inspected`.

---

## Step 4 — LLM synthesis

Read `context.json`, requirement text, **and source code evidence collected in Step 3**. Produce `exploration.json` only.

Also read the candidate test families and authenticated goal-source identities from
the graph input. Candidates bound what the deterministic resolver may choose after
Explore; they do not state the selected families and must not suppress applicable
business obligations.

### LLM Hard Rules

1. All numbers, `case_id`, `issue_id`, module names **must** come from `context.json` fields (`evidence[]`, `impact.seeds[]`, `impact.candidate_cases[]`, `impact.historical_problems[]`, `historical_issues[]`, `case_signals[]`) **or from source code evidence (SC-* IDs) collected in Step 3**. Cite them by id: `CF-*`, `CS-*`, `HI-*`. The product finalizer rejects any id that does not resolve to `context.json` or to your `SC-*` list.
2. Every `priority_hint` / watchlist item **must** reference ≥1 evidence ID (`context.evidence[].id` or Step 3 `SC-*` ID) via `evidence_ids[]`.
3. No `evidence_ids` → item MUST be `confidence: low`. Do not generate `priority_hint`/watchlist items with no evidence.
4. `case_design_guidance` is an **evidence-anchored channel** — never write case files. `priority_hints` is the **sole channel for risk-area signals** (there is no separate `hotspots` array — every `priority_hint` carries its own `confidence`, following the same §5.7 rules as watchlist). When ALL evidence (historical AND source code) is empty, set `priority_hints`, `suggested_scenarios`, and `regression_focus` to `[]`. Do **not** fill them with generic advice — generic "at least cover" guidance belongs exclusively in `minimum_required_coverage`; degraded disclaimers belong exclusively in `executive_summary`.
4a. `test_strategy` is the **macro plan channel** — scope / data focus / layer recommendation / approach. It is a proposal consumed once by the deterministic resolver, which preserves required obligations and policy within the candidate families. Case design consumes the frozen result. Never ask the user about scope/data/layer/approach as an `open_questions_for_case_design` item. `test_strategy.layer_recommendation` MUST enumerate all four layers (`API`, `E2E`, `Fuzz`, `Performance`) with explicit `recommended: true|false`, `rationale`, and `evidence_ids`. Every layer recommendation entry MUST include `evidence_ids`: use supporting evidence IDs for a recommended layer and an empty list for an evidence-limited declined layer. When evidence is empty, emit all four as `recommended: false` with the evidence-limit reason, set `scope` and `approach` to JSON `null`, and set `depth` to `"smoke"` rather than guessing. Keep applicable required obligations even when their layer is not recommended.
5. Respect `parse_confidence_cap` on evidence — do not exceed cap in item confidence. All `source: "source_code"` evidence has cap `medium`; do not assign `high` confidence to any item backed only by SC-* evidence.
6. Use `evidence[]` IDs only — do not use unstable path expressions like `context.test_health[menus].pass_rate`.
7. If `context.staleness.stale == true` → cap all confidence at `medium`; add staleness disclaimer to Executive Summary.
8. `evidence_inventory` and `minimum_required_coverage` are top-level metadata fields. Do **not** place them under `case_design_guidance.priority_hints` or `watchlist`.
9. In degraded / weak-data conditions, keep `case_design_guidance.priority_hints` and `watchlist` empty unless there is direct `evidence_ids[]` support (SC-* IDs from Step 3 count); never turn `minimum_required_coverage` into an evidence-backed `priority_hint`.
10. **Channel separation (hard):** each piece of output content has exactly one home.
    - "Why the advisory is limited" → `executive_summary` only.
    - "What evidence was / wasn't available" → `evidence_inventory` only.
    - "What to cover at minimum" → `minimum_required_coverage` only.
    - "Prioritised risk signals from evidence" → `watchlist` / `case_design_guidance.priority_hints` (when evidence exists). A single risk insight belongs in exactly ONE of these two, never both — do not restate the same evidence as both a `priority_hint` and a `watchlist` item.
    - "Proposed macro test plan (scope/data/layer/approach)" → `test_strategy` only.
    - "Per-pitfall assertion-intent question (ignore / assert known-bug / assert ideal)" → `open_questions_for_case_design` only.
    Duplicating the same content across channels is a violation.
10a. **`priority_hints` vs `watchlist` (deciding which channel):** use `watchlist` when the risk item maps cleanly to one of the 8 case-design clarifying categories (`maps_to_clarifying_categories`) AND you want `aa-case-reviewer`'s downstream gate to enforce disposition (high-confidence watchlist items must appear in `proposal.md` as `adopted`/`override`). Use `priority_hints` for everything else — general risk-area signals, code-structure-only findings, or items that don't need a hard reviewer gate. When in doubt, prefer `priority_hints` (lighter-weight, no enforced gate).
11. When producing items backed exclusively by `source: "source_code"` evidence (SC-* IDs), add `"source_basis": "code_structure_only"` to each such `case_design_guidance` item, and note in `executive_summary` that results are derived from code structure analysis with no historical execution data.
12. `open_questions_for_case_design` items are advisory draft only in Step 4. Set `status: "unanswered"` and leave `answer`, `answer_text`, `assertion_intent`, `answered_via`, and `deferred_reason` as `null`; they are resolved in Step 5 according to `run_context.interaction_mode`.
13. Every `open_questions_for_case_design` item MUST set `pitfall_ref` to the guidance id it resolves (`PH-*` or `WL-*` preferred; `SC-*` only when no PH/WL exists yet). Each item asks: should we ignore the assertion for this discovered pitfall, or what should it assert (known-bug behavior vs ideal behavior). Do **not** generate open_questions about module confirmation, change type, test types, data needs, or target selection/depth — those proposals belong exclusively in `test_strategy`. After Step 5, the answer MUST be propagated into the linked `priority_hint` / `watchlist` / `suggested_scenario` per the reconciliation table — an answered OQ must never contradict its linked guidance item.
14. If a `confidence: low` or `confidence: medium` guidance item already contains an assertion direction, it MUST have a linked `open_question` and must not bypass Step 5 resolution. `validate the explore advisory artifact` enforces this.
15. `context_ref` is a locked artifact-local reference. Set it to the exact string
    `"explore/context.json"`. Do not expand it to
    `qa/results/explore/context.json`, an absolute path, or any other
    equivalent-looking path; the product finalizer rejects non-canonical values.

### exploration.json schema (MVP)

Required top-level fields: `schema_version`, `change_id`, `context_ref`, `generated_at`,
`executive_summary`, `watchlist`, `evidence_inventory`, `source_code_evidence`,
`case_design_guidance`, `minimum_required_coverage`, `open_questions_for_case_design`,
and `test_strategy`. `schema_version` must be exactly `"1"`.
`test_strategy.layer_recommendation` must contain all four layers,
including explicit declined entries when evidence does not support a layer.
Every entry must include `layer`, `recommended`, `rationale`, and
`evidence_ids`; `evidence_ids` may be empty only when the layer is declined for
lack of supporting evidence.
`test_strategy` must always contain the keys `scope`, `data_focus`, `depth`,
`layer_recommendation`, and `approach`. Nullable means an explicit JSON `null`,
not an omitted key. With no supporting evidence, use `scope: null`,
`data_focus: []`, `depth: "smoke"`, and `approach: null`.
`data_focus` is an array of plain strings such as `["Dept.name", "Dept.parent_id"]`,
never objects with `field` or `evidence_ids` keys. Evidence identifiers belong on
`source_code_evidence` and `layer_recommendation`, not inside `data_focus`.
The required `context_ref` value is exactly `"explore/context.json"`.

### `case_design_guidance.priority_hints` — schema and derivation

Every `priority_hint` item MUST have: `id` (`PH-\d+`), `hint` (natural language), `confidence` (`high | medium | low`, governed by the same §5.7 rules as `watchlist`), `evidence_ids[]`. Optional: `source_basis` (`code_structure_only` when backed exclusively by SC-* evidence), `case_id` (only if it points at an existing case being revisited).

**Draft hints (Step 4 only)** describe the discovered pitfall neutrally — e.g. "发现 superuser 旁路". They are **not final** until Step 5 reconciliation rewrites or removes them based on `open_questions` answers.

After Step 5, each surviving `priority_hint` that corresponds to an answered open_question MUST carry:
- `open_question_ref` (`OQ-*`)
- `assertion_intent` (copied from the OQ)
- `hint` rewritten to state **what to assert**, not merely **what was found** (see Step 5 reconciliation table)

```json
{"id": "PH-001", "hint": "断言理想行为：is_superuser=True 的用户在无角色绑定时应被拒绝访问 DependPermission 端点（不应旁路权限校验）。", "confidence": "medium", "evidence_ids": ["SC-RBAC-001"], "source_basis": "code_structure_only", "open_question_ref": "OQ-004", "assertion_intent": "assert_ideal"}
```

### `priority_hints` vs `suggested_scenarios` — different granularity, not duplicates

These two live at **different levels** — never restate the same sentence in both:

| | `priority_hints` (PH) | `suggested_scenarios` (SS) |
|---|---|---|
| Answers | **测什么方向** — the conclusion: should this pitfall be tested, and in which assertion direction | **怎么测** — a concrete, case-ready scenario derived from that conclusion |
| Level | Strategic / one line per pitfall | Tactical / one PH can fan out into **multiple** SS |
| Carries the assertion decision? | **Yes** — `assertion_intent` + `open_question_ref`, authoritative | No — it inherits the PH's direction and only spells out execution |
| Link | Referenced by SS | Each SS MUST carry `priority_hint_ref` pointing at its PH |

**Hard rule — SS must add execution detail beyond its PH.** A `suggested_scenario` is only worth emitting if its `description` contains at least one concrete testable detail the PH does not already state, e.g.:
- a concrete input value / request body (`{menu_ids:[99999]}` where 99999 is a nonexistent id), or
- a specific asserted status code / response field (`→ 400, detail contains "menu not found"`), or
- a specific precondition / data state (`role already bound to 2 users, then delete`).

If a scenario would only restate the PH's wording in different words (no new input value, no new assertion target), **do NOT emit it** — let `aa-case-design` derive cases straight from the PH instead. One PH SHOULD fan out into multiple SS along distinct input/assertion dimensions (e.g. PH "无效引用应返回 4xx" → SS-a: invalid `menu_id`; SS-b: invalid `api path/method`), each differing in its concrete detail — not the same sentence split in two.

`suggested_scenarios` and `regression_focus` stay descriptive/derived from `priority_hints` + evidence — reference the same `evidence_ids` rather than re-deriving a risk not already backed by a `priority_hint` or `watchlist` item. After Step 5, any scenario that primarily covers a pitfall with a decided `assertion_intent` MUST rewrite its expected outcome to match that intent (or be removed when intent is `ignore`).

**Good vs bad SS (relative to PH-001 "无效引用应返回 400/404 而非 500"):**

| Bad (mere restatement) | Good (adds execution detail) |
|---|---|
| "update_authorized 传入不存在的 menu_id，应返回 400/404 而非 500" | "PUT /role/authorized body `{menu_ids:[99999]}`（99999 不存在）→ 断言 400，`detail` 含 'menu not found'，且角色原有 menus 不被清空" |

The fields in this section are the authoritative MVP advisory shape; the product
finalizer enforces the installed model after read-back.

### test_strategy — derivation rules (新增)

`test_strategy` answers the **macro** question "整体测试范围/数据/层级/方案怎么定" from evidence, so `aa-case-design` doesn't have to start from zero. It is synthesized — never asked as an open_question.

| Field | Derive from |
|-------|-------------|
| `scope.in_scope` / `scope.out_of_scope` | Changed files/symbols (`context.json` `impact.seeds[].path`) + requirement text scope; uncertain areas go to `out_of_scope` with a note in `executive_summary`, not a guess. When no evidence supports a scope, the required `scope` key is JSON `null`. |
| `data_focus` | Model/entity fields read in Step 3 (SC-* `model_field` evidence) that are central to the change |
| `layer_recommendation` | Exactly one entry per `API/E2E/Fuzz/Performance`; every entry includes `evidence_ids`: `recommended: true` only when evidence supports it and cites those IDs; `recommended: false` when evidence does not support the layer and uses `evidence_ids: []` plus a concrete evidence-limit reason |
| `approach` | One sentence combining the recommended layers, e.g. "API + E2E，并对 UserCreate/UserUpdate schema 增加 Fuzz 覆盖" |

`depth` (`smoke | core | exhaustive`) reflects how much evidence supports broad coverage — `low`/no evidence caps depth at `smoke`.

Layer recommendation triggers:

| Layer | Recommend when |
|-------|----------------|
| API | REST/RPC endpoints exist for the change scope |
| E2E | A user-facing page/flow exists for the change scope |
| Fuzz | Endpoints accept user-input schemas (create/update bodies, query parsers, file/import input) |
| Performance | A high-frequency, core, or heavy-query path is identified in scope |

Silent omission of a layer is invalid. If evidence is insufficient to recommend a layer, include it as `recommended: false` with the reason. Normally decline non-candidate families. The deterministic resolver freezes the family set before Case design; recommendations cannot remove required obligations, expand candidates, or authorize later family changes.

### Required degraded-output metadata

When advisory is degraded and evidence-backed signals are unavailable, `watchlist` remains an empty array, `case_design_guidance` sub-arrays (including `priority_hints`) are all `[]`, and the advisory MUST still populate `evidence_inventory` and `minimum_required_coverage` per the rules below.

#### evidence_inventory — derivation rules

Three buckets; populate dynamically for every change, do **not** copy-paste the example verbatim.

| Bucket | Definition | Rule |
|--------|-----------|------|
| `available` | Data that **was read and used** by this advisory | Always: `requirement_text`, `explore_context`. Add route/model/frontend items here **only if actually read in Step 3**. |
| `missing` | Data the aggregator **tried to collect** (within its scope) but found empty or unavailable | = fields that are empty in `context.json` and whose absence is a degraded_reason (`no_diff` → `git_diff`/`changed_files`; `no_history` → `historical_issues`/`previous_failure_analysis`). |
| `not_inspected` | Data that **exists and could be read** but was **not read in this run** | Always: `existing_tests` (source files), `api_schema` (live schema). Move `api_routes` / `rbac_<module>_model` / `frontend_structure` to `available` if Step 3 actually read them. |

> **Caution — `existing_tests` ambiguity:** test *source files* are always `not_inspected`; test *health signals* (`test_health[]` aggregated from archive) may be `missing` if the archive returned no data. These are different things — do not conflate them.

*Example (menu-management, no diff, no history, routes+model read in Step 3):*
```json
"evidence_inventory": {
  "available": ["requirement_text", "explore_context", "api_routes", "rbac_menu_model"],
  "missing": ["git_diff", "changed_files", "historical_issues",
              "previous_failure_analysis"],
  "not_inspected": ["existing_tests", "api_schema"]
}
```

*Example (menu-management, no diff, no history, source also empty):*
```json
"evidence_inventory": {
  "available": ["requirement_text", "explore_context"],
  "missing": ["git_diff", "changed_files", "historical_issues",
              "previous_failure_analysis"],
  "not_inspected": ["existing_tests", "api_routes", "api_schema", "rbac_menu_model"]
}
```

#### minimum_required_coverage — derivation rules

`minimum_required_coverage` is a **JSON array of obligation drafts**. Do **not** write
the legacy family object (`{"api": [...], "e2e": [], "negative": [...],
"data_integrity": [...]}`); the product finalizer rejects that shape as
`invalid_output`.

Generate one draft per applicable obligation from the **module's domain shape**
(CRUD operations + tree/hierarchy if applicable + RBAC/auth + negative/integrity),
the requirement, and authenticated catalog/data knowledge. Empty categories are
omitted — do not emit placeholder drafts.

| `category` | What to include | `proposed_key` | default `layer` |
|-----------|-----------------|----------------|-----------------|
| `api` | One draft per main API operation the module exposes (CRUD + any module-specific queries) | snake_case `verb_noun` | `api` |
| `e2e` | Every applicable business-required user journey | exact key from authenticated data knowledge `journeys` | `e2e` |
| `negative` | Required-field, foreign-key, boundary, or authorization obligations | exact capability-catalog leaf | `api` |
| `data_integrity` | Consistency invariants such as hierarchy, ordering, or relationship constraints | exact capability-catalog leaf | `api` |

Each draft MUST include every field below. Use `[]` / `null` when a list or optional
id has no value; do not omit keys.

| Field | Rule |
|-------|------|
| `draft_id` | Unique in this array (`D-API-001`, `D-NEG-001`, …) |
| `proposed_key` | The operation, journey, or catalog leaf from the table |
| `category` | `api` / `e2e` / `negative` / `data_integrity` only. Do not emit `e2e_if_enabled`. |
| `layer` | `api`, `e2e`, or `both`. Use `both` only when both kinds of evidence are required. |
| `statement` | One sentence stating what must hold |
| `applicability_conditions` | Resolved conditions, or `[]` |
| `impact_row_ids` | Inventory row ids this draft covers, or `[]` |
| `proposed_profile_id` | A known profile id, or `null` |
| `prerequisites` | Setup the later observation needs, or `[]` |
| `observation_goals` | Later observation drafts, or `[]` |
| `basis_quotes` | Quotes whose `source_id` is in the host source catalog, or `[]` |
| `open_questions` | Unresolved questions that block freezing, or `[]` |

A `recommended: false` E2E layer row must not erase a required journey when `e2e`
is still a candidate family and the requirement still needs that journey. An explicit API-only requirement, or a candidate set that does not include `e2e`, makes E2E journeys inapplicable: do not emit `category: "e2e"` drafts. Do not keep a catalog journey as required after declining E2E for API-only scope.

Resolve conditional requiredness/applicability from requirement and authenticated
policy/data evidence before finalizing. Write `category: "e2e"` drafts only when
`e2e` remains a candidate family and the requirement still needs those journeys.
An unresolved condition belongs in `open_questions`; do not invent `required`,
erase the draft, or use a family recommendation as its condition. Keep
`draft_id` and `proposed_key` unique across the array. If a required closed key
is unavailable, leave `proposed_key` null and record the unresolved obligation
in `open_questions` instead of inventing a key or silently omitting it.

*Example (menu-management, only when these exact catalog leaves and journey keys are authenticated):*
```json
"minimum_required_coverage": [
  {
    "draft_id": "D-API-001",
    "proposed_key": "create_menu",
    "category": "api",
    "layer": "api",
    "statement": "POST create must persist a menu under an authenticated parent.",
    "applicability_conditions": [],
    "impact_row_ids": [],
    "proposed_profile_id": null,
    "prerequisites": [],
    "observation_goals": [],
    "basis_quotes": [],
    "open_questions": []
  },
  {
    "draft_id": "D-E2E-001",
    "proposed_key": "admin_can_enter_menu_management",
    "category": "e2e",
    "layer": "e2e",
    "statement": "An admin can open the menu-management page.",
    "applicability_conditions": [],
    "impact_row_ids": [],
    "proposed_profile_id": null,
    "prerequisites": [],
    "observation_goals": [],
    "basis_quotes": [],
    "open_questions": []
  },
  {
    "draft_id": "D-NEG-001",
    "proposed_key": "entities.menu.constraints.name_required",
    "category": "negative",
    "layer": "api",
    "statement": "Create and update reject a missing menu name.",
    "applicability_conditions": [],
    "impact_row_ids": [],
    "proposed_profile_id": null,
    "prerequisites": [],
    "observation_goals": [],
    "basis_quotes": [],
    "open_questions": []
  },
  {
    "draft_id": "D-DATA-001",
    "proposed_key": "entities.menu.constraints.parent_child_tree",
    "category": "data_integrity",
    "layer": "api",
    "statement": "Parent/child menu links stay a tree after writes.",
    "applicability_conditions": [],
    "impact_row_ids": [],
    "proposed_profile_id": null,
    "prerequisites": [],
    "observation_goals": [],
    "basis_quotes": [],
    "open_questions": []
  }
]
```

### Confidence rules (§5.7)

| level | conditions |
|-------|------------|
| **high** | ≥1 evidence with `below_fail_threshold=true` (test_health) OR historical_issue source 1–2; diff module confidence ≥ medium; `staleness.stale == false` |
| **medium** | valid evidence_ids but not high; stale archive; or issue from source 3 |
| **low** | no direct evidence; source-4 only; weak module mapping — emit only if genuinely informative, omit otherwise |

### Clarifying category enum (`maps_to_clarifying_categories`)

`aa-case-design` has 8 clarifying categories in total (see its own SKILL.md), but `open_questions_for_case_design` from explore may **only** use the 3 assertion-related categories below — explore's 反问 is scoped to per-pitfall assertion intent, not macro planning. Macro categories (`module_confirmation`, `change_type`, `test_types`, `data_needs`, `target_selection_depth`) are covered by `test_strategy` instead, as a proposal — never as an open_question.

| enum | case-design category | when explore uses it |
|------|----------------------|----------------------|
| `success_assertions` | Success assertions | pitfall's assertion content is undecided (assert known-bug behavior vs assert ideal behavior) |
| `exception_scenarios` | Exception scenarios | pitfall is an edge/error path whose handling is undecided |
| `out_of_scope` | Out of scope | pitfall should plausibly be ignored (known/accepted behavior) rather than asserted at all |

---

## Step 4b — Change impact inventory

Write `qa/results/explore/impact-inventory.json` with the native `write` tool in one
operation, then read it back. It is a table of impact rows; the graph seals it and the
frozen plan binds it, so case design and retro can cite `IR-*` ids.

```json
{
  "schema_version": "1",
  "change_id": "<change_id>",
  "context_ref": "explore/context.json",
  "rows": [
    {
      "row_id": "IR-001",
      "change_evidence_ids": ["CF-001", "SC-003"],
      "affected_behavior": {"kind": "api", "key": "DELETE /api/v1/dept/delete"},
      "case_module": "system/dept",
      "obligation": "deleting a department must also remove its DeptClosure rows and leave user.dept empty",
      "expected_basis_ids": ["SC-005", "HI-001"],
      "assets": {"case_ids": ["TC_DEPT_API_011"], "factory_leafs": ["capabilities.domain_factories.dept.make_dept"], "problem_ids": ["PROB-1"]},
      "disposition": "modify",
      "gap_reason": null,
      "confidence": "medium"
    }
  ],
  "exclusions": [
    {"seed_id": "CF-004", "reason": "router registration only; behavior is covered by CF-001 rows"}
  ]
}
```

| Column | Question it answers | Rules |
|--------|---------------------|-------|
| `change_evidence_ids` | Which requirement, file, or symbol changed? | ≥1 id; `CF-*` from `impact.seeds[]` or your `SC-*` |
| `affected_behavior` | Which API, journey, role, or data constraint may be affected? | `kind` ∈ `api`, `journey`, `role`, `data_constraint`; `data_constraint` keys must be exact typed catalog leafs and `journey` keys exact data-knowledge journeys, otherwise use `capability_gap` |
| `case_module` | Which `qa/cases/<module>/` directory should receive the delta? | required on `add`/`modify`; slash-separated slugs such as `system/dept`; rows that belong in one `case.yaml` MUST share the same module; prefer an existing catalog path when `impact.candidate_cases[]` already has one |
| `obligation` + `expected_basis_ids` | What must be verified and on what basis? | one sentence; basis ids from `SC-*`, `HI-*`, `CS-*` |
| `assets` | Which cases, factories, and problems can be reused? | `case_ids` from `impact.candidate_cases[].case_id`; `factory_leafs` from `impact.factory_leafs`; `problem_ids` from `impact.historical_problems[].problem_id` |
| `disposition` | Reuse, modify, add, or is something missing? | `reuse`/`modify` require `assets.case_ids`; `add` requires none; `capability_gap` and `pending_confirmation` require `gap_reason` |

Hard rules:

1. Completeness: every `impact.seeds[].seed_id` must appear in at least one row's `change_evidence_ids` or in `exclusions[]` with a reason. Never drop a seed silently.
2. Do not invent ids. `CF-*`, `CS-*`, `HI-*` come from `context.json`; `SC-*` from Step 3. The product finalizer rejects any id that does not resolve.
3. Prefer `capability_gap` over a guessed catalog key, and `pending_confirmation` over a guessed business rule. Open rows are visible to case design and retro; wrong closed rows are not.
4. When `impact.seeds` is empty (no diff, no explicit paths), rows still cite `SC-*` evidence and `exclusions` is `[]`.
5. The inventory does not replace `exploration.json`; `test_strategy`, `minimum_required_coverage`, and `priority_hints` keep their own channels.
6. Name `case_module` on every `add` or `modify` row. One requirement commonly spans several modules — emit one inventory row per affected behavior, and reuse the same `case_module` only when those rows belong in the same `case.yaml`. The operator never supplies modules; case-design writes every inferred `qa/cases/<case_module>/case.yaml` path.

---

## Step 5 — Resolve open_questions (mode-aware)

After writing the advisory draft, check `open_questions_for_case_design[]`:

- If empty → skip this step, proceed to Step 6.
- If non-empty and `run_context.interaction_mode == autonomous` or `run_context` is absent → apply **auto_default** resolution (no user questions), then reconcile guidance and proceed to Step 6.
- If non-empty and `run_context.interaction_mode == interactive` → ask the user one question at a time, in array order. Each question is about exactly one discovered pitfall (`pitfall_ref`) and asks only: ignore it, or assert which behavior.

### Autonomous branch (`auto_default`)

For every `status: "unanswered"` OQ:

- If the linked pitfall is an ideal-behavior / product-correctness / security / permission / validation issue → set `assertion_intent = "assert_ideal"`.
- If the correct direction cannot be inferred → set `assertion_intent = "undecided"`; downstream case-design must use neutral wording and avoid asserting the pitfall as accepted behavior.
- Set `status = "answered"` and `answered_via = "auto_default"`.
- Leave `answer` / `answer_text` empty unless a short default rationale is useful.

Then run Step 5 reconciliation exactly like interactive answers. Auto decisions are allowed in `autonomous` mode, but they must be visible in `exploration.json`; never rewrite a low-confidence assertion as if a human confirmed it.

### Interactive branch

**Question format:**

```
探索发现一个代码坑（<i>/<N>，关联 <pitfall_ref>）：

<pitfall 描述，来自 evidence>

这个坑测试应该如何处理？
A. 断言当前行为（已知 bug / 已知限制）
B. 断言理想行为（视为需修复的缺陷）
C. 忽略，不针对此坑写断言

请选择 A/B/C，或输入"跳过"（此问题将留给 case-design 阶段处理）。
```

Do NOT ask about test scope, layer, data needs, or target selection in this step — those are already proposed in `test_strategy` for `aa-case-design` to confirm.

- User provides answer →
  - write the free-text answer to `open_questions_for_case_design[i].answer`
  - write the same text to `open_questions_for_case_design[i].answer_text` for the new lifecycle schema
  - map the choice to `open_questions_for_case_design[i].assertion_intent`: A → `assert_known_bug`, B → `assert_ideal`, C → `ignore` (open-ended answers that don't fit A/B/C → `undecided`, with the raw text kept in `answer`)
  - set `status = "answered"` and `answered_via = "aa-intake"` when called from `aa-intake`; otherwise `answered_via = "explore"` for standalone legacy interactive use.
  - when `answered_via = "aa-intake"`, also set `confirmed_by = "user"` and `confirmed_at = <ISO timestamp>` (or `user_confirmed = true`). `validate the explore advisory artifact` reads graph-owned `run_context` and fails interactive intake if answers are auto-filled without this user confirmation metadata.
- User inputs "跳过" (or equivalent) → set `status = "deferred"`, leave `assertion_intent = null` / `answered_via = null`, and set `deferred_reason` (for example, `user skipped during intake`).
- After all questions are asked (or user skips ≥3 in a row), **reconcile `case_design_guidance` with the collected answers** (mandatory — do not proceed to Step 6 until done), then output:
  `"已记录回答，继续生成最终 advisory。"` and proceed to Step 6.

Answers are persisted into `exploration.json` — `aa-case-design` reads the propagated `assertion_intent` on `priority_hints` / `watchlist` / `suggested_scenarios`, not just the raw OQ entries.

### Step 5 reconciliation — propagate assertion decisions into guidance

For each `open_questions_for_case_design[]` item with `status = "answered"` and `answered_via in ["explore", "auto_default", "aa-intake"]` (legacy: `answer != null` and `answered_via = "explore"`), find the linked guidance item(s) by `pitfall_ref`:

| `pitfall_ref` pattern | Linked guidance |
|---|---|
| `PH-*` | `case_design_guidance.priority_hints[]` item with matching `id` |
| `WL-*` | `watchlist[]` item with matching `id` |
| `SC-*` or other evidence id | `priority_hint` or `watchlist` item whose `evidence_ids` includes that id; prefer `PH-*` when an open_question exists for the same pitfall |

Apply by `assertion_intent`:

| `assertion_intent` | `priority_hints` | `watchlist` | `suggested_scenarios` / `regression_focus` |
|---|---|---|---|
| **`ignore`** | **Remove** the matching `PH-*` entirely — do not leave a neutral hint (see HS-002 / OQ-001: ignored pitfall → no PH). | Set `assertion_intent: "ignore"` + `open_question_ref`; keep item for traceability but case-design must not generate assertions for it. | **Remove** scenarios whose primary purpose is this pitfall. |
| **`assert_ideal`** | **Keep** `PH-*`; set `open_question_ref`, `assertion_intent: "assert_ideal"`; **rewrite `hint`** to state the ideal expected behavior explicitly (what should happen / what should be rejected). | Same fields on matching `WL-*`. | Rewrite scenario expected outcome to the **ideal** behavior — e.g. "superuser 无角色时应收到 403", not "验证 superuser 能绕过去". |
| **`assert_known_bug`** | **Keep** `PH-*`; set `open_question_ref`, `assertion_intent: "assert_known_bug"`; **rewrite `hint`** to state the current buggy behavior to assert. | Same on `WL-*`. | Rewrite scenario to assert **current** behavior. |
| **`undecided`** or skipped (`answer == null`) | Keep Step 4 neutral pitfall wording; omit `assertion_intent` / `open_question_ref`. | Unchanged. | Keep ambiguous wording; case-design may re-ask via pending OQ. |

**Hint wording rule (critical):** When `assertion_intent` is decided, a `priority_hint.hint` MUST NOT read like a neutral reproduction prompt ("优先覆盖 X 旁路：用户能 Y"). It MUST read like a test directive:

| Wrong (neutral / contradicts OQ) | Right (propagated) |
|---|---|
| "优先覆盖 superuser 旁路：is_superuser=True 用户无需角色绑定即可访问所有端点" + OQ=`assert_ideal` | "断言理想行为：is_superuser=True 的用户在无角色绑定时应被拒绝访问 DependPermission 端点" |
| "优先覆盖 reset_password 越权场景：角色能否重置他人密码？" + OQ=`assert_ideal` | "断言理想行为：reset_password 应限制只能管理员或本人重置，持有权限的角色不应能重置他人密码" |
| dev token 后门 + OQ=`ignore` | **No** matching `priority_hint` at all |

When an open_question's `pitfall_ref` is a `PH-*` id, set that `PH-*` item's `open_question_ref` to the `OQ-*` id. When `pitfall_ref` is `WL-*` only (no `PH-*`), propagate only to `watchlist` — do not duplicate into a new `priority_hint`.

After reconciliation, re-run a mental check: for every answered OQ, no surviving guidance item should imply the opposite assertion direction.

---

## Step 6 — Read-back completeness check

Read `exploration.json` and `impact-inventory.json` and verify the required top-level fields above, the three
`case_design_guidance` arrays, all three `evidence_inventory` arrays, a non-empty
`minimum_required_coverage` array of obligation drafts, every required `test_strategy` key (`scope`,
`data_focus`, `depth`, `layer_recommendation`, `approach`), and four
`test_strategy.layer_recommendation` entries, each with an explicit
`evidence_ids` array, and that every inventory row has all required columns. Do not run a validator command. The product finalizer performs the
authoritative typed validation after this agent returns.

- Incomplete → rewrite the complete object and repeat the read-back check.
- Complete → proceed to Step 7.

---

## Step 7 — Graph-owned state delta

Ensure the written artifacts contain the inputs needed for the graph to derive the
following delta. Do not return this delta separately; the graph owns phase state:

```yaml
phases:
  explore:
    status: done | failed
    outputs:
      - explore/context.json
      - explore/exploration.json    # only when done
      - explore/impact-inventory.json    # only when done
    priority_hints_count: <n>
    watchlist_high_count: <n>
    degraded: <bool from context.json>
    source_code_read: <bool>         # true if Step 3 found and read any source files
    test_strategy_populated: <bool>  # true if test_strategy has any layer_recommendation entries
    open_questions_answered: <n>     # count of questions answered in Step 5
    validation_errors: []
```

---

## Final Output (runtime contract)

Do not output a user-facing summary, step log, phase delta, or compliance checklist.
After both artifacts are written and read-back validation succeeds, return structured JSON only:

```json
{"output_files":["qa/results/explore/exploration-draft.json","qa/results/explore/impact-inventory.json"]}
```

Every successful run returns exactly the non-empty receipt above. If missing or
unreadable context prevents writing a valid artifact, fail without structured success;
never return `{"output_files":[]}`. Degraded and no-source runs are successful only
after the complete `exploration.json` has been written and read back.

---

## Phase 1 gate (summary)

- `status == pending` → Phase 1 **STOP**
- `status == done` → Phase 1 must read `exploration.json`; missing file → STOP
- A non-`done` terminal state already materialized by the graph is handled by the
  graph's locked mode. The explorer MUST NOT synthesize such a state as successful
  completion or use it to omit `exploration.json`.
