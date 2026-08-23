# OpenCode provider-live benchmark

**Item:** `opencode-ret-dept-management`
**Product:** `assurance-opencode`
**Entrypoint:** `full`
**Started:** 2026-08-23T05:09:42Z
**Ended:** 2026-08-23T05:18:20Z
**Terminal status:** `running`
**Lock digest:** `ef6f9e1b8778bdc68c8a487d17e8d3d7bb7070e5c18f170766bb8fd53691b72f`
**Outcome:** blocked

## Routing

Every prepare ID is locked to `openai/gpt-5.6-terra` / `max`.

## Status

```json
{
  "coverage_progress": null,
  "entrypoint": "full",
  "lock_digest": "ef6f9e1b8778bdc68c8a487d17e8d3d7bb7070e5c18f170766bb8fd53691b72f",
  "selected_test_families": [
    "api",
    "e2e",
    "fuzz",
    "performance"
  ],
  "status": "running",
  "terminal_reason": null
}
```

## Validation

```json
{
  "last_run": {
    "actions": [],
    "invocation_id": "opencode-ret-dept-management",
    "status": "interrupted",
    "terminal_reason": "activity_recovery"
  },
  "transitions": [
    {
      "at": "2026-08-23T05:10:05Z",
      "status": "interrupted",
      "terminal_reason": "activity_recovery"
    },
    {
      "at": "2026-08-23T05:17:55Z",
      "status": "running",
      "terminal_reason": "activity_recovery"
    }
  ]
}
```

## Notes

OpenCode at the pinned origin was healthy. Isolated `aa-next` compile/start succeeded. The first `aa-next run` returned `interrupted` / `activity_recovery` with empty actions (exit 30). Authoritative `aa-next status --json` stayed `running`: graph `full`/`intake` running, `prepare` succeeded, `execute` still running, `pending_interrupt` null, coverage unset.

The intake execute activity was bound to the live OpenCode adapter and then cancelled with reason `timeout`. The compiled workflow timeout class `short` is 30 seconds; that is shorter than a live intake host turn. Because `pending_interrupt` declared no actions, `aa-next resume` was not invoked. A second `aa-next run` on the same invocation again returned `interrupted` / `activity_recovery` with empty actions and no ledger progress.

Expected terminal `completed` was not reached. Export was not attempted. Provider conversation text was not used as status. Runtime secret bytes were authorized only through the Task 19 env-handle form and are not present in the manifest, this file, or git. Phase 3 adapter-fixture evidence is not this run.
