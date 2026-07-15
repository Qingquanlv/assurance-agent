# AA Workflow — Fallback Runbook (archived full text)

> **Not the primary entry.** Prefer `aa workflow run` / `workflow_start`.
> This file is loaded only when the aa workflow driver is unavailable and the agent must
> orchestrate inline / via Scheme E. Keep in sync with phase skills + schema;
> do not duplicate into `SKILL.md`.

# AA Workflow (fallback)

## Purpose

This is the **orchestrator entry skill** for running the full AA QA workflow inside an OpenCode client.

**Mode binding:** `aa-workflow` is the **full / autonomous** entrypoint. It runs the existing end-to-end workflow without planned Explore / case-design clarification dialogue. If the user wants design-time questions, use `aa-intake` first and then `aa-execute`.

It is not a case design, review, or codegen skill. It coordinates the entire workflow by:

- Calling `aa status --change <id> --next --json` each iteration to get the deterministic dispatch list
- Dispatching each ready phase to a bounded `task` subagent (the agent named by the schema) that loads the phase skill and writes only that phase's produces
- After each dispatch hash check passes: running `aa gate check` for gated phases and applying state deltas to `workflow-state.yaml` (orchestrator-owned — subagents and phase CLI commands never touch it during dispatch)
- Running CLI phases (`execution`, `healing-rerun`) directly via `aa run`
- Stopping on terminal conditions (gate `stop`/`reject`, `aa status` terminal flag, or max-iteration guard)
- Producing a structured final summary via `aa report`

Do not perform detailed case design, review, fix, or codegen work directly inside this skill — load the appropriate phase skill and execute it inline.

---

## Execution Mode — Detection in Phase 1.1

This workflow supports two execution modes. **Phase 1.1 probes the `task` tool** and sets `execution_mode` in `workflow-state.yaml`:

| `execution_mode` | Condition | Behaviour |
|---|---|---|
| `subagent-dispatch` | `task` tool call succeeded in Phase 1.1 probe | Scheme E: each phase delegated to a bounded `task` subagent **except `explore`** (see below) |
| `inline` | `task` tool unavailable or probe failed | All phases execute directly in the primary agent |

Regardless of mode, the **orchestrator always owns** `aa status --next --json`, `aa gate check`, and `workflow-state.yaml`. Subagents (in dispatch mode) never touch these.

---

## Execution Mode — Scheme E (subagent-dispatch)

Used when `execution_mode == subagent-dispatch`. The primary agent is the orchestrator; it never does phase work itself **except Phase 1.2 (`aa-explore`) in fallback mode**, which runs **inline in the primary agent** (see Explore rule below). All other ready phases are dispatched to bounded `task` subagents. Under `aa workflow run`, explore is dispatched to `aa-doc-author`.

### Explore rule (fallback vs driver)

| Mode | Explore executor |
|---|---|
| **Driver** (`aa workflow run`) | Dispatch to `aa-doc-author` (Bash floor allows `aa risk *`) |
| **Fallback task mode** (this skill) | **Inline** in the primary agent — do **not** `task(agent=aa-doc-author, skill=aa-explore)` |

| Reason (fallback only) | Detail |
|---|---|
| Bash required | Explore Steps 1 and 6 call `aa risk context` / `aa risk validate-advisory` |
| Subagent runtime | OpenCode `task` subagents typically receive **no Bash tool**; `permission.bash` cannot create one |
| Authorized executor (fallback) | Primary agent only — full `aa-explore` skill (Steps 1–7) |

When `aa status --next --json` returns `explore` under **fallback**, the orchestrator executes it inline, then applies `phases.explore` state delta to `workflow-state.yaml` before continuing the loop.

### Determinism boundary (invariant)
| Owned by orchestrator (primary agent) | Owned by phase subagent |
|---|---|
| `aa status --next --json` (what runs next) | Load the named phase skill |
| `aa gate check` (may it advance) | Produce only that phase's `produces` files |
| Write `workflow-state.yaml` | Nothing else |
| `aa run` for CLI phases | — |
| **`aa-explore` (Phase 1.2) — inline in fallback; driver-dispatched otherwise** | — |

The subagent never runs `aa gate`, `aa status`, or any other gate-decision command, and never modifies `workflow-state.yaml`. These constraints are enforced by `.opencode/agents/*.md` permission floors (bash deny + explicit `workflow-state.yaml: deny`) and verified by the orchestrator's state-hash check (see Phase 0).

### Orchestration loop (subagent-dispatch mode)

```text
loop (max 50 iterations):
  dispatch_list = aa status --change <id> --next --json  → .next[]
  if terminal: break
  H0 = sha256(workflow-state.yaml)         ← state corruption check
  for each entry in dispatch_list:
    if entry.phase == "explore":
      # INLINE-ONLY — never dispatch; subagents lack Bash for aa risk CLI
      Read aa-explore/SKILL.md from disk; set phases.explore.skill_loaded = true
      Execute aa-explore inline in primary agent (Steps 1–7: risk context, source read, advisory.json, validate-advisory)
      Verify explore/context.json and explore/advisory.json on disk
      Apply phases.explore state delta to workflow-state.yaml
      aa gate check --phase explore --change <id> --json   (only when schema gate applies)
      H0 = sha256(workflow-state.yaml)     ← re-snapshot after orchestrator-owned explore state delta
    else if entry.kind == "cli":
      bash: aa run --change <id>          ← execution / healing-rerun; writes artifacts/events only
    else:
      task(subagent = entry.agent,
           prompt  = "Call skill(name='<entry.skill>'). Produce only the
                      outputs for phase <entry.phase> as described in the
                      skill. Do NOT run aa gate/status. Do NOT modify
                      workflow-state.yaml.")
  assert sha256(workflow-state.yaml) == H0  ← HALT if any subagent dispatch body touched state
  for each gated phase that just completed:
    aa gate check --phase <phase> --change <id> --json
  for each completed CLI phase:
    aa state apply --change <id> --phase <phase>  ← execution / healing-rerun
  for each completed CLI-backed agent phase:
    aa state apply --change <id> --phase <phase>  ← inspect / report
  apply subagent-reported state_delta to workflow-state.yaml
after terminal: prompt user for archive
```

Independent branches (`api-plan`/`e2e-plan`, `api-codegen`/`e2e-codegen`) may run as parallel `task` calls within the same iteration. All others run sequentially.

### Subagent retry on failure

If a subagent returns without producing the expected `produces` files, `aa status` keeps the phase `ready`. The orchestrator retries the same phase at most **2 times**. On repeated failure, halt and report which phase failed and what files are missing.

---

## Invocation

Users invoke this workflow in OpenCode by loading the skill directly:

```text
use skill aa-workflow

Requirement:
<describe what to test>

Run mode:
full

Max case fix attempts:
2

Max plan fix attempts:
2

Force continue:
false
```

---

## Runtime Parameters

| Parameter | Default | Description |
|---|---|---|
| `run_mode` | `full` | Which phases to execute |
| `test_types` | `api,e2e` | Which test types to run: `api`, `e2e`, or `api,e2e` |
| `max_case_fix_attempts` | `2` | Max times to run aa-case-fixer before giving up |
| `max_plan_fix_attempts` | `2` | Max times to run aa-e2e-plan-fixer before giving up |
| `force_continue` | `false` | Bypass **only** `human_review_required` and `risk_level in ["high","critical"]` stops. **Must NOT** bypass codegen hard gates (see **Codegen Hard Gates** and **force_continue Boundaries**) |
| `run_dashboard` | `false` | Whether to launch aa-dashboard after completion |
| `run_tests` | `true` | Whether to execute tests via aa-run after codegen |
| `max_healing_attempts` | `3` | Max healing attempts after inspect finds `fix_proposal_eligible` failures |
| `auto_archive` | `false` | Deprecated/ignored by `aa-workflow`: the workflow never archives automatically. Phase 14 only reports whether the change is archive-eligible. Run `aa-archive` separately only after an explicit user request. |
| `auto_archive_with_fallback` | `false` | Deprecated/ignored by `aa-workflow`: fallback PASS_WITH_WARNINGS can be reported as archive-eligible only with explicit human confirmation outside the workflow. |
| `e2e_framework` | `python-playwright` | Runtime parameter — E2E stack selection (`python-playwright` only; TS Playwright not supported). Maps to `phases.e2e_codegen.generated_tests.framework: pytest-playwright` in workflow-state. |


Users may override any parameter in the OpenCode message. Parameters not provided use the defaults above. After resolving overrides, write the final values to `workflow-state.yaml.params`; all later `aa status` and `aa gate check` calls use that block as the deterministic runtime parameter source.

After `workflow-state.yaml.params` exists, stamp this derived run context before any phase dispatch:

```bash
aa state configure --change <change-id> --orchestrator aa-workflow
```

This writes `run_context.orchestrator_skill = aa-workflow`, `interaction_mode = autonomous`, and `active_scope = full`. Do not hand-write `run_context`; use the CLI so run_mode/scope consistency checks run.

### Healing evidence enforcement (CLI-authoritative)

Do not hand-write healing status or attempts in `workflow-state.yaml`. Mechanical status
(`proposal_created` / `applied`) and attempt count are derived from the active execution
batch, fresh analysis, proposal SHA, CLI apply events, safety evidence, and decisions.
`aa state heal --to` remains only for orchestrator judgments.

```bash
# After fixers, aa heal record-apply, safety pass/accepted risk, and applied-tree pin
aa run --change <id>
aa state apply --change <id> --phase healing-rerun

# After the re-inspect (validates source_batch_id freshness, records phases.healing_reinspect)
# The aa workflow driver supplies freshness and skill-load evidence in-process.
aa state apply --change <id> --phase healing-reinspect

# After healing-reinspect shows no eligible failures
aa state heal --change <id> --to resolved
```

**Human review STOP recovery** — never hand-write `review/*.json` to `decision: pass`:

```bash
aa decide --change <id> --at e2e-plan-review \
  --action fix_and_proceed --reason "<user decision>"
# Then run fixer (human_approved mode) → reviewer re-review → aa gate check
```

`aa status` audits gate hash drift (`tampered`), illegal verdict migrations, and healing consistency on every call.

---

## Supported Run Modes

| Mode | Phases Executed |
|---|---|
| `full` | All phases from case design to test execution (API + E2E) |
| `case-only` | Case design + case review (+ fix if needed) |
| `api-only` | API plan + plan review (+ fix if needed) + API codegen + run |
| `e2e-only` | E2E plan + plan review (+ fix if needed) + E2E codegen + run |
| `plan-only` | API + E2E plan + plan review (+ fix if needed). Requires existing case files. |
| `codegen-only` | API + E2E codegen only. Requires existing plan files. |
| `review-case` | Case review + fix only. Requires existing case files. |
| `review-plan` | Plan review + fix only. Requires existing plan files. |

Default: `full`.

**Important — `aa-case-design` is only invoked in `full` and `case-only` modes.**

For `api-only`, `e2e-only`, `plan-only`, `codegen-only`, `review-case`, and `review-plan`:

- Do **not** invoke `aa-case-design`.
- Require a valid `change_id` from the user before starting.
- Validate that the required input files for the selected mode already exist before proceeding.
- If required files are missing, **stop** and tell the user which files are needed.

| Mode | Required files before starting |
|---|---|
| `api-only` | `qa/changes/<change-id>/cases/**/*.yaml` |
| `e2e-only` | `qa/changes/<change-id>/cases/**/*.yaml` |
| `plan-only` | `qa/changes/<change-id>/cases/**/*.yaml` |
| `codegen-only` | Per `test_types`: `plans/api-plan.md`, `plans/api-test-data-plan.md`, `plans/api-codegen-plan.md`, `review/api-plan-review.json` (API); `plans/e2e-plan.md`, `plans/e2e-test-data-plan.md`, `plans/e2e-codegen-plan.md`, `plans/m4-review-summary.md`, `review/plan-review.json` (E2E); `.aa/data-knowledge.yaml` (always) |
| `review-case` | `qa/changes/<change-id>/cases/**/*.yaml` |
| `review-plan` | Per `test_types`: API — `plans/api-plan.md`, `plans/api-test-data-plan.md`, `plans/api-codegen-plan.md`, `plans/m3-review-summary.md`; E2E — `plans/e2e-plan.md`, `plans/e2e-test-data-plan.md`, `plans/e2e-codegen-plan.md`, `plans/m4-review-summary.md` |

---

## Phase 0 — Test Infra Bootstrap (one-shot, idempotent)

Runs **once per project**, before Phase 1.1. Scaffolds the shared pytest/Locust test infrastructure that all later phases silently assume is on disk:

- `tests/config.py` — unified settings shared by API / E2E / Fuzz / Performance.
- `tests/conftest.py` — root conftest including the session-autouse `_verify_sut_ready` fixture (API / E2E / Fuzz; Performance has its own readiness contract in `aa-performance-codegen`).
- `tests/schema_validation.py` — `assert_matches_schema` + `_LOCAL_SCHEMAS` registry used by `aa-api-codegen` happy-path tests.

```yaml
phases:
  test_infra_bootstrap:
    status: pending | done | skipped | failed
    files:
      - tests/config.py
      - tests/conftest.py
      - tests/schema_validation.py
    created: []   # subset newly written this run
    kept: []      # subset that already existed and passed the contract
```

### Steps

```
Load aa-test-infra-bootstrap
  The skill:
    - checks each of the 3 files against its per-file contract on disk
    - CREATEs any missing file (with defaults sourced from run.py / web/.env / app/core/init_app.py — asks the user when a default cannot be verified)
    - KEEPs any contract-conformant file untouched
    - STOPs and asks the user if a file exists but does NOT match the contract (never silently overwrites)
    - determines the phases.test_infra_bootstrap state delta (status, files, created, kept);
      the orchestrator applies it to workflow-state.yaml (see State-delta convention)
```

### Gate

- `status == done` and all 3 files present → Phase 1.1 may proceed.
- `status == pending` → **STOP** (must run `aa-test-infra-bootstrap` or set `skipped` with explicit user override).
- Any file BLOCKED by contract mismatch → **STOP**, surface the diff to the user.

**Boundary:** Phase 0 only writes the 3 shared scaffold files under `tests/`. It must not generate fixtures, helpers, factories, or test code (those belong to the `aa-*-codegen` skills).

---

## Phase 1.1 — Skill Registry Check

Before starting any workflow phase, verify all required skills can be loaded in the **primary agent**, then **detect the execution mode**.

```yaml
required_skills:
  - aa-test-infra-bootstrap
  - aa-explore
  - aa-case-design
  - aa-case-reviewer
  - aa-case-fixer
  - aa-fact-baseline
  - aa-api-plan
  - aa-e2e-plan
  - aa-api-plan-reviewer
  - aa-e2e-plan-reviewer
  - aa-api-plan-fixer
  - aa-e2e-plan-fixer
  - aa-api-codegen
  - aa-e2e-codegen
  - aa-run
  - aa-inspect
  - aa-report-generator

conditional_skills:
  # Loaded only when the scoped cases include the matching test type (see Phase 2.5 Layer Scan).
  # Fuzz layer (cases with type == Fuzz):
  - aa-fuzz-plan            # Phase 3C: schemathesis fuzz plan
  - aa-fuzz-plan-reviewer   # Phase 4C: fuzz plan review
  - aa-fuzz-codegen         # Phase 6C: fuzz test codegen
  # Performance layer (cases with type == Performance):
  - aa-performance-plan            # Phase 3D: Locust performance plan
  - aa-performance-plan-reviewer   # Phase 4D: performance plan review
  - aa-performance-codegen         # Phase 6D: performance test codegen
  # If a layer is in scope but its skills are missing: STOP before Phase 3 and report
  # the missing skill — do NOT silently drop the layer (that would understate quality).

healing_skills:
  - aa-fix-proposal        # Phase 9: generate fix proposals from failure-analysis.json
  - aa-api-codegen-fixer   # Phase 10A: apply API test code fixes
  - aa-e2e-codegen-fixer   # Phase 10B: apply E2E test code fixes
  # Required for healing loop when max_healing_attempts > 0.
  # Missing healing skills do NOT block Phases 2.1–8, but set gates.healing_available = false.
  # If execution later FAIL and eligible failures exist: STOP after Phase 8 — cannot heal.
```

### Task-tool detection (determines execution_mode)

After skill loading, probe whether the `task` tool is callable:

```
Task Tool Detection:
  → Attempt to call the task tool with a minimal no-op:
      task(prompt  = "Reply with the single word: task-tool-available. Do nothing else.",
           agent   = "aa-doc-author")   ← use a known bounded agent with minimal permissions
  → If the call returns successfully (any response received):
      task_tool_available = true
      execution_mode = subagent-dispatch
  → If the call errors / the task tool is not in the available tool list / the call is rejected:
      task_tool_available = false
      execution_mode = inline
      Record warning: TASK-TOOL-UNAVAILABLE (see below)
```

The probe response content is irrelevant — only whether the call itself succeeded without error. Do NOT retry the probe; a single failure is sufficient to set `execution_mode = inline`.

**Warning written when task tool is unavailable:**

```yaml
- id: TASK-TOOL-UNAVAILABLE
  severity: low
  title: task tool not available — falling back to inline execution
  detail: >
    The task tool could not be called successfully during Phase 1.1 detection.
    This workflow will execute all phases inline in the primary agent instead
    of using subagent dispatch (Scheme E). Parallel plan/codegen branches will
    run sequentially in inline mode.
  status: acknowledged
```

After completing skill verification and detection, **write the initial `workflow-state.yaml`** with resolved runtime `params`, `execution_mode`, and `phases.skill_registry_check.status = pass`. Then immediately stamp the derived run context:

```bash
aa state configure --change <change-id> --orchestrator aa-workflow
```

This marks the run as `interaction_mode: autonomous` and `active_scope: full`; downstream phase skills (`aa-explore`, `aa-case-design`) must read it before deciding whether to ask planned clarification questions.

---

## Phase 1.2 — Explore

Runs **after Phase 1.1** and **before Phase 2.1** (`aa-case-design`) when `run_mode` is `full` or `case-only`.

**Skip** when `run_mode == codegen-only` → set `phases.explore.status = skipped` and proceed to Phase 2.1 without advisory.

### workflow-state (initial fields)

```yaml
params:
  run_mode: full
  test_types: [api, e2e]
  run_tests: true
  max_case_fix_attempts: 2
  max_plan_fix_attempts: 2
  max_healing_attempts: 3

phases:
  explore:
    status: pending | done | skipped | unavailable | failed
    mode: advisory          # advisory | required — default advisory
    weak_data_treat_as: done  # done | unavailable — default done
    outputs: []
    priority_hints_count: 0
    watchlist_high_count: 0
    degraded: false
    validation_errors: []
```

### Steps

**Executor:** primary agent **always** (inline), including when `execution_mode == subagent-dispatch`. Never dispatch to `aa-doc-author`.

```
Load aa-explore in primary agent
  The skill owns the full pipeline internally:
    - runs aa risk context (CLI) — requires Bash; reason explore is inline-only
    - applies weak_data_treat_as branch
    - shallow-reads source code structure
    - writes explore/advisory.json only
    - runs aa risk validate-advisory (CLI)
    - reports an orchestrator-owned state delta for phases.explore (status, counts, validation_errors, skill_loaded)
```

See `aa-explore` SKILL.md for the step-by-step.

**Boundary:** Phase 1.2 only produces `explore/` artifacts. Do not inline or simulate `aa-case-design`.

### Phase 2.1 gate (summary)

- `status == pending` → **STOP** (must run Phase 1.2 or explicit `skipped`)
- `status == done` → Phase 2.1 **must read** advisory; missing files → STOP
- `mode == required` and `status in [failed, unavailable]` → **STOP**
- `mode == advisory` and `status in [skipped, unavailable, failed]` → warning + continue without advisory

---

If any **required** skill fails to load via the skill tool:

First, attempt the **on-disk fallback**:

- Verify the skill's `SKILL.md` exists on disk at the expected skill path.
- If it exists: read the file directly, follow its instructions inline, and record a `SKILL-TOOL-UNREGISTERED` warning in `workflow-state.yaml`.
- Treat on-disk direct read as functionally equivalent to `use skill <name>` — the LLM reads and follows the skill content either way.

Only **STOP** if the skill cannot be loaded **and** its `SKILL.md` does not exist on disk:

- Report the missing skill name.
- Do not start case design.
- Do **not** check subagent skill resolution — this workflow does not use subagent skill loading.

**Fixed warning — skill tool registration failure with on-disk fallback:**

```yaml
- id: SKILL-TOOL-UNREGISTERED
  severity: low
  title: Skill exists on disk but not registered with skill tool
  detail: >
    One or more required skills could not be loaded via the skill tool but
    were found on disk. The workflow will read SKILL.md directly as an equivalent
    fallback. This may indicate a skill path conflict or OpenCode registration
    timing issue.
  affected_skills: []   # list skill names here
  status: acknowledged
```

If any **healing_skills** fail to load:

- Do **not** stop at Phase 1.1 — phases 2.1–8 are unaffected.
- Record warning in `workflow-state.yaml` under `agent_warnings`:
  - `AA-HEALING-SKILLS-UNAVAILABLE` (fixed ID — see below)
- Set `gates.healing_available = false` in `workflow-state.yaml`.
- If `phases.execution.status == FAIL` and `inspect/failure-analysis.json` contains `fix_proposal_eligible` failures, **STOP** after Phase 8 and report that healing skills are unavailable — do not proceed to archive.
- If `phases.execution.status in [PASS, PASS_WITH_WARNINGS]`, healing is not needed and can be skipped safely.

