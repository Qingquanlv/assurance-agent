# Phase 3 provider-live closeout

**Date:** 2026-08-23  
**Status:** DEFERRED — OpenCode passed; Cursor live parked by user (keep original CURSOR_API_KEY design)  
**Worktree:** `pure-graph-engine-phase3-spec`

Do not treat this as Task 9 acceptance. One of the two required live scripts is still exit 1.

## OpenCode fixture — passed

**Command:** `bash benchmark/agent-runtime-phase3/run-opencode.sh`  
**Exit status:** `0`  
**Item:** `phase3-opencode-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Lock digest:** `da3be2279db02e6899cceaee0715b33b2433de2e7ba8751ffeaba037800a0869`  
**Terminal status:** `succeeded`  
**Result dir:** `benchmark/agent-runtime-phase3/results/opencode-20260823-020800`

Declared artifacts:

| Path | sha256 |
|---|---|
| `engine/invocations/phase3-opencode-live/invocation.lock.json` | `da3be2279db02e6899cceaee0715b33b2433de2e7ba8751ffeaba037800a0869` |
| `engine/invocations/phase3-opencode-live/checkpoint.json` | `2062bbfdd8369fbd9872396f6e6824b7394fefbc53b006a5c04ce950985c962e` |
| `engine/invocations/phase3-opencode-live/workspace/HEAD.json` | `fb71726250255193fac30750a088459c3f64110c28da202cfc1b762892da9771` |

Published workspace output is `workspace/trees/8e5d0b20940bfee2c33520caf82aa2f37998b31626a846de0a78a041aae3579e/result.json` with digest `86f85b3898c11dad47aba8ee2bf20e913ed08dccde985a1c6c5b3fefc66d6c79`.

## Cursor fixture — blocked

**Command:** `bash benchmark/agent-runtime-phase3/run-cursor.sh`  
**Exit status:** `1`  
**Item:** `phase3-cursor-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Result dir:** `benchmark/agent-runtime-phase3/results/cursor-20260823-020841`

Preflight: pinned executable digest/version match; `CURSOR_API_KEY` loaded from macOS keychain service `cursor-access-token` (value not printed, not invented).

Pinned `cursor-agent` exited 1 in 1.32s. Stderr (ANSI warning, decoded from the process receipt): the provided API key is invalid and was loaded from `CURSOR_API_KEY`. Stdout was empty, so the stream parser raised `exactly one system init is required`. `CursorDispatchIncomplete` then crashed the production worker instead of returning a typed `TaskOutcome`.

This is an external credential block. No substitute key was used.

## Fixes that unblocked OpenCode (this session)

- `203a688` — ignore out-of-group workspace holders at quiescence
- `3fa7105` — treat completed write-tool JSON as the structured result
- `1e01b44` — register the Phase 3 live fixture result schema
- `5afbbba` — read the fixture artifact from the published HEAD tree

## Required to close Task 9

User 2026-08-23: keep the original confined `CURSOR_API_KEY` design; park Cursor live. Do not switch the adapter to inherit `cursor-agent login`. Re-run `bash benchmark/agent-runtime-phase3/run-cursor.sh` later with a real CLI API key in `CURSOR_API_KEY` (not the IDE keychain `cursor-access-token`). Task 9 is not accepted. Task 10 proceeds with this residual.
