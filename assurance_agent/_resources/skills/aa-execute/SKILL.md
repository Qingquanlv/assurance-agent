---
name: aa-execute
description: "Fallback for two-stage execute mode. Prefer workflow_start / `aa workflow run --entrypoint execute`. Operator recovery uses aa-workflow/FALLBACK-RUNBOOK.md (status / resume / import only)."
---

# AA Execute

## Preferred entry (driver)

After `aa-intake` completes and the user confirms:

1. Call OpenCode tool **`workflow_start`** with `entrypoint: execute`, **or**
2. CLI: `aa workflow run --change <change-id> --entrypoint execute`

Do not re-run intake phases. The driver enforces execute-entrypoint preflight (case-review pass,
cases present, test infra, etc.). GraphRuntime owns progression; do not hand-edit
`workflow-state.json` or invent phase completion.

## When the preferred entry is unavailable

1. Resolve `<change-id>` and params (`run_mode` ∈ `full` | `api-only` | `e2e-only` | `plan-only` | `codegen-only` | `review-plan`).
2. Still prefer CLI GraphRuntime entry (do not hand-write `run_context`):

```bash
aa workflow run --change <change-id> --entrypoint execute
```

3. **Preflight** (STOP if unmet — execute never asks interactive bootstrap questions):
   - `review/case-review.json` with `decision == "pass"`
   - `qa/changes/<id>/cases/` present
   - no unanswered explore open questions
   - `tests/config.py`, `tests/conftest.py`, `tests/schema_validation.py` conformant
4. If the run is stuck, follow **`skills/aa-workflow/FALLBACK-RUNBOOK.md`**:
   inspect `aa status`, plain-resume expired leases, resolve listed interrupts, or
   run validated checkpoint import. Never delete events, edit checkpoints, or mark tasks
   complete from bare files.
5. Explore / case-design / case-review / case-fix stay out of scope for this entrypoint.

## Completion

Terminal when `aa status --change <id> --next --json` reports `completed` for
`active_entrypoint: execute` (or driver exit 0 / paused 30 for human review).

## Human review

Never hand-edit review JSON to `pass`. Use
`aa workflow resume --change <id> --interrupt <interrupt-id> --action <action> --reason "<text>"`
after the user decides, then continue with plain `aa workflow resume` / `workflow_start` if needed.
