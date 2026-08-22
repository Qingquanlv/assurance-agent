# Phase 3 provider-live closeout

**Date:** 2026-08-22  
**Status:** BLOCKED  
**Worktree:** `pure-graph-engine-phase3-spec`

## Summary

Adapter credential and profile fixes landed; preflight now passes for both installed providers. Live runs reach `Engine.production` but fail during handler execute because the production worker receives no task activity port (`activity port is required`).

## OpenCode fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-opencode.sh`  
**Exit status:** `1`  
**Item:** `phase3-opencode-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Lock digest:** `91bda6fd87dc17ee2c9a010898331a0e44d8859b7c83833f59b080998699b9de`  
**Result dir:** `benchmark/agent-runtime-phase3/results/opencode-20260822-234204`

Preflight: health `1.18.4`, `/config` reachable (product shape, no `protocol_profile`), `/session` HTTP 200 without token; `OPENCODE_PHASE3_TOKEN` set to empty in-process.

**Blocking error:** `engine terminal status 'failed' != expected 'succeeded'` — handler execute raised `ValueError: activity port is required`.

## Cursor fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-cursor.sh`  
**Exit status:** `1`  
**Item:** `phase3-cursor-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Lock digest:** `fa5c518c831567d9d0fe4788543f362c125ec99fe35d8450f7053e0052481004`  
**Result dir:** `benchmark/agent-runtime-phase3/results/cursor-20260822-234235`

Preflight: pinned executable digest/version match; `CURSOR_API_KEY` loaded from macOS keychain service `cursor-access-token` (not printed).

**Blocking error:** `engine terminal status 'failed' != expected 'succeeded'` — handler execute raised `ValueError: activity port is required`.

## Deterministic gates (passed)

```bash
uv run pytest tests/phase5/test_phase3_live_fixture_contract.py packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/agent_runtime/test_phase3_acceptance_artifacts.py -q
```

```
260 passed
```

## Required to unblock

Wire task-activity preparation into the phase3 live fixture product/workflow so production worker execute calls receive a ledger-backed activity port before adapter handlers run.
