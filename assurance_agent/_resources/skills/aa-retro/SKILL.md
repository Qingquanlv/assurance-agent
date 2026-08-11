---
name: aa-retro
description: Use when a Retro v3 context contains validated actionable signals that require Improvement Candidate proposals.
---

# Retro v3 Improvement Proposer

Read only `qa/retro/<retro-id>/context-agent.json`. This is the multiline,
content-equivalent projection of the runtime-owned canonical `context.json`; use it
so every signal remains visible to bounded file readers. Do not read, modify, or hash
`context.json` yourself. Write only:

- `qa/retro/<retro-id>/proposal-candidates.json`
- `qa/retro/<retro-id>/retro-summary.md`

Map validated signals to concrete process Improvements. Do not repeat domain analysis,
read raw evidence, inspect another Retro run, or modify any project artifact.

Each Candidate must use schema version `3`, cite one or more `signal_ids` present in
the context, and retain immutable `source_refs` from those signals. Allowed pairs are:

- `prompt_improvement` → `memory_patch`
- `fixture_improvement` → `memory_patch` or `change_draft`
- `test_improvement` → `memory_patch` or `change_draft`
- `workflow_improvement` → `change_draft`
- `domain_knowledge` → `knowledge_delta`

`domain_knowledge` is allowed only when `context.integrity.status == complete`, must
cite a Problem, and must contain a valid L2 delta. Other Improvement kinds remain
eligible for incomplete analysis when supported by an `ok` domain's signals.

For a capability delta, preserve the exact L2 hierarchy and leaf objects. In
particular, `domain_factories.<entity>.<name>` is a `CapabilityLeaf` object (never a
list), and adapters must be nested under one of `api`, `e2e`, `fuzz`, or
`performance` before the entity name:

```json
{
  "schema_version": "1",
  "mode": "delta",
  "capabilities": {
    "domain_factories": {
      "menu": {
        "make_menu": {
          "kind": "async_factory",
          "symbol": "tests.testdata.domain.menu.make_menu",
          "entity": "menu"
        }
      }
    },
    "adapters": {
      "e2e": {
        "menu": {
          "make_menu": {
            "kind": "isolated_worker",
            "symbol": "tests.e2e.adapters.menu.make_menu",
            "entity": "menu"
          }
        }
      }
    }
  }
}
```

Never place an entity directly below `capabilities.adapters`, and include only
new or changed L1 leaves in a delta.

Write `ImprovementCandidateDocumentDraftV3`: include `schema_version`, `retro_id`,
and `candidates`; never write `context_sha256` or calculate a digest. The runtime
inserts the exact digest of canonical `context.json` before freeze. Every Candidate must include target,
rationale, proposed change, verification, risk, and confidence. Zero Candidates is
valid only when there are no actionable signals; explain that in the summary.

Never add Problem lifecycle fields such as classification, severity, status, version,
root cause, resolution, or disposition.