**When `max_healing_attempts == 0`:** treat healing as disabled regardless of whether healing skills loaded. Set `gates.healing_available = false` and `phases.healing.status = skipped`.

**Fixed warning — healing skills unavailable:**

```yaml
- id: AA-HEALING-SKILLS-UNAVAILABLE
  severity: medium
  title: Healing skills are unavailable or disabled
  impact:
    - healing loop cannot run if execution fails with eligible failures
    - STOP after Phase 8 if FAIL with eligible failures
  status: acknowledged
```

---

## Phase Dispatch Table

The orchestrator does NOT hardcode a phase sequence. It calls `aa status --change <id> --next --json` each iteration and dispatches exactly what the schema engine returns. The table below shows the static mapping baked into `workflow-schema.yaml`; it is shown here for reference only.

| Schema phase id | kind | skill | agent / executor |
|---|---|---|---|
| skill-registry-check | orchestrator-internal | — | — |
| explore | **inline-only** | aa-explore | **primary agent** (never `task` / `aa-doc-author`) |
| case-design | agent | aa-case-design | aa-doc-author |
| case-review | agent | aa-case-reviewer | aa-reviewer |
| case-fix | agent | aa-case-fixer | aa-doc-author |
| fact-baseline | agent | aa-fact-baseline | aa-doc-author |
| api-plan | agent | aa-api-plan | aa-doc-author |
| api-plan-review | agent | aa-api-plan-reviewer | aa-reviewer |
| api-plan-fix | agent | aa-api-plan-fixer | aa-doc-author |
| api-codegen | agent | aa-api-codegen | aa-test-author |
| e2e-plan | agent | aa-e2e-plan | aa-doc-author |
| e2e-plan-review | agent | aa-e2e-plan-reviewer | aa-reviewer |
| e2e-plan-fix | agent | aa-e2e-plan-fixer | aa-doc-author |
| e2e-codegen | agent | aa-e2e-codegen | aa-test-author |
| execution | cli | — | — |
| inspect | agent | aa-inspect | aa-reviewer |
| report | agent | aa-report-generator | aa-reporter |
| fix-proposal | agent | aa-fix-proposal | aa-doc-author |
| api-codegen-fix | agent | aa-api-codegen-fixer | aa-test-author |
| e2e-codegen-fix | agent | aa-e2e-codegen-fixer | aa-test-author |
| healing-rerun | cli | — | — |
| healing-reinspect | agent | aa-inspect | aa-reviewer |
| archive | agent | aa-archive | aa-archiver |

After each dispatch batch, the orchestrator first verifies the `workflow-state.yaml` hash is unchanged, then applies state deltas before calling `aa status` again. Dispatch-table CLI phases use `aa state apply --change <id> --phase execution|healing-rerun` (auto-stamped `skill_loaded: n/a`); the aa workflow driver applies agent-backed phases through the core reducer with its internal freshness watermark and SKILL.md path.

---

## Full Runbook (explore → report, state-write obligations)

This ordered runbook makes **every autonomous phase** gap-free, in the same style as `aa-intake/SKILL.md` (front phases) and `aa-execute/SKILL.md` (execute phases). The Phase Dispatch Table above says *which* skill/agent runs each phase; this runbook says *what state write is mandatory after each phase*. It exists because producing artifacts on disk is **NOT** phase completion — the recurring failure mode is "`aa run` wrote `execution/` but `phases.execution.status` stayed `pending` / `skill_loaded: false`, so the workflow stalled before inspect".

**Relationship to `aa-intake` (front phases — explore → case-review).** The front-phase *sequence and state writes* are identical to the `aa-intake` Intake Runbook; the only difference is **interaction mode**, which `aa state configure --orchestrator aa-workflow` pins to `autonomous` (vs `interactive` for intake). In autonomous mode the phase skills read `run_context.interaction_mode` and MUST NOT prompt the user:

- **Explore** answers open questions via `auto_default` (no per-pitfall user questions).
- **Case Design** records an autonomous approval (`approval.approved_by != user`); it does not block for explicit user sign-off.
- **Case Review** still runs `aa gate check`; a `reject` / `needs_human_review` verdict still **STOPs** (autonomous mode cannot self-approve a human gate).

Everything else (phase order, produces, gate checks, `⟶ state:` writes) is the same in both orchestrators — keep them in lockstep when editing.

**State-write mechanism differs by phase (do not skip):**

- **CLI-backed phases** (`execution`, `inspect`, `healing-rerun`, `healing-reinspect`, `report`) — the `aa state apply --change <id> --phase <x>` call **IS** the state write. It is mandatory and enforced. Running the underlying tool (`aa run`, `aa-inspect`, `aa-report-generator`) and seeing output files is not enough.
- **Agent phases** (`fact_baseline`, `api_plan`, `e2e_plan`, `*_plan_review`, `api_codegen`, `e2e_codegen`, `archive`) — no CLI writer; the orchestrator MUST hand-update `phases.<x>.status` (+ produces/gate refs) in `workflow-state.yaml` before advancing.
- **Healing status** (`healing.status`) — CLI-only via `aa state heal`; never hand-write.

Every line below ends with its state-write obligation as `⟶ state: …`. After each `⟶ state:` write, run `aa status --change <id> --next --json` and dispatch exactly what it returns. Do not skip a step because it "looks done" — verify each phase's produces on disk first. The per-phase input/output/gate contracts remain the **Phase Contracts** section below; this runbook only sequences them and pins the state write.

```
Phase 0 — Test Infra Bootstrap   (one-shot, idempotent; before Phase 1.1)
  → load aa-test-infra-bootstrap; CREATE missing / KEEP conformant / STOP on contract mismatch
  → autonomous: ask user ONLY when a default cannot be verified from run.py / web/.env / init_app.py
  ⟶ state: hand-update phases.test_infra_bootstrap.status = done (+ created/kept); gate → Phase 1.1

Phase 1.1 — Skill Registry Check + execution-mode detect   (orchestrator-local)
  → verify required skills loadable; probe task tool → execution_mode = subagent-dispatch | inline
  → write initial workflow-state.yaml; aa state configure --change <id> --orchestrator aa-workflow
    (stamps interaction_mode: autonomous, active_scope: full — do NOT hand-write run_context)
  ⟶ state: phases.skill_registry_check.status = pass; then aa status

Phase 1.2 — Explore   (INLINE in primary agent; never dispatched, even in subagent-dispatch mode)
  → load aa-explore; interaction_mode: autonomous → answer open questions via auto_default (NO user prompts)
  → write explore/advisory.json; run aa risk validate-advisory
  → skip when run_mode == codegen-only (status = skipped)
  ⟶ state: hand-update phases.explore.status = done|skipped|unavailable (+ counts); then aa status

Phase 2.1 — Case Design   (autonomous: NO user-approval gate)
  → dispatch aa-case-design → .qa.yaml + proposal.md + cases/<module>/case.yaml
  → autonomous approval recorded by the skill (approval.approved_by != user); do not block for user sign-off
  ⟶ state: hand-update phases.case_design.status = done (+ outputs); then aa status

Phase 2.2 — Case Review (initial)
  → dispatch aa-case-reviewer → review/case-review.json + case-review-summary.md (reviewer JSON is the gate)
  → aa gate check --change <id> --phase case-review --json      ← REQUIRED
  ⟶ state: hand-update phases.case_review.status + gate verdict ref; then aa status

Phase 2.3 — Case Fix loop   (if gate needs_fix)
  → up to max_case_fix_attempts: aa-case-fixer → aa-case-reviewer → aa gate check
  → pass → exit loop; reject / needs_human_review → STOP (autonomous cannot self-approve); exhausted → STOP
  ⟶ state: hand-update phases.case_review.status after each re-review; then aa status

Phase 2.4 — Fact Baseline
  → dispatch aa-fact-baseline → facts/fact-baseline.json
  → if facts unavailable: write source: unavailable + warning, DO NOT stop
  ⟶ state: hand-update phases.fact_baseline.status = done (+ file/source refs); then aa status

Phase 2.5 — Layer Scan  (orchestrator-local)
  → resolve layers.{api,e2e,fuzz,performance} from test_types + case types

[API branch — if layers.api]
Phase 3A — API Plan            → dispatch aa-api-plan → plans/api-*.md
  ⟶ state: hand-update phases.api_plan.status = done (+ produces ref); then aa status
Phase 4A — API Plan Review     → dispatch aa-api-plan-reviewer → review/api-plan-review.json
  → aa gate check --change <id> --phase api-plan-review --json      ← REQUIRED
  ⟶ state: hand-update phases.api_plan_review.status + gate verdict ref; then aa status
Phase 5A — API Plan Fix loop   → if needs_fix: aa-api-plan-fixer → aa-api-plan-reviewer → aa gate check
  → repeat up to max_plan_fix_attempts; reject / human_review_required → STOP (see State machine enforcement)
Phase 6A — API Codegen pre-check → verify .aa/data-knowledge.yaml exists, else STOP (hard gate; force_continue MUST NOT bypass)
Phase 6A — API Codegen         → dispatch aa-api-codegen (INLINE per Phase-Local Execution Rule) → codegen/api-codegen-summary.md + target files
  ⟶ state: hand-update phases.api_codegen.status = done (+ produces ref); then aa status

[E2E branch — if layers.e2e]
Phase 3B — E2E Plan            → dispatch aa-e2e-plan → plans/e2e-*.md
  ⟶ state: hand-update phases.e2e_plan.status = done (+ produces ref); then aa status
Phase 4B — E2E Plan Review     → dispatch aa-e2e-plan-reviewer → review/plan-review.json
  → aa gate check --change <id> --phase e2e-plan-review --json      ← REQUIRED
  ⟶ state: hand-update phases.e2e_plan_review.status + gate verdict ref; then aa status
Phase 5B — E2E Plan Fix loop   → if needs_fix: aa-e2e-plan-fixer → aa-e2e-plan-reviewer → aa gate check
Phase 6B — E2E Codegen pre-check → verify .aa/data-knowledge.yaml exists, else STOP (hard gate)
Phase 6B — E2E Codegen         → dispatch aa-e2e-codegen (INLINE) → codegen/e2e-codegen-summary.md + target files
  → framework pytest-playwright; never emit *.spec.ts
  ⟶ state: hand-update phases.e2e_codegen.status = done (+ produces ref); then aa status

[Fuzz / Performance branches — if layers.fuzz / layers.performance: same Plan → Review(gate check) → Codegen shape, INLINE codegen]

Phase 6 Completion Gate
  → for each selected layer: phases.<layer>_codegen.status == done AND summary + all Target Files on disk, else STOP
  → if run_tests == false: phases.execution.status = SKIPPED → jump to Phase 13/14

Phase 7 — Execution  (MANDATORY when run_tests == true and Phase 6 gate passed; skipping is a STOP)
  → bash: aa run --change <id>
  → verify execution/execution-manifest.yaml on disk; read final_status from it (never from a claim)
  → TEST-CHANGE DISCIPLINE: after this formal batch, modifying test files is healing-only unless a current-tree `execution.test-changes` decision exists
  ⟶ state (CLI-backed, MANDATORY): aa state apply --change <id> --phase execution
  → then aa status

Phase 8 — Inspect  (if execution FAIL / PASS_WITH_WARNINGS, or known-product-issues present)
  → dispatch aa-inspect → inspect/failure-analysis.json + failure-summary.md
  ⟶ state (driver-owned, MANDATORY): apply inspect through the in-process core reducer
  → Inspect Safety Gate: if inspect modified any forbidden file → STOP
  → Healing entry decision:
      PASS / PASS_WITH_WARNINGS and no fix_proposal_eligible → aa state heal --to not_needed → Phase 13
      FAIL and no fix_proposal_eligible                       → aa state heal --to not_needed → STOP (needs human)
      gates.healing_available == false                        → aa state heal --to skipped ; FAIL → STOP
      inspect_mode != primary                                 → STOP (healing needs primary inspect)
      FAIL and fix_proposal_eligible exist                    → enter Healing loop (Phases 9–12; do NOT jump to Report)
  → Read evidence-derived healing status before proceeding.

[Healing loop — Phases 9–12: see Phase Contracts. healing.status CLI-only via aa state heal;
 healing-rerun/healing-reinspect are CLI-backed ⟶ aa state apply --phase healing-rerun|healing-reinspect]

Phase 13 — Report  (terminal, non-gating)
  → PRECONDITION: healing.status ∈ {not_needed, skipped, resolved, exhausted, failed}, else go back to Phase 8 / healing loop
  → dispatch aa-report-generator → report/quality-report.{json,md} + executive-summary.md
  ⟶ state (driver-owned, MANDATORY): apply report through the in-process core reducer
  → then aa status

Phase 14 — Archive Eligibility Recommendation  (evaluate only; never load aa-archive here)
  ⟶ state: hand-update phases.archive.status = eligible | not_eligible (+ blockers)
```

**Skill Load Gate still applies to every runbook line** (see below): each dispatched phase's executor MUST read that phase's `SKILL.md` and the orchestrator MUST set `skill_loaded/skill_md_path/skill_loaded_at` before the phase's `status` reaches a terminal value.

- **CLI-only phases (`execution`, `healing-rerun`)** have no dedicated executor SKILL.md — the `aa run` CLI does the work. `aa state apply --phase execution|healing-rerun` stamps `skill_loaded: n/a` automatically.
- **Agent-backed CLI-assisted phases (`inspect`, `healing-reinspect`, `report`)** require their SKILL.md (`aa-inspect/SKILL.md`, `aa-report-generator/SKILL.md`). The aa workflow driver records skill-load and freshness evidence atomically through the core reducer; these values are internal rather than public CLI options.

---

## Phase Completion Rule

Loading a skill is **never** enough to mark a phase complete.

A phase is complete only when **all** of the following are true:

0. **Skill Load Gate (mandatory, mode-aware)** — before any phase work executes, the phase's SKILL.md MUST be read from disk by the **phase executor** (not from memory, not from a prior conversation turn):
   - `inline` mode **or `explore` phase**: the primary agent reads the SKILL.md itself, then sets `phases.<phase_name>.skill_loaded = true`, `skill_md_path = <absolute path read>`, `skill_loaded_at = <ISO 8601 timestamp>` in `workflow-state.yaml`.
   - `subagent-dispatch` mode (all phases **except `explore`**): the dispatched phase subagent reads the SKILL.md (the dispatch prompt names the skill) and MUST report the absolute SKILL.md path it read in its final message. The **orchestrator** then writes the same three fields into `workflow-state.yaml` on the subagent's behalf. The subagent never writes them itself. If the subagent's report does not confirm the skill was read, treat the phase as not executed and retry per the subagent retry rule.
   If `skill_loaded != true` when the phase's `status` is set to a terminal value (`done` / `pass` / `fail`), the workflow MUST treat the phase as **not complete** and STOP.
   This gate exists because the executor can otherwise skip reading SKILL.md and execute from plan files alone — which has caused traceability gaps (e.g. case_id not embedded in test function names, missing `assert_matches_schema` calls, missing `codegen-summary.md`).
1. The phase skill was loaded and executed by the assigned phase executor (primary agent in inline mode **and for `explore` in subagent-dispatch mode**; the dispatched subagent in subagent-dispatch mode for all other agent phases; for CLI phases, `aa run` / `aa report ...` completed successfully and wrote artifacts/events only).
2. The expected output files exist on disk (verified by reading them).
3. The output files were **re-read from disk** by the primary agent after the skill completed.
4. The orchestrator applied the phase's `state_delta` to `workflow-state.yaml` (status set to the correct terminal value). In dispatch mode this write happens **after** the state-hash check passes — never inside the subagent or inside a phase CLI command. For `execution`, `healing-rerun`, `inspect`, and `report`, use `aa state apply --change <change-id> --phase <phase>` at this point.
5. The next-phase gate was explicitly evaluated before advancing.
6. **CLI status and gate check are authoritative** (not advisory):
   - After writing each phase's outputs to disk, verifying the dispatch hash, and applying the state delta to `workflow-state.yaml`, run `aa status --change <change-id> --next --json` to determine whether the phase is `done` and which phase(s) run next.
   - For gated phases, also run `aa gate check --change <change-id> --phase <phase-id> --json` and record the verdict in `workflow-state.yaml` before advancing.
   - The CLI output is the ground truth — never override it with prose reasoning.
   - If the CLI is unavailable (binary missing, schema validation fails), halt and report the error before proceeding.

Do **not** report a phase as done based on:

- skill loaded
- subagent started but not yet returned
- background task still running
- chat summary from an agent
- agent claim without file verification
- partial output (some files present but not all required outputs)

**Codegen phases are especially strict** — see Phase 6A and Phase 6B Phase-Local Execution Rules.

---

## Skill Load Gate (Phase Entry Check)

Before executing **any** phase work (reading inputs, generating outputs, writing files):

1. Identify the skill name for the phase from the table below.
2. The **phase executor** reads the SKILL.md file from disk — not from conversation memory, not from a prior turn's context:
   - `inline` mode **or `explore` phase**: the primary agent reads it with the `Read` tool.
   - `subagent-dispatch` mode (phases **other than `explore`**): the dispatched subagent reads it (via `skill(...)` or `Read`) and reports the absolute path read in its final message.
3. The **orchestrator** (primary agent) updates `workflow-state.yaml` `phases.<phase_name>` with:
   - `skill_loaded: true`
   - `skill_md_path: <absolute path to SKILL.md>`
   - `skill_loaded_at: <ISO 8601 timestamp>`
   In inline mode **and for `explore` in subagent-dispatch mode**, the orchestrator writes this before starting the phase work. In dispatch mode for other phases, it writes this together with the phase's `state_delta`, after the subagent returns and the state-hash check passes — the subagent never writes `workflow-state.yaml`.

If the orchestrator attempts to set `phases.<phase_name>.status` to a terminal value (`done` / `pass` / `fail` / `skipped`) while `skill_loaded != true`, the workflow MUST STOP and report:

```
SKILL_LOAD_GATE_VIOLATION: phase <phase_name> cannot be marked <status> because skill_loaded is not true.
Read the SKILL.md at <skill_md_path> first, set skill_loaded=true, then re-execute the phase.
```

### Phase → Skill Path Map

The skill base directory is `/Users/lvqingquan/skills/assurance-workflow-skills/skills/`.

| Phase | skill_name | SKILL.md relative path |
|---|---|---|
| test_infra_bootstrap | aa-test-infra-bootstrap | `aa-test-infra-bootstrap/SKILL.md` |
| explore | aa-explore | `aa-explore/SKILL.md` |
| case_design | aa-case-design | `aa-case-design/SKILL.md` |
| case_review | aa-case-reviewer | `aa-case-reviewer/SKILL.md` |
| fact_baseline | aa-fact-baseline | `aa-fact-baseline/SKILL.md` |
| api_plan | aa-api-plan | `aa-api-plan/SKILL.md` |
| e2e_plan | aa-e2e-plan | `aa-e2e-plan/SKILL.md` |
| api_plan_review | aa-api-plan-reviewer | `aa-api-plan-reviewer/SKILL.md` |
| e2e_plan_review | aa-e2e-plan-reviewer | `aa-e2e-plan-reviewer/SKILL.md` |
| api_codegen | aa-api-codegen | `aa-api-codegen/SKILL.md` |
| e2e_codegen | aa-e2e-codegen | `aa-e2e-codegen/SKILL.md` |
| fuzz_plan | aa-fuzz-plan | `aa-fuzz-plan/SKILL.md` |
| fuzz_plan_review | aa-fuzz-plan-reviewer | `aa-fuzz-plan-reviewer/SKILL.md` |
| fuzz_codegen | aa-fuzz-codegen | `aa-fuzz-codegen/SKILL.md` |
| performance_plan | aa-performance-plan | `aa-performance-plan/SKILL.md` |
| performance_plan_review | aa-performance-plan-reviewer | `aa-performance-plan-reviewer/SKILL.md` |
| performance_codegen | aa-performance-codegen | `aa-performance-codegen/SKILL.md` |
| execution | — (CLI-only) | n/a — `aa run`; stamped `skill_loaded: n/a` |
| healing-rerun | — (CLI-only) | n/a — `aa run`; stamped `skill_loaded: n/a` |
| inspect | aa-inspect | `aa-inspect/SKILL.md` (driver stamps via core reducer) |
| healing (fix-proposal) | aa-fix-proposal | `aa-fix-proposal/SKILL.md` |
| healing (api-fixer) | aa-api-codegen-fixer | `aa-api-codegen-fixer/SKILL.md` |
| healing (e2e-fixer) | aa-e2e-codegen-fixer | `aa-e2e-codegen-fixer/SKILL.md` |
| report | aa-report-generator | `aa-report-generator/SKILL.md` (driver stamps via core reducer) |
| archive | aa-archive | `aa-archive/SKILL.md` |

Phases that do not have a dedicated SKILL.md (e.g. `skill_registry_check`, `layers`) are exempt — set `skill_loaded: n/a` for those.

### Why This Gate Exists

Without this gate, the primary agent can skip reading a phase's SKILL.md and execute from plan files alone. This has caused:

- **Traceability gaps**: `aa-api-codegen/SKILL.md` mandates `test_<case_id_lowercase>__<description>` function naming so the `aa` CLI can extract case_id from JUnit XML. Without reading the SKILL.md, the agent generated `test_list_roles_returns_paginated_envelope` — no case_id token, all 12 tests unmapped in execution results.
- **Missing mandatory artifacts**: `aa-api-codegen/SKILL.md` requires `codegen/api-codegen-summary.md` and `assert_matches_schema()` calls. Without reading the SKILL.md, neither was generated.
- **ORM field-length bugs**: `aa-api-codegen/SKILL.md` documents `Role.name max_length=20` and warns that overflow surfaces as HTTP 500, not 422. Without reading the SKILL.md, the agent generated names exceeding 20 chars and only discovered the bug after `aa run` failed with 500s.

