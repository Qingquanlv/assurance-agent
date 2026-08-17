---
name: aa-explore
description: "Phase 1.1 Explore: run `aa risk context`, shallow-read source code, synthesize advisory.json (including a test_strategy macro-plan proposal), then collect per-pitfall assertion-intent answers (ignore / assert known-bug / assert ideal) to open_questions one at a time. Use before aa-case-design. Artifacts live under explore/ (context.json, advisory.json). Never modify context.json or write case.yaml."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.json` if it exists → `phases.explore` and top-level `run_context`.
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

1. Write `qa/changes/<change-id>/explore/advisory.json`
2. Immediately read `qa/changes/<change-id>/explore/advisory.json` back. This is a hard completion condition: if the read reports missing/error, continue writing and do not validate or return. Prefer the native `write` tool. When `write` is unavailable in the provider runtime, base64-encode the complete UTF-8 JSON and use the bounded structured CLI fallback:

   ```bash
   aa risk write-advisory \
     --change <change-id> \
     --project-dir <project-root> \
     --payload-base64 <base64-of-complete-advisory-json>
   ```

   Never use Python, `tee`, a heredoc, `ast_grep_replace`, or a shell command suffix to create this artifact.
3. Do **not** write `qa/changes/<change-id>/explore/advisory.md`
4. Run `aa risk validate-advisory --change <change-id> --project-dir <project-root>` only after the read-back succeeds.
5. On validation failure → report state delta `phases.explore.status = failed` with `validation_errors`
6. On success → report state delta `phases.explore.status = done` with counts and outputs (see Step 7). Inline mode: apply the delta to `workflow-state.json` directly; dispatched subagent: never write `workflow-state.json` — report the values in your final message and the orchestrator applies them.

---

# Skill: aa-explore

## Purpose

Produce the **Explore** artifacts (Phase 0.5) from deterministic historical facts + requirement text, before `aa-case-design` begins. The Skill owns the full Explore pipeline internally.

**Does not** write `case.yaml`, modify `context.json`, or simulate `aa-case-design`.

**Workflow binding:** When invoked from `aa-workflow`, this skill **always** runs **inline in the primary agent** — even when `execution_mode == subagent-dispatch`. It is never dispatched to `aa-doc-author` (subagents lack Bash for `aa risk` CLI).

---

## Step 1 — Run `aa risk context`

```bash
aa risk context \
  --change <change-id> \
  --project-dir <project-root> \
  [--diff-base main] \
  [--archive-depth 10] \
  [--requirement <path-if-file>] \
  [--staleness-days 30]
