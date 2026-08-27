# OpenCode provider-live benchmark

The Phase 5 live item drives the **real SUT**. The Assurance result is the
change directory, not a harness result tree.

```text
<sut>/qa/changes/<change-id>/
```

`results/<run>/` holds only harness diagnostics: logs, timing, isolated
wheels/venv, and `evidence.json`. It is not an alternate result source and
must not contain `project/`, `export/`, `workspace/`, `HEAD.json`, a latest
pointer, or a result registry.

## Run identity

A run derives a unique deterministic change ID from item ID + stamp + nonce:

```text
BENCH-<item-id>-<stamp>-<nonce>
```

The driver invokes:

```text
aa start|run|status --project-dir <sut> --change <id>
aa export --project-dir <sut> --change <id>   # only after achieved
```

Export is called once after the change is achieved. Failure leaves original
SUT tests unchanged and does not publish.

## Evidence fields

Live evidence records `sut_root`, `change_id`, `change_root`, terminal
status, publish receipt, provider session/process reference, and logs.
Those fields are diagnostics for the harness, not a second result tree.

## Routing

Every OpenCode prepare ID is locked to `openai/gpt-5.6-terra` / `max`.
Product-installed locked OpenCode profiles are preflighted; the live run
does not install OpenCode configuration into the SUT.

## Commands

```bash
benchmark/assurance-product-phase5/run-opencode.sh
benchmark/assurance-product-phase5/run-cursor.sh
```

## Task 17 observed live attempt (2026-08-26)

Not a workflow acceptance.

- Item: `opencode-ret-dept-management`
- Change ID: `BENCH-opencode-ret-dept-management-20260826-061801-680125c3`
- Resolved SUT: `/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin`
  (worktree SUT copy has no `app/` / `web/`)
- Provider: `openai/gpt-5.6-terra` / `max` at `http://127.0.0.1:4096` (`healthy`, `1.18.4`)
- Outcome: `blocked` before `aa start`
- Session: none
- Publish receipt: none
- Change directory: never created
- Notes: resolved OpenCode config missing `assurance-v1-*` agents and
  `assurance_boundary_v1`. Env tokens were unset; none were substituted.
- Harness diagnostics:
  `benchmark/assurance-product-phase5/results/opencode-20260826-061801-680125c3/evidence.json`

## Task 3 admission attempt (2026-08-26)

Not a workflow acceptance.

- The Task 17 diagnostic above remains the preceding failed live attempt.
- The fresh pinned command was requested with its existing 28,800-second horizon:
  `benchmark/assurance-product-phase5/run-opencode.sh`.
- The execution-approval layer rejected the invocation before process start. No
  provider request, session, change directory, receipt, export, or business-step
  evidence was produced.
- This is an execution authorization blocker, not an OpenCode quota/auth/network
  outcome and not proof of a completed Phase 3 or Phase 5 Task 24 workflow.