The gate makes the "did I read the contract?" check **explicit and auditable** in `workflow-state.yaml`, rather than relying on the agent's self-discipline.

---

## workflow-state.yaml

Path: `qa/changes/<change-id>/workflow-state.yaml`

This file is the **canonical cross-phase state source**. Every phase reads it at entry. At exit, phase executors report or materialize a state delta; only the orchestrator writes `workflow-state.yaml`, and in subagent-dispatch mode that write happens after the dispatch hash check passes.

```yaml
schema_version: "1.0"
change_id: <change-id>
module: <module>

params:                      # resolved runtime parameters (defaults + user overrides)
  run_mode: full
  test_types: [api, e2e]
  run_tests: true
  max_case_fix_attempts: 2
  max_plan_fix_attempts: 2
  max_healing_attempts: 3

execution_mode: subagent-dispatch | inline   # set by Phase 1.1 task-tool detection

phases:
  # ─── Skill Load Gate (common fields — see "Skill Load Gate" section above) ───
  # Every phase below that has a dedicated SKILL.md MUST carry these three fields:
  #   skill_loaded: false | true | n/a   # n/a for phases without a dedicated SKILL.md
  #   skill_md_path: <absolute path>     # set when skill_loaded transitions to true
  #   skill_loaded_at: <ISO 8601>        # set when skill_loaded transitions to true
  # The workflow MUST NOT allow status to reach a terminal value while skill_loaded is false
  # (for phases that have a SKILL.md). See Phase Completion Rule item 0.

  skill_registry_check:
    status: pass | fail | skipped
    skill_loaded: n/a   # no dedicated SKILL.md — registry check is self-contained
    reason: <optional — e.g. standalone aa-case-design invocation>
    task_tool_available: true | false   # set by Phase 1.1 detection probe
    checked_skills:
      - aa-case-design
      - aa-case-reviewer
      - aa-case-fixer
      - aa-fact-baseline
      - aa-api-plan
      - aa-e2e-plan
      - aa-api-plan-reviewer
      - aa-e2e-plan-reviewer
      - aa-api-plan-fixer
      - aa-e2e-plan-fixer
      - aa-api-codegen
      - aa-e2e-codegen
      - aa-run
      - aa-inspect
      - aa-report-generator
      - aa-explore
      - aa-test-infra-bootstrap

  test_infra_bootstrap:
    status: pending | done | skipped | failed
    skill_loaded: false       # → true after Read(aa-test-infra-bootstrap/SKILL.md)
    skill_md_path: null       # → absolute path when skill_loaded=true
    skill_loaded_at: null     # → ISO 8601 timestamp when skill_loaded=true
    files: []      # tests/config.py, tests/conftest.py, tests/schema_validation.py
    created: []
    kept: []

  explore:
    status: pending | done | skipped | unavailable | failed
    skill_loaded: false       # → true after Read(aa-explore/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    mode: advisory | required
    weak_data_treat_as: done | unavailable
    outputs: []
    priority_hints_count: 0
    watchlist_high_count: 0
    degraded: false
    validation_errors: []

  case_design:
    status: pending | done | failed
    skill_loaded: false       # → true after Read(aa-case-design/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    gate_file: .qa.yaml       # case-design-gate checks .qa.yaml.approval in interactive intake
    outputs:
      - .qa.yaml
      - proposal.md
      - cases/<module>/case.yaml

  case_review:
    status: pending | pass | needs_fix | needs_human_review | reject
    skill_loaded: false       # → true after Read(aa-case-reviewer/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    gate_file: review/case-review.json
    fix_attempts: []   # appended by aa-case-fixer each run

  fact_baseline:
    status: pending | done | unavailable | failed
    skill_loaded: false       # → true after Read(aa-fact-baseline/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    # unavailable = seed file not found AND db_probe not configured; planning continues with warning
    source: null      # seed_file | db_probe | both | unavailable
    file: facts/fact-baseline.json

  # Phase 2.5 Layer Scan — which test layers are in scope (drives conditional phases 3C/3D/4C/4D/6C/6D)
  layers:
    api: true | false
    e2e: true | false
    fuzz: true | false
    performance: true | false

  api_plan:
    status: pending | done | failed
    skill_loaded: false       # → true after Read(aa-api-plan/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    outputs:
      - plans/api-plan.md
      - plans/api-test-data-plan.md
      - plans/api-codegen-plan.md

  e2e_plan:
    status: pending | done | failed
    skill_loaded: false       # → true after Read(aa-e2e-plan/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    outputs:
      - plans/e2e-plan.md
      - plans/e2e-test-data-plan.md
      - plans/e2e-codegen-plan.md

  api_plan_review:
    status: pending | pass | needs_fix | needs_human_review | reject | stale
    skill_loaded: false       # → true after Read(aa-api-plan-reviewer/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    # stale = review JSON was written before a fact correction; it cannot gate codegen until re-reviewed
    # reason: fact_correction_after_review | fact_mismatch_before_review
    stale_reason: null
    gate_file: review/api-plan-review.json
    fix_attempts: []   # appended by aa-api-plan-fixer each run

  e2e_plan_review:
    status: pending | pass | needs_fix | needs_human_review | reject | stale
    skill_loaded: false       # → true after Read(aa-e2e-plan-reviewer/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    stale_reason: null
    gate_file: review/plan-review.json
    fix_attempts: []   # appended by aa-e2e-plan-fixer each run

  api_codegen:
    status: pending | done | failed | stopped | needs_patch
    skill_loaded: false       # → true after Read(aa-api-codegen/SKILL.md) — CRITICAL: this phase has the most hard rules that get skipped
    skill_md_path: null
    skill_loaded_at: null
    # needs_patch = codegen completed but generated tests contain stale facts (e.g. wrong role name)
    # requires manual patch + re-collect before Phase 7 can proceed
    stop_reason: null   # set when status == stopped; e.g. "codegen delegated to nested/background worker; must execute in the assigned phase executor"
    needs_patch_reason: null  # set when status == needs_patch; e.g. "stale_role_name_fact"
    review_gate_file: review/api-plan-review.json
    codegen_readiness: ready | ready_with_warnings
    generated_tests:
      framework: pytest
      language: python
      files: []
      semantic_validity: valid | stale   # stale = generated with stale facts; set to valid after patch+collect
    warnings_carried: []
    known_product_issues:
      present: false
      file: null

  e2e_codegen:
    status: pending | done | failed | stopped | needs_patch
    skill_loaded: false       # → true after Read(aa-e2e-codegen/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    stop_reason: null   # set when status == stopped; e.g. "codegen delegated to nested/background worker; must execute in the assigned phase executor"
    needs_patch_reason: null
    review_gate_file: review/plan-review.json
    codegen_readiness: ready | ready_with_warnings
    generated_tests:
      framework: pytest-playwright   # from runtime e2e_framework=python-playwright
      language: python
      files: []
      semantic_validity: valid | stale
    data_setup:
      scripts: []
      reused: []
    fixtures:
      generated: []
      reused: []
    warnings_carried: []
    known_product_issues:
      present: false
      file: null

  # Fuzz layer phases — present only when layers.fuzz == true
  fuzz_plan:
    status: pending | done | failed | skipped
    skill_loaded: false       # → true after Read(aa-fuzz-plan/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    outputs: [plans/fuzz-plan.md, plans/fuzz-codegen-plan.md]
  fuzz_plan_review:
    status: pending | pass | needs_fix | reject | skipped
    skill_loaded: false       # → true after Read(aa-fuzz-plan-reviewer/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    gate_file: review/fuzz-plan-review.json
  fuzz_codegen:
    status: pending | done | failed | stopped | skipped
    skill_loaded: false       # → true after Read(aa-fuzz-codegen/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    generated_tests: { files: [] }   # tests/fuzz/**

  # Performance layer phases — present only when layers.performance == true
  performance_plan:
    status: pending | done | failed | skipped
    skill_loaded: false       # → true after Read(aa-performance-plan/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    outputs: [plans/performance-plan.md, plans/performance-codegen-plan.md]
  performance_plan_review:
    status: pending | pass | needs_fix | reject | skipped
    skill_loaded: false       # → true after Read(aa-performance-plan-reviewer/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    gate_file: review/performance-plan-review.json
  performance_codegen:
    status: pending | done | failed | stopped | skipped
    skill_loaded: false       # → true after Read(aa-performance-codegen/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    generated_tests: { files: [] }   # tests/perf/**

  execution:
    status: pending | PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED
    skill_loaded: n/a         # CLI-only phase (aa run); no dedicated executor SKILL.md. `aa state apply --phase execution` stamps n/a.
    skill_md_path: null
    skill_loaded_at: null
    batch_id: <YYYYMMDD-HHmmss>
    manifest: execution/execution-manifest.yaml
    started_at: <ISO 8601 timestamp>       # from events.jsonl execution_start
    completed_at: <ISO 8601 timestamp>     # from events.jsonl execution_end / phase_transition
    duration_ms: 0                         # from events.jsonl execution_end

  inspect:
    status: pending | done | failed | partial   # partial = fallback mode without classification
    skill_loaded: false       # → true after Read(aa-inspect/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    inspect_mode: primary | partial              # primary = CLI classified; partial = fallback summary only
    classification_performed: true | false
    outputs:
      - inspect/failure-analysis.json           # primary mode only (source of truth)
      - inspect/failure-summary.md              # source of truth
      - inspect/inspection-partial.json       # partial mode only
      - inspect/known-product-issues.md         # if known issues found or confirmed
      - inspect/inspection-error.json         # if primary CLI failed / outputs missing
      # execution/ copies are optional pointers for downstream consumers

  healing:
    status: pending | not_needed | proposal_created | applied | resolved | exhausted | skipped | failed
    skill_loaded: false       # → true after Read(aa-fix-proposal/SKILL.md) (Phase 9 entry)
    skill_md_path: null
    skill_loaded_at: null
    # Phases 10A/10B each have their own SKILL.md (aa-api-codegen-fixer / aa-e2e-codegen-fixer);
    # the orchestrator MUST Read each before invoking the corresponding fixer, but only one
    # skill_loaded field is kept here for the healing phase as a whole. If granular tracking
    # is needed, append to healing.attempts[].skill_loaded instead.
    # Status transitions:
    #   pending          → initial value
    #   not_needed       → no eligible failures after inspect, or eligible_count == 0 from fix-proposal
    #   proposal_created → Phase 9 wrote fix-proposal.json with eligible_count > 0
    #   applied          → Phase 10 fixers ran and Fixer Safety Gate passed (set by orchestrator)
    #   resolved         → Phase 12 re-inspect shows PASS or PASS_WITH_WARNINGS (healing succeeded)
    #   exhausted        → max_healing_attempts reached, still FAIL (set at end of Phase 12 loop)
    #   skipped          → healing disabled (gates.healing_available == false or max_healing_attempts == 0)
    #   failed           → fixer hard error (forbidden file, patch mismatch, fixer-error.json written)
    attempts: []
    # Each attempt:
    # - attempt: <n>
    #   source_analysis: inspect/failure-analysis.json
    #   eligible_failures: [<id>, ...]
    #   proposal_file: healing/fix-proposal.md
    #   proposal_json: healing/fix-proposal.json
    #   api_apply_status: applied | no_op | failed    # set by aa-api-codegen-fixer
    #   api_apply_summary: healing/api-apply-summary.json
    #   e2e_apply_status: applied | no_op | failed    # set by aa-e2e-codegen-fixer
    #   e2e_apply_summary: healing/e2e-apply-summary.json
    #   applied_files: [<path>, ...]                  # aggregate, set by orchestrator after safety gate
    #   rerun_batch_id: <YYYYMMDD-HHmmss>             # set after Phase 11
    #   result: PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED   # set after Phase 12
    #   stop_reason: <if result == stopped or failed>
    # NOTE: phases.healing.status = applied is set by the orchestrator ONLY after
    #       both fixers complete (api_apply_status and e2e_apply_status resolved)
    #       and the Fixer Safety Gate passes.

  report:
    status: pending | done | failed | skipped
    skill_loaded: false       # → true after Read(aa-report-generator/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    quality_score: <int 0-100 | null>   # from report/quality-report.json (CLI-computed)

  archive:
    status: pending | eligible | not_eligible | skipped
    skill_loaded: false       # → true after Read(aa-archive/SKILL.md)
    skill_md_path: null
    skill_loaded_at: null
    can_archive: true | false
    recommendation: <run aa-archive separately | do_not_archive | not_applicable>
    blockers: []

gates:
  data_knowledge:
    status: present | missing
    decision: pass | stop   # force_continue MUST NOT bypass this gate before codegen
  healing_available: true | false   # true if all healing_skills loaded AND max_healing_attempts > 0; false otherwise

known_product_issues: []

agent_warnings:
  # Authoritative CLI warnings (written when applicable).
  - id: AA-ORCHESTRATION-SHADOW-DISAGREEMENT
    severity: medium | high
    phase: <phase-id>
    command: aa status | aa gate check
    prose_decision:
      status: <done | stopped | next phase | gate verdict>
      reason: <brief prose-gate reason>
    cli_decision:
      status: <CLI phase status or gate verdict>
      next: [<phase-id>, ...]
      terminal: <null | stopped | completed>
    action_taken: followed_prose_workflow
    status: recorded
  - id: AA-ORCHESTRATION-SHADOW-UNAVAILABLE
    severity: low
    phase: <phase-id>
    command: aa status | aa gate check
    error: <CLI unavailable | schema missing | schema validation failed | command failed>
    action_taken: continued_with_prose_workflow
    status: recorded
```

---

## Phase Contracts

**State-delta convention (applies to every contract below):** `workflow-state.yaml` has exactly **one writer — the orchestrator (primary agent)**. Each contract's `state_delta:` block lists the `workflow-state.yaml` fields whose values the phase determines; it is **not** a file the phase executor writes.

- `inline` mode: the executor and the orchestrator are the same agent — it applies the delta itself immediately after producing the outputs.
- `subagent-dispatch` mode: the phase subagent NEVER writes `workflow-state.yaml` (permission floor + state-hash guard). It reports the delta values in its final message; the orchestrator verifies the output files on disk, then applies the delta. If a delta value is missing from the subagent's report, the orchestrator derives it by reading the phase's output files.

### Phase 2.1 — Case Design

```yaml
inputs:
  - user requirement text
  - qa/cases/** (existing cases)
  - source code (backend/frontend)
outputs:
  - qa/changes/<change-id>/.qa.yaml
  - qa/changes/<change-id>/proposal.md
  - qa/changes/<change-id>/cases/<module>/case.yaml
state_delta:
  - phases.case_design.status = done
  - phases.case_design.outputs
  # workflow-state.yaml itself is created by the orchestrator in Phase 1.1 if missing
```

### Phase 2.2 — Case Review

```yaml
inputs:
  - workflow-state.yaml
  - .qa.yaml
  - proposal.md
  - cases/<module>/case.yaml
outputs:
  - review/case-review.json
  - review/case-review-summary.md
state_delta:
  - phases.case_review.status = pass | fail
gate: decision == "pass"
```

### Phase 2.4 — Fact Baseline

```yaml
skill: aa-fact-baseline
inputs:
  - workflow-state.yaml
  - app/core/init_app.py (or equivalent seed/fixture file)
  - .aa/config.yaml (for db_probe flag)
  - live DB read-only probe (optional, if db_probe: true in config)
outputs:
  - facts/fact-baseline.json
state_delta:
  - phases.fact_baseline.status
  - phases.fact_baseline.source
  - phases.fact_baseline.file
  - phases.fact_baseline.warnings
gate: facts/fact-baseline.json must exist before Phase 4A or 4B run
```

**fact-baseline.json schema:**

```json
{
  "change_id": "<change-id>",
  "generated_at": "<ISO timestamp>",
  "source": "seed_file | db_probe | both",
  "seed_file": "<path to seed/init file read>",
  "facts": {
    "builtin_roles": [
      {"id": 1, "name": "管理员", "source": "seed_file"}
    ],
    "auth": {
      "default_username": "admin",
      "default_password": "<from seed>",
      "source": "seed_file"
    },
    "route_prefix": "/api/v1",
    "token_header": "Authorization",
    "token_scheme": "Bearer"
  },
  "warnings": []
}
```

If the seed file cannot be found and db_probe is false, write `fact-baseline.json` with `"source": "unavailable"` and record a warning — do **not** STOP; continue to Phase 3 but planners must note that fact validation is skipped.

### Phase 2.5 — Layer Scan *(orchestrator-local in both modes — determines which test layers run)*

Before dispatching plan phases, the orchestrator scans the finalized scoped cases to decide which of the four test layers are in scope. This step is never dispatched to a subagent. Each case has a single `type` (`API | E2E | Fuzz | Performance`) and an `automation.required` flag (see `aa-case-design`).

```yaml
inputs:
  - qa/changes/<change-id>/cases/**/case*.yaml   # finalized after Phase 2.3 (case fix)
state_delta:
  - layers block (written directly by the orchestrator)
```

```yaml
# workflow-state.yaml
layers:
  api:         true   # any case with type == API && automation.required == true
  e2e:         true   # any case with type == E2E && automation.required == true
  fuzz:        false  # any case with type == Fuzz && automation.required == true
  performance: false  # any case with type == Performance && automation.required == true
```

Rules:
- A layer is **in scope** when ≥1 finalized case has the matching `type` and `automation.required == true`.
- For every in-scope layer, its plan / review / codegen phases (A=API, B=E2E, C=Fuzz, D=Performance) are **mandatory** — skipping an in-scope layer understates quality and is a gate violation.
- If a layer is in scope but its `conditional_skills` are missing from the registry, **STOP** before Phase 3 and report the missing skill. Do **not** silently drop the layer.
- Layers that are not in scope are skipped cleanly; their `aa run` results report `SKIPPED` (not a failure) and the report marks the dimension `N/A`.

### Phase 3A — API Plan

```yaml
inputs:
  - workflow-state.yaml
  - proposal.md
  - cases/<module>/case.yaml
  - .aa/config.yaml
  - .aa/data-knowledge.yaml (or plans/data-knowledge.proposal.yaml)
  - facts/fact-baseline.json   ← NEW: validate key facts before writing plan
  - backend source files
outputs:
  - plans/api-plan.md
  - plans/api-test-data-plan.md
  - plans/api-codegen-plan.md
  - plans/m3-review-summary.md
state_delta:
  - phases.api_plan.status = done
```

### Phase 3B — E2E Plan

```yaml
inputs:
  - workflow-state.yaml
  - proposal.md
  - cases/<module>/case.yaml
  - .aa/config.yaml
  - .aa/data-knowledge.yaml (or plans/data-knowledge.proposal.yaml)
  - frontend source files
outputs:
  - plans/e2e-plan.md
  - plans/e2e-test-data-plan.md
  - plans/e2e-codegen-plan.md
  - plans/m4-review-summary.md
state_delta:
  - phases.e2e_plan.status = done
```

### Phase 4A — API Plan Review

```yaml
inputs:
  - workflow-state.yaml
  - plans/api-plan.md
  - plans/api-test-data-plan.md
  - plans/api-codegen-plan.md
outputs:
  - review/api-plan-review.json
  - review/api-plan-review-summary.md
state_delta:
  - phases.api_plan_review.status = pass | fail
gate:
  - decision == "pass"
  - codegen_readiness in ["ready", "ready_with_warnings"]
```

### Phase 4B — E2E Plan Review

```yaml
inputs:
  - workflow-state.yaml
  - plans/e2e-plan.md
  - plans/e2e-test-data-plan.md
  - plans/e2e-codegen-plan.md
outputs:
  - review/plan-review.json
  - review/plan-review-summary.md
state_delta:
  - phases.e2e_plan_review.status = pass | fail
gate:
  - decision == "pass"
  - codegen_readiness in ["ready", "ready_with_warnings"]
```

### Phase 6A — API Codegen

```yaml
inputs:
  - workflow-state.yaml
  - plans/api-plan.md
  - plans/api-test-data-plan.md
  - plans/api-codegen-plan.md
  - plans/m3-review-summary.md
  - review/api-plan-review.json  # full gate — see Codegen Hard Gates
  - .aa/data-knowledge.yaml     # must exist on disk
outputs:
  - files listed in api-codegen-plan.md Target Files only
    # helpers / fixtures / conftest are optional — only when plan + data-knowledge authorize
  - qa/changes/<change-id>/codegen/api-codegen-summary.md
state_delta:
  - phases.api_codegen: status, review_gate_file, codegen_readiness, generated_tests.files, warnings_carried, known_product_issues
gate:
  - Codegen Hard Gates (API) — force_continue MUST NOT bypass
```

### Phase 6A Phase-Local Execution Rule

`aa-api-codegen` **MUST execute in the assigned phase executor**. In `execution_mode: inline`, that executor is the primary agent. In `execution_mode: subagent-dispatch`, that executor is the bounded `task` subagent created for Phase 6A.

The executor must load and run `aa-api-codegen` itself. Loading the skill is not phase completion, and the executor must not delegate the codegen work to a nested subagent or background worker.

**Forbidden:**

```text
❌ nested delegate_task / task from inside the Phase 6A executor
❌ background API codegen worker
❌ treating nested subagent or background task output as Phase 6A completion
```

