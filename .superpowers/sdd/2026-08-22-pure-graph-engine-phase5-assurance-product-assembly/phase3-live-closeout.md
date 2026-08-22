# Phase 3 provider-live closeout

**Date:** 2026-08-22  
**Status:** BLOCKED  
**Worktree:** `pure-graph-engine-phase3-spec`

## Summary

Contract test, manifest `items[]` transformation, and fail-closed `Engine.production` runner landed. Both mandatory live fixture scripts were executed sequentially and exited non-zero. Task 9 remains incomplete; no success evidence was fabricated.

## OpenCode fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-opencode.sh`  
**Exit status:** `1`

**Blocking condition:** Live OpenCode at pinned `http://127.0.0.1:4096` responds to `/global/health`, but `/config` does not advertise the pinned `opencode-http-v1` protocol profile expected by `AcceptedOpenCodeProfile`. Preflight fails closed before engine start.

**Representative error:**

```
phase3-live: pinned OpenCode protocol profile is unavailable on the live server (opencode-http-v1): 20 validation errors for AcceptedOpenCodeProfile
protocol_profile
  Field required
...
```

**Item:** `phase3-opencode-live`  
**Adapter version:** `0.1.0`  
**Model (frozen request):** `provider_default`

## Cursor fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-cursor.sh`  
**Exit status:** `1`

**Blocking condition:** Pinned executable exists and matches digest, but runtime secret source `CURSOR_API_KEY` is unset. Runner refuses to invent credentials.

**Representative error:**

```
phase3-live: Cursor secret 'CURSOR_API_KEY' is unset; refusing to invent credentials
```

**Item:** `phase3-cursor-live`  
**Adapter version:** `0.1.0`  
**Model (frozen request):** `provider_default`

## Deterministic gates (passed)

```bash
uv run pytest tests/phase5/test_phase3_live_fixture_contract.py packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests -q
```

```
243 passed in ~60s
```

```bash
uv run pytest tests/agent_runtime/test_phase3_acceptance_artifacts.py -q
```

(Pass after acceptance test updates for the new runner interface.)

## Required to unblock

1. **OpenCode:** Run a server build that exposes the pinned `opencode-http-v1` profile fields on `/config`, set `OPENCODE_PHASE3_TOKEN`, rerun `bash benchmark/agent-runtime-phase3/run-opencode.sh` to terminal success with all declared artifacts.
2. **Cursor:** Export `CURSOR_API_KEY`, rerun `bash benchmark/agent-runtime-phase3/run-cursor.sh` to terminal success with all declared artifacts.

On success, replace this file with observed lock digest, artifact paths, and exit status `0` for each item.