```

- CLI **exit != 0** → set `phases.explore.status = failed`; output one-line error; stop.
- CLI success → read `qa/changes/<change-id>/explore/context.json`.

---

## Step 2 — Weak-data gate

Check `context.degraded` and `phases.explore.weak_data_treat_as` (default: `done`):

| `degraded` | `weak_data_treat_as` | `degraded_reasons` | Action |
|------------|----------------------|--------------------|--------|
| false | any | — | Continue to Step 3 (source read) |
| true | `done` | partial (not all 3) | Continue to Step 3; cap confidence at `medium` |
| true | `unavailable` | any | Skip Steps 3–5 → `status = unavailable` → END |
| true | any | all 3 present | **Do NOT immediately go to unavailable** — proceed to Step 3 to attempt source code read first |

**Revised hard rule:** If ALL THREE of `no_diff`, `no_cases`, `no_history` are present in `degraded_reasons`, proceed to Step 3 and attempt source code read. Only if source code is also empty (no routes, no models, empty project directory) does `status = unavailable` apply. Source code structure is valid evidence; do not fabricate unavailability when the codebase is readable.

When `status = unavailable` (confirmed after Step 3): report the `phases.explore.status = unavailable` state delta (see Context Contract), output one-line notice, stop.

---

## Step 3 — Shallow source code read (新增)

Read source code structure to supplement or replace missing historical evidence. This step is always executed unless `weak_data_treat_as = unavailable`.

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
- THEN → `status = unavailable`; report the state delta (see Context Contract); output one-line notice; stop.

Otherwise continue to Step 4.

### evidence_inventory update

Move actually-read items from `not_inspected` to `available`:
- If routes were read → `api_routes` moves to `available`.
- If model files were read → `rbac_<module>_model` moves to `available`.
- If frontend structure was listed → add `frontend_structure` to `available`.
- Test source files always remain in `not_inspected`.

---

## Step 4 — LLM synthesis

Read `context.json`, requirement text, **and source code evidence collected in Step 3**. Produce `advisory.json` only.

### LLM Hard Rules

1. All numbers, `case_id`, `issue_id`, module names **must** come from `context.json` fields (`evidence[]`, `impact.*`, `historical_issues[]`, `case_signals[]`) **or from source code evidence (SC-* IDs) collected in Step 3**.
2. Every `priority_hint` / watchlist item **must** reference ≥1 evidence ID (`context.evidence[].id` or Step 3 `SC-*` ID) via `evidence_ids[]`.
3. No `evidence_ids` → item MUST be `confidence: low`. Do not generate `priority_hint`/watchlist items with no evidence.
4. `case_design_guidance` is an **evidence-anchored channel** — never write case files. `priority_hints` is the **sole channel for risk-area signals** (there is no separate `hotspots` array — every `priority_hint` carries its own `confidence`, following the same §5.7 rules as watchlist). When ALL evidence (historical AND source code) is empty, set `priority_hints`, `suggested_scenarios`, and `regression_focus` to `[]`. Do **not** fill them with generic advice — generic "at least cover" guidance belongs exclusively in `minimum_required_coverage`; degraded disclaimers belong exclusively in `executive_summary`.
4a. `test_strategy` is the **macro plan channel** — scope / data focus / layer recommendation / approach. It is a **proposal**, not a decision: `aa-case-design` confirms or overrides it during its own clarifying questions. Never ask the user about scope/data/layer/approach as an `open_questions_for_case_design` item — that question belongs to `aa-case-design`, not explore. `test_strategy.layer_recommendation` MUST enumerate all four layers (`API`, `E2E`, `Fuzz`, `Performance`) with explicit `recommended: true|false` and `rationale`; when evidence is empty, emit all four as `recommended: false` with the evidence-limit reason and leave `scope`/`approach` unset rather than guessing.
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
13. Every `open_questions_for_case_design` item MUST set `pitfall_ref` to the guidance id it resolves (`PH-*` or `WL-*` preferred; `SC-*` only when no PH/WL exists yet). Each item asks: should we ignore the assertion for this discovered pitfall, or what should it assert (known-bug behavior vs ideal behavior). Do **not** generate open_questions about module confirmation, change type, test types, data needs, or target selection/depth — those macro decisions belong exclusively in `test_strategy` for `aa-case-design` to resolve. After Step 5, the answer MUST be propagated into the linked `priority_hint` / `watchlist` / `suggested_scenario` per the reconciliation table — an answered OQ must never contradict its linked guidance item.
14. If a `confidence: low` or `confidence: medium` guidance item already contains an assertion direction, it MUST have a linked `open_question` and must not bypass Step 5 resolution. `aa risk validate-advisory` enforces this.

### advisory.json schema (MVP)

Required top-level fields: `schema_version`, `change_id`, `context_ref`, `generated_at`, `executive_summary`, `watchlist`, `evidence_inventory`, `case_design_guidance`, `minimum_required_coverage`, `open_questions_for_case_design`. `test_strategy` is optional but should be populated whenever evidence supports it.

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

See `schemas/explore-advisory.schema.json` for the MVP advisory shape.

### test_strategy — derivation rules (新增)

`test_strategy` answers the **macro** question "整体测试范围/数据/层级/方案怎么定" from evidence, so `aa-case-design` doesn't have to start from zero. It is synthesized — never asked as an open_question.

| Field | Derive from |
|-------|-------------|
| `scope.in_scope` / `scope.out_of_scope` | Diff-changed modules/files (`context.json` `impact.modules`) + requirement text scope; uncertain areas go to `out_of_scope` with a note in `executive_summary`, not a guess |
| `data_focus` | Model/entity fields read in Step 3 (SC-* `model_field` evidence) that are central to the change |
| `layer_recommendation` | Exactly one entry per `API/E2E/Fuzz/Performance`: `recommended: true` only when evidence supports it; `recommended: false` when evidence does not support the layer; cite `evidence_ids` for supported recommendations and give a concrete evidence-limit reason for declined ones |
| `approach` | One sentence combining the recommended layers, e.g. "API + E2E，并对 UserCreate/UserUpdate schema 增加 Fuzz 覆盖" |

`depth` (`smoke | core | exhaustive`) reflects how much evidence supports broad coverage — `low`/no evidence caps depth at `smoke`.

Layer recommendation triggers:

| Layer | Recommend when |
|-------|----------------|
| API | REST/RPC endpoints exist for the change scope |
| E2E | A user-facing page/flow exists for the change scope |
| Fuzz | Endpoints accept user-input schemas (create/update bodies, query parsers, file/import input) |
| Performance | A high-frequency, core, or heavy-query path is identified in scope |

Silent omission of a layer is invalid. If evidence is insufficient to recommend a layer, include it as `recommended: false` with the reason. These are advisory recommendations only — `aa-case-design` still owns the user-facing test-type decision and must offer Fuzz/Performance opt-in regardless of what is recommended here.

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

Generate from the **module's domain shape** (CRUD operations + tree/hierarchy if applicable + RBAC/auth + negative/integrity), not from a fixed literal list. The four sub-arrays are:

| Sub-array | What to include |
|-----------|----------------|
| `api` | One entry per main API operation the module exposes (CRUD + any module-specific queries). Name in snake_case (`verb_noun`). |
| `e2e_if_enabled` | Only if E2E is in scope; cover admin happy-path flows + role-based visibility. Omit or set `[]` if E2E is excluded. |
| `negative` | Missing required fields, invalid foreign-key refs, boundary violations, unauthorized access. |
| `data_integrity` | Consistency invariants specific to this module (tree consistency, sort order, join-table consistency). |

*Example (menu-management):*
```json
"minimum_required_coverage": {
  "api": ["create_menu", "list_menu_tree", "get_menu_detail",
          "update_menu", "delete_menu", "unauthorized_access"],
  "e2e_if_enabled": ["admin_can_enter_menu_management",
                     "admin_creates_menu_and_sees_navigation",
                     "role_based_menu_visibility",
                     "non_admin_cannot_access_menu_management"],
  "negative": ["missing_required_fields", "invalid_parent_id",
               "delete_parent_menu_with_children",
               "unauthorized_menu_management_api_access"],
  "data_integrity": ["parent_child_tree_consistency",
                     "sort_order_consistency",
                     "role_menu_relation_consistency"]
}
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