If a nested subagent or background task is used to attempt API codegen — **regardless of whether skill loading succeeded** — STOP immediately:

- Set `phases.api_codegen.status = stopped` and `phases.api_codegen.stop_reason = "codegen delegated to nested/background worker; must execute in the assigned phase executor"`.
- Update `subagents`:
  - `used = true`
  - `purpose = [forbidden_codegen_attempt]`
  - `loaded_skills = true` only if the unauthorized worker **successfully** loaded an AA skill; otherwise keep `false`
  - `unauthorized_skill_loading = true` if the nested/background worker attempted to load any AA skill (whether or not it succeeded)
  - `attempted_phase_execution = true` (always set when a nested/background worker is spawned for codegen)
  - `affected_gates = false`
- Do **not** proceed to any later phase.
- Require the assigned phase executor to re-run the affected codegen phase directly.

**Phase 6A is complete only when all are true:**

1. `aa-api-codegen` executed directly in the assigned phase executor.
2. No nested subagent or background task executed API codegen.
3. All files listed in `api-codegen-plan.md` Target Files exist on disk (re-read by the orchestrator).
4. `qa/changes/<change-id>/codegen/api-codegen-summary.md` exists on disk.
5. `workflow-state.yaml` has `phases.api_codegen.status = done`.
6. `workflow-state.yaml` lists `generated_tests.files`, `warnings_carried`, and `known_product_issues`.

If any condition fails, **STOP** and report the exact missing condition.

### Phase 6B — E2E Codegen

```yaml
inputs:
  - workflow-state.yaml
  - plans/e2e-plan.md
  - plans/e2e-test-data-plan.md
  - plans/e2e-codegen-plan.md
  - plans/m4-review-summary.md
  - review/plan-review.json  # full gate — see Codegen Hard Gates
  - .aa/data-knowledge.yaml # must exist on disk
outputs:
  - files listed in e2e-codegen-plan.md Target Files only
    # scripts / fixtures / conftest are optional — only when plan + data-knowledge authorize
  - qa/changes/<change-id>/codegen/e2e-codegen-summary.md
state_delta:
  - phases.e2e_codegen: status, review_gate_file, codegen_readiness, generated_tests.files, data_setup, fixtures, warnings_carried, known_product_issues
gate:
  - Codegen Hard Gates (E2E) — force_continue MUST NOT bypass
```

### Phase 6B Phase-Local Execution Rule

`aa-e2e-codegen` **MUST execute in the assigned phase executor**. In `execution_mode: inline`, that executor is the primary agent. In `execution_mode: subagent-dispatch`, that executor is the bounded `task` subagent created for Phase 6B.

The executor must load and run `aa-e2e-codegen` itself. Loading the skill is not phase completion, and the executor must not delegate the codegen work to a nested subagent or background worker.

**Forbidden:**

```text
❌ nested delegate_task / task from inside the Phase 6B executor
❌ background E2E codegen worker
❌ treating nested subagent or background task output as Phase 6B completion
```

If a nested subagent or background task is used to attempt E2E codegen — **regardless of whether skill loading succeeded** — STOP immediately:

- Set `phases.e2e_codegen.status = stopped` and `phases.e2e_codegen.stop_reason = "codegen delegated to nested/background worker; must execute in the assigned phase executor"`.
- Update `subagents`:
  - `used = true`
  - `purpose = [forbidden_codegen_attempt]`
  - `loaded_skills = true` only if the unauthorized worker **successfully** loaded an AA skill; otherwise keep `false`
  - `unauthorized_skill_loading = true` if the nested/background worker attempted to load any AA skill (whether or not it succeeded)
  - `attempted_phase_execution = true` (always set when a nested/background worker is spawned for codegen)
  - `affected_gates = false`
- Do **not** proceed to any later phase.
- Require the assigned phase executor to re-run the affected codegen phase directly.

**Phase 6B is complete only when all are true:**

1. `aa-e2e-codegen` executed directly in the assigned phase executor.
2. No nested subagent or background task executed E2E codegen.
3. All files listed in `e2e-codegen-plan.md` Target Files exist on disk (re-read by the orchestrator).
4. `qa/changes/<change-id>/codegen/e2e-codegen-summary.md` exists on disk.
5. `workflow-state.yaml` has `phases.e2e_codegen.status = done`.
6. `workflow-state.yaml` lists `generated_tests.files`, `data_setup`, `fixtures`, `warnings_carried`, and `known_product_issues`.

If any condition fails, **STOP** and report the exact missing condition.

### Phases 3C / 4C / 6C — Fuzz Layer *(only when `layers.fuzz == true`)*

The fuzz layer mirrors the functional plan→review→codegen flow with schemathesis. It runs in parallel with API/E2E across the same milestone boundaries (plan → review → codegen).

```yaml
# Phase 3C — Fuzz Plan (aa-fuzz-plan)
inputs:  [workflow-state.yaml, cases with type==Fuzz, fact-baseline.json, OpenAPI schema source]
outputs: [plans/fuzz-plan.md, plans/fuzz-codegen-plan.md, plans/fuzz-review-summary.md]

# Phase 4C — Fuzz Plan Review (aa-fuzz-plan-reviewer)
outputs: [review/fuzz-plan-review.json]   # decision: approved | changes_requested
gate: review/fuzz-plan-review.json must be approved before Phase 6C

# Phase 6C — Fuzz Codegen (aa-fuzz-codegen)
inputs:  [plans/fuzz-codegen-plan.md, review/fuzz-plan-review.json (approved)]
outputs: [tests/fuzz/** (schemathesis tests), codegen/fuzz-codegen-summary.md]
gate: Codegen Hard Gates apply — Test Failure Integrity (C5) enforced for fuzz tests
```

- Fuzz codegen, like all codegen, **MUST execute directly in the assigned phase executor** (same Phase-Local Execution Rule as 6A/6B).
- Fuzz tests run under `aa run` via pytest (`tests/fuzz/`) and produce `execution/fuzz-result.json`.
- A fuzz failure is **never** auto-healed: `fuzz_stateful_failure` → `needs_review`; `fuzz_configuration_error` → fix the setup and re-run.

### Phases 3D / 4D / 6D — Performance Layer *(only when `layers.performance == true`)*

The performance layer mirrors the same flow with Locust and absolute thresholds (no baseline comparison).

```yaml
# Phase 3D — Performance Plan (aa-performance-plan)
inputs:  [workflow-state.yaml, cases with type==Performance (confirmed thresholds), fact-baseline.json]
outputs: [plans/performance-plan.md, plans/performance-codegen-plan.md, plans/performance-review-summary.md]

# Phase 4D — Performance Plan Review (aa-performance-plan-reviewer)
outputs: [review/performance-plan-review.json]   # decision: approved | changes_requested
gate: review/performance-plan-review.json must be approved before Phase 6D

# Phase 6D — Performance Codegen (aa-performance-codegen)
inputs:  [plans/performance-codegen-plan.md, review/performance-plan-review.json (approved)]
outputs: [tests/perf/** (locustfiles), codegen/performance-codegen-summary.md]
gate: Codegen Hard Gates apply — Test Failure Integrity (C5) enforced for locustfiles
```

- Performance codegen **MUST execute directly in the assigned phase executor** (same Phase-Local Execution Rule as 6A/6B).
- Performance tests run under `aa run` via Locust headless and produce `execution/performance-result.json`. Each scenario's verdict is derived deterministically from measured p95 / error_rate vs. the case's absolute thresholds.
- If Locust is unavailable or the target produced no measurements, the performance dimension is `SKIPPED` (a warning, not a failure).
- A performance failure is **never** auto-healed: `perf_threshold_exceeded` → `needs_review`; `perf_environment` → environment issue; `perf_script_error` → fix the locustfile and re-run.

### Phase 7 — Execution

```yaml
inputs:
  - workflow-state.yaml
  - generated test files (from phases 6A/6B)
  - .aa/config.yaml
outputs:
  - execution/runs/<batch-id>/api-result.json       # only if manifest.selected_targets.api == true
  - execution/runs/<batch-id>/e2e-result.json       # only if manifest.selected_targets.e2e == true
  - execution/runs/<batch-id>/coverage-result.json   # when api ran with pytest-cov (M1)
  - execution/runs/<batch-id>/fuzz-result.json       # only if layers.fuzz == true (M3)
  - execution/runs/<batch-id>/performance-result.json # only if layers.performance == true (M3)
  - execution/runs/<batch-id>/summary.md              # primary mode — always in batch dir
  - execution/runs/<batch-id>/execution-manifest.yaml # skill-written, always
  - execution/api-result.json    (latest pointer — only if api selected)
  - execution/e2e-result.json    (latest pointer — only if e2e selected)
  - execution/coverage-result.json (latest pointer — when api ran)
  - execution/fuzz-result.json   (latest pointer — only if layers.fuzz)
  - execution/performance-result.json (latest pointer — only if layers.performance)
  - execution/summary.md         (latest pointer — always required in primary mode)
  - execution/execution-manifest.yaml  (latest pointer, always)
  - execution/known-product-issues.md  (snapshot from qa/changes/<change-id>/known-product-issues.md if present)
state_delta:
  - phases.execution.status = PASS|PASS_WITH_WARNINGS|FAIL|SKIPPED   # orchestrator reads final_status from the manifest
  - phases.execution.batch_id
rules:
  - Do not fail because an unselected target result file is absent.
runner:
  primary: aa run --change <change-id>
  fallback: direct pytest — only when .aa/config.yaml has allow_direct_pytest_fallback:true
            OR user explicitly accepts fallback risk.
            Fallback pass → final_status MUST be PASS_WITH_WARNINGS, never PASS.
            No fallback allowed + CLI missing → final_status MUST be FAIL.
gate:
  - verify execution/execution-manifest.yaml exists after run
  - read final_status from manifest, not from chat or claim
```

### Phase 8 — Inspect / Failure Analysis

```yaml
inputs:
  - workflow-state.yaml
  - execution/execution-manifest.yaml          # required; stop if missing
  - execution/summary.md
  - execution/api-result.json                  # only if execution-manifest selected_targets.api == true
  - execution/e2e-result.json                  # only if execution-manifest selected_targets.e2e == true
  - execution/known-product-issues.md          # if present (codegen-level snapshot)
outputs:
  - inspect/failure-analysis.json              # primary mode only (source of truth)
  - inspect/failure-summary.md                 # source of truth
  - inspect/inspection-partial.json            # partial mode only
  - inspect/known-product-issues.md            # if known issues found or confirmed
  - execution/failure-analysis.json            # optional copy/pointer for downstream (primary mode)
  - execution/failure-summary.md               # optional copy/pointer for downstream
  - human-readable final report
state_delta:
  - phases.inspect: status, inspect_mode, classification_performed
status_update_rules:
  - primary mode: phases.inspect.status = done, inspect_mode = primary, classification_performed = true
  - partial mode: phases.inspect.status = partial, inspect_mode = partial, classification_performed = false
  - PASS → PASS_WITH_WARNINGS allowed if known_product_issue or coverage_gap confirmed (primary mode only)
  - FAIL → never changed to PASS by inspect
  - PASS_WITH_WARNINGS → never changed to PASS by inspect
```

### Phase 9 — Fix Proposal *(healing, optional)*

```yaml
trigger: inspect/failure-analysis.json contains failures with fix_proposal_eligible == true
requires:
  - phases.inspect.inspect_mode == primary
  - phases.inspect.classification_performed == true
  - inspect/failure-analysis.json exists
inputs:
  - workflow-state.yaml
  - inspect/failure-analysis.json
  - inspect/failure-summary.md
  - execution/execution-manifest.yaml
  - generated test files (tests/api/ and/or tests/e2e/)
outputs:
  - healing/fix-proposal.md
  - healing/fix-proposal.json
state_delta:
  - phases.healing.status = proposal_created
gate:
  - only failures where fix_proposal_eligible == true
  - known_product_issue / coverage_gap / business_logic_failure are NOT eligible
```

### Phase 10A — API Codegen Fix *(healing)*

```yaml
inputs:
  - healing/fix-proposal.json           (proposals with target==api, eligible==true, risk_level in [low, medium])
  - workflow-state.yaml                  (phases.healing.status must be proposal_created)
  - inspect/failure-analysis.json
  - codegen/api-codegen-summary.md       (trusted source for allowed files)
outputs:
  - tests/api/test_<module>_api.py       (modified — only if proposal targets it)
  - healing/api-apply-summary.json       (required)
  - healing/api-apply-summary.md         (required)
  - healing/api-fixer-error.json         (written only on hard error)
state_delta:
  - phases.healing.attempts[n].api_apply_status = applied | no_op | failed   # from api-apply-summary.json
rules:
  - do NOT change product code (app/, web/, src/)
  - do NOT change assertion expected values
  - do NOT add pytest.mark.skip / xfail
  - do NOT delete failing tests
  - do NOT apply high risk proposals (risk_level == high → skipped_proposals)
  - patch_plan[].file MUST be in files_to_modify before any patch applied
  - files_to_modify MUST be in trusted source (workflow-state or codegen-summary)
  - do NOT set phases.healing.status = applied (orchestrator's job)
```

### Phase 10B — E2E Codegen Fix *(healing)*

```yaml
inputs:
  - healing/fix-proposal.json           (proposals with target==e2e, eligible==true, risk_level in [low, medium])
  - workflow-state.yaml                  (phases.healing.status must be proposal_created)
  - inspect/failure-analysis.json
  - codegen/e2e-codegen-summary.md       (trusted source for allowed files)
outputs:
  - tests/e2e/test_<module>_e2e.py       (modified — only if proposal targets it)
  - healing/e2e-apply-summary.json       (required)
  - healing/e2e-apply-summary.md         (required)
  - healing/e2e-fixer-error.json         (written only on hard error)
state_delta:
  - phases.healing.attempts[n].e2e_apply_status = applied | no_op | failed   # from e2e-apply-summary.json
rules:
  - same as Phase 10A (with e2e-specific allowed file patterns)
  - do NOT set phases.healing.status = applied (orchestrator's job)
```

### Phase 11 — Re-run *(healing)*

```yaml
inputs:
  - workflow-state.yaml
  - modified test files
outputs:
  - execution/runs/<new-batch-id>/   (full run archive)
  - execution/execution-manifest.yaml (updated, latest pointer)
state_delta:
  - phases.execution.status
  - phases.healing.attempts[n].rerun_batch_id
```

### Phase 12 — Re-inspect *(healing)*

```yaml
inputs:
  - workflow-state.yaml
  - execution/execution-manifest.yaml  (updated by Phase 11 — must have new batch_id)
  - selected result files (per manifest.selected_targets)
outputs:
  - inspect/failure-analysis.json   (updated, source of truth; must include source_batch_id)
  - inspect/failure-summary.md      (updated)
  - inspect/inspect-safety-check.json  (evidence file from Inspect Safety Gate)
state_delta:
  - phases.inspect.status
  - phases.healing.attempts[n].result
  - phases.healing.attempts[n].reinspect_batch_id
  - phases.healing.attempts[n].failure_outcomes   # CLI-derived per-target result (passed|reclassified|unresolved)
gate:
  - inspect/failure-analysis.json.source_batch_id MUST equal phases.execution.batch_id
    (re-inspect must be based on the Phase 11 re-run batch, not a prior batch)
  - if final_status in [PASS, PASS_WITH_WARNINGS]:
      phases.healing.status = resolved → exit healing loop → advance to Phase 14
  - if final_status == FAIL and healing.attempts < max_healing_attempts and eligible failures remain:
      → loop to Phase 9
  - if healing.attempts exhausted → phases.healing.status = exhausted → STOP
  - PER-TARGET RESOLUTION GATE (RETRO-009): `aa state heal --to resolved` is rejected with
    HEAL-TARGET-UNRESOLVED unless EVERY failure this attempt targeted is cleared in the
    post-rerun analysis. The target set is CLI-derived at proposal_created time from the
    `fix_proposal_eligible: true` failures (keyed by stable `<target>:<case_id>`, NOT the
    agent-authored FAIL-NNN labels). A target is "cleared" only if it passed (absent from
    failures[]) OR carries an explicit `reclassified` stamp from `aa report reclassify`
    (evidence-logged). A target silently downgraded to a non-eligible category without that
    stamp still counts as unresolved — loop back, reclassify with evidence, or go to exhausted.
    The CLI records the per-target verdict in phases.healing.attempts[n].failure_outcomes; do
    not hand-write it.
```

### Phase 13 — Report Generation *(terminal, non-gating)*

```yaml
skill: aa-report-generator
inputs:
  - workflow-state.yaml
  - inspect/quality-gate-result.json   (required — produced by Phase 8 / Phase 12)
  - inspect/failure-analysis.json
  - execution/api-result.json / e2e-result.json / coverage-result.json
outputs:
  - report/quality-report.json    (CLI-written, deterministic quality_score)
  - report/quality-report.md
  - report/executive-summary.md
state_delta:
  - phases.report.status = done
  - phases.report.quality_score = <int>   # from report/quality-report.json
gate:
  - entry: phases.inspect.status == done AND inspect/quality-gate-result.json exists
           (after healing converges; if healing ran, use the converged re-inspect gate)
  - PRECONDITION — use the evidence-derived healing result; do not inspect or edit
      persisted `phases.healing.status`.
  - this phase is a TERMINAL ARTIFACT and does NOT make gate decisions —
    the release gate remains inspect/quality-gate-result.json.
  - report generation does NOT block Phase 14 archive eligibility recommendation; a report-generation
    failure sets phases.report.status = failed and records a warning, but does
    not change phases.execution.status or phases.inspect final_status.
  - the workflow final summary MUST reference report/executive-summary.md when present.
notes:
  - Runs regardless of final_status (PASS / PASS_WITH_WARNINGS / FAIL) so a failing
    change still gets a report; Phase 14 only reports whether archive would be allowed.
  - The CLI computes quality_score; the skill MUST NOT recompute or alter it, and
    MUST NOT say "safe to release" on FAIL or with unresolved product defects.
  - The chat final response MUST read report/quality-report.json when present and
    include its quality_score, risk_level, release_recommendation, and key counts
    in the "Current Execution Result Statistics" block.
  - If report/quality-report.json is missing or invalid, the chat final response
    MUST show quality_score/risk_level/release_recommendation as "unknown" and
    add a warning. Do not recompute the score from execution files in chat.
```

### Phase 14 — Archive Eligibility Recommendation

```yaml
inputs:
  - workflow-state.yaml
  - review/case-review.json (decision == "pass")
  - review/api-plan-review.json (if api tested)
  - review/plan-review.json (if e2e tested)
  - execution/api-result.json (only if manifest.selected_targets.api == true)
  - execution/e2e-result.json (only if manifest.selected_targets.e2e == true)
  - execution/summary.md
  - execution/execution-manifest.yaml
  - execution/known-product-issues.md (if present)
  - inspect/known-product-issues.md (if present)
state_delta:
  - phases.archive.status = eligible | not_eligible | skipped
  - phases.archive.can_archive = true | false
  - phases.archive.recommendation
  - phases.archive.blockers[]
gate:
  - evaluate eligibility only; NEVER load `aa-archive` inside `aa-workflow`
  - `auto_archive` and `auto_archive_with_fallback` do not trigger archive execution
  - all applicable review JSON files have decision == "pass"
  - phases.execution.status in [PASS, PASS_WITH_WARNINGS]  — not eligible when FAIL
  - phases.healing.status in [not_needed, resolved, skipped] — not eligible when applied/exhausted/failed
    NOTE: "applied" alone is NOT sufficient — rerun + re-inspect (Phase 11+12) must complete (→ resolved)
    NOTE: skipped is archive-eligible ONLY when phases.execution.status in [PASS, PASS_WITH_WARNINGS]
          (the execution gate above already enforces this, but the constraint is stated explicitly here)
  - inspect/failure-analysis.json.source_batch_id == phases.execution.batch_id
    (latest inspect must be based on latest execution batch, not a stale earlier run)
  - no unresolved fix_proposal_eligible failures in latest inspect/failure-analysis.json
  - inspect/inspect-safety-check.json exists AND passed == true for latest inspect phase
    (Phase 8 for no-healing path; Phase 12 for healing path)
  - if phases.healing.status == resolved:
      healing/fixer-safety-check.json exists AND passed == true
  - if phases.execution.status == PASS_WITH_WARNINGS caused by known product issues:
      require phases.inspect.status in [done, partial]
      known product issues must be explicitly listed and acknowledged
  - if phases.execution.status == PASS_WITH_WARNINGS caused by fallback runner (no standard result files):
      report `can_archive = false` unless the final response explicitly says human confirmation
      is required before running `aa-archive` separately
  - if known product issues exist, require phases.inspect.status in [done, partial]
not_eligible_when:
  - phases.execution.status == FAIL
  - phases.healing.status == applied    # applied means fixer ran but rerun/re-inspect not yet done
  - phases.healing.status == exhausted
  - phases.healing.status == failed
  - inspect/failure-analysis.json.source_batch_id != phases.execution.batch_id  (stale inspect)
  - inspect/failure-analysis.json contains unresolved fix_proposal_eligible failures
notes:
  - Phase 14 is advisory inside `aa-workflow`. It answers: "Can this change be archived?"
  - If eligible, final response should recommend: "Archive eligible — run `aa-archive` / use skill aa-archive only if you want to merge cases and copy artifacts."
  - If not eligible, final response must list blockers and the next step to become eligible.
  - Do not create or modify `qa/cases/` or `qa/archive/` in Phase 14.
```

---

## Workflow (run_mode = case-only)

