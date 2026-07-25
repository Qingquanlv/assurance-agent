---
name: aa-retro
description: Use after aa retro has generated qa/retro/<retro-id>/context.json to emit schema-v2 Improvement Candidates. Candidate-only; never modify skills, schema, memory, Issue Ledgers, or project files.
---

# AA Retro Candidate Skill

Use this skill when asked to analyze the current Retro run's `context.json` and emit Improvement Candidates for the Assurance Agent system.

## Inputs

- Required (only): `qa/retro/<retro-id>/context.json`
- Do **not** read any other path: other Retro runs, Issue Ledgers, raw archive, Improvement Ledgers, memory files, data knowledge files, workflow schema, product source files, or historical promotions.

## Outputs

Write only:

- `qa/retro/<retro-id>/proposal-candidates.json` — schema_version `"2"` Candidate document
- `qa/retro/<retro-id>/retro-summary.md` — human summary of this run

Pin `retro_id` and `context_sha256` to the current `context.json`. Never declare Problem lifecycle fields (`classification`, `severity`, `status`, `version`, `root_cause`, dispositions).

## Hard Rules

- Cite only source IDs that appear in `context.source_manifest` (resolvable via Issue/Workflow/Eval evidence IDs already frozen in context).
- Use the five `ImprovementKind` values and three `DeliveryKind` values; respect the compatibility matrix below.
- Candidate batches are all-or-nothing: every Candidate must be valid, or write zero Candidates and explain gaps in the summary.
- Zero Candidates is a successful no-op when evidence is weak.
- Never mutate Skills, workflow schema, memory, Issue Ledgers, Improvement Ledgers, or project source files.

## ImprovementKind × DeliveryKind

| ImprovementKind | Allowed DeliveryKind |
|---|---|
| `prompt_improvement` | `memory_patch` |
| `fixture_improvement` | `memory_patch`, `change_draft` |
| `test_improvement` | `memory_patch`, `change_draft` |
| `workflow_improvement` | `change_draft` |
| `domain_knowledge` | `knowledge_delta` |

## Required Analysis Order

1. **Skill execution drift** (`signals.workflow.skill_execution_drift`) — evaluate first; emit at least one `prompt_improvement` / `memory_patch` per drifted phase when the signal is present.
2. **Issue-derived signals** (`signals.issue.*`) — cite immutable Problem/Occurrence/event IDs from context; never invent Problem assessment fields.
3. Workflow gate pushback / healing efficiency, then eval trends.
4. Draft Candidates; write `proposal-candidates.json` + `retro-summary.md`.

## Domain Knowledge Eligibility

Emit `domain_knowledge` / `knowledge_delta` only when `context.integrity` allows domain knowledge (Issue analysis/sync complete). Incomplete Issue integrity blocks only `domain_knowledge`; other kinds remain eligible.

`knowledge_delta` payload must be an L2 delta (`schema_version`, `mode: "delta"`, and only new/changed L1 leaves). Cite at least one `problem_ids` entry. Do not embed Problem status, severity, or temporary workarounds in the delta.

## Candidate Shape

```json
{
  "schema_version": "2",
  "retro_id": "retro-20260725",
  "context_sha256": "sha256:…",
  "candidates": [
    {
      "candidate_id": "IMP-CAND-001",
      "kind": "workflow_improvement",
      "delivery": "change_draft",
      "source_refs": {
        "problem_ids": ["PROB-…"],
        "occurrence_ids": [],
        "issue_event_ids": [],
        "workflow_evidence_ids": [],
        "eval_run_ids": []
      },
      "target": "assurance_agent/workflow/inspect",
      "rationale": "Why the frozen evidence implies a process/product improvement",
      "proposed_change": "Concrete intended change",
      "verification": {
        "suites": ["workflow-full"],
        "required_cases": [],
        "success_criteria": "Observable success condition"
      },
      "risk": "low",
      "confidence": "high"
    },
    {
      "candidate_id": "IMP-CAND-002",
      "kind": "prompt_improvement",
      "delivery": "memory_patch",
      "source_refs": {
        "workflow_evidence_ids": ["RET-…#workflow-state:report"]
      },
      "target": ".aa/memory/aa-report-generator.md",
      "rationale": "Skill execution drift: phase ran with skill_loaded=false",
      "proposed_change": "Append a rule to load the phase SKILL.md before reporting skill_loaded=true",
      "verification": {
        "suites": ["workflow-full"],
        "success_criteria": "No skill_execution_drift for report"
      },
      "risk": "low",
      "confidence": "high"
    },
    {
      "candidate_id": "IMP-CAND-003",
      "kind": "domain_knowledge",
      "delivery": "knowledge_delta",
      "source_refs": {
        "problem_ids": ["PROB-…"]
      },
      "target": ".aa/data-knowledge.yaml",
      "rationale": "Stable reusable entity constraint missing from L1",
      "proposed_change": "Add dept.required_fields including name",
      "knowledge_delta": {
        "schema_version": "1",
        "mode": "delta",
        "entities": {
          "dept": {"required_fields": ["name"]}
        }
      },
      "verification": {
        "suites": ["workflow-api-codegen"],
        "success_criteria": "Generated dept fixtures include name"
      },
      "risk": "low",
      "confidence": "medium"
    }
  ]
}
```

`candidate_id` is run-local only. Canonical Improvement IDs are assigned later by the deterministic reconciler.

## Output: retro-summary.md

Summarize:

- evidence window and Change count from context
- skill execution drift: phase / whether a Candidate was filed
- top repeated Issue/Workflow/Eval signals
- Candidates grouped by `ImprovementKind` / `DeliveryKind`
- observations skipped for weak evidence
