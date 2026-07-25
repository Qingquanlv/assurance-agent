---
name: aa-retro
description: Use after aa retro has generated qa/retro/<retro-id>/context.json to produce evidence-backed improvement proposals. Proposal-only; never directly modify skills, schema, memory, or project files.
---

# AA Retro Proposal Skill

Use this skill when asked to analyze `qa/retro/<retro-id>/context.json` and propose improvements for the Assurance Agent system.

## Inputs

- Required: `qa/retro/<retro-id>/context.json`
- Optional read-only context:
  - recent `qa/retro/*/promotions.json` files (for rejected / needs_rework history)
  - `qa/retro/<retro-id>/evidence/<change-id>/` snapshots for `evidence_source: "unarchived"`
  - `qa/issues/problems.json` and `qa/issues/events.jsonl` (read-only lifecycle evidence; never mutate)
  - `.aa/memory/**`
  - `.aa/data-knowledge.yaml`
  - `schemas/workflow-schema.yaml`
  - relevant `skills/*/SKILL.md`

## Hard Rules

- Do not modify SKILL.md, workflow schema, `.aa/memory/**`, `.aa/data-knowledge.yaml`, `qa/issues/**`, or project source files.
- Write only:
  - `qa/retro/<retro-id>/proposals.json`
  - `qa/retro/<retro-id>/retro-summary.md`
- Every proposal must cite `evidence_ids` that already exist in `context.json`.
- If evidence is weak or missing, do not create a proposal — **except** skill-execution drift (see below), which is mandatory when the signal is present.
- Generate proposals with `status: "proposed"` only.
- Do not repeat proposals that are equivalent to recent `rejected` proposals.
- For observations similar to recent `needs_rework` proposals, incorporate the `rework_note` and materially revise the proposal instead of resubmitting the same text.
- Every proposal **must** include machine fields `finding_kind`, `apply_kind`, and structured `payload` (see Three-Track Routing below). Natural-language `problem` / `proposed_change` are for humans; export and apply use `payload`.
- **Write gate:** after you finish, nightly `collect` runs `accept_proposals`, which rewrites `proposals.json` into the canonical three-track shape. Entries that cannot be routed (missing/unknown `apply_kind`, or `domain_knowledge` without a real L2 `payload`) fail the collect — same fail-closed role as graph `invalid_output`. Do not omit `finding_kind` / `payload` and rely on prose alone.

## Required Analysis Order

1. **Skill execution drift** (`signals.skill_execution`) — evaluate first; see next section.
2. **Issue lifecycle** (`signals.occurrence_trends`, `signals.issue_regressions`, `signals.problem_decisions`, `signals.problem_resolutions`, `signals.not_an_issue_patterns`) — cite immutable Change/Project event ids from `context.json`; never mutate Problems or Ledgers.
3. Failure distribution, gate pushback, healing efficiency, human overrides, reclassifications, eval trend.
4. Draft proposals; write `proposals.json` + `retro-summary.md`.

## Skill Execution Drift (mandatory)

`signals.skill_execution` is produced when a workflow phase finished with `skill_loaded: false` (phase ran without loading its skill contract). `true` and `n/a` are not drift.

**If `signals.skill_execution` is non-empty, you MUST create at least one proposal per drifted phase** (unless an equivalent proposal was recently `rejected`, or you are revising a recent `needs_rework` with a material rewrite). Do not bury drift only under "rejected observations" or skip it because other failure signals look more interesting.

For each drifted phase entry:

- Cite that entry's `evidence_ids` (e.g. `RET-…#workflow-state:report`).
- Use `finding_kind: "prompt_rule"`, `apply_kind: "memory_append"`.
- Target the phase's skill memory file; if the root cause is orchestrator not enforcing load-before-work, also consider a second proposal targeting `.aa/memory/aa-workflow.md`.
- `problem` must name the phase and state it is skill-execution drift (`skill_loaded=false`).
- `payload.body` must append a concrete rule: read that phase's `SKILL.md` before doing phase work, and only then set / report `skill_loaded: true`.

Phase → memory target → eval_suite:

| phase (examples) | target | eval_suite |
|---|---|---|
| `case_design` / case review | `.aa/memory/aa-case-design.md` (or reviewer skill memory) | `workflow-case` |
| `api_plan` / `api_codegen` / related | matching `.aa/memory/aa-api-*.md` | `workflow-api-codegen` |
| `e2e_plan` / `e2e_codegen` / related | matching `.aa/memory/aa-e2e-*.md` | `workflow-e2e-codegen` |
| `execution` / `healing` / `healing_rerun` | `.aa/memory/aa-run.md` or `.aa/memory/aa-execute.md` | `workflow-run` |
| `inspect` / `healing_reinspect` | `.aa/memory/aa-inspect.md` | `workflow-full` |
| `report` | `.aa/memory/aa-report-generator.md` | `workflow-full` |
| unknown / orchestrator-wide | `.aa/memory/aa-workflow.md` | `workflow-full` |

Single-change drift still gets a proposal (nightly may auto-tag `needs_rework` when unique change evidence `< 2`; that is driver policy, not a reason to omit the proposal).

## Issue Lifecycle Evidence (read-only)

When `context.json` carries Issue lifecycle signals, treat them as immutable audit evidence:

| signal family | meaning | proposal guidance |
|---|---|---|
| `occurrence_trends` | repeated Occurrence classifications in the window | `workflow_bug` when classifier/reconciler wiring is wrong; `domain_knowledge` only when L1 facts are missing |
| `issue_regressions` | resolved Problems that recurred | `workflow_bug` for verification/healing gaps |
| `problem_decisions` | human review actions (`confirm_assessment`, `mark_not_an_issue`, etc.) | usually no new proposal unless the workflow made the human repeat the same correction |
| `problem_resolutions` | `resolved` or `verification_pending` outcomes | `workflow_bug` when verification scope or healing linkage is incomplete |
| `not_an_issue_patterns` | repeated `mark_not_an_issue` by classification | `prompt_rule` or `workflow_bug` when the same misclassification keeps recurring |

Cite evidence ids exactly as emitted (e.g. `CH-1#issue-EVT-…`, `project#problem-EVT-…`). Retro acceptance validates those ids against `context.json`; retro collect/accept never writes `qa/issues/**` or `.aa/data-knowledge.yaml`. Knowledge changes require export + `aa knowledge promote` after human review.

## Three-Track Routing

Use **`finding_kind`** to choose the track. **`layer`** is optional human context only (agent / interaction / team); it does **not** route proposals.

| finding_kind | apply_kind | Where it goes | Human action after export |
|---|---|---|---|
| `prompt_rule` | `memory_append` | `.aa/memory/<skill>.md` via `aa retro promote` → eval → apply | Review queue → promote → nightly eval |
| `workflow_bug` | `issue_export` | `qa/retro/<id>/issue-drafts/<proposal-id>.yaml` via `aa retro export-issues` | Dev issue / PR from exported draft |
| `domain_knowledge` | `knowledge_delta` | `qa/retro/<id>/knowledge-delta/<proposal-id>.proposal.yaml` via `aa retro export-knowledge` | `aa knowledge promote --from <path>` merges into L1 |

**Mandatory correspondence** (validator rejects mismatches):

- `prompt_rule` ↔ `memory_append`
- `workflow_bug` ↔ `issue_export`
- `domain_knowledge` ↔ `knowledge_delta`

### Track 1 — `prompt_rule` / `memory_append`

- `target`: `.aa/memory/aa-<skill>.md` (must stay under `.aa/memory/`).
- `payload`: `{ "body": "<exact memory rule text to append>" }`
- `eval_suite`: required (see Eval Suite Selection).
- Routed to nightly review queue when evidence spans enough distinct changes.

### Track 2 — `workflow_bug` / `issue_export`

- Use when the fix belongs in engine code, inspect classifier, gate wiring, etc.—not a skill memory rule.
- `target`: human-readable code area (e.g. `assurance_agent/workflow/inspect`).
- `payload` (all required):

```json
{
  "title": "short issue title",
  "target": "assurance_agent/workflow/report/failure_classifier.py",
  "severity": "low|medium|high",
  "evidence_ids": ["RET-…#FAIL-001"],
  "proposed_change": "what to change in code or contract"
}
```

- No `eval_suite` for memory eval; export via `aa retro export-issues --retro <id>`.

### Track 3 — `domain_knowledge` / `knowledge_delta`

- Use when retro discovered missing or contradictory **L1 domain facts** (auth, accounts, entities, capabilities).
- `payload`: canonical L2 proposal object with `schema_version`, `mode: "delta"`, optional `based_on_l1_version`, and only the **new/changed** L1 leaves (same shape as `plans/data-knowledge.proposal.*.yaml`).