**Scope:** Phases 1.1 → 1.2 → 2.1 → 2.2 → 2.3 (conditional fix loop) → 2.4 → 2.5 → **STOP**.  
**Does not run:** plan/review/fix phases (3–5), codegen (6), execution (7), inspect/healing (8–12), archive (14).

**Required inputs before start:**

- Valid `change_id`
- `.aa/data-knowledge.yaml` (or promotable proposal) on disk under `qa/changes/<change-id>/`

**Phase sequence:**

**If `execution_mode == subagent-dispatch`:** follow the Scheme E orchestration loop (see "Execution Mode — Scheme E" section above). The loop calls `aa status --next --json` each iteration and dispatches ready phases to bounded `task` subagents — **except `explore`**, which always runs inline in the primary agent.

**If `execution_mode == inline`:** execute each phase sequentially in the primary agent as described below.

```
Phase 0 — Test Infra Bootstrap (one-shot, idempotent)
  → Load aa-test-infra-bootstrap
  → CREATE / KEEP / BLOCK each of tests/config.py, tests/conftest.py, tests/schema_validation.py per per-file contract
  → Update workflow-state.yaml: phases.test_infra_bootstrap (status, files, created, kept)
  → STOP if any file is BLOCKED (contract mismatch requires user decision)

Phase 1.1 — Skill Registry Check
  → Verify aa-test-infra-bootstrap, aa-explore, aa-case-design, aa-case-reviewer, aa-case-fixer, aa-fact-baseline load
  → Probe task tool; set execution_mode accordingly (inline if probe fails, subagent-dispatch if succeeds)
  → Write workflow-state.yaml
  → STOP if any required case skill missing

Phase 1.2 — Explore
  → Load aa-explore
  → Execute inline (writes explore/context.json and advisory artifacts when available)
  → Apply Phase 2.1 explore gate semantics
  → Update workflow-state.yaml: phases.explore.status

Phase 2.1 — Case Design
  → Load aa-case-design
  → Execute inline (writes cases/<module>/case.yaml; .qa.yaml and proposal.md may be pre-seeded or co-written)
  → Verify Phase 2.1 outputs on disk: .qa.yaml, proposal.md, cases/<module>/case.yaml
  → Apply case-design-gate (interactive intake requires .qa.yaml.approval.approved_by/approved_approach/approved_at)
  → Update workflow-state.yaml: phases.case_design.status = done

Phase 2.2 — Case Review (initial)
  → Load aa-case-reviewer
  → Re-read case.yaml, proposal.md, workflow-state.yaml from disk
  → Write review/case-review.json
  → Apply case review gate
  → Update workflow-state.yaml: phases.case_review.status

Phase 2.3 — Case Fix Loop (if gate requires it)
  → Max attempts = max_case_fix_attempts
  → aa-case-fixer → aa-case-reviewer → re-apply gate until pass, reject, human_review_required, or exhausted

Phase 2.4 — Fact Baseline (always after case review passes)
  → Load aa-fact-baseline
  → Write facts/fact-baseline.json (or unavailable + warning)
  → Update workflow-state.yaml: phases.fact_baseline (status, source, file, warnings)

Phase 2.5 — Layer Scan (inline)
  → Scan finalized cases/**/case*.yaml
  → Write workflow-state.yaml layers.{api,e2e,fuzz,performance}
  → Record which test layers are in scope for downstream modes (informational only in case-only)

STOP — case-only complete
  → Do NOT invoke plan, codegen, aa-run, inspect, healing, or archive skills
  → Final artifacts: cases/, review/case-review.json, facts/fact-baseline.json, workflow-state.yaml
  → Report case count, review decision, and layers.* summary
```

---

## Workflow (run_mode = codegen-only)

**Scope:** Phase 1.1 (codegen-related registry) → validate required upstream files → **6A / 6B / 6C / 6D** per `test_types` and `layers.*` → **Phase 6 Completion Gate** → STOP unless `run_tests == true` (then Phase 7 only — no inspect/healing/archive unless user opts in elsewhere).

**Does not run:** risk/case phases (1.2, 2.1–2.5), plan authoring/review/fix phases (3–5).

**Required inputs before start:**

- Valid `change_id`
- Existing **passing** case review: `review/case-review.json` with `decision == pass`
- Existing plans + plan reviews per active layer (e.g. L2-api seed: `plans/api-*.md`, `review/api-plan-review.json`)
- `.aa/data-knowledge.yaml` on disk
- `workflow-state.yaml` with `layers.*` consistent with scoped cases

If any required file is missing, **STOP** and list paths — do not invoke plan skills to backfill in codegen-only.

**Phase sequence:**

**If `execution_mode == subagent-dispatch`:** follow the Scheme E orchestration loop. The loop drives codegen phases (6A–6D) via bounded `task` subagents; Phase 7 is a CLI phase and is executed **directly by the orchestrator** via `aa run` (never dispatched). Codegen phases still carry the Phase-Local Execution Rule (see §6A/6B): the codegen `task` prompt must say "load and execute the codegen skill directly in this phase subagent; do not spawn nested/background workers; do not run aa gate/status; do not modify workflow-state.yaml."

**If `execution_mode == inline`:** execute all phases directly in the primary agent as described below.

```
Phase 1.1 — Skill Registry Check (codegen subset)
  → Verify skills for active layers only:
      api: aa-api-codegen (+ aa-api-codegen-fixer if healing context)
      e2e: aa-e2e-codegen (+ aa-e2e-codegen-fixer)
      fuzz: aa-fuzz-codegen (when layers.fuzz)
      performance: aa-performance-codegen (when layers.performance)
  → Verify aa-run loads only when run_tests == true
  → Write/update workflow-state.yaml

Validate codegen-only required files (orchestrator inline)
  → For each in-scope layer, confirm plan + review JSON exist and review decision == pass
  → Confirm facts/fact-baseline.json exists (warning OK if unavailable)
  → If validation fails: STOP

[Per active layer — same order as full workflow: API (6A) before E2E (6B) before Fuzz (6C) before Perf (6D)]

Phase 6A — API Codegen (if layers.api OR test_types includes api)
  → Pre-check .aa/data-knowledge.yaml
  → Load aa-api-codegen inline
  → Apply Codegen Hard Gates (API)
  → Verify Target Files from api-codegen-plan.md + codegen/api-codegen-summary.md
  → Update workflow-state.yaml: phases.api_codegen

Phase 6B — E2E Codegen (if layers.e2e OR test_types includes e2e)
  → Pre-check .aa/data-knowledge.yaml
  → Load aa-e2e-codegen inline
  → Apply Codegen Hard Gates (E2E)
  → Verify Target Files + codegen/e2e-codegen-summary.md
  → Update workflow-state.yaml: phases.e2e_codegen

Phase 6C — Fuzz Codegen (if layers.fuzz)
  → Load aa-fuzz-codegen inline → verify outputs → update phases.fuzz_codegen

Phase 6D — Performance Codegen (if layers.performance)
  → Load aa-performance-codegen inline → verify outputs → update phases.performance_codegen

Phase 6 Completion Gate
  → For each in-scope layer: phases.*_codegen.status == done, summary markdown exists, Target Files on disk
  → If any check fails: STOP

If run_tests == false:
  → Record phases.execution.status = SKIPPED
  → STOP (codegen-only complete)

If run_tests == true:
  → Phase 7 — Test Execution (aa-run) ONLY
  → Verify execution/execution-manifest.yaml on disk
  → Update workflow-state.yaml: phases.execution
  → STOP — do not auto-enter inspect/healing/archive in codegen-only unless user explicitly requests
```

---

## Full Workflow (run_mode = full)

**If `execution_mode == subagent-dispatch`:** follow the Scheme E orchestration loop above. Each ready phase is dispatched to a bounded `task` subagent **except `explore` (inline in primary agent)**; parallel branches (`api-plan`/`e2e-plan`, `api-codegen`/`e2e-codegen`) may run as parallel `task` calls in the same iteration.

**If `execution_mode == inline`:** all phases execute sequentially in the primary agent as described below. No subagents or tasks are used. Parallel branches run sequentially.

After every phase that writes artifacts or updates `workflow-state.yaml`, apply the **Phase Completion Rule** above, including the shadow-mode CLI calls (`aa status` always; `aa gate check` when the phase has a schema gate). The prose workflow remains authoritative during shadow mode; CLI disagreements are recorded, not used to override routing.

**Inline mode phase sequence (when `execution_mode == inline`):**

```
Phase 1.1 — Skill Registry Check
  → Verify all required skills can load in primary agent
  → If any skill fails to load: STOP, report missing skill name
  → Write workflow-state.yaml (create, set execution_mode per Phase 1.1 detection)
  → Record OPENCODE-SKILL-RESOLUTION-001 warning in workflow-state.yaml

Phase 1.2 — Explore
  → Load skill aa-explore in primary agent
  → Execute inline (runs risk context, writes explore artifacts, validates advisory)
  → Apply Phase 2.1 explore gate semantics
  → Update workflow-state.yaml: phases.explore.status

Phase 2.1 — Case Design
  → Load skill aa-case-design in primary agent
  → Execute inline (writes cases/<module>/case.yaml)
  → Verify Phase 2.1 outputs on disk: .qa.yaml, proposal.md, cases/<module>/case.yaml
  → Update workflow-state.yaml: phases.case_design.status = done

Phase 2.2 — Case Review (initial)
  → Load skill aa-case-reviewer in primary agent
  → Re-read from disk: workflow-state.yaml, case.yaml, proposal.md
  → Execute inline
  → Read case-review.json from disk
  → Apply case review gate
  → Update workflow-state.yaml: phases.case_review.status

Phase 2.3 — Case Fix Loop (if gate requires it)
  → For each attempt (max = max_case_fix_attempts):
      → Load skill aa-case-fixer in primary agent
      → Re-read from disk: case-review.json, case.yaml
      → Execute inline
      → Load skill aa-case-reviewer in primary agent
      → Re-read from disk: case.yaml (updated)
      → Execute inline
      → Read new case-review.json from disk
      → Apply case review gate
      → If pass → exit loop, update workflow-state.yaml
      → If reject or human_review_required → stop
      → If attempts exhausted → stop

Phase 2.4 — Fact Baseline (always runs after Case Review passes)
  → Load skill aa-fact-baseline in primary agent
  → Read seed/init sources: look for app/core/init_app.py, db/seed*.py, fixtures/, or equivalent
  → If live DB probe is configured (.aa/config.yaml has db_probe: true): run read-only probe
  → Capture: built-in role id/name, auth credentials, route prefix, token header mechanism
  → Write facts/fact-baseline.json
  → Update workflow-state.yaml: phases.fact_baseline (status, source, file, warnings)

[API branch — run if test_types includes "api"]

Phase 3A — API Plan
  → Load skill aa-api-plan in primary agent
  → Re-read from disk: workflow-state.yaml, proposal.md, case.yaml
  → Execute inline
  → Verify api-plan.md, api-test-data-plan.md, api-codegen-plan.md exist on disk
  → Update workflow-state.yaml: phases.api_plan.status = done

Phase 4A — API Plan Review (initial)
  → Load skill aa-api-plan-reviewer in primary agent
  → Re-read from disk: workflow-state.yaml, api-plan.md, api-test-data-plan.md, api-codegen-plan.md
  → Execute inline
  → Read api-plan-review.json from disk
  → Apply API plan review gate
  → Update workflow-state.yaml: phases.api_plan_review.status

Phase 5A — API Plan Fix Loop (if gate requires it)
  → For each attempt (max = max_plan_fix_attempts):
      → Load skill aa-api-plan-fixer in primary agent
      → Re-read from disk: api-plan-review.json, api-plan.md
      → Execute inline
      → Load skill aa-api-plan-reviewer in primary agent
      → Re-read from disk: updated plan files
      → Execute inline
      → Read new api-plan-review.json from disk
      → Apply API plan review gate
      → If pass → exit loop, update workflow-state.yaml
      → If reject or human_review_required → stop
      → If attempts exhausted → stop

Phase 6A — API Codegen pre-check
  → Verify `.aa/data-knowledge.yaml` exists on disk
  → If missing: STOP — tell user to create or promote data-knowledge.yaml
  → Update workflow-state.yaml: gates.data_knowledge

Phase 6A — API Codegen
  → Load skill aa-api-codegen in primary agent
  → Re-read from disk: workflow-state.yaml, api-plan.md, api-codegen-plan.md, api-plan-review.json, .aa/data-knowledge.yaml
  → Apply Codegen Hard Gates (API) — force_continue MUST NOT bypass
  → Execute inline   ← MUST be inline in primary agent; DO NOT delegate to subagent or background task
  → Verify files listed in api-codegen-plan.md Target Files exist (helpers/fixtures/conftest only if plan authorized)
  → Verify codegen/api-codegen-summary.md exists
  → Update workflow-state.yaml: phases.api_codegen (review_gate_file, codegen_readiness, generated_tests.files, warnings_carried)

[E2E branch — run if test_types includes "e2e"]

Phase 3B — E2E Plan
  → Load skill aa-e2e-plan in primary agent
  → Re-read from disk: workflow-state.yaml, proposal.md, case.yaml
  → Execute inline
  → Verify e2e-plan.md, e2e-test-data-plan.md, e2e-codegen-plan.md exist on disk
  → Update workflow-state.yaml: phases.e2e_plan.status = done

Phase 4B — E2E Plan Review (initial)
  → Load skill aa-e2e-plan-reviewer in primary agent
  → Re-read from disk: workflow-state.yaml, e2e-plan.md, e2e-test-data-plan.md, e2e-codegen-plan.md
  → Execute inline
  → Read plan-review.json from disk
  → Apply plan review gate
  → Update workflow-state.yaml: phases.e2e_plan_review.status

Phase 5B — E2E Plan Fix Loop (if gate requires it)
  → For each attempt (max = max_plan_fix_attempts):
      → Load skill aa-e2e-plan-fixer in primary agent
      → Re-read from disk: plan-review.json, e2e-plan.md
      → Execute inline
      → Load skill aa-e2e-plan-reviewer in primary agent
      → Re-read from disk: updated plan files
      → Execute inline
      → Read new plan-review.json from disk
      → Apply plan review gate
      → If pass → exit loop, update workflow-state.yaml
      → If reject or human_review_required → stop
      → If attempts exhausted → stop

Phase 6B — E2E Codegen pre-check
  → Verify `.aa/data-knowledge.yaml` exists on disk
  → If missing: STOP — tell user to create or promote data-knowledge.yaml
  → Update workflow-state.yaml: gates.data_knowledge

Phase 6B — E2E Codegen
  → Load skill aa-e2e-codegen in primary agent
  → Re-read from disk: workflow-state.yaml, e2e-plan.md, e2e-codegen-plan.md, plan-review.json, .aa/data-knowledge.yaml
  → Apply Codegen Hard Gates (E2E) — force_continue MUST NOT bypass
  → Execute inline   ← MUST be inline in primary agent; DO NOT delegate to subagent or background task
  → Verify files listed in e2e-codegen-plan.md Target Files exist (scripts/fixtures/conftest only if plan authorized)
  → Verify codegen/e2e-codegen-summary.md exists
  → E2E framework: runtime `e2e_framework=python-playwright` → workflow-state `generated_tests.framework=pytest-playwright`
  → Do NOT generate *.spec.ts files
  → Update workflow-state.yaml: phases.e2e_codegen (review_gate_file, codegen_readiness, generated_tests.files, data_setup, fixtures, warnings_carried)

Phase 6 Completion Gate — verify before Phase 7
  → If test_types includes "api":
      - phases.api_codegen.status MUST == done  (not stopped, not failed, not pending)
      - codegen/api-codegen-summary.md MUST exist on disk
      - All Target Files from api-codegen-plan.md MUST exist on disk
      - If any condition fails: STOP — report which codegen output is missing
  → If test_types includes "e2e":
      - phases.e2e_codegen.status MUST == done
      - codegen/e2e-codegen-summary.md MUST exist on disk
      - All Target Files from e2e-codegen-plan.md MUST exist on disk
      - If any condition fails: STOP — report which codegen output is missing
  → If run_tests == false: skip Phase 7, record phases.execution.status = SKIPPED, advance to Phase 14 eligibility recommendation

Phase 7 — Test Execution (run_tests = true; MUST execute if Phase 6 gate passed)
  → If run_tests == true and Phase 6 Completion Gate passed: this phase is MANDATORY — skipping it is a STOP condition
  → Load skill aa-run in primary agent
  → Re-read from disk: workflow-state.yaml, .aa/config.yaml
  → Execute inline: aa-run must attempt `aa run --change <change-id>` first (primary mode)
  → Direct pytest fallback is controlled only by aa-run Runner Mode:
      - allowed only when .aa/config.yaml has allow_direct_pytest_fallback: true
      - or user explicitly accepts fallback risk
      - fallback pass → final_status MUST be PASS_WITH_WARNINGS, never PASS
      - no fallback allowed + CLI missing → final_status MUST be FAIL
  → Verify execution/execution-manifest.yaml exists on disk after run
      - If missing after run: STOP — do not report execution as complete
  → Read final_status from execution/execution-manifest.yaml (not from chat or claim)
  → Update workflow-state.yaml: phases.execution.status = final_status, phases.execution.batch_id
  → If run_tests == true but this phase is not reached: workflow status MUST be stopped, not completed

Phase 8 — Inspect / Failure Analysis
  → Load skill aa-inspect if: phases.execution.status in [FAIL, PASS_WITH_WARNINGS]
      OR qa/changes/<change-id>/known-product-issues.md exists
      OR qa/changes/<change-id>/execution/known-product-issues.md exists
  → Re-read from disk: workflow-state.yaml, execution/execution-manifest.yaml (required)
  → If execution-manifest.yaml missing: STOP, ask user to re-run aa-run
  → Re-read result files only for selected targets (per manifest.selected_targets)
  → Execute inline
  → Primary outputs land in inspect/ (source of truth); execution/ gets optional copies
  → Update workflow-state.yaml: phases.inspect.status = done (primary) or partial (fallback); inspect_mode = primary | partial; classification_performed = true | false
  → Apply Status Update Rules: only PASS→PASS_WITH_WARNINGS allowed, never degrade FAIL
  → Output structured summary (see Final Response Format)
  → Report agent_warnings from workflow-state.yaml
  → Load skill aa-dashboard if run_dashboard = true

  → Apply Inspect Safety Gate (see Inspect Safety Gate section)
      If any forbidden file was modified by inspect: STOP

  → Evaluate healing entry conditions:
      If phases.execution.status in [PASS, PASS_WITH_WARNINGS] AND no fix_proposal_eligible failures:
        Set phases.healing.status = not_needed
        → Proceed to Phase 14 archive eligibility recommendation
      If phases.execution.status == FAIL AND no fix_proposal_eligible failures:
        Set phases.healing.status = not_needed
        → STOP: no eligible auto-fix; require human intervention
      If phases.execution.status == FAIL AND fix_proposal_eligible failures exist:
        If gates.healing_available == false: STOP — report healing skills unavailable
        If phases.inspect.inspect_mode != primary: STOP — healing requires primary inspect mode
        → Enter healing loop (Phase 9)

[Healing loop — Phases 9–12]

Phase 9 — Fix Proposal
  → Load skill aa-fix-proposal in primary agent   ← MUST be inline in primary agent
  → Re-read from disk: workflow-state.yaml, inspect/failure-analysis.json (required), inspect/failure-summary.md, execution/execution-manifest.yaml
  → Execute inline
  → Verify healing/fix-proposal.json exists and is valid JSON
  → Verify healing/fix-proposal.md exists
  → If fix-proposal.json.summary.eligible_count == 0:
      Set phases.healing.status = not_needed
      If phases.execution.status == FAIL: STOP — no eligible fixes; require human intervention
  → Confirm the proposal is based on the active analysis batch. Do not write healing
    status or attempts; proposal state and distinct attempts are derived from evidence.

Phase 10 — Apply Fixes
  Healing fixers support only API/E2E test-code targets. Fuzz and performance
  failures are manual-review / separate-change outcomes unless a future
  dedicated fixer target is added. Do not modify `tests/fuzz/**` or
  `tests/perf/**` inside the API/E2E healing loop, and do not coerce a
  fuzz/performance failure into an API/E2E proposal.

  [If fix-proposal.json contains API proposals (target == "api" AND eligible == true):]
  → Load skill aa-api-codegen-fixer in primary agent   ← MUST be inline in primary agent
  → Re-read from disk: workflow-state.yaml, healing/fix-proposal.json, inspect/failure-analysis.json
  → Execute inline
  → Run `aa heal record-apply --change <id> --target api --proposal <applied proposal ids>`
  → Verify healing/api-apply-summary.json exists and is valid JSON
  → Verify every file in api-apply-summary.json.files_modified is an allowed API test file
  → If healing/api-fixer-error.json was written: STOP — forbidden modification attempted

  [If fix-proposal.json contains E2E proposals (target == "e2e" AND eligible == true):]
  → Load skill aa-e2e-codegen-fixer in primary agent   ← MUST be inline in primary agent
  → Re-read from disk: workflow-state.yaml, healing/fix-proposal.json, inspect/failure-analysis.json
  → Execute inline
  → Run `aa heal record-apply --change <id> --target e2e --proposal <applied proposal ids>`
  → Verify healing/e2e-apply-summary.json exists and is valid JSON
  → Verify every file in e2e-apply-summary.json.files_modified is an allowed E2E test file
  → If healing/e2e-fixer-error.json was written: STOP — forbidden modification attempted

  Derived healing validation requires each eligible target's current apply-summary SHA
  to match its latest `heal_record_apply` event for the current proposal identity.
  A hand-written summary is not a substitute for `aa heal record-apply`.

  → Apply Fixer Safety Gate (see Fixer Safety Gate section)
      If product code changed, assertions weakened, tests deleted, skip/xfail added, or unrelated tests changed:
        STOP — do NOT proceed to Phase 11

  → Orchestrator sets global apply status (after BOTH fixers complete and safety gate passes):
      Read api_apply_status from attempt entry (applied | no_op | failed)
      Read e2e_apply_status from attempt entry (applied | no_op | failed)

      If any status == failed:
        → phases.healing.status = failed; STOP

      If ALL statuses == no_op (no fixer applied any change):
        → STOP: eligible proposals existed in fix-proposal.json but no fixer applied any change
        → reason: "eligible proposals listed but all fixers returned no_op — likely a proposal/file authorization mismatch"
        → phases.healing.status = failed
        → Do NOT proceed to Phase 11 rerun (running unchanged code again is meaningless)

      If all statuses are in [applied, no_op] AND at least one status == applied:
        → Generate the CLI fixer safety check, route `needs_human_review` through
          `aa decide --at healing.safety`, and pin the applied rerun tree.
        → Do not write workflow-state healing status; `applied` is evidence-derived.

Phase 11 — Re-run
  → Load skill aa-run in primary agent   ← MUST be inline in primary agent
  → Re-read from disk: workflow-state.yaml, .aa/config.yaml
  → Execute inline: attempt `aa run --change <change-id>` (primary mode)
  → Verify execution/runs/<new-batch-id>/ was created (new batch, different from Phase 7 batch)
  → Verify execution/execution-manifest.yaml was updated with new batch_id
  → Read final_status from updated execution/execution-manifest.yaml (not from chat or claim)
  → Update workflow-state.yaml:
      phases.execution.status = final_status
      phases.healing.attempts[n].rerun_batch_id = <new-batch-id>
  → If execution-manifest.yaml not updated with a new batch: STOP — do not claim rerun completed

Phase 12 — Re-inspect
  → Load skill aa-inspect in primary agent   ← MUST be inline in primary agent
  → Re-read from disk: workflow-state.yaml, execution/execution-manifest.yaml (updated in Phase 11)
  → Execute inline
  → Verify inspect/failure-analysis.json was updated:
      - Read inspect/failure-analysis.json.source_batch_id
      - Verify source_batch_id == phases.execution.batch_id (from latest execution-manifest.yaml)
      - If mismatch: STOP — re-inspect analyzed a stale batch, not the Phase 11 re-run
  → Verify inspect/failure-summary.md was updated
  → Update workflow-state.yaml:
      phases.inspect.status
      phases.healing.attempts[n].result = final_status
      phases.healing.attempts[n].reinspect_batch_id = source_batch_id from failure-analysis.json

  → Apply Inspect Safety Gate (same rules as Phase 8 safety gate)

  → Apply healing loop gate:
      If phases.execution.status in [PASS, PASS_WITH_WARNINGS]:
        → Set phases.healing.status = resolved
        → Update attempt entry: phases.healing.attempts[n].result = PASS or PASS_WITH_WARNINGS
        → Exit healing loop — proceed to Phase 14 archive eligibility recommendation
      If phases.execution.status == FAIL AND healing.attempts < max_healing_attempts AND fix_proposal_eligible failures remain:
        → Update attempt entry: phases.healing.attempts[n].result = FAIL
        → Loop back to Phase 9 (re-read files from disk first): write a NEW fix-proposal against
          the latest batch. Its new proposal SHA + source batch becomes the next attempt identity.
      If healing.attempts exhausted (count == max_healing_attempts):
        → Set phases.healing.status = exhausted
        → Update attempt entry: phases.healing.attempts[n].result = FAIL
        → STOP: healing attempts exhausted; require human intervention
        → Do NOT proceed to archive

Phase 14 — Archive Eligibility Recommendation
  → Evaluate archive eligibility only. Do NOT load aa-archive and do NOT write qa/archive/.
  → Check:
      - phases.execution.status in [PASS, PASS_WITH_WARNINGS] — if FAIL: can_archive=false
      - phases.healing.status in [not_needed, resolved, skipped] — if applied/exhausted/failed: can_archive=false
        ("applied" alone is NOT sufficient — healing must have completed Phase 11+12 to reach "resolved")
        ("skipped" is only archive-eligible when execution.status in [PASS, PASS_WITH_WARNINGS])
      - inspect/failure-analysis.json.source_batch_id == phases.execution.batch_id — if stale: can_archive=false
      - no unresolved fix_proposal_eligible failures in latest inspect/failure-analysis.json
      - inspect/inspect-safety-check.json exists and passed == true (latest phase)
      - if phases.healing.status == resolved: healing/fixer-safety-check.json exists and passed == true
      - if PASS_WITH_WARNINGS from known product issues: require phases.inspect.status in [done, partial]; issues explicitly acknowledged
      - if PASS_WITH_WARNINGS from fallback runner: require separate human confirmation before archive
      - all applicable review JSON files have decision == "pass"
  → If all checks pass:
      - Update workflow-state.yaml: phases.archive.status = eligible, can_archive = true,
        recommendation = "run aa-archive separately only if the user wants to merge cases and copy artifacts"
  → If any check fails:
      - Update workflow-state.yaml: phases.archive.status = not_eligible, can_archive = false,
        blockers = [specific failed checks], recommendation = "do_not_archive"
  → Final response MUST include the archive eligibility recommendation and blockers (if any).
```

---

## Expected Files per Phase

### Case Design outputs

```text
qa/changes/<change-id>/.qa.yaml
qa/changes/<change-id>/proposal.md
qa/changes/<change-id>/cases/**/*.yaml
```

### Case Review outputs

```text
qa/changes/<change-id>/review/case-review.json
qa/changes/<change-id>/review/case-review-summary.md
```

### Fact Baseline outputs (Phase 2.4)

```text
qa/changes/<change-id>/facts/fact-baseline.json
```

### API Plan outputs

```text
qa/changes/<change-id>/plans/api-plan.md
qa/changes/<change-id>/plans/api-test-data-plan.md
qa/changes/<change-id>/plans/api-codegen-plan.md
qa/changes/<change-id>/plans/m3-review-summary.md
qa/changes/<change-id>/plans/data-knowledge.proposal.yaml  (only if .aa/data-knowledge.yaml is missing)
```

### E2E Plan outputs

```text
qa/changes/<change-id>/plans/e2e-plan.md
qa/changes/<change-id>/plans/e2e-test-data-plan.md
qa/changes/<change-id>/plans/e2e-codegen-plan.md
qa/changes/<change-id>/plans/m4-review-summary.md
qa/changes/<change-id>/plans/data-knowledge.proposal.yaml  (only if .aa/data-knowledge.yaml is missing)
```

Plan skills MUST NOT write directly to `.aa/data-knowledge.yaml`. Only write proposals to `data-knowledge.proposal.yaml`.

### Plan Review outputs

```text
qa/changes/<change-id>/review/plan-review.json
qa/changes/<change-id>/review/plan-review-summary.md
```

### API Codegen outputs

Only files listed in `api-codegen-plan.md` Target Files (helpers / fixtures / conftest are optional per plan):

```text
qa/changes/<change-id>/codegen/api-codegen-summary.md   ← always
tests/api/test_<module>_api.py                          ← when mapped in Target Files
tests/api/helpers/<module>_api.py                       ← only if plan helper mapping non-empty
tests/testdata/domain/<entity>.py                       ← shared factory; create-if-missing or reuse
tests/api/adapters/<module>.py                          ← API-only pytest/transport adapter
tests/api/conftest.py                                   ← only if plan explicitly requires
```

Note: execution result files are written by `aa-run` in Phase 7, not by `aa-api-codegen`.

### E2E Codegen outputs

Only files listed in `e2e-codegen-plan.md` Target Files (scripts / fixtures / conftest are optional per plan):

E2E framework: **Python Playwright** — default naming `tests/e2e/test_<module>_e2e.py` (NOT `*.spec.ts`).

```text
qa/changes/<change-id>/codegen/e2e-codegen-summary.md   ← always
tests/e2e/test_<module>_e2e.py                          ← when mapped in Target Files
tests/e2e/scripts/<module>_data_setup.py                ← only if plan + data-knowledge authorize
tests/e2e/adapters/**/*.py                              ← only if plan + data-knowledge authorize
tests/e2e/conftest.py                                   ← only if plan explicitly requires
```

Note: execution result files are written by `aa-run` in Phase 7, not by `aa-e2e-codegen`.

### Execution outputs (written by aa-run, Phase 7)

```text
qa/changes/<change-id>/execution/api-result.json              ← only if manifest.selected_targets.api == true
qa/changes/<change-id>/execution/e2e-result.json              ← only if manifest.selected_targets.e2e == true
qa/changes/<change-id>/execution/summary.md                   ← always required in primary mode
qa/changes/<change-id>/execution/execution-manifest.yaml      ← always present
qa/changes/<change-id>/execution/known-product-issues.md      ← snapshot from change-level record (if present)
qa/changes/<change-id>/execution/runs/<batch-id>/             ← per-run archive (never overwritten)
```

Do not fail because an unselected target result file is absent.

### Inspect outputs (written by aa-inspect, Phase 8)

```text
qa/changes/<change-id>/inspect/failure-analysis.json          ← primary mode only (source of truth)
qa/changes/<change-id>/inspect/failure-summary.md             ← source of truth
qa/changes/<change-id>/inspect/inspection-partial.json        ← partial mode only
qa/changes/<change-id>/inspect/known-product-issues.md        ← if known issues found or confirmed
qa/changes/<change-id>/inspect/inspection-error.json          ← if primary CLI failed / outputs missing

