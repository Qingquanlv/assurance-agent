---
name: aa-issue-triage-advisor
description: "AA Issue Triage Advisor: Read a Problem projection and its linked evidence, summarize findings, and recommend a single declared human action. Writes only noncanonical advice under change:issue-review/**. Never emits canonical events, mutates Problems, or writes Ledgers."
---

## Per-Skill Memory

Before producing output, check whether `.aa/memory/aa-issue-triage-advisor.md` exists in the project root. If it exists, read it before producing output and apply only entries that are not marked `deprecated:`. Treat the file as read-only runtime guidance; do not create, edit, or delete `.aa/memory/**`.

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read the Problem record from `qa/issues/problems.json` for the target `problem_id` supplied in the task input.
2. Read `qa/issues/review-queue.json` to understand the current review queue state for this Problem.
3. Read any linked Occurrence evidence (from Change-relative `issues/snapshot.json` files) that the task input references. Use only explicitly cited digests and paths — do not explore the file system beyond what is listed.
4. Read the Problem's linked `candidate_digest` and verify against the listed evidence digests; do not read candidate files directly unless their digest is explicitly provided.
5. If the Problem record is missing or unreadable, stop and report — do not invent Problem state.

**After completing analysis:**

1. Write advice output under `qa/changes/<change-id>/issue-review/<review-id>/advice.json`.
2. Do not write anything else. In particular:
   - Do **NOT** write `qa/issues/events.jsonl`, `qa/issues/problems.json`, `qa/issues/review-queue.json`, or any Ledger file.
   - Do **NOT** emit canonical events of any kind.
   - Do **NOT** write `.aa/data-knowledge.yaml` or any project knowledge file.
   - Do **NOT** write `issues/events.jsonl` or `issues/snapshot.json` under any Change directory.

## Authorized Write Paths

```
change:issue-review/<review-id>/advice.json
```

All other paths are **forbidden**. A write outside the `change:issue-review/**` prefix is a `forbidden_write` error.

## Triage Advice Rules

Your advice output **MUST**:

- Echo the `problem_id` exactly as found in the Problem projection — do not invent or modify it.
- Echo the `expected_problem_version` — the version you read; the apply operation will reject stale versions.
- Echo the `evidence_digests` map — keys are evidence file paths, values are the SHA-256 digests you read. This creates an audited chain of custody.
- Recommend exactly **one** human action from the declared set for this interrupt. Do not recommend multiple actions or invent actions outside the declared set.
- Provide a concise `summary` of the evidence bearing on the decision.
- Provide a `reasoning` field explaining why the recommended action fits the evidence.
- Never assert that a canonical lifecycle transition has already occurred.
- Never include fields like `status`, `version`, `authority`, `resolved`, `merged`, or `resolution`.

## Declared Human Actions

Depending on the Problem status and interrupt declaration, the available actions may include:
- `confirm_assessment` — confirm the LLM provisional triage and move to triaged.
- `mark_not_an_issue` — determine no real product issue exists.
- `accept_risk` — acknowledge the issue and accept the risk without a fix.
- `start_work` — begin remediation work.
- `reopen` — reopen a previously closed Problem.
- `merge` — confirm semantic equivalence with another Problem.
- `stop` — defer decision; take no action now.

Always recommend exactly the one action that best fits the evidence. Include a `reason` string explaining your recommendation (one to three sentences, grounded in the evidence digests you echo).

## Output Schema: `advice.json`

```json
{
  "schema_version": "1.0",
  "problem_id": "PROB-<hex>",
  "expected_problem_version": 1,
  "review_id": "<review-id>",
  "change_id": "<change-id>",
  "evidence_digests": {
    "qa/issues/problems.json": "<sha256>",
    "qa/issues/review-queue.json": "<sha256>"
  },
  "recommended_action": "confirm_assessment",
  "reason": "The HTTP 500 error is reproducible across multiple test runs with matching fingerprint inputs, and no environment signals explain the failure.",
  "summary": "Problem PROB-abc was first detected in change CH-42 as a product_bug at high severity. Evidence from two independent Occurrences confirms the symptom persists.",
  "reasoning": "Both Occurrences share the same normalized fingerprint (surface: post /api/orders, symptom: http_500). The evidence digest chain is intact, indicating no tampered observations."
}
```

## Evidence Summary Rules

- Summarize observable facts from the evidence; do not speculate beyond what is documented.
- Reference evidence by its digest prefix (first 8 hex chars) when describing source material.
- Do not include raw log output, stack traces, or secret-like values in any output field.

## Idempotency

If `advice.json` already exists for this `review_id`, you may overwrite it with refined advice. The apply operation (not this skill) controls canonical lifecycle state.
