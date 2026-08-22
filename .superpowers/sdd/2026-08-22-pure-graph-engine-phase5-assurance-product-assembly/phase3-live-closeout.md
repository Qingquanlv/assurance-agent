# Phase 3 provider-live closeout

**Date:** 2026-08-22  
**Status:** BLOCKED  
**Worktree:** `pure-graph-engine-phase3-spec`

## Summary

Engine fix landed: bound adapter aliases (`fixture.binding.run` → `runtime.opencode.execute` / `runtime.cursor.execute`) now traverse the recoverable wave path, minting a non-None `activity_id` on production host execute calls. Production worker `_ParentActivityPort` now implements `snapshot` RPC. Preflight passes for both providers; live runs reach adapter handlers with a ledger-backed activity port but time out before producing `result.json`.

## OpenCode fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-opencode.sh`  
**Exit status:** `1`  
**Item:** `phase3-opencode-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Lock digest:** `26f93741b2df64eea6d01dc98d3a46048da7ca1c3e85e6cf9c9499c8b73d63c4`  
**Result dir:** `benchmark/agent-runtime-phase3/results/opencode-20260822-235336`

Preflight: health `1.18.4`, `/config` reachable (product shape, no `protocol_profile`), `/session` HTTP 200 without token.

**Blocking error:** `engine terminal status 'interrupted' != expected 'succeeded'` — task activity prepared (`6dee35fc…`) but scheduler cancelled with `reason=timeout` after ~120s (`max_seconds` limit); no terminal adapter outcome or workspace `result.json`.

## Cursor fixture

**Command:** `bash benchmark/agent-runtime-phase3/run-cursor.sh`  
**Exit status:** `1`  
**Item:** `phase3-cursor-live`  
**Adapter version:** `0.1.0`  
**Model:** `provider_default`  
**Lock digest:** `31e989c7c1c55066119acc871209adf90f72e3f59693269e2efb112f29928584`  
**Result dir:** `benchmark/agent-runtime-phase3/results/cursor-20260822-235540`

Preflight: pinned executable digest/version match; `CURSOR_API_KEY` loaded from macOS keychain service `cursor-access-token` (not printed).

**Blocking error:** `engine terminal status 'interrupted' != expected 'succeeded'` — task activity prepared (`916c15a1…`) but scheduler cancelled with `reason=timeout` after ~120s; no terminal adapter outcome or workspace `result.json`.

## Deterministic gates (passed)

```bash
uv run pytest packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_production_host.py packages/graph-engine/tests/runtime/test_production_host_faults.py -q
# 95 passed

uv run pytest tests/phase5/test_phase3_live_fixture_contract.py packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/agent_runtime/test_phase3_acceptance_artifacts.py -q
# 260 passed
```

## Required to unblock

Adapter handlers must complete within the fixture `max_seconds` (120) budget and write `result.json` to the attempt workspace, or the fixture timeout / observation horizon must be raised for live provider latency.
