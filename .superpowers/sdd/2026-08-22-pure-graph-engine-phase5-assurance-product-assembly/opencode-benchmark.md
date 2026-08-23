# OpenCode provider-live benchmark

**Item:** `opencode-ret-dept-management`
**Product:** `assurance-opencode`
**Entrypoint:** `full`
**Started:** 2026-08-23T06:46:10Z
**Ended:** (in progress)
**Terminal status:** `running`
**Lock digest:** `9fbf87b7d489cb78d563fe9954ff25d8d4606f2203fe0c77fec54e94286a279f`
**Outcome:** incomplete

## Routing

Every prepare ID is locked to `openai/gpt-5.6-terra` / `max`.

## Status

Authoritative invocation remains `running` (`pending_interrupt` null, coverage unset). Ledger: intake `prepare` succeeded; intake `execute` bound (`task_activity_bound`) and has stayed bound without `prompt identity conflict`. Export has not been attempted. Resume was not invoked. Provider conversation text was not used as status. Runtime secret bytes are not present in this file.

```json
{
  "coverage_progress": null,
  "entrypoint": "full",
  "lock_digest": "9fbf87b7d489cb78d563fe9954ff25d8d4606f2203fe0c77fec54e94286a279f",
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

## Notes

Fresh invocation `opencode-20260823-064610` (not a resume). Isolated compile/start succeeded. Compiled workflow class `short.run_seconds` is `3600`.

Two earlier fresh attempts after the worker-crash fix (`opencode-20260823-062828`, `opencode-20260823-063339`) reached bind, then `execute` returned `TaskOutcome.failed(external_effect, prompt identity conflict, retryable=False)` instead of raising `OpenCodeDispatchIncomplete`. No `activity_recovery`. Live GET `/session/{id}/message/{id}` is `{info, parts}` with `info.id` equal to the minted `messageID`; OpenCode prepends extra text to the first stored part. Containment on same-`messageID` texts is what let this third run stay `running` past that re-observe.

Expected terminal `completed` has not been reached yet. The live `run-opencode.sh` process was left running.
