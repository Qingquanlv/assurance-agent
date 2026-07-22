---
name: aa-workflow
description: "Full AA QA workflow entry. Prefer `aa workflow run --entrypoint full` (GraphRuntime) or workflow_start. Operator recovery: FALLBACK-RUNBOOK.md in this skill directory."
---

# AA Workflow

## Preferred entry (GraphRuntime)

```bash
aa workflow run --change <change-id> --entrypoint full
# or from chat: workflow_start tool with entrypoint=full
```

GraphRuntime owns progression (ledger, checkpoints, retries, interrupts). Phase skills under
`skills/aa-*` remain per-node contracts; agents never write the ledger or edit checkpoints.

| Mode | Explore |
|---|---|
| **Driver** | Dispatched as a graph agent node |
| **Operator recovery** | See `FALLBACK-RUNBOOK.md` — inspect / resume / import only |

## When to open the runbook

Use `FALLBACK-RUNBOOK.md` when recovering a stuck change (expired lease, interrupt, digest drift).
There is **no** agent-orchestrated phase fallback.

## Mode binding

- **Full / autonomous** end-to-end: `--entrypoint full`.
- Design-time questions → `aa-intake` first, then `workflow_start` (`entrypoint: execute`) or
  `aa workflow run --entrypoint execute`.
- Case-only: `--entrypoint case`.

## Invariants

- Subagents never run `aa gate` / `aa status` as progression writers, never edit `events.jsonl`,
  and never hand-mark tasks complete from files.
- Artifact presence is **not** completion — only ledger `task_*` / `task_imported` events are.
- Human safety approval uses `aa workflow resume --interrupt ...`.
- `allow_test_changes` stays on `aa decide` (policy), not graph gates.
