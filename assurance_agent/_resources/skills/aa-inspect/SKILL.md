---
name: aa-inspect
description: "AA M5: Inspect execution results and classify test failures via `aa report inspect --change <change-id>`. Use when tests have failed and the user wants to understand why. Classifies failures into categories (locator, assertion, environment, etc.) and writes inspect/failure-analysis.json, failure-summary.md, and inspect/quality-gate-result.json. Never classifies failures without the CLI."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-inspect.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.json`.
2. Verify `phases.execution.status` is set (execution must have run).
3. Read `execution/execution-manifest.json`. If it is missing, stop and ask the user to re-run `aa-run`. Do not infer runner status from result files alone.
4. Check fallback / normalised result availability:
   - If `fallback_runner.used == true` and `fallback_runner.normalised_results_available == false`:
     - Primary CLI inspect **cannot** run.
     - Use fallback partial inspect **only if** allowed (see **Inspector Mode**).
     - Otherwise **STOP** and ask to re-run execution with valid Assurance Agent CLI.
5. Read result files based on `execution-manifest.json` `batch_id` and `selected_targets` — **primary mode only**:
   - Resolve `execution/runs/<batch-id>/` from the manifest.
   - If `selected_targets.api == true`, require `execution/runs/<batch-id>/api-result.json`.
   - If `selected_targets.e2e == true`, require `execution/runs/<batch-id>/e2e-result.json`.
   - If `selected_targets.fuzz == true`, require `execution/runs/<batch-id>/fuzz-result.json`.
   - If `selected_targets.performance == true`, require `execution/runs/<batch-id>/performance-result.json`.
   - When `selected_targets.api == true`, read `execution/runs/<batch-id>/coverage-result.json` (coverage is derived from API, not a separate target).
   - Do not require result files for targets that were not selected.
   - Latest pointer files under `execution/*.json` are fallback/compatibility only and must not override manifest batch files.
   - The CLI (`aa run`) writes all batch result files listed above — see `aa-run` skill **Output Files**. The inspect CLI normalises both CLI map-shaped manifests and skill extended manifests (`primary_runner.result_files[]`).
   - In fallback partial mode with no normalised results, skip this requirement and use raw logs / manifest / `run-summary-note.md` instead.
6. Run **AA CLI Identity Check** before primary inspect (see below).
7. If any required input for the chosen inspect mode is missing, stop and report.
8. Use files as the sole source of truth.

**After completing work (primary mode — CLI succeeded):**

1. Verify CLI wrote `inspect/failure-analysis.json`. If missing, see **CLI Output Missing**.
2. Write optional pointers into `execution/` (for downstream consumers):
   - `qa/changes/<change-id>/execution/failure-analysis.json` (copy/pointer)
   - `qa/changes/<change-id>/execution/failure-summary.md` (copy/pointer)
3. Report the `workflow-state.json` state delta (inline mode: apply it directly; dispatched subagent: never write `workflow-state.json` — report the values in your final message and the orchestrator applies them):
   - `phases.inspect.status = done`
   - `phases.inspect.inspect_mode = primary`
   - `phases.inspect.classification_performed = true`
   - `phases.execution.status` change only under **Status Update Rules**
   - Inspect-discovered `known_product_issue` / `coverage_gap` classifications recorded in `phases.inspect` metadata only — Issue lifecycle authority belongs to the Issue subgraph, not inspect artifacts

**After completing work (fallback partial mode):**

1. Write:
   - `qa/changes/<change-id>/inspect/failure-summary.md` (raw summary only)
   - `qa/changes/<change-id>/inspect/inspection-partial.json` (see **Fallback Partial Output**)
2. Do **not** write `inspect/failure-analysis.json`.
3. Report the `workflow-state.json` state delta (same rule as primary mode — orchestrator applies it when dispatched):
   - `phases.inspect.status = partial`
   - `phases.inspect.inspect_mode = partial`
   - `phases.inspect.classification_performed = false`
   - Do **not** upgrade `phases.execution.status` (see **Status Update Rules**)

**After CLI failure (primary attempted, outputs missing):**

1. Write `qa/changes/<change-id>/inspect/inspection-error.json` or `inspect/inspection-error.md`
2. Report state delta `phases.inspect.status = failed` (orchestrator applies it when dispatched)
3. Do not classify manually

---

# Skill: aa-inspect

## Purpose

Inspect execution results and artifacts for a specific change, classify failures by category, and present a failure summary to the user.

## Skill vs CLI Boundary

**Primary mode:** The CLI (`aa report inspect`) is the only trusted classifier. The skill invokes the CLI and verifies `inspect/failure-analysis.json`.

**Fallback partial mode:** The skill may summarize raw artifacts but **must not** assign failure categories or write `failure-analysis.json`.

**Never:** Fabricate categories or promote inspect findings into canonical Issue/Problem lifecycle state without the Issue reconciler/review workflow.

## Assertion Expectation Reclassification

Default `assertion_failure` remains non-fixable. If inspection determines the failure is a test expected-value error (not product behavior), the skill MUST use the CLI to record the judgment:

```bash
aa report reclassify --change <change-id> \
  --failure <case_id-or-test-name> \
  --to assertion_expectation_error \
  --evidence "<case.yaml / plan evidence proving the expected value>"
```

Rules:

- Evidence is mandatory and must cite the case or plan expectation.
- Do not edit `inspect/failure-analysis.json` directly.
- Reclassified failures become `fix_proposal_eligible=true` and `needs_review=true`; downstream fix proposals require human approval before apply.

## AA CLI Identity Check

This skill calls the **Assurance Agent CLI** (`aa`) — **not** the Amazon Web Services CLI.

Before running `aa report inspect --change <change-id>`, run:

```bash
aa --version
```

**Hard rule:** The output **must** identify the Assurance Agent CLI.

| Condition | Treatment |
|-----------|-----------|
| Command missing | `aa_cli_missing` |
| Output indicates Amazon AA CLI, or does not identify Assurance Agent CLI | `aa_cli_invalid` |

In either case:

- Do **not** run `aa report inspect --change <change-id>`
- If fallback is allowed → use partial inspect mode (see **Inspector Mode**)
- If fallback is not allowed → **STOP** and report

## Inspector Mode

**Primary mode:**

1. Run **AA CLI Identity Check**.
2. Verify `execution-manifest.json` does **not** require fallback-only inspect (see Context Contract step 4).
3. If checks pass, attempt `aa report inspect --change <change-id>`.

**Fallback mode (partial inspect):**

Allowed **only when**:

- `allow_direct_inspect_fallback: true` in `.aa/config.yaml`, or
- the user explicitly accepts the fallback risk.

Fallback reasons (record in `inspection-partial.json`):

```text
aa_cli_missing | aa_cli_invalid | cli_failed | analysis_files_missing | normalised_results_unavailable
```

Fallback rules:

- May summarize existing result files, raw logs, or manifest metadata.
- **Must not** assign failure categories — classification is the CLI's exclusive responsibility.
- **Must not** write `inspect/failure-analysis.json`.
- **Must** write `inspect/inspection-partial.json` and `inspect/failure-summary.md`.
- Set `phases.inspect.status = partial`, `inspect_mode = partial`, `classification_performed = false`.
- Do **not** treat partial inspect as complete diagnosis or healing input.

## Fallback Partial Output

**Fallback mode must NOT write:**

- `inspect/failure-analysis.json`

**Fallback mode MAY write:**

- `inspect/failure-summary.md`
- `inspect/inspection-partial.json`

`inspection-partial.json` structure (no comments in actual file):

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "inspect_mode": "partial",
  "classification_performed": false,
  "reason": "aa_cli_missing",
  "selected_targets": {
    "api": true,
    "e2e": false
  },
  "raw_result_summary": {},
  "next_action": "Fix AA CLI or rerun with CLI inspect"
}
```

Valid `reason` values: `aa_cli_missing | aa_cli_invalid | cli_failed | analysis_files_missing | normalised_results_unavailable`

## CLI Invocation

Run **AA CLI Identity Check** before primary mode.

If identity check passes and primary inspect is allowed:

```bash
aa report inspect --change <change-id>
```

Example:

```bash
aa report inspect --change REQ-002-user-logout
```

If identity check fails or normalised results unavailable, apply **Inspector Mode** fallback rules — do not invoke Amazon AA CLI.

MCP is optional and must not replace the CLI execution chain.

## What the CLI Does

`aa report inspect` reads execution artifacts and classifies each failure:

1. Reads `execution/execution-manifest.json` to determine `batch_id`, `selected_targets`, and result file paths (normalises CLI map format and skill extended `primary_runner.result_files[]`).
2. Reads `execution/runs/<batch-id>/*-result.json` according to manifest `selected_targets` — **never** uses latest pointers as primary source.
3. Reads coverage from `execution/runs/<batch-id>/coverage-result.json` when `selected_targets.api == true`; skips coverage when api was unselected.
4. Prefers `execution/runs/<batch-id>/quality-gate-result.json` written by `aa run`; recomputes only if absent.
5. Reads raw logs, traces, screenshots, and videos under `execution/runs/<batch-id>/`.
6. Reads associated `case.yaml` and test source files.
7. Classifies each failure into a defined category.
8. Populates `coverage_gaps[]` in `failure-analysis.json` from the coverage result's low-coverage files (never a Fix Proposal source).
9. Writes `inspect/failure-analysis.json`, `failure-summary.md`, and `inspect/quality-gate-result.json`.

## failure-analysis.json Schema

> **Schema source of truth:** the complete, enforced field contracts for inspect outputs
> live in `assurance_agent/artifacts/models/inspect.py`. Runtime `finalize` validates
> authored files against those models. The examples below are illustrative only.

Primary inspect output written by the CLI (skill verifies; does not fabricate). Minimal top-level structure — see `assurance_agent/artifacts/models/inspect.py` for the full contract.

`inspect_mode` enum (JSON and `workflow-state.json`): `primary | partial`

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "source_batch_id": "<batch-id>",
  "inspect_mode": "primary",
  "classification_performed": true,
  "inspection_status": "completed",
  "final_status": "FAIL",
  "failures": [],
  "known_product_issues": [],
  "coverage_gaps": []
}
```

> `coverage_gaps[]` is populated by the CLI from `execution/coverage-result.json` (files below the line threshold). It is informational — `coverage_gap` is **never** a Fix Proposal source.

## quality-gate-result.json Schema (M1)

The unified, deterministic gate conclusion. CLI-written; the skill verifies and **must not** fabricate or alter `final_status`. Field contract: `assurance_agent/artifacts/models/inspect.py`.

**Minimal illustrative shape:**

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "dimensions": {},
  "final_status": "PASS_WITH_WARNINGS"
}
```

`final_status` is computed worst-wins (`FAIL > PASS_WITH_WARNINGS > PASS > SKIPPED`):
- Functional: any failed API / E2E / **fuzz** test → `FAIL`; ran & none failed → `PASS`; nothing ran → `SKIPPED`. Fuzz folds into the functional dimension (input-robustness is functional correctness); `functional.fuzz` is present only when fuzz actually ran.
- Coverage: below threshold → `PASS_WITH_WARNINGS` (default `gate_mode: warn`) or `FAIL` (`gate_mode: block`); `available == false` → `SKIPPED`.
- Non-functional (performance): any scenario `verdict == FAIL` → `FAIL`; ran & all pass → `PASS`; locust unavailable / no measurements → `SKIPPED`. The `non_functional` block is present only when a performance result exists.

> M1 emits only `functional` + `coverage`. M3 Phase D adds `dimensions.functional.fuzz` (when fuzz ran) and `dimensions.non_functional` (when performance ran).

Failure `category` enum:

```text
locator_failure | wait_strategy_failure | test_code_error | test_data_failure | environment_failure | assertion_failure | business_logic_failure | case_semantic_failure | fuzz_configuration_error | fuzz_stateful_failure | perf_script_error | perf_threshold_exceeded | perf_environment | known_product_issue | coverage_gap | unknown
```

**Fuzz categories (M3, `target == fuzz`):**
- `fuzz_configuration_error` — schema fetch / auth / hypothesis health-check / setup misconfig. **Not** a product bug, **not** auto-fixable; fix the fuzz setup and rerun.
- `fuzz_stateful_failure` — schemathesis surfaced a server fault (5xx / schema violation) under generated input. Treated as a product robustness issue → `needs_review` (never auto-fixed).

**Performance categories (M3, `target == performance`):**
- `perf_script_error` — locustfile error / could not run. Fix the script; not auto-fixable.
- `perf_threshold_exceeded` — measured p95 / error_rate exceeded the absolute threshold from the case. → `needs_review` (never auto-fixed). Review whether the endpoint regressed or the threshold is too strict.
- `perf_environment` — target environment unreachable / no traffic recorded. Environment issue (overall performance status SKIPPED), not a product bug.

Failure `recommended_next_action` enum:

```text
run_fix_proposal | human_review | fix_product | fix_environment | rerun
```

Known issue entry in `known_product_issues[]` (within failure-analysis.json):

```json
{
  "id": "KPI-001",
  "category": "known_product_issue",
  "severity": "major",
  "endpoint": "GET /api/v1/menu/get",
  "symptom": "returns 500",
  "root_cause": "raw ORM object returned through Success(data=result) without serialization",
  "workaround": "tests verify detail/update result through GET /api/v1/menu/list",
  "coverage_gap": "direct GET /api/v1/menu/get endpoint remains unverified",
  "status": "open",
  "requires_promotion": true,
  "next_action": "fix backend serialization in get_menu"
}
```

## Failure Categories

| Category | Fix Proposal |
|----------|:------------:|
| locator_failure | ✓ |
| wait_strategy_failure | ✓ |
| test_code_error | ✓ |
| test_data_failure | review |
| environment_failure | ✗ |
| assertion_failure | ✗ |
| business_logic_failure | ✗ |
| case_semantic_failure | ✗ |
| known_product_issue | ✗ |
| coverage_gap | ✗ |
| fuzz_configuration_error | ✗ |
| fuzz_stateful_failure | review |
| perf_script_error | ✗ |
| perf_threshold_exceeded | review |
| perf_environment | ✗ |
| unknown | review |

## Known Product Issue Classification

When test execution reveals a real product behavior problem but tests use a workaround to continue, classify it as a known product issue — not as a test failure.

Supported inspect analysis categories:

```text
product_bug | known_product_issue | coverage_gap | test_bug | test_data_issue | environment_issue | unknown
```

Examples:

- Endpoint returns 500 due to backend serialization error.
- Test validates behavior through an alternate endpoint because the direct endpoint is broken.
- Product behavior is incorrect but not blocking the remaining test suite.

### Issue Lifecycle Boundary

Inspect classifies failures into execution-evidence categories including `known_product_issue` and `coverage_gap`. These labels are **classification hints only** — they do not create Problems, mutate Ledgers, or change execution `final_status` by themselves.

Hard rules:

- Inspect artifacts (`failure-analysis.json`, `failure-summary.md`) are execution evidence, not canonical Issue lifecycle state.
- Do **not** read or write legacy known-product issue Markdown/JSON files.
- Product bugs and coverage gaps discovered during inspect are reconciled later by `inspect-with-issues` into Observations/Occurrences and the Project Problem Ledger.
- `known_product_issue` and `coverage_gap` must **not** enter Fix Proposal — require product fix or human Problem review.

If inspect confirms product-risk signals, the workflow summary must **not** claim `no risk` or `clean pass` — Issue risk is reported separately after Issue analysis.

## Output Files (read after CLI completes)

```
qa/changes/<change-id>/inspect/           ← primary analysis outputs (source of truth)
├── failure-analysis.json                ← primary mode only (CLI-written)
├── failure-summary.md
├── quality-gate-result.json             ← unified gate conclusion (M1; CLI-written)
├── metrics.json                         ← PR metric vector (workflow materialize; NOT this CLI)
├── trace-projection.json                ← reconciled traceability facts (workflow-written; NOT this CLI)
├── trace-sufficiency.json               ← facts the independent trace gate routes on (workflow-written)
├── inspection-partial.json              ← fallback partial mode only
└── inspection-error.json              ← primary CLI failed / outputs missing

qa/changes/<change-id>/execution/         ← optional pointers/copies for downstream consumers
├── failure-analysis.json                ← pointer/copy (primary mode only)
└── failure-summary.md                   ← pointer/copy
```

## Reconciled Trace Projection and the Independent Trace Gate

After Issue reconciliation the workflow runs `operation:materialize-trace-projection`,
which publishes two change-level documents beside this skill's outputs:

```text
qa/changes/<change-id>/inspect/trace-projection.json    ← reconciled facts (cases → tests → executions → open Problems)
qa/changes/<change-id>/inspect/trace-sufficiency.json   ← integrity / sufficiency / open-problem facts, plus gap codes
```

Materialization is the last thing the inspect subgraph does; it adjudicates nothing. The
`trace-sufficiency-gate` reads the second document one level up, in the `assurance` graph
after `healing` returns (and after the metrics materialize + `metrics-sufficiency-gate`)
and before `report`, and routes the change to `pass` / `needs_human_review` / `stop`.
PR metrics are a separate judgement: `operation:materialize-pr-metrics` writes
`inspect/metrics.json` once after healing, and `metrics-sufficiency-gate` routes on it
via `evaluate_metrics_sufficiency` — that verdict never rewrites
`quality-gate-result.json` or `execution-manifest.json`. That placement matters for this
skill: inspection runs again after every healing rerun, so the trace documents are
rewritten per batch and only the last one is adjudicated. Rules for this skill:

- **Never write, copy, or edit either file.** They are written by the workflow node, not by
  `aa report inspect`; a skill-authored copy would be indistinguishable from a folded one.
- **The trace verdict is not the execution verdict.** `quality-gate-result.json.final_status`
  and `execution/execution-manifest.json` state what the *tests* found. The trace gate is a
  second, independent judgement about whether the evidence behind them is good enough to
  believe, and it must never cause either document to be rewritten.
- **Do not pre-empt the gate.** Do not derive a pass/stop conclusion from
  `trace-sufficiency.json`, and do not report `phases.inspect` as failed because the facts
  are thin: healing may still fix what they describe, and if it does not, the escalation is a
  decision for a person, taken at the top-level interrupt with actions `accept_risk` / `stop`.
- **Read-only if you read them at all.** `integrity` must be read before `sufficient`: an
  `incomplete` projection means an input could not be read, so its row verdicts (including a
  vacuous "everything is sufficient" over zero rows) say nothing about this change.

## CLI Output Missing

If the CLI completes but required primary output is missing (`inspect/failure-analysis.json`):

- Do **not** classify manually
- Set `phases.inspect.status = failed`
- Write `inspect/inspection-error.json` or `inspect/inspection-error.md` with:
  - `reason: analysis_files_missing`
  - `batch_id` from manifest
  - recommendation to rerun `aa report inspect --change <change-id>`
- Do **not** write `failure-analysis.json`

## Steps

1. Confirm execution has run (`phases.execution.status` set).
2. Read `execution/execution-manifest.json` — stop if missing.
3. Check fallback / normalised result rules (Context Contract step 4).
4. Run **AA CLI Identity Check**.
5. If primary inspect allowed: call `aa report inspect --change <change-id>`; wait for completion.
6. If primary not allowed: apply fallback partial inspect if permitted; otherwise STOP.
7. Verify outputs per mode (primary → `failure-analysis.json`; partial → `inspection-partial.json`).
8. Read `inspect/failure-analysis.json` (primary) or `inspect/inspection-partial.json` (partial).
9. Read `inspect/failure-summary.md`.
10. Present summary to user (category breakdown in primary mode; raw summary in partial mode).
11. Report the state delta per mode (see Context Contract **After completing work** — orchestrator applies it to `workflow-state.json` when dispatched).
12. Do **not** generate fix proposals in this skill — eligible failures are consumed by downstream healing skills.

## Status Update Rules

### Primary mode

`aa-inspect` must **not** change `phases.execution.status` based on Issue/Problem state. Execution status reflects test outcomes only; Issue risk is computed separately.

Set `phases.inspect.status = done`, `inspect_mode = primary`, `classification_performed = true`.

### Fallback partial mode

- Do **not** change `phases.execution.status` except never upgrade it.
- Set `phases.inspect.status = partial`
- Set `phases.inspect.inspect_mode = partial`
- Set `phases.inspect.classification_performed = false`
- Do **not** set `phases.inspect.status = done` — partial inspect is not complete diagnosis.

Valid `phases.execution.status` values: `PASS | PASS_WITH_WARNINGS | FAIL | SKIPPED`.

If `final_status == PASS_WITH_WARNINGS`, downstream workflow status must be `completed_with_warnings`, not `completed`.

## Hard Rules

- **Never classify failures yourself** in primary mode — the CLI is the only trusted classifier.
- **Never fabricate** `failure-analysis.json` or category assignments.
- Run **AA CLI Identity Check** before `aa report inspect` — do not invoke Amazon AA CLI.
- If CLI completes but `inspect/failure-analysis.json` is missing, write `inspection-error.*` and set `phases.inspect.status = failed` — do not guess.
- Do **not** invoke MCP as a substitute for the CLI.
- Always read `execution/execution-manifest.json` before inspecting.
- If `fallback_runner.used == true` and `normalised_results_available == false`, primary inspect cannot run — use partial fallback or STOP.
- Only require normalised result files for selected targets in **primary** mode.
- Do **not** read or write legacy known-product issue Markdown/JSON files.
- Environment failures must **not** enter a Fix Proposal pipeline.
- `known_product_issue` and `coverage_gap` must **not** enter Fix Proposal — require product fix.
- In fallback partial mode: no `failure-analysis.json`, no categories, `phases.inspect.status = partial`.

---

## Inspect Role in Healing

`aa-inspect` is the **diagnostic** stage of healing — not the repair stage.

It may:

- Classify failures into categories
- Collect limited evidence (logs, traces, screenshots)
- Determine whether failures have `fix_proposal_eligible == true`
- Write `inspect/failure-analysis.json`
- Write `inspect/failure-summary.md`
- Recommend that Phase 10 (`aa-fix-proposal`) be invoked

It **MUST NOT**:

- Modify test files (`tests/`)
- Modify fixtures
- Modify product code (`app/`, `web/`, `src/`)
- Apply any fix
- Execute tests as verification (diagnostic probes are not reruns — see **Diagnostic Probe Policy**)
- Write `failure-analysis.json` in fallback/partial mode

The repair pipeline is: `aa-fix-proposal` → `aa-api-codegen-fixer` / `aa-e2e-codegen-fixer` → `aa-run` → `aa-inspect` (re-inspect).

---

## Extended failure-analysis Failure Fields

Each failure in `failure-analysis.json.failures[]` written by the CLI (primary mode) should include the fields defined in `assurance_agent/artifacts/models/inspect.py` — and the skill must verify these fields are present when reading for downstream healing.

If the CLI writes these fields, the skill must preserve them. If the CLI does not write them, the skill must **not** fabricate them — leave missing fields absent rather than guessing.

---

## Fix Proposal Eligibility Classification

These rules define `fix_proposal_eligible` for each category (written by the CLI; verified by this skill):

```text
locator_failure:
  fix_proposal_eligible: true
  requires_test_code_change: true
  requires_product_change: false

wait_strategy_failure:
  fix_proposal_eligible: true
  requires_test_code_change: true
  requires_product_change: false

test_code_error:
  fix_proposal_eligible: true
  requires_test_code_change: true
  requires_product_change: false

test_data_failure:
  fix_proposal_eligible: true   # only if test data generation can be fixed without changing product code or expected assertions
  requires_test_code_change: true
  requires_product_change: false (when eligible)
  NOTE: if product change is required → fix_proposal_eligible: false

environment_failure:
  fix_proposal_eligible: false
  requires_test_code_change: false
  requires_product_change: false
  recommended_next_action: fix_environment

assertion_failure:
  fix_proposal_eligible: false
  requires_test_code_change: false
  requires_product_change: false (may indicate product change needed — investigate)
  recommended_next_action: human_review

business_logic_failure:
  fix_proposal_eligible: false
  requires_test_code_change: false
  requires_product_change: true
  recommended_next_action: fix_product

known_product_issue:
  fix_proposal_eligible: false
  requires_product_change: true
  recommended_next_action: fix_product

coverage_gap:
  fix_proposal_eligible: false
  recommended_next_action: separate_change_required

product_bug:
  fix_proposal_eligible: false
  requires_product_change: true
  recommended_next_action: fix_product

unrelated_existing_failure:
  fix_proposal_eligible: false
  in_current_change_scope: false
  recommended_next_action: manual_investigation

unknown:
  fix_proposal_eligible: false
  recommended_next_action: manual_investigation
```

**Hard rule:** `aa-inspect` must **not** override the CLI's `fix_proposal_eligible` assignment. If the CLI writes `fix_proposal_eligible: false` for a `locator_failure`, do not change it — report the discrepancy for human review.

---

## Diagnostic Probe Policy

`aa-inspect` may run a limited number of targeted probes to gather evidence for classification.

**Probe limits:**

- At most **1 diagnostic probe per failure category** per inspect run
- At most **3 diagnostic probes total** per inspect run

**What counts as a probe:**

- Running a single targeted test or assertion to gather output/trace
- Reading a specific log entry matching a known failure pattern
- Checking a route or endpoint directly to confirm a product-level behavior

**What does NOT count as a probe:**

- Running `aa run --change <change-id>` (that is a rerun, not a probe)
- Reading existing log/trace files already present in `execution/`

**Recording probes:**

All probes must be recorded in:

```text
qa/changes/<change-id>/inspect/diagnostic-probes.json
```

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "probes": [
    {
      "probe_id": "PROBE-001",
      "category": "locator_failure",
      "failure_id": "FAIL-002",
      "command": "...",
      "purpose": "Confirm selector .product-card is absent in current DOM",
      "result": "confirmed"
    }
  ]
}
```

**Probes must NOT:**

- Update `execution/execution-manifest.json`
- Update `phases.execution.status`
- Be reported as reruns
- Count toward `max_healing_attempts`

---

## No File Modification

`aa-inspect` **MUST NOT** modify files outside its designated output directories.

**Allowed output paths:**

```text
qa/changes/<change-id>/inspect/**   (except the two workflow-owned files below)
qa/changes/<change-id>/execution/failure-analysis.json   (optional pointer/copy, primary mode only)
qa/changes/<change-id>/execution/failure-summary.md      (optional pointer/copy, primary mode only)
qa/changes/<change-id>/inspect/diagnostic-probes.json    (probe record)
```

**Workflow-owned, never written by this skill** (see **Reconciled Trace Projection and the
Independent Trace Gate**):

```text
qa/changes/<change-id>/inspect/trace-projection.json
qa/changes/<change-id>/inspect/trace-sufficiency.json
```

**Forbidden during inspect:**

```text
tests/**
app/**
web/**
src/**
.aa/**
qa/changes/<change-id>/cases/**
qa/changes/<change-id>/plans/**
qa/changes/<change-id>/review/**
qa/changes/<change-id>/healing/**
qa/changes/<change-id>/execution/execution-manifest.json   (must not update)
```

If the orchestrator (`aa-workflow`) detects that `aa-inspect` modified any forbidden path during Phase 9 or Phase 13, it must:

1. Set `phases.inspect.status = failed`
2. Set `workflow_status = stopped`
3. Stop the workflow immediately
