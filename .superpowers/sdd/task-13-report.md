# Task 13 Report — Audited Legacy-Root Supersede and Single-Use V6 Replacement

## Status

**DONE**

## What Was Implemented

### Core types and protocol (`assurance_agent/workflow/graph/supersede.py`)
- `SupersedeAction`, `SupersedeEligibility`, `SupersedeResult`, `SupersedeError`
- Descendant closure, subtree digest, deterministic `supersede_id` / `replacement_authorization_id`
- Typed eligibility (never parses exception text): root/latest/legacy-block/quiescence/params/v6 staging
- Subtree quiescence: live leases, open attempts, uncommitted supersteps/write-sets, pending publications, unacked effects, retry sidecars, concurrent children
- Staged v6 definition-request persistence for exact retry reload
- Prepared-fence recovery: commit when authoritative supersede event exists, else abort

### Events / fold / projection
- `GraphInvocationSupersededEvent` in `graph_events.py`
- Optional all-or-none `supersedes_invocation_id` + `replacement_authorization_id` on `GraphInvocationStartedEvent`
- Checkpoint fold: idempotent supersede replay; root + descendants project `stopped` / `superseded`; `GraphProjection.supersede_id` remains queryable while status renderers say `stopped`

### Runtime (`GraphRuntime.supersede`)
- Two durable steps: (1) `RootEffectFenceStore.prepare_terminal` → progression append → `commit_terminal`; (2) consume unused replacement auth and start/resume at most one v6 replacement root
- Ordinary active-root and `restart: once` guards preserved; only unused replacement authorization bypasses them
- Direct child/root resume returns stable superseded result; write-set/publication/effect paths respect the fence
- Import-checkpoint / generic `run` cannot consume replacement authority

### CLI
- `aa workflow supersede --change --invocation --action rerun-v6|stop --who --reason [--params]`
- Stable nonzero exit with typed `reason_code` on ineligible/conflicting requests
- `stop` rejects `--params` and produces no replacement authority

### Driver / status
- `DriverState.supersede_reason` + typed projectors
- `supersede_audit_id()` helper on status surface

## Verification

```text
uv run pytest -q \
  tests/unit/workflow/graph/test_supersede.py \
  tests/integration/test_cli_workflow_v2.py \
  tests/integration/test_graph_runtime_faults.py
→ 85 passed

uv run ruff check assurance_agent/workflow/graph/supersede.py \
  assurance_agent/workflow/graph/runtime.py \
  assurance_agent/commands/workflow_cmd.py \
  tests/unit/workflow/graph/test_supersede.py
→ All checks passed

uv run pyright
→ 0 errors, 0 warnings, 0 informations

runtime_commit_safety/v1 digest (pinned pre-Task-13, unchanged as fence caller):
29341c4ad68044d44532441c785c0a59697310f7bc8e4469b645b860767c2bd6
```

## Files Ready to Stage (not committed)

- `assurance_agent/workflow/graph/supersede.py` (new)
- `assurance_agent/workflow/core/graph_events.py`
- `assurance_agent/workflow/graph/models.py`
- `assurance_agent/workflow/graph/checkpoint.py`
- `assurance_agent/workflow/graph/runtime.py`
- `assurance_agent/workflow/graph/status.py`
- `assurance_agent/workflow/driver/driver_state.py`
- `assurance_agent/commands/workflow_cmd.py`
- `tests/unit/workflow/graph/test_supersede.py` (new)
- `tests/integration/test_cli_workflow_v2.py`
- `tests/integration/test_graph_runtime_faults.py`

## Notes

- Tip already included Task 12/14 commits (`1263175`); no wait required.
- Cursor-loop files left untouched (pre-existing dirty worktree noise).
- Commit step intentionally skipped per instruction.