Then run Step 5 reconciliation exactly like interactive answers. Auto decisions are allowed in `autonomous` mode, but they must be visible in `advisory.json`; never rewrite a low-confidence assertion as if a human confirmed it.

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
  - when `answered_via = "aa-intake"`, also set `confirmed_by = "user"` and `confirmed_at = <ISO timestamp>` (or `user_confirmed = true`). `aa risk validate-advisory` reads `workflow-state.json.run_context` and fails interactive intake if answers are auto-filled without this user confirmation metadata.
- User inputs "跳过" (or equivalent) → set `status = "deferred"`, leave `assertion_intent = null` / `answered_via = null`, and set `deferred_reason` (for example, `user skipped during intake`).
- After all questions are asked (or user skips ≥3 in a row), **reconcile `case_design_guidance` with the collected answers** (mandatory — do not proceed to Step 6 until done), then output:
  `"已记录回答，继续生成最终 advisory。"` and proceed to Step 6.

Answers are persisted into `advisory.json` — `aa-case-design` reads the propagated `assertion_intent` on `priority_hints` / `watchlist` / `suggested_scenarios`, not just the raw OQ entries.

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

## Step 6 — Validate

```bash
aa risk validate-advisory --change <change-id> --project-dir <project-root>
```

- Fail → `status = failed`; if `mode == required` → STOP before Phase 1.
- Pass → proceed to Step 7.

---

## Step 7 — `workflow-state.json` State Delta

Report the following delta (applied by the state owner per the Context Contract):

```yaml
phases:
  explore:
    status: done | failed | unavailable
    outputs:
      - explore/context.json
      - explore/advisory.json    # only when done
    priority_hints_count: <n>
    watchlist_high_count: <n>
    degraded: <bool from context.json>
    source_code_read: <bool>         # true if Step 3 found and read any source files
    test_strategy_populated: <bool>  # true if test_strategy has any layer_recommendation entries
    open_questions_answered: <n>     # count of questions answered in Step 5
    validation_errors: []
```

---

## Final Output (user-facing)

**Do NOT output a step-by-step execution log or compliance checklist.** After writing artifacts, show:

**When `status = done`:**

```
✓ Explore 完成 — explore/advisory.json

[Priority Hints]
- PH-001 <hint> [high/medium]
- ...

[Watchlist]
- WL-001 <item> [high/medium] → <case-design category>
- ...

[Test Strategy 提案]
- approach: <test_strategy.approach>
- layers: <layer> (<depth>) — <rationale> ...

（low confidence 项已省略；详见 advisory.json）

下一步：运行 aa-case-design，Skill 将读取 test_strategy 提案与已记录的断言取舍。
```

- 仅展示 `confidence: high` 或 `medium` 的项。
- 若所有项均为 `low` 但有代码 evidence（SC-* 证据），输出：`⚠ 无历史证据；advisory 基于代码结构合成，置信度上限 medium。`
- 若所有项均为 `low` 且无任何 evidence，输出：`⚠ 无历史证据，advisory 未能提供有效信号。建议补充 qa/archive/ 后重试。`
- 不展示文件清单、执行步骤表、合规说明。

**When `status = unavailable`:**

```
⚠ Explore 跳过 — 历史数据不足（<degraded_reasons>），源码也为空
Phase 1 aa-case-design 将在无 advisory 的情况下继续。
```

**When `status = failed`:**

```
✗ Explore 失败 — <error>
```

---

## Phase 1 gate (summary)

- `status == pending` → Phase 1 **STOP**
- `status == done` → Phase 1 must read advisory; missing files → STOP
- `mode == required` and `status in [failed, unavailable]` → Phase 1 **STOP**
- `mode == advisory` and `status in [skipped, unavailable, failed]` → warning + continue without advisory