qa/changes/<change-id>/execution/failure-analysis.json        ← optional copy/pointer (primary mode)
qa/changes/<change-id>/execution/failure-summary.md           ← optional copy/pointer
```

**Inspect mode (`phases.inspect.inspect_mode`):**

| Value | `classification_performed` | Key outputs |
|-------|---------------------------|-------------|
| `primary` | `true` | `failure-analysis.json` |
| `partial` | `false` | `inspection-partial.json` |

Healing loop (Phases 9–12) requires `inspect_mode == primary` and `inspect/failure-analysis.json`.

**Directory semantics:**

- `execution/` — runner products: result JSON, manifest, known-product-issues snapshot
- `inspect/` — analysis products: failure classification, summaries, known-issue confirmations

Before advancing to the next phase, verify the required output files for the current phase exist. If any required file is missing, stop and report which files are missing.

---

## Mandatory Review JSON Gate

A phase may **never** advance based on natural language approval, a fixer's claim that it fixed something, or an agent's own judgment. Every critical transition is gated by a structured review JSON file written by the corresponding reviewer.

The mandatory gate files are:

| Gate | File | Required before |
|---|---|---|
| Case gate | `qa/changes/<change-id>/review/case-review.json` | API/E2E planning |
| API plan gate | `qa/changes/<change-id>/review/api-plan-review.json` | `aa-api-codegen` |
| E2E plan gate | `qa/changes/<change-id>/review/plan-review.json` | `aa-e2e-codegen` |

**Universal gate rules — applied to every gate file above:**

1. If the required review JSON file does **not exist** → **STOP**.
2. If the file is **not valid JSON** → **STOP**.
3. If the file is missing any **required field** (see field lists below) → **STOP**.
4. If `decision != "pass"` and the state is not an auto-fixable state → **STOP**.

The workflow may only read JSON fields to decide `continue` / `fix` / `stop`. It must not substitute any other signal for these fields.

### Case gate required fields

`qa/changes/<change-id>/review/case-review.json` must contain:

```json
{
  "schema_version": "1.0",
  "review_type": "case",
  "change_id": "<change-id>",
  "decision": "pass|needs_fix|needs_human_review|reject",
  "risk_level": "low|medium|high|critical",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "summary": "",
  "reviewed_files": [],
  "blockers": [],
  "findings": [],
  "needs_review": [],
  "auto_fix_plan": [],
  "next_action": "continue|run_case_fixer|human_review|stop",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

Gate decision:

- `decision == "pass"` → continue to API/E2E planning
- `decision == "reject"` → STOP
- `human_review_required == true` and `force_continue == false` → STOP
- `risk_level in ["high","critical"]` and `force_continue == false` → STOP
- `decision == "needs_fix"` and `auto_fix_allowed == true` → run `aa-case-fixer`
- otherwise → STOP

### Fact Baseline Gate (before Phase 4A / 4B — Plan Review)

Before running `aa-api-plan-reviewer` (Phase 4A) or `aa-e2e-plan-reviewer` (Phase 4B), verify:

- `facts/fact-baseline.json` exists
- If `source != "unavailable"`: key facts (role names/ids, auth credentials, route prefix, token header) in the plan documents **must match** values in `fact-baseline.json`
  - If mismatch found: mark the plan review as **blocked** and set `phases.api_plan_review.status = stale` (or `e2e_plan_review`) with `reason: fact_mismatch_before_review`
  - Correct the plan documents first, then run the reviewer
- If `source == "unavailable"`: log a warning but do not block; reviewer proceeds with a `fact_validation_skipped: true` note in the review JSON

This gate prevents review-JSON artifacts from being written against stale facts, avoiding the full-chain contamination described in P0-4.

---

### API plan gate required fields

`qa/changes/<change-id>/review/api-plan-review.json` must contain:

```json
{
  "schema_version": "1.0",
  "review_type": "api-plan",
  "change_id": "<change-id>",
  "decision": "pass|needs_fix|needs_human_review|reject",
  "risk_level": "low|medium|high|critical",
  "codegen_readiness": "ready|ready_with_warnings|not_ready",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "summary": "",
  "reviewed_files": [],
  "blockers": [],
  "findings": [],
  "needs_review": [],
  "auto_fix_plan": [],
  "next_action": "continue|run_api_plan_fixer|human_review|stop",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

Gate decision:

- `decision == "pass"` and `codegen_readiness in ["ready","ready_with_warnings"]` and `blockers` empty and no blocking `needs_review` → continue to `aa-api-codegen` (also requires `.aa/data-knowledge.yaml` — see Codegen Hard Gates)
- `decision == "reject"` → STOP
- `human_review_required == true` and `force_continue == false` → STOP
- `risk_level in ["high","critical"]` and `force_continue == false` → STOP
- `codegen_readiness == "not_ready"` → STOP (**force_continue MUST NOT bypass**)
- `blockers` non-empty → STOP (**force_continue MUST NOT bypass**)
- any `needs_review` item with `blocking == true` → STOP (**force_continue MUST NOT bypass**)
- `decision == "needs_fix"` and `auto_fix_allowed == true` and `next_action == "run_api_plan_fixer"` → run `aa-api-plan-fixer`
- otherwise → STOP

### E2E plan gate required fields

`qa/changes/<change-id>/review/plan-review.json` must contain:

```json
{
  "schema_version": "1.0",
  "review_type": "e2e-plan",
  "change_id": "<change-id>",
  "decision": "pass|needs_fix|needs_human_review|reject",
  "risk_level": "low|medium|high|critical",
  "codegen_readiness": "ready|ready_with_warnings|not_ready",
  "auto_fix_allowed": false,
  "human_review_required": false,
  "summary": "",
  "reviewed_files": [],
  "blockers": [],
  "findings": [],
  "needs_review": [],
  "auto_fix_plan": [],
  "next_action": "continue|run_e2e_plan_fixer|human_review|stop",
  "created_at": "YYYY-MM-DDTHH:mm:ssZ"
}
```

Gate decision:

- `decision == "pass"` and `codegen_readiness in ["ready","ready_with_warnings"]` and `blockers` empty and no blocking `needs_review` → continue to `aa-e2e-codegen` (also requires `.aa/data-knowledge.yaml` — see Codegen Hard Gates)
- `decision == "reject"` → STOP
- `human_review_required == true` and `force_continue == false` → STOP
- `risk_level in ["high","critical"]` and `force_continue == false` → STOP
- `codegen_readiness == "not_ready"` → STOP (**force_continue MUST NOT bypass**)
- `blockers` non-empty → STOP (**force_continue MUST NOT bypass**)
- any `needs_review` item with `blocking == true` → STOP (**force_continue MUST NOT bypass**)
- `decision == "needs_fix"` and `auto_fix_allowed == true` and `next_action == "run_e2e_plan_fixer"` → run `aa-e2e-plan-fixer`
- otherwise → STOP

---

## force_continue Boundaries

`force_continue` may **only** bypass:

- `human_review_required == true`
- `risk_level in ["high", "critical"]`

`force_continue` **MUST NOT** bypass:

- missing required review JSON
- invalid review JSON
- `decision != "pass"` for codegen entry
- `codegen_readiness == "not_ready"`
- `blockers` non-empty
- any `needs_review` item with `blocking == true`
- missing `.aa/data-knowledge.yaml` before codegen
- missing required phase output files

---

## Fact Correction Mode

Fact Correction Mode activates when a **newly discovered source-of-truth fact** (from live DB probe, seed file re-read, or code inspection) invalidates artifacts that were produced in earlier phases — including artifacts that already passed review.

### When to Activate

Activate Fact Correction Mode if ANY of the following is true after Phase 4 or later:

- A role name / id in `proposal.md`, `case.yaml`, or plan files does not match `facts/fact-baseline.json`
- Auth credentials, route prefix, or token header in plan files differ from seed source
- A live DB probe returns data that contradicts an assumption embedded in approved plans

### What Fact Correction Mode Allows

- **Modify** `proposal.md`, `cases/<module>/case.yaml`, `plans/api-plan.md`, `plans/e2e-plan.md`, and generated test files — but ONLY for the discovered fact, no scope creep
- **NOT** allowed to add new test cases or change test logic beyond the fact correction

### Required Actions

1. **Mark affected review gates as stale** in `workflow-state.yaml` before making any edits:

   ```yaml
   phases:
     api_plan_review:
       status: stale
       stale_reason: fact_correction_after_review
     e2e_plan_review:
       status: stale
       stale_reason: fact_correction_after_review
     api_codegen:
       status: needs_patch
       needs_patch_reason: stale_role_name_fact
       generated_tests:
         semantic_validity: stale
   ```

2. **Apply corrections** to plan files and generated test files.

3. **Write `facts/fact-correction-summary.json`**:

   ```json
   {
     "change_id": "<change-id>",
     "triggered_at": "Phase 6B codegen pre-check",
     "discovered_at": "<ISO timestamp>",
     "discovery_source": "live_db_probe | seed_file | code_inspection",
     "corrections": [
       {
         "artifact": "plans/api-plan.md",
         "field": "role.name",
         "stale_value": "admin",
         "correct_value": "管理员",
         "files_patched": ["tests/api/test_role_api.py"]
       }
     ],
     "stale_gates": ["api_plan_review", "e2e_plan_review"],
     "stale_codegen": ["api_codegen"],
     "re_review_required": ["api_plan_review", "e2e_plan_review"]
   }
   ```

4. **Re-run affected reviewers** (`aa-api-plan-reviewer`, `aa-e2e-plan-reviewer`) to produce fresh review JSON.

5. **Re-collect** any patched test files: `pytest --collect-only <patched_test_file>` — collect-only confirms syntax, not semantics.

6. **Update `workflow-state.yaml`** after re-review passes:

   ```yaml
   phases:
     api_plan_review:
       status: pass
       stale_reason: null
     api_codegen:
       status: needs_patch   # remains until Phase 7 execution confirms semantics
       generated_tests:
         semantic_validity: stale   # updated to valid only after Phase 7 PASS
   ```

### Fact Correction Mode Is NOT a Substitute for Standard Fixers

`aa-api-plan-fixer` and `aa-e2e-plan-fixer` require `decision == needs_fix` in the review JSON. Fact Correction Mode handles a different case: `decision == pass` but the review is **stale** due to a fact discovered after review. These are separate flows:

| Situation | Mechanism |
|---|---|
| Review says `needs_fix` | Standard fixer (`aa-api-plan-fixer`) |
| Review is `pass` but facts changed | Fact Correction Mode (inline patch + re-review) |
| Codegen artifact contains stale facts | Set `needs_patch`, patch inline, re-collect, re-review |

---

## Codegen Hard Gates

Before **Phase 6A (API Codegen)** or **Phase 6B (E2E Codegen)**, verify in order — **STOP** on first failure. **`force_continue` MUST NOT bypass any item below.**

### API Codegen (Phase 6A)

Read `review/api-plan-review.json` and verify:

- valid JSON; `review_type == "api-plan"`; `change_id == <change-id>`
- `decision == "pass"`
- `codegen_readiness in ["ready", "ready_with_warnings"]`
- `blockers` is empty
- no `needs_review` item has `blocking == true`
- `.aa/data-knowledge.yaml` exists on disk
- required plan files exist (`api-plan.md`, `api-test-data-plan.md`, `api-codegen-plan.md`, `m3-review-summary.md`)
- generated setup follows the shared-domain plus API-adapter rule in `aa-api-codegen`; shared factories preserve invariants, adapters own execution/cleanup, and no adapter is reused across test layers

### E2E Codegen (Phase 6B)

Read `review/plan-review.json` and verify:

- valid JSON; `review_type == "e2e-plan"`; `change_id == <change-id>`
- `decision == "pass"`
- `codegen_readiness in ["ready", "ready_with_warnings"]`
- `blockers` is empty
- no `needs_review` item has `blocking == true`
- `.aa/data-knowledge.yaml` exists on disk
- required plan files exist (`e2e-plan.md`, `e2e-test-data-plan.md`, `e2e-codegen-plan.md`, `m4-review-summary.md`)

---

## User Approval Is Not a Gate Artifact

Natural language approval from the user is **not** sufficient to advance to codegen or archive.

If the user manually approves a plan, the corresponding reviewer must still write the review JSON file recording that approval:

- User approves case artifacts → `aa-case-reviewer` must write `case-review.json`
- User approves API plan → `aa-api-plan-reviewer` must write `api-plan-review.json`
- User approves E2E plan → `aa-e2e-plan-reviewer` must write `plan-review.json`

The workflow must never proceed based only on messages such as:

```text
approved
looks good
proceed to codegen
```

These messages may be used as **input** to the reviewer, but the reviewer must still produce the machine-readable JSON gate. Only a freshly written review JSON with `decision == "pass"` releases the gate.

---

## Retry Must Be Based on JSON

The fix/review loop is driven entirely by review JSON, never by claims:

```text
reviewer writes JSON
workflow reads JSON
if decision == needs_fix and auto_fix_allowed == true → run fixer
fixer applies changes and writes apply-summary (NOT review JSON)
reviewer runs again and writes a NEW review JSON
workflow reads the new JSON
only decision == pass can continue
```

Forbidden transitions:

- ❌ fixer says "fixed" → continue
- ❌ reviewer says "looks good" in chat → continue
- ❌ user says "approved" → continue

Required transition:

- ✅ new review JSON `decision == "pass"` → continue

---

## When to Ask the User

**Do NOT ask the user to choose phase execution order.** The workflow routing table defines the sequence (Phase 3A → 4A → 5A → 6A for API; Phase 3B → 4B → 5B → 6B for E2E). This is not a user decision.

**Do NOT ask** for:

- Whether to run Phase 6A before or after Phase 6B — workflow defines: API first, then E2E
- Which phases to run — determined by **run_mode** (stage boundary) and **test_types** / **layers.*** (API/E2E/Fuzz/Perf subset). Eval prompt parameters override `.qa.yaml` defaults when they conflict.
- Whether to proceed to the next phase after a `pass` — proceed automatically
- What fix approach to use when `auto_fix_allowed == true` — run the designated fixer

**DO ask** the user only in these cases:

| Situation | What to ask |
|---|---|
| `human_review_required == true` in any review JSON | Present findings, ask for decision |
| `risk_level in ["high", "critical"]` (without `force_continue`) | Present risk, ask to proceed or stop |
| Fact correction affects already-approved artifacts | Present the conflict, ask: in-place correction or rollback to planning |
| Fallback runner risk (aa-run not found, test framework missing) | Present alternatives |
| User explicitly asks to archive after workflow completion | Explain the Phase 14 eligibility result; run `aa-archive` only as a separate, explicit action outside `aa-workflow` |
| Healing exhausted, still FAIL | Ask: accept failures, re-plan, or stop |

Any decision not in this list is made by the workflow itself. Do not surface it as a user question.

---

## Document-driven Parallel Work Does Not Bypass the Gate

In `inline` mode, this workflow does not launch skill-loading subagents; the only permitted form of parallel work is **document-driven**: a subagent receives a plan document as input and executes it directly, without loading skills. In `subagent-dispatch` mode, the orchestrator's bounded phase subagents are the authorized executors and DO load their assigned phase skill — but every gate below still applies identically, because gate decisions (`aa gate check`) and `workflow-state.yaml` writes remain orchestrator-only.

### Conductor path (`aa conductor`)

When context limits or headless automation require per-phase fresh contexts, use **`aa conductor`** instead of ad-hoc task delegation:

```bash
aa conductor brief --change <id> --phase <phase>   # compile + materialize one brief
aa conductor run --change <id>                     # deterministic loop (default runtime: local)
aa conductor graph --change <id>                   # rebuild execution-graph.json
```

Conductor reads phase skills / mapping config, writes `task-brief.json` + prompt under `qa/changes/<id>/orchestration/`, and (when `subagents.runtime: opencode-command`) spawns `opencode run` children that execute **only** the brief. After each dispatch, conductor validates outputs and may roll back out-of-bounds filesystem changes.

**Policy unchanged for conductor children:** conductor-spawned workers execute only the materialized brief — they must not load AA skills, must not mutate `workflow-state.yaml`, must not run gate checks, and must not decide PASS/FAIL. (This is stricter than Scheme E phase subagents, which are authorized to load their assigned phase skill.) Conductor adds runtime enforcement via agent permission files (`.opencode/agents/aa-*.md`) and the validator stack (required outputs → git-diff → self-report). Conductor is opt-in via `.aa/config.yaml`; without it, the workflow runs per `execution_mode` (Scheme E dispatch or inline).

If optional document-driven parallel work fails or is unavailable, the workflow may continue inline only when the same required artifacts are produced:

- Parallel work fails → inline reviewer may generate the review JSON.
- Parallel work fails → you may **not** skip review and proceed directly to codegen or archive.

Every gate listed in **Mandatory Review JSON Gate** applies identically whether the phase ran as parallel document-driven work or fully inline.

---

## Subagent Usage Disambiguation

This section clarifies when subagent usage is **allowed** vs. **forbidden**, and how to record it unambiguously in `workflow-state.yaml`.

**Authorized vs. unauthorized:** in `subagent-dispatch` mode, bounded phase subagents dispatched by the orchestration loop are **authorized** executors for their assigned phases — **except `explore`**, which always runs inline in the primary agent (never `task(agent=aa-doc-author)`). Authorized subagents load their assigned phase skill and write that phase's output files. The rules below govern **unauthorized** subagents: anything spawned outside the orchestration loop, any nested worker spawned from inside a phase executor, and any subagent in `inline` mode (where no phase dispatch exists).

### Allowed: exploration / document-driven subagents (both modes)

```text
✅ explore agent reads backend source files
✅ explore agent reads frontend directory structure
✅ document-driven subagent receives plan.md and produces a non-gate artifact
```

These do **not** constitute a forbidden-pattern violation. However, they **must be recorded**:

```yaml
subagents:
  used: true
  purpose:
    - source_exploration_only        # if used for codebase exploration
    - document_driven_parallel       # if used for parallel document work
    - phase_dispatch                 # subagent-dispatch mode only: authorized bounded phase subagents
  loaded_skills: false               # inline mode: MUST remain false.
                                     # dispatch mode: true is expected for authorized phase subagents;
                                     #   record which skills under loaded_skill_names.
  unauthorized_skill_loading: false  # MUST remain false in both modes
  attempted_phase_execution: false   # inline mode: MUST remain false.
                                     # dispatch mode: authorized dispatches are recorded via purpose: phase_dispatch,
                                     #   not via this flag.
  affected_gates: false              # MUST remain false — gate decisions are orchestrator-only in both modes
```

### Forbidden: unauthorized skill-loading or gate-deciding subagents

```text
❌ (inline mode) any subagent is asked to load an aa-* skill
❌ (inline mode) any subagent writes case-review.json / api-plan-review.json
❌ (both modes) any subagent runs aa gate check / aa status, or decides pass/fail
❌ (both modes) any subagent writes workflow-state.yaml
❌ (dispatch mode) any subagent other than the dispatched phase subagent produces that phase's gate artifact
```

If any of these occurs, set the corresponding flag and **STOP**:

```yaml
subagents:
  used: true
  loaded_skills: true                # a subagent loaded an AA skill without authorization
  unauthorized_skill_loading: true   # subagent attempted/performed skill loading outside an authorized dispatch
  attempted_phase_execution: true    # a subagent executed a phase without authorization
  affected_gates: true               # a subagent produced a gate decision, substituted a gate artifact,
                                     # or modified workflow-state.yaml
```

### Forbidden: nested / background codegen workers

Codegen MUST execute directly in the **assigned phase executor** (primary agent in inline mode; the dispatched Phase 6A/6B/6C/6D subagent in dispatch mode). It must never be re-delegated.

**Forbidden patterns:**

```text
❌ (inline mode) delegate_task / task for aa-api-codegen or aa-e2e-codegen
❌ (dispatch mode) nested delegate_task / task spawned from inside the codegen phase subagent
❌ background API codegen worker (either mode)
❌ background E2E codegen worker (either mode)
❌ treating nested/background codegen output as Phase 6 completion
```

If a nested subagent or background task is used to attempt any AA phase codegen — **regardless of whether the skill loading succeeded or failed**:

1. **STOP** the workflow immediately.
2. Set `phases.api_codegen.status = stopped` or `phases.e2e_codegen.status = stopped` (whichever was affected).
3. Set `stop_reason: "codegen delegated to nested/background worker; must execute in the assigned phase executor"`.
4. Update `subagents`:

```yaml
subagents:
  used: true
  purpose:
    - forbidden_codegen_attempt
  loaded_skills: false         # true only if the unauthorized worker successfully loaded an AA skill
  unauthorized_skill_loading: true  # the worker attempted skill loading outside an authorized dispatch
  attempted_phase_execution: true # true always when an unauthorized codegen worker was spawned
  affected_gates: false
```

5. Do **not** proceed to any later phase.
6. Require the assigned phase executor to re-run codegen directly from the point of failure.

### Conflict with analyze-mode / search-mode pre-exploration

**Hard rule: search-mode and analyze-mode parallel agents are DISABLED by default during aa-workflow.**

When aa-workflow is running (Phase 1.1 through Phase 14), the agent MUST NOT launch background/parallel explore, librarian, or search agents — regardless of any global `[search-mode]` or `[analyze-mode]` instruction at the top of the session context. This rule bans *additional* search/analyze agents; it does not change how AA phases themselves execute — phases follow `execution_mode` (`explore` always inline in the primary agent; other phases inline or dispatched per Scheme E).

**The only allowed exception** is source exploration *before Phase 2.1 begins*, and only when ALL of the following hold:

1. No AA skill has been loaded yet (`load_skills == []` in the invocation context).
2. The explore agent reads source files only — no writes, no gate artifacts.
3. No AA skill is loaded by any explore agent.
4. No gate artifact (`case-review.json`, `api-plan-review.json`, `plan-review.json`) is produced by an explore agent.
5. All AA phases (Phase 2.1–14) are then executed per `execution_mode` — inline in the primary agent, or via the orchestrator's bounded phase subagents (Scheme E). No further ad-hoc search/analyze agents are launched.

If the exception applies, record it immediately in `workflow-state.yaml`:

```yaml
subagents:
  used: true
  purpose: [source_exploration_only]
  loaded_skills: false
  affected_gates: false
```

**If search-mode/analyze-mode agents were launched AFTER Phase 1.1 started**, this is a forbidden-pattern violation — treat identically to a forbidden codegen subagent (STOP, set `subagents.affected_gates = true`).

---

## Case Review Gate

Read `qa/changes/<change-id>/review/case-review.json` and apply this gate:

| Condition | Action |
|---|---|
| `decision == "pass"` | Continue to API/E2E plan (per test_types) |
| `decision == "reject"` | **Stop** |
| `human_review_required == true` | **Stop** unless `force_continue = true` |
| `risk_level in ["high", "critical"]` | **Stop** unless `force_continue = true` |
| `decision == "needs_fix"` and `auto_fix_allowed == true` | Run `aa-case-fixer` |
| Any other condition | **Stop** and ask for human review |

**Case review retry policy:**

- Initial case review does not count as a fix attempt.
- Max fix attempts: `max_case_fix_attempts` (default 2).
- Max total case review runs: `max_case_fix_attempts + 1` (default 3).
- If fix attempts are exhausted and decision is still not `pass`, stop.

---

## API Plan Review Gate

Read `qa/changes/<change-id>/review/api-plan-review.json` and apply this gate:

| Condition | Action |
|---|---|
| `decision == "pass"` | Continue to API codegen (subject to **Codegen Hard Gates**) |
| `decision == "reject"` | **Stop** |
| `human_review_required == true` | **Stop** unless `force_continue = true` |
| `risk_level in ["high", "critical"]` | **Stop** unless `force_continue = true` |
| `codegen_readiness == "not_ready"` | **Stop** — `force_continue` MUST NOT bypass |
| `blockers` non-empty | **Stop** — `force_continue` MUST NOT bypass |
| any `needs_review` with `blocking == true` | **Stop** — `force_continue` MUST NOT bypass |
| `decision == "needs_fix"` and `auto_fix_allowed == true` and `next_action == "run_api_plan_fixer"` | Run `aa-api-plan-fixer` |
| Any other condition | **Stop** and ask for human review |

**API plan review retry policy:**

- Initial API plan review does not count as a fix attempt.
- Max fix attempts: `max_plan_fix_attempts` (default 2).
- Max total API plan review runs: `max_plan_fix_attempts + 1` (default 3).
- If fix attempts are exhausted and decision is still not `pass`, stop.

---

## E2E Plan Review Gate

Read `qa/changes/<change-id>/review/plan-review.json` and apply this gate:

| Condition | Action |
|---|---|
| `decision == "pass"` | Continue to E2E codegen (subject to **Codegen Hard Gates**) |
| `decision == "reject"` | **Stop** |
| `human_review_required == true` | **Stop** unless `force_continue = true` |
| `risk_level in ["high", "critical"]` | **Stop** unless `force_continue = true` |
| `codegen_readiness == "not_ready"` | **Stop** — `force_continue` MUST NOT bypass |
| `blockers` non-empty | **Stop** — `force_continue` MUST NOT bypass |
| any `needs_review` with `blocking == true` | **Stop** — `force_continue` MUST NOT bypass |
| `decision == "needs_fix"` and `auto_fix_allowed == true` and `next_action == "run_e2e_plan_fixer"` | Run `aa-e2e-plan-fixer` |
| Any other condition | **Stop** and ask for human review |

**E2E plan review retry policy:**

- Initial E2E plan review does not count as a fix attempt.
- Max fix attempts: `max_plan_fix_attempts` (default 2).
- Max total E2E plan review runs: `max_plan_fix_attempts + 1` (default 3).
- If fix attempts are exhausted and decision is still not `pass`, stop.

---

## Healing Execution Policy

`aa-inspect` is the **diagnosis** stage of healing, not the repair stage.

The healing loop is:

```
aa-run (Phase 7)
  → aa-inspect (Phase 8)
  → aa-fix-proposal (Phase 9)
  → aa-api-codegen-fixer / aa-e2e-codegen-fixer (Phase 10)
  → aa-run (Phase 11: Re-run)
  → aa-inspect (Phase 12: Re-inspect)
  → (repeat if still eligible failures and attempts < max)
  → archive eligibility recommendation (Phase 14; no archive execution)
```

**Eligible failure categories (fix_proposal_eligible == true):**

- `locator_failure`
- `wait_strategy_failure`
- `test_code_error`
- `test_data_failure` *(conditional — only if test data generation can be fixed without changing product code or expected assertions)*

**Human or external fix required (not eligible for healing loop):**

- `environment_failure`
- `assertion_failure`
- `business_logic_failure`
- `case_semantic_failure`
- `known_product_issue`
- `coverage_gap`
- `product_bug`
- `unrelated_existing_failure`
- `unknown`

**Healing entry conditions (after Phase 8):**

```text
IF phases.execution.status == FAIL
   AND inspect/failure-analysis.json contains failures with fix_proposal_eligible == true:
   AND phases.inspect.inspect_mode == primary
   AND phases.inspect.classification_performed == true:

  IF gates.healing_available == true → enter Phase 9
  IF gates.healing_available == false → STOP: report healing skills unavailable; do not archive

IF no eligible failures exist (or all fix_proposal_eligible == false):
  Set phases.healing.status = not_needed
  IF phases.execution.status == FAIL → STOP: no auto-fix available; require human intervention
  IF phases.execution.status in [PASS, PASS_WITH_WARNINGS] → proceed to Phase 14 archive eligibility recommendation
```

Healing status readers derive `pending`, `proposal_created`, and `applied` from the
current evidence chain. Resume by re-reading status; there is no reconciliation write.

**Healing hard rules:**

- **Never** change assertion expected values automatically.
- **Never** fix product bugs by changing tests.
- **Never** hide `known_product_issue` or `coverage_gap`.
- **Never** change `FAIL` to `PASS` without a successful re-run from `aa-run` that produces `final_status == PASS`.
- Every healing apply (Phase 10) must be followed by a re-run (Phase 11).
- Every re-run (Phase 11) must be followed by a re-inspect (Phase 12).
- **Never** allow a diagnostic probe from Phase 8/12 to count as an official re-run.
- **Never** allow `phases.execution.status` to change from `FAIL` to `PASS` unless Phase 11 `aa-run` produced a new batch with `final_status == PASS`.
- **Never** allow healing to modify product code (`app/`, `web/`, `src/`).
- **Never** allow healing to modify unrelated tests outside the current change scope.
- **Never** modify test files between official `aa run` batches outside the healing loop. `aa run` records `tests_tree_sha256` in `execution-manifest.yaml` and refuses changed tests unless `phases.healing.status == applied`. `--rerun-reason` is only for code-unchanged environment retries.
- **Never** manufacture a test-change authorization — this project's `execution-policy.json` can forbid it. A human may use `aa decide --at execution.test-changes --action allow_test_changes` for one current test tree when policy permits. On `TESTS-CHANGED-WITHOUT-HEALING` / `ALLOW-TEST-CHANGES-FORBIDDEN`, enter the healing loop or STOP.
- `aa run` also records `product_tree_sha256`; during healing, product-code changes are mechanically rejected with `PRODUCT-CHANGED-DURING-HEALING` and have no bypass flag.
- `healing/fixer-safety-check.json` is generated by the CLI-owned healing core after apply records. Agent-authored versions are overwritten and must not be trusted as self-attestation.
- If an `assertion_failure` is judged to be a test expected-value error, record that judgment with `aa report reclassify --change <id> --failure <id> --to assertion_expectation_error --evidence "<case/plan evidence>"`. Never edit `inspect/failure-analysis.json` directly.
- If the healing skills (`aa-fix-proposal`, `aa-api-codegen-fixer`, `aa-e2e-codegen-fixer`) are not installed (`gates.healing_available == false`):
  - Set `phases.healing.status = skipped`.
  - If `phases.execution.status == FAIL` and `fix_proposal_eligible` failures exist in `inspect/failure-analysis.json`: **STOP** — report missing healing skills; do not archive a failed execution.
  - If `phases.execution.status in [PASS, PASS_WITH_WARNINGS]`: healing may be skipped; proceed to Phase 14 archive eligibility recommendation.
- Max healing attempts: controlled by `max_healing_attempts` (default: `3`).

**Healing attempt counting rules:**

```text
An attempt is STARTED when Phase 9 produces fix-proposal.json with eligible_count > 0.
  → The attempt index MUST be allocated in workflow-state.yaml BEFORE Phase 10 starts.
  → From this point forward, the attempt counts toward max_healing_attempts.
  → There is NO mechanism to "undo" an attempt allocation.

An attempt is COMPLETED only after Phase 12 (re-inspect) runs and sets attempt.result.

Attempt result values:
  - PASS | PASS_WITH_WARNINGS  → healed; exit loop; set phases.healing.status = resolved
  - FAIL                        → not healed; check if more attempts remain
  - failed                      → hard error; stop immediately regardless of remaining attempts

The attempt ALWAYS counts even if:
  - Phase 10 fixer fails (api-fixer-error.json / e2e-fixer-error.json written)
  - Fixer Safety Gate fails (fixer-safety-check.json passed == false)
  - Phase 11 rerun fails to produce a new batch
  - Phase 12 re-inspect detects stale batch analysis

In all hard-error cases:
  → Set attempt.result = failed
  → Set phases.healing.status = failed
  → STOP immediately — do not loop back to Phase 9

max_healing_attempts = 3 means at most three complete or attempted cycles of:
  Phase 9 (fix-proposal) → Phase 10 (fixer) → Phase 11 (rerun) → Phase 12 (re-inspect)
```

---

## Inspect Safety Gate

Apply this gate **after Phase 8 (initial inspect)** and **after Phase 12 (re-inspect)**:

```text
Inspect Safety Gate:

BEFORE aa-inspect runs (Phase 8 and Phase 12):
  Record a filesystem baseline — either:
  a) Run: git diff --name-only HEAD  (if repo is git-tracked)
  b) Or: snapshot mtime/hash of all files under tests/, app/, web/, src/, .aa/,
         qa/changes/<change-id>/cases/, plans/, review/, healing/,
         qa/changes/<change-id>/execution/execution-manifest.yaml,
         qa/changes/<change-id>/execution/known-product-issues.md

