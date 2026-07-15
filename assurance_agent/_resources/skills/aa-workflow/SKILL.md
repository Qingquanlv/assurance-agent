---
name: aa-workflow
description: "Full AA QA workflow entry. Prefer `aa workflow run --scope full` (the aa workflow driver) or workflow_start. Fallback only when the driver is unavailable — then follow FALLBACK-RUNBOOK.md in this skill directory."
---

# AA Workflow

## Preferred entry (driver)

```bash
aa workflow run --change <change-id> --scope full
# or from chat (after intake / for full): workflow_start tool with scope=full
```

The aa workflow driver owns `aa status` / `aa gate check` / `workflow-state.yaml`, phase dispatch,
review/fix loops, healing, and human-review pause/resume. Phase skills under `skills/aa-*`
remain the per-phase contracts; do not re-implement them here.

| Mode | Explore |
|---|---|
| **Driver** | Dispatched to `aa-doc-author` |
| **Fallback** (this skill) | **Inline** in the primary agent (subagents often lack Bash for `aa risk *`) |

## When to use this skill (fallback)

Use this skill only if:

1. `aa workflow` CLI / driver is unavailable in the environment, **or**
2. The user explicitly asks for legacy agent-orchestrated mode.

Then:

1. Read **`skills/aa-workflow/FALLBACK-RUNBOOK.md`** (same directory) and follow it end-to-end.
2. Keep explore **inline** in fallback task mode.
3. Orchestrator still owns `aa status --next`, `aa gate check`, and `workflow-state.yaml`.

Do **not** paste or re-derive the full runbook into the chat — open the fallback file.

## Mode binding

- **Full / autonomous** end-to-end without design-time clarification dialogue.
- Design-time questions → `aa-intake` first, then `workflow_start` (`scope: execute`) or `aa workflow run --scope execute`.
- Two-stage execute without driver → `aa-execute` fallback skill (also thin; points here).

## Invariants (all modes)

- Subagents / phase agents never run `aa gate` / `aa status` or edit `workflow-state.yaml`.
- Producing artifacts on disk is **not** phase completion — state apply / hand-update + `aa status` required (see fallback runbook).
- `force_continue` and review gates are schema/CLI-owned (`schemas/workflow-schema.yaml`).