```json
{
  "schema_version": "1",
  "mode": "delta",
  "based_on_l1_version": 1,
  "auth": {
    "api_admin_token": {
      "method": "token",
      "symbol": "tests.api.conftest.admin_token"
    }
  }
}
```

- Export via `aa retro export-knowledge --retro <id>`, then merge with `aa knowledge promote --from qa/retro/<id>/knowledge-delta/<proposal-id>.proposal.yaml`.

## Evidence Source Handling

`context.json` may include evidence from both archived and unarchived changes.
Treat both as valid if their evidence_ids exist in context.json. When explaining or
summarizing evidence:

- `evidence_source: "archive"` means the stable evidence is under `qa/archive/<change-id>/`.
- `evidence_source: "unarchived"` means the stable evidence snapshot is under
  `qa/retro/<retro-id>/evidence/<change-id>/`; do not rely on live `qa/changes/`
  because benchmark cleanup may remove it before review.

## Eval Suite Selection

Use an existing eval suite only (required for `memory_append` proposals):

| Target | eval_suite |
|---|---|
| `.aa/memory/aa-api-codegen.md` | `workflow-api-codegen` |
| `.aa/memory/aa-e2e-codegen.md` | `workflow-e2e-codegen` |
| `.aa/memory/aa-fuzz-codegen.md` | `workflow-fuzz-codegen` |
| `.aa/memory/aa-performance-codegen.md` | `workflow-performance-codegen` |
| `.aa/memory/aa-case-design.md` | `workflow-case` |
| run/healing execution memory | `workflow-run` |
| Other valid `.aa/memory/aa-*.md` targets | `workflow-full` |

Never invent suite names such as `workflow-inspect-codegen`.

## Output: proposals.json

Machine fields are mandatory on every entry. A minimal valid memory proposal is:

```json
{
  "id": "RETRO-001",
  "finding_kind": "prompt_rule",
  "apply_kind": "memory_append",
  "payload": { "body": "<exact rule text to append>" },
  "target": ".aa/memory/aa-<skill>.md",
  "eval_suite": "workflow-<suite>",
  "evidence_ids": ["RET-…#…"],
  "problem": "<human summary>",
  "status": "proposed"
}
```

Full multi-track example:

```json
{
  "retro_id": "retro-20260708",
  "proposals": [
    {
      "id": "RETRO-001",
      "finding_kind": "prompt_rule",
      "apply_kind": "memory_append",
      "layer": "agent",
      "target": ".aa/memory/aa-api-codegen.md",
      "problem": "Repeated test data failures for department name length",
      "evidence_ids": ["RET-a#fail-1"],
      "proposed_change": "Append a rule to keep generated department names within ORM max_length constraints.",
      "payload": {
        "body": "Append a rule to keep generated department names within ORM max_length constraints."
      },
      "eval_suite": "workflow-api-codegen",
      "risk": "low",
      "confidence": "high",
      "status": "proposed"
    },
    {
      "id": "RETRO-002",
      "finding_kind": "workflow_bug",
      "apply_kind": "issue_export",
      "layer": "interaction",
      "target": "assurance_agent/workflow/inspect",
      "problem": "Inspect classifies schema KeyError as unknown despite explicit log excerpt",
      "evidence_ids": ["RET-b#fail-2"],
      "proposed_change": "Classify _LOCAL_SCHEMAS KeyError as test_data_failure, not unknown.",
      "payload": {
        "title": "Inspect misclassifies schema KeyError as unknown",
        "target": "assurance_agent/workflow/inspect",
        "severity": "medium",
        "evidence_ids": ["RET-b#fail-2"],
        "proposed_change": "Classify _LOCAL_SCHEMAS KeyError as test_data_failure, not unknown."
      },
      "risk": "low",
      "confidence": "medium",
      "status": "proposed"
    }
  ]
}
```

## Output: retro-summary.md

Summarize:

- evidence window and change count
- **skill execution drift**: for each `signals.skill_execution` entry, state phase / count / whether a proposal was filed (or why skipped: recent reject / rework revision only)
- top repeated failures
- proposed changes grouped by **finding_kind** (prompt_rule / workflow_bug / domain_knowledge) and expected downstream command (`promote`, `export-issues`, `export-knowledge` → `knowledge promote`)
- rejected observations with insufficient evidence
- eval suite required for each `memory_append` proposal