AFTER aa-inspect completes:
  Compare against baseline to detect any file changes.

  ALLOWED changes:
    qa/changes/<change-id>/inspect/**
    qa/changes/<change-id>/execution/failure-analysis.json   (optional copy)
    qa/changes/<change-id>/execution/failure-summary.md      (optional copy)
    qa/changes/<change-id>/inspect/diagnostic-probes.json
    qa/changes/<change-id>/inspect/inspect-safety-check.json  (this file itself)

  FORBIDDEN changes:
    tests/**
    app/**
    web/**
    src/**
    .aa/**
    qa/changes/<change-id>/cases/**
    qa/changes/<change-id>/plans/**
    qa/changes/<change-id>/review/**
    qa/changes/<change-id>/healing/**
    qa/changes/<change-id>/execution/execution-manifest.yaml
    qa/changes/<change-id>/execution/known-product-issues.md

WRITE evidence file — MANDATORY, regardless of outcome.
Writing this file is NOT optional. If the orchestrator cannot write it, treat as passed == false.

  qa/changes/<change-id>/inspect/inspect-safety-check.json:
  {
    "schema_version": "1.0",
    "change_id": "<change-id>",
    "phase": "phase-9 | phase-13",
    "source_batch_id": "<batch-id from latest execution-manifest.yaml>",
    "checked_at": "YYYY-MM-DDTHH:mm:ssZ",
    "forbidden_modified_files": [],
    "allowed_modified_files": [
      "qa/changes/<change-id>/inspect/failure-analysis.json"
    ],
    "passed": true
  }

IF passed == false OR file does not exist:
  → Set phases.inspect.status = failed
  → Set workflow_status = stopped
  → STOP immediately
  → Report which forbidden file was modified (or that the evidence file is missing)
  → Do NOT proceed to healing or archive
```

---

## Fixer Safety Gate

Apply this gate **after Phase 10 (apply fixes)**:

```text
Fixer Safety Gate:

After aa-api-codegen-fixer and/or aa-e2e-codegen-fixer complete, verify:

1. patch_plan/files_to_modify consistency:
   Every modified file is listed in fix-proposal.json.proposals[].files_to_modify.
   Every patch_plan[].file is also in files_to_modify.
   → If mismatch: violation = true

2. Trusted source authorization:
   Every modified file appears in at least one trusted source:
   - workflow-state.yaml phases.api_codegen.generated_tests.files (API)
   - workflow-state.yaml phases.e2e_codegen.generated_tests.files (E2E)
   - codegen/api-codegen-summary.md or e2e-codegen-summary.md generated/reused files
   → If not authorized: violation = true

3. No product code was modified:
   FORBIDDEN modified paths: app/**, web/**, src/**
   → If found: violation = true

4. No assertion expected values were changed:
   → If found: violation = true

5. No tests were deleted:
   → If found: violation = true

6. No pytest.mark.skip, xfail, or equivalent was added:
   → If found: violation = true

7. No unrelated test files (outside change scope) were modified:
   → If found: violation = true

8. No high-risk proposal was applied (high risk → must appear in skipped_proposals, not applied_proposals):
   → If found in applied_proposals: violation = true

WRITE evidence file — MANDATORY, regardless of outcome.
Writing this file is NOT optional. If the orchestrator cannot write it, treat as passed == false.

  qa/changes/<change-id>/healing/fixer-safety-check.json:
  {
    "schema_version": "1.0",
    "change_id": "<change-id>",
    "attempt": <n>,
    "checked_at": "YYYY-MM-DDTHH:mm:ssZ",
    "modified_files": [],
    "forbidden_modified_files": [],
    "unauthorized_files": [],
    "patch_plan_mismatch": false,
    "product_code_modified": false,
    "assertion_expected_value_changes_detected": false,
    "tests_deleted": false,
    "skip_or_xfail_added": false,
    "unrelated_tests_modified": false,
    "high_risk_proposal_applied": false,
    "passed": true,
    "violations": []
  }

IF passed == false OR file does not exist:
  → Set phases.healing.status = failed
  → Set workflow_status = stopped
  → STOP immediately
  → Do NOT rerun
  → Report which file and which rule was violated (or that the evidence file is missing)
```

---

## Stop Conditions

Stop the workflow immediately when any of the following is true:

- Required input files are missing and cannot be generated by the current phase
- Review JSON is missing or is invalid JSON
- `decision == "reject"` in any review
- `human_review_required == true` and `force_continue == false`
- `risk_level in ["high", "critical"]` and `force_continue == false`
- **Codegen hard gate failure** (always stops — `force_continue` MUST NOT bypass):
  - `codegen_readiness == "not_ready"`
  - `blockers` non-empty in plan review JSON
  - any `needs_review` item with `blocking == true`
  - missing `.aa/data-knowledge.yaml` before codegen
  - `decision != "pass"` when entering codegen
- Fix attempt count exceeds `max_case_fix_attempts` or `max_plan_fix_attempts`
- Healing attempt count exceeds `max_healing_attempts` (`phases.healing.status == exhausted`)
- `phases.execution.status == FAIL` and healing skills are unavailable (`gates.healing_available == false`) and `fix_proposal_eligible` failures exist
- Archive is attempted inside `aa-workflow` instead of being left as a separate explicit `aa-archive` action
- Codegen requires guessing unknown product behavior (route, auth, selector, factory)
- Test execution environment is unavailable (`aa` CLI or uv/pytest not installed)
- A document-driven optional subagent returns an unrecoverable error (inline orchestration does not use skill-loading subagents)
- **Codegen subagent violations (always stops — no bypass):**
  - Any AA phase skill is loaded/executed by an **unauthorized** subagent or background task (in inline mode, that is any subagent; in dispatch mode, anything other than the orchestrator's dispatched phase subagent)
  - `aa-api-codegen` or `aa-e2e-codegen` executes in a nested/background worker instead of directly in the assigned phase executor
  - Phase 6A or Phase 6B codegen skill is loaded but not executed by the assigned phase executor (skill loaded ≠ phase complete)
  - Phase 6A codegen summary (`codegen/api-codegen-summary.md`) is missing after codegen was claimed complete
  - Phase 6B codegen summary (`codegen/e2e-codegen-summary.md`) is missing after codegen was claimed complete
  - `workflow-state.yaml` does not show `phases.api_codegen.status = done` or `phases.e2e_codegen.status = done` when required
- **Phase 7 gate violations:**
  - `run_tests == true` and Phase 6 Completion Gate passed, but Phase 7 `aa-run` is not executed
  - Phase 7 executes but `execution/execution-manifest.yaml` is missing after the run
- **Healing loop violations:**
  - `phases.execution.status == FAIL` and no `fix_proposal_eligible` failures and `phases.healing.status == not_needed` (no auto-fix path; require human)
  - `gates.healing_available == false` and `phases.execution.status == FAIL` and `fix_proposal_eligible` failures exist
  - Inspect safety gate triggered: `aa-inspect` modified forbidden files during Phase 8 or Phase 12
  - Fixer safety gate triggered: fixer modified product code, weakened assertions, added skip/xfail, deleted tests, or modified unrelated tests
  - Phase 11 `aa-run` did not produce a new batch — `execution-manifest.yaml` not updated
  - `phases.healing.status == exhausted` (max attempts reached)
  - `phases.healing.status == failed` (fixer wrote error file or safety gate triggered)

When stopped, **report** the exact stop reason, which file or gate triggered it, and what a human must do to resume. In orchestrated mode, do **not** ask for chat confirmation to bypass a gate.

---

## Context Budget Check & Session Handoff

Inline orchestration shares one context window across all 14 phases. As phases accumulate, context fills up. When it does, the agent MUST stop cleanly and hand off to a new session — NOT power through with truncated SKILL.md reads and incomplete code generation.

### Self-Monitoring Rule (every turn, not just phase boundaries)

The `monitor-status-injector` hook emits `[Context Status: X% used (...)]` in every `<system-reminder>` block. The agent MUST treat this as a **hard signal**, not informational:

- **At every assistant turn**, before deciding the next action, read the most recent `Context Status: X%` value.
- If `X% >= 75%` → **STOP all further phase work this turn**, regardless of:
  - `[SYSTEM REMINDER - TODO CONTINUATION]` messages (the continuation hook does not know context state; it will catch up after handoff)
  - pending todos
  - in-progress phase work
  - user pressure to continue
- The only allowed actions at `X% >= 75%` are: write `handoff.md`, cancel pending todos via `todowrite`, end the response.

This rule overrides the todo continuation hook. The two hooks (`monitor-status-injector` and `todo-continuation-enforcer`) do not coordinate — the agent IS the coordinator. When they conflict, **context monitor wins**.

### Why the Continuation Hook Conflicts with Context Pressure

When pending todos exist, `todo-continuation-enforcer` injects `[SYSTEM REMINDER - TODO CONTINUATION]` after every response, regardless of context state. Without this rule, the agent will:

1. See `Context Status: 85% used` — interpret as advisory
2. See `[SYSTEM REMINDER - TODO CONTINUATION]` — interpret as mandatory
3. Continue executing → context goes to 95% → 105% (overflow) → `anthropic-context-window-limit-recovery` triggers truncation / summarize
4. Summarized context loses SKILL.md hard rules → next phase generates incomplete code

The fix is purely behavioral: agent must prioritize `Context Status` over `TODO CONTINUATION` when they conflict. **The Skill Load Gate enforces "read the contract"; this Self-Monitoring Rule enforces "respect the budget".**

### When to Trigger (Phase Entry — still applies)

Before entering **any** phase (after the Skill Load Gate check, before reading the phase's SKILL.md), the agent MUST also assess context budget:

- If remaining context < 25% → **MUST handoff** (no phase work this turn).
- If remaining context < 40% AND the upcoming phase is a codegen phase (6A / 6B) → **MUST handoff** (codegen phases are context-heavy: SKILL.md + all plans + case.yaml + data-knowledge + generating multiple files).
- If remaining context < 40% AND the upcoming phase has a SKILL.md > 400 lines → **SHOULD handoff** (large SKILL.md + inputs will consume most of the remaining budget).

The agent cannot know its exact token count, but it MUST use the `<system-reminder>` context-window monitor messages as the signal. When the monitor reports ≥ 75% used, treat the upcoming phase as triggering the handoff rules above.

### Handoff Procedure

When context budget is insufficient:

1. **Do NOT start the phase.** Do not read the SKILL.md. Do not generate any files.
2. **Write a handoff document** to `qa/changes/<change-id>/handoff.md`:

```markdown
# Session Handoff — <change-id>

**Date:** <ISO 8601>
**Reason:** Context budget exhausted (<percentage>% used).
**Next phase to execute:** <phase_name> (e.g. api_codegen)

## Completed phases (status from workflow-state.yaml)

| Phase | Status | skill_loaded |
|---|---|---|
| ... | ... | ... |

## Remaining phases

1. <next_phase> — NOT STARTED (this is where the next session picks up)
2. <phase_after>
3. ...

## Resume instructions

1. Read `qa/changes/<change-id>/workflow-state.yaml` (sole source of truth).
2. Read `qa/changes/<change-id>/handoff.md` (this file).
3. Find the first phase with `status: pending` in workflow-state.yaml.
4. Execute the Skill Load Gate for that phase (per execution_mode: primary reads the SKILL.md inline, or the dispatched phase subagent reads it).
5. Continue the workflow per `execution_mode` recorded in workflow-state.yaml.
```

3. **Update `workflow-state.yaml`** with a handoff marker:

```yaml
handoff:
  active: true
  reason: "context_budget_exhausted"
  next_phase: <phase_name>
  handoff_file: qa/changes/<change-id>/handoff.md
  written_at: <ISO 8601>
```

4. **Cancel all pending todos.** Do NOT leave `in_progress` or `pending` todos — the todo continuation hook will force the agent to resume in the same exhausted context. Use `todowrite` to mark ALL remaining items as `cancelled` with a note:

```
cancelled — context budget exhausted; see handoff.md for resume instructions
```

5. **End the response** with:

```
Context budget exhausted (<percentage>% used). Workflow paused at phase <next_phase>.

Handoff document: qa/changes/<change-id>/handoff.md
All pending todos cancelled (todo continuation hook suppressed).

To resume in a new session:
1. Start a new conversation
2. Say: "Continue aa-workflow for change <change-id> from <next_phase>"
3. The new session will read workflow-state.yaml + handoff.md and continue
```

### Why This Exists

Without this mechanism, the agent faces two bad options when context fills up:

- **Power through**: read truncated SKILL.md → miss hard rules → generate incomplete code (missing case_id prefixes, missing `assert_matches_schema`, missing `codegen-summary.md`) → discover defects only after `aa run` produces `unmapped_tests` or test failures.
- **Stop abruptly**: leave pending todos → todo continuation hook forces resume in the same exhausted context → same truncation problem.

The handoff procedure gives the agent a **third option**: stop cleanly, write a resume document, cancel todos so the hook doesn't fire, and let the next session pick up with a fresh context.

### Resume Protocol (New Session)

When a new session starts with "Continue aa-workflow for change `<change-id>`":

1. Read `qa/changes/<change-id>/workflow-state.yaml`.
2. If `handoff.active == true`:
   - Read `qa/changes/<change-id>/handoff.md` for context.
   - Set `handoff.active = false` in workflow-state.yaml (handoff consumed).
   - Find the first phase with `status: pending` — this is the resume point.
   - Execute the Skill Load Gate for that phase.
   - Continue the workflow inline.
3. If `handoff.active != true`:
   - No handoff in progress; start from the beginning or ask the user.

---

## Final Response Format

After the workflow completes or stops, always output a chat-visible execution result summary.
The summary is part of the workflow contract, not an optional recap.

Before writing the final response, re-read these artifacts from disk when they exist:

- `qa/changes/<change-id>/workflow-state.yaml`
- `qa/changes/<change-id>/execution/summary.md`
- `qa/changes/<change-id>/execution/execution-manifest.yaml`
- `qa/changes/<change-id>/report/quality-report.json`
- `qa/changes/<change-id>/report/executive-summary.md`
- `qa/changes/<change-id>/inspect/failure-analysis.json`
- `qa/changes/<change-id>/inspect/failure-summary.md`
- `qa/changes/<change-id>/execution/api-result.json`
- `qa/changes/<change-id>/execution/e2e-result.json`
- `qa/changes/<change-id>/execution/fuzz-result.json`
- `qa/changes/<change-id>/execution/performance-result.json`

Do not fabricate missing values. If an artifact is missing, show the corresponding
field as `unknown`, `missing`, or `not_applicable` and include a warning. The only
source for `quality_score`, `risk_level`, and `release_recommendation` is
`report/quality-report.json`; if it is missing or invalid, do not recompute those
fields from test counts.

The final chat response MUST start with this block:

```text
Current Execution Result Statistics

Overview:
  Change ID: <change-id>
  Workflow phases: <completed>/<expected> (<Phase X -> Phase Y summary>)
  Workflow status: <completed | completed_with_warnings | failed | stopped>
  Final status: <PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED | unknown>
  Quality score: <N/100 | unknown>
  Risk level: <low | medium | high | critical | unknown>
  Release recommendation: <release | release_with_warnings | do_not_release | unknown>

Test statistics:
  API: <passed>/<total> passed, <failed> failed, <skipped> skipped, duration <value | unknown>
  E2E: <passed>/<total> passed, <failed> failed, <skipped> skipped, duration <value | unknown>
  Fuzz: <passed>/<total> passed, <failed> failed, <skipped> skipped, duration <value | not_applicable | unknown>
  Performance: <passed>/<total> passed, <failed> failed, <skipped> skipped, duration <value | not_applicable | unknown>

Failure summary:
  Failed cases: <N | unknown>
  Main categories: <category=count list | none | unknown>
  Fix-proposal eligible: <N | unknown>
  Healing: <not_needed | proposal_created | applied | resolved | exhausted | failed | skipped | unknown>

Artifacts:
  Execution summary: execution/summary.md | missing
  Quality report: report/quality-report.md | missing
  Executive summary: report/executive-summary.md | missing
  Failure summary: inspect/failure-summary.md | missing | not_applicable
  Archive eligibility: eligible | not_eligible | skipped | unknown
  Archive blockers: <count/list | none | unknown>

Next action:
  <one concrete recommendation based on status and gates>
```

Then include the detailed gate/status structure below when useful for debugging or audit:

```
AA workflow <completed | completed_with_warnings | failed | stopped>.

Workflow status: <completed | completed_with_warnings | failed | stopped>
Change ID: <change-id>
Current phase: <phase that completed or stopped>
Stop reason: <if stopped>

Review gates:
  case-review.json: present | missing | invalid | pass | needs_fix | needs_human_review | reject
  api-plan-review.json: present | missing | invalid | pass | needs_fix | needs_human_review | reject | not_applicable
  plan-review.json: present | missing | invalid | pass | needs_fix | needs_human_review | reject | not_applicable

Case files:
  .qa.yaml: present | missing
  proposal.md: present | missing
  cases: <N files>

Case review:
  decision: <pass | needs_fix | reject | human_review | —>
  risk_level: <low | medium | high | critical | —>
  fix attempts used: <N>

API plan files:
  api-plan.md: present | missing
  api-codegen-plan.md: present | missing
  api plan review: <pass | needs_fix | reject | —>
  api plan fix attempts: <N>

E2E plan files:
  e2e-plan.md: present | missing
  e2e-test-data-plan.md: present | missing
  e2e-codegen-plan.md: present | missing
  e2e plan review: <pass | needs_fix | reject | —>
  e2e plan fix attempts: <N>

Generated test files:
  tests/api/: <N files>
  tests/e2e/: <N files> (Python Playwright, test_*_e2e.py)

Codegen:
  api_codegen: pending | done | failed | stopped | not_applicable
  e2e_codegen: pending | done | failed | stopped | not_applicable
  api_codegen_summary: present | missing | not_applicable
  e2e_codegen_summary: present | missing | not_applicable
  stop_reason: <if api_codegen or e2e_codegen == stopped>

Execution:
  expected: true | false   # true when run_tests == true and Phase 6 gate passed
  executed: true | false   # true only if aa-run ran inline in primary agent
  runner: primary (aa run) | fallback (direct pytest) | —
  final_status: <PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED | —>
  manifest: execution/execution-manifest.yaml | missing | not_applicable
  stop_reason: <if expected == true but executed == false>

Known product issues:
  count: <N>
  status: none | present
  files:
    - qa/changes/<change-id>/known-product-issues.md       (source of truth, human/reviewer acknowledged; codegen append-only implementation notes)
    - qa/changes/<change-id>/execution/known-product-issues.md  (execution snapshot)
    - qa/changes/<change-id>/inspect/known-product-issues.md    (inspect-confirmed)

Healing:
  status: <not_needed | proposal_created | applied | resolved | exhausted | failed | skipped | —>
  attempts: <N>
  healing_available: <true | false>
  eligible_failures: <N>
  applied_files: <list or —>
  latest_inspect_batch_id: <batch_id or —>   # must match phases.execution.batch_id before archive

Execution mode: <inline | subagent-dispatch>
Subagent usage: <none | exploration_only | phase_dispatch>
  used: <true | false>
  purpose: <phase_dispatch | source_exploration_only | document_driven_parallel | none>
  loaded_skills: <true | false>   # inline mode: must be false.
                                  # subagent-dispatch mode: true is expected (authorized phase subagents).
  unauthorized_skill_loading: <true | false>  # must be false in both modes
  affected_gates: <true | false>  # must be false in both modes (gates are orchestrator-only)

Agent Warnings:
  - OPENCODE-SKILL-RESOLUTION-001: workflow executed inline because subagent skill inheritance is unreliable (inline-fallback runs only).
  - AA-HEALING-SKILLS-UNAVAILABLE: optional healing skills missing; healing loop unavailable on eligible failures.

Warnings:
  - <any warnings from healing, fallback runner, known issues, missing optional skills, etc.>

Next action:
  <what the user should do next>
```

Do not report any phase as complete unless its expected output files exist and were verified.

Do not claim the workflow succeeded if it stopped before Phase 9.

If `run_tests == true`, do **not** report the workflow as `completed` or `completed_with_warnings` unless:

- Phase 7 (`aa-run`) executed directly in the orchestrator (primary agent) — this holds in both modes, because CLI phases are never dispatched.
- `execution/execution-manifest.yaml` exists on disk.
- `final_status` was read from the manifest (not from chat or claim).

If `run_tests == true` but Phase 7 did not execute, report `Workflow status: stopped` and include `Execution.stop_reason`.

**Workflow status mapping:**

| Condition | Workflow status |
|---|---|
| All gates pass, execution `PASS`, no known issues | `completed` |
| All gates pass, execution `PASS_WITH_WARNINGS` or known issues exist | `completed_with_warnings` |
| Execution `FAIL`, healing `exhausted`, or healing `failed` | `failed` |
| Workflow stops before execution due to missing inputs, invalid gates, `human_review_required`, missing `.aa/data-knowledge.yaml`, unavailable environment, or missing required skill | `stopped` |
| `run_tests == true` but Phase 7 was not executed (codegen stopped, subagent violation, or Phase 6 gate failed) | `stopped` |
| `run_tests == true`, Phase 7 executed, but `execution/execution-manifest.yaml` is missing | `stopped` |
| Codegen delegated from the assigned phase executor to a nested subagent or background task (`forbidden_codegen_attempt`) | `stopped` |

Do not use `completed` when execution is `PASS_WITH_WARNINGS` or known product issues are present — use `completed_with_warnings`.

Do not report the workflow as `completed` or `completed_with_warnings` unless every applicable review gate (`case-review.json`, and `api-plan-review.json` / `plan-review.json` per `test_types`) is present, valid, and has `decision == "pass"`.

---

## Known Product Issue Reporting

The workflow must distinguish between:

```text
tests passed cleanly
tests passed with known product issues
tests failed
```

If any of the following exist, the final workflow `Status` must be `completed_with_warnings`, not `completed`:

- `qa/changes/<change-id>/execution/known-product-issues.md`
- `qa/changes/<change-id>/inspect/known-product-issues.md`
- `execution/api-result.json` or `execution/e2e-result.json` contains `known_product_issues.count > 0`

The final summary must explicitly mention:

- Affected endpoint or feature
- Severity
- Workaround used
- Coverage gap
- Status
- Next action

### Forbidden Final Summary Language (when known product issues exist)

Never use:

```text
no risk
fully clean
clean pass
all good
no issues
completed
```

Use instead:

```text
completed_with_warnings        (workflow-level status)
PASS_WITH_WARNINGS             (execution final_status)
archive eligible with warnings (Phase 14 recommendation, not an executed archive status)
```
