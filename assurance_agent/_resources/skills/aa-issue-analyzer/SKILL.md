---
name: aa-issue-analyzer
description: "AA Issue Analyzer: Read immutable Observations and execution evidence, then propose Issue Candidates for reconciliation. Writes only change:inspect/issue-candidates.json and change:inspect/issue-analysis-status.json. Never writes Ledgers, snapshots, or project Issue state."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-issue-analyzer.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/inspect/observations.json`. This file contains the authoritative, immutable Observation batch for this execution. Do not invent or modify Observations.
2. Read `qa/changes/<change-id>/inspect/issue-evidence-manifest.json`. This file lists all allowlisted evidence files and their content digests. Use it to identify which evidence files you may read.
3. Read only evidence files explicitly listed in the manifest's `entries[].path` values (resolved relative to the change directory). Do not read any other files under `change:`.
4. Read `qa/issues/problems.json` (project Problem projection) and `qa/issues/review-queue.json` if they exist. These are read-only; you must not modify them.
5. If `inspect/observations.json` or `inspect/issue-evidence-manifest.json` is missing or unreadable, stop and report failure — do not invent evidence.

**After completing analysis:**

1. Write `qa/changes/<change-id>/inspect/issue-candidates.json` — the structured candidate proposals.
2. Write `qa/changes/<change-id>/inspect/issue-analysis-status.json` — the analysis status record.
3. Do not write anything else. In particular:
   - Do **NOT** write `issues/events.jsonl`, `issues/snapshot.json`, or any Ledger file.
   - Do **NOT** write any file under `project:qa/issues/**`.
   - Do **NOT** write `.aa/data-knowledge.yaml` or any project knowledge file.

## Authorized Write Paths

```
change:inspect/issue-candidates.json
change:inspect/issue-analysis-status.json
```

All other paths are **forbidden**. A write outside these two paths is a `forbidden_write` error.

## Candidate Proposal Rules

Every candidate you propose **MUST**:

- Cite at least one `observation_id` from the Observations batch (field `observation_ids`, minimum one entry). Candidates that do not cite a valid Observation ID are invalid.
- Contain a unique `candidate_id` within this batch (e.g., `CAND-001`, `CAND-002`).
- Propose a `classification` (`product_bug`, `test_bug`, `test_data_issue`, `environment_issue`, `coverage_gap`, `performance_issue`, `workflow_issue`, `unknown`) — this is a **proposal**, not canonical state.
- Propose a `severity` (`critical`, `high`, `medium`, `low`) — this is a **proposal**, not canonical state.
- Include a `root_cause_hypothesis` — descriptive prose about the likely root cause.
- Include `affected_surface` with `kind` and `value`. `kind` MUST be exactly one of:
  `endpoint`, `module`, `case`, `test`, `workflow`, `environment`, `unknown`.
  Do not invent narrower kinds such as `knowledge`, `plan`, or `ui`; express that
  detail in `value` and map process artifacts/configuration to `workflow`, source
  components to `module`, UI or API routes to `endpoint`, and ambiguous surfaces
  to `unknown`.
- Include `fingerprint_inputs` with `surface`, `symptom`, and optional `qualifiers`.
  The symptom is a stable behavior identity, not a prose summary: use lowercase
  underscore tokens that describe observable behavior and keep it unchanged
  when the same behavior recurs. Omit qualifiers by default. Add them only when
  a stable context fact is required to distinguish two Problems with the same
  surface and symptom; never use evidence source (`fuzz`, review name, case ID),
  guessed root cause, implementation helper, or wording variants as qualifiers.
- Include `possible_problem_ids` — a list of existing Problem IDs from `problems.json` that may match semantically (or empty list if no semantic match).
- Include a `confidence` score between 0.0 and 1.0.
- Include a `recommended_action` string.

**Proposals only — never set canonical state.** The following fields are NOT allowed in candidate output:
- `status`, `version`, `authority`, `resolved`, `merged`, `resolution`, `not_an_issue`, `accepted_risk`
- Any field that asserts a lifecycle decision was already made

## Output Schema: `issue-candidates.json`

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id from manifest>",
  "evidence_bundle_digest": "<digest from manifest>",
  "candidates": [
    {
      "candidate_id": "CAND-001",
      "observation_ids": ["OBS-<hex>"],
      "proposed": {
        "title": "...",
        "classification": "product_bug",
        "severity": "high",
        "root_cause_hypothesis": "..."
      },
      "affected_surface": {"kind": "endpoint", "value": "POST /api/users"},
      "fingerprint_inputs": {"surface": "post /api/users", "symptom": "http_500"},
      "possible_problem_ids": [],
      "confidence": 0.85,
      "recommended_action": "investigate and confirm"
    }
  ]
}
```

## Output Schema: `issue-analysis-status.json`

```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "status": "completed",
  "evidence_bundle_digest": "<digest from manifest>",
  "candidate_count": 1
}
```

Do not write `candidate_digest` and do not run a hash command or create a helper
script, hook, or temporary file to compute it. The runtime computes and inserts
the canonical digest after validating both declared outputs and before freezing
the task write-set.

If analysis fails irrecoverably due to unreadable evidence:
```json
{
  "schema_version": "1.0",
  "change_id": "<change-id>",
  "batch_id": "<batch-id>",
  "status": "failed",
  "evidence_bundle_digest": "<digest from manifest>",
  "candidate_count": 0,
  "reason": "invalid_output"
}
```

## Evidence Reading Discipline

- Read **only** files listed in the evidence manifest `entries[].path`.
- Do not read `execution/`, `codegen/`, `review/`, `healing/`, `plans/`, `facts/`, or `cases/` directly unless they appear in the manifest.
- Redact any secret-like values (tokens, passwords, keys) before including them in a hypothesis. Do not include raw secret values in any output field.
- Do not read or reference `.aa/data-knowledge.yaml`.

## Problem Projection Use

- Read `qa/issues/problems.json` to find existing Problems whose fingerprints might semantically match a candidate.
- When you judge that an observation is the same Problem and that Problem's
  `fingerprint.preimage` is present, reuse the preimage exactly:
  - set `affected_surface.kind` from `surface_kind`;
  - set `affected_surface.value` and `fingerprint_inputs.surface` from
    `surface_identity`;
  - set `fingerprint_inputs.symptom` and `qualifiers` from the preimage;
  - do not add that Problem ID to `possible_problem_ids`.
  This is the only automatic-link path. Do not paraphrase or enrich a retained
  preimage—the reconciler must compute the existing digest exactly.
- If a Problem is only semantically similar, or is a legacy Problem whose
  fingerprint has no `preimage`, populate `possible_problem_ids` for human
  merge review. Never force a semantic match into an exact identity.
- Never set any field asserting that a Problem was created, updated, resolved, or merged.

## Idempotency

If `issue-candidates.json` already exists with matching `batch_id` and `evidence_bundle_digest`, you may overwrite it with refined output. The reconciler (not the analyzer) controls the authoritative lifecycle.
