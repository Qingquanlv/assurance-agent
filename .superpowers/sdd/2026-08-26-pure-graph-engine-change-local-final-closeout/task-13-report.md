# Task 13 Report — Run the Final OpenCode Provider-live Benchmark

## Status: BLOCKED

A fresh post-cutover OpenCode live run authenticated the pinned provider
and completed intake through case-review. Controller ruling treated the
`human-review` stop as operator resume, not a product defect. The same
change was resumed with `approve` and generation started, then isolated
`aa run` returned `activity_recovery` twelve times with no new events or
provider dispatch. The change is not `achieved`. Publish was not invoked.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Committed HEAD: `0444ab389320ee91e4460bb4217bc33f8d36b281`
- Pre-run dirty `git status --short`: 81 lines, SHA-256
  `45f7927daa69f9871420bccb1221fa980ede24a9b2a27e00fe3982de6a2865fb`
- The dirty tree was left in place. Only this task's files are staged.
- Item: `opencode-ret-dept-management`
- Product: `assurance-opencode`
- Provider binding: `http://127.0.0.1:4096` / `opencode-http-v1`
- Locked route: `openai/gpt-5.6-terra` / `max`
- Manifest SHA-256:
  `0bfc82b577eddc952df28c5e4db5aa3505e78c699a40530f9baa3cc66d027c85`
- OpenCode `/global/health` before start: HTTP 200, `healthy=true`,
  version `1.18.4`
- Secret env `AA_NEXT_OPENCODE_TOKEN` was unset; no ambient model/endpoint
  overrides were present.

### Already-installed RECORD digests (no new smoke)

These hash the worktree `.venv` `RECORD` files that were already installed
before this run. They are not the isolated live wheels.

| distribution | RECORD SHA-256 |
|---|---|
| graph-engine | `8767bc8059f062966b5283ee66a5790a045b5731a06133858cea2713979e7b6b` |
| agent-runtime-contracts | `0208afb7d82ff48ce327ca1fe5048202d9bfad27cf6d566af319de130e79de7f` |
| assurance-intake | `313e17675fcbaba0f8053704fd3cbfcab07039c7783e9d96741c7dcf0605f711` |
| assurance-generation | `18944e670ef4a62f0308e1c00b2566bc74bcb8103188948c496c0ff8ab5ae1db` |
| assurance-execution | `2857650db8c0fe678332e2fb299ee2eb42630387163e3c0bca19d4d9d1c03814` |
| assurance-healing | `0cee2772f7c580cb078a9693b78b5708e50f51080d86d8e8be2a66ac7e261dcb` |
| assurance-quality | `ce556fa2a46cd83a50f5bef65ff9c390282f0ca70e327822b0510846b7212c9a` |
| assurance-improvement | `fa87e409c1f529214a31673abfbe9bb2de4347ac2f095c88bf79e78105f92a93` |
| assurance-product | `1f2952732ac26d0f63ba1f6e57a60539b1f77d4077d59fe52e692068f8a3855b` |
| agent-runtime-opencode | `32edf88b15981f7c41456546d334ae889495943ab97179bdb8c40d60600b9e2e` |
| agent-runtime-cursor | `4a4b189d44be2971d767e27f809978340bf1fcefb51040931c72faa118291a3e` |

## What I ran

Exact command from the worktree root:

```bash
bash benchmark/assurance-product/run-opencode.sh
```

The committed product console is `aa`. The harness still looked up
`venv/bin` under the retired name, so the isolated install could not
start. One authorized path fix changed that lookup to `aa`. No product,
capability, or adapter code was edited.

Fresh invocation (not reused):

- Result dir: `benchmark/assurance-product/results/opencode-20260827-054218-9a2df6f6/`
- Change id: `BENCH-opencode-ret-dept-management-20260827-054218-9a2df6f6`
- SUT: `/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin`
- Started: `2026-08-27T05:42:18Z`
- Ended: `2026-08-27T05:50:02Z`
- Process exit: `30`

Harness `evidence.json` SHA-256:
`7405dc84ae21ca65ad885e64ba3870af0488f8b7375f558a446dbbc62ee005a2`

Isolated wheels actually used by this run (SHA-256 of each `.whl`):

| distribution | wheel SHA-256 |
|---|---|
| graph-engine | `8c838caf3f692e2670d84ebdef99c323ecec5bf054a78c0959d73f871f389b26` |
| agent-runtime-contracts | `15822077129c7a9e36b2dfb16cd6945451aa88c52cd45f8c0420da5966b5531a` |
| assurance-intake | `d0f2ad29bd09a265501e6ef42150e5ccf0960db8176b084314c9c269ff667818` |
| assurance-generation | `cfb81d55392e9599f187f602dcbb09ee70ed069b481514b4a4c75b2bfff63c2a` |
| assurance-execution | `df389c24726ef53dd1f2a4a1eff401bc74fd59094927e03a73df55dd4866df52` |
| assurance-healing | `cd5a034861732f2141fb1ead16499e5bb738aa2fe35815175e81bff981244087` |
| assurance-quality | `2863fe0f852bf24a6c4bdd01ae65daecf14580d68b198c3a8afc695fd704a77a` |
| assurance-improvement | `07b406ad724ae8c0e96c28e6ba0edc5aaf8e309776a5165f6fb3702fefa666c4` |
| assurance-product | `5d85e3c4fee6b0d19a5fb41efedfa55879ff414fb8526fed3d377bbc39e23522` |
| agent-runtime-opencode | `09b4cc7490932a9eb5fc5fd9860d2738e844ae88fc700bd549986d6aaaa58916` |
| agent-runtime-cursor | `3c23e3e5d004371493f2564b4ddaaa8a9aa3f6824118db62ac9220d16a2c89b4` |

## Terminal business projection

Observed `status.json` after the harness finished:

- Invocation status: `interrupted`
- Change state: `interrupted`
- Publication: `not_ready`
- Pending interrupt: `human-review` / `needs_human_review` / `approve|reject`
- Succeeded logical finalizers: `intake`, `explore`, `case-design`,
  `case-review`
- Interrupted node: `human-review`
- Adapter activities: 4 opaque ids, first
  `7dbc9308a008288fa351d9df867ba78e0390630d32c1cc878b751dafee8d545a`

Promoted artifacts under the fresh change (no prior change reused):

- `explore/exploration.json`
- `cases/system/dept/case.yaml`
- `proposal.md`
- `trace/minimum-coverage-matrix.json`
- `review/case-review.json`

Case-review decision (redacted): `needs_human_review`, `risk_level=high`,
`auto_fix_allowed=false`. Findings `CR-SOURCE-001` and `CR-MRC-001` say
department source and declared constraint keys were not independently
verifiable from the projected files; reviewed source was only `run.py`.
This is a deterministic graph interrupt, not quota or network failure.

## Export / publish

The harness never reached `aa export` because the change is not
`achieved`. Task 12 already locks `publish_achieved` rejection of
non-achieved changes. No publish receipt exists, and none is invented.
Idempotent export and optional archive were not exercised on this change.

## Secrets and legacy

- Harness `evidence.json` and `run.log` have no credential-looking text.
- Canonical `final-opencode.json` omits session text, provider text, and
  Tree/HEAD/export vocabulary.
- Isolated CLI used was `aa`. Result artifacts were not staged.

The leftover untracked
`benchmark/assurance-product/tests/validate_live_run.py` was used only as
a fail-closed reference. It correctly rejects this record because
`admission_status` is not `complete` and the workflow is not `achieved`.
That validator was not staged.

## Canonical evidence

Wrote
`.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/final-opencode.json`
bound to source commit `0444ab389320ee91e4460bb4217bc33f8d36b281` and the
fresh harness evidence digest above.

## Continuation (same change)

Controller ruling: the first stop was operator resume, not a product
defect. No new `run-opencode.sh` and no new change id.

Isolated CLI (not worktree `.venv`):
`benchmark/assurance-product/results/opencode-20260827-054218-9a2df6f6/venv/bin/aa`

Reconstructed flags from that result dir:

- `--product assurance-opencode`
- `--binding-dist assurance-product-bindings-10b8baed00795536`
- `--binding-entrypoint deployment`
- `--binding-declaration assurance_product_bindings_10b8baed00795536/assurance-deployment-plugin.json`
- `--config-tree` that result dir's `config-tree`
- the same OpenCode handle-to-environment mapping the original run used
- `--project-dir` `/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin`
- `--change` / `--invocation-id` `BENCH-opencode-ret-dept-management-20260827-054218-9a2df6f6`

Resume attempts:

1. `05:59:16Z` — exit 40, `environment secret source is unset`. The original
   harness had bound an empty env value. No ledger change.
2. `06:01:05Z` — empty env value restored to match the original handle
   binding. CLI printed `invocation has no pending interrupt` (engine
   resume requires status `running`). The ledger nevertheless has
   `interrupt_resumed` action `approve` with the required reason, and
   `human-review` is `succeeded`.

Generation then started: four family `plan.prepare` tasks promoted, four
`execute` nodes activated, and one `task_activity_prepared`. Isolated
`aa run` then returned this JSON twelve times:

```json
{"actions":[],"status":"interrupted","terminal_reason":"activity_recovery"}
```

Event count stayed at 291. No `task_activity_dispatch_started` for the
new generation activity. No OpenCode session created after resume.
Adapter evidence remains the original four intake activities. The loop
was stopped at `06:14:30Z` rather than waiting out eight hours with no
progress.

Fresh isolated `aa status` after stop: invocation `running`, change
`running`, publication `not_ready`, `human-review` succeeded, generation
executes still running, no pending interrupt.

Export was not invoked. No publish receipt. No archive.

## Files changed

Created then updated:

- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/final-opencode.json`
- this Task 13 report

First commit also included `benchmark/assurance-product/run_item.py`
(isolated console path `aa` only). Continuation commit stages only the
two SDD files.

Not staged: leftover Phase 4/5 hunks, `benchmark/**/results/`, `tmp/`,
provider sessions, frozen Phase 5 admission, Task 3 diagnostics, or
`validate_live_run.py`.

## Self-review

- The live command was a new invocation. Prior change ids
  `144256-0fbe82c2`, `141439-41852bc4`, `132110-a9d717c6`,
  `BENCH-opencode-ret-dept-management-*` from earlier days, and
  `RET-dept-management-20260818-*` were not reused.
- Continuation used the same change and isolated CLI. Achieved, publish,
  and archive are not claimed.
- Operator resume of `human-review`/`approve` was performed. The harness
  was not rewritten to auto-resume.
- No product-fix campaign was started for the stuck generation dispatch.
- Cursor live was not run.

## Issues or concerns

- After operator approve, generation execute prepares an activity and
  every later `aa run` yields `activity_recovery` without provider
  dispatch. That is a deterministic post-resume stall, not quota/network.
- Isolated wheels were built from the dirty worktree at source commit
  `0444ab389320ee91e4460bb4217bc33f8d36b281`. Evidence commit `f43cc40`
  only recorded the first stop.
- Case-review independently verified only `run.py`.
- Task 3 Phase 5 live admission remains incomplete and was not rewritten.

## Correction — prepared-undispatched same-attempt dispatch

The live stall after `human-review`/`approve` is a scheduler recovery seam
bug, not a provider or harness defect. A generation `execute` attempt can
be left `running` with activity `prepared` and no dispatch fingerprint.
`recover_live_activities` does not add that attempt to
`_same_attempt_execute` when reconcile is indeterminate, so
`resume_running` skipped it and `run_until_blocked` returned
`activity_recovery` with no new events. Status remains `BLOCKED`; this
change is not rewritten as `achieved`.

TDD on `codex/pure-graph-engine-phase3-spec` from HEAD `3492913`:

### RED

Added
`test_prepared_undispatched_resume_dispatches_same_attempt` and
`test_already_dispatched_live_activity_still_yields_activity_recovery`
in `packages/graph-engine/tests/runtime/test_activity_recovery.py`.

Command:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py::test_prepared_undispatched_resume_dispatches_same_attempt packages/graph-engine/tests/runtime/test_activity_recovery.py::test_already_dispatched_live_activity_still_yields_activity_recovery -q
```

Result: 1 failed, 1 passed. Prepared+undispatched
`run_until_blocked` returned `terminal_reason='activity_recovery'`
before any host `execute`. Already-dispatched stayed
`activity_recovery`.

### GREEN

`resume_running` now treats a non-expired prepared activity with no
dispatch fingerprint as same-attempt execute: it opens the existing
workspace and passes the existing `activity_id` into `_execute`. It
does not treat that state as "no live activity" and `begin()` a new
workspace. Expired non-adopted live activities still skip instead of
raising `LeaseUnavailableError`.

Commands:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py -q
uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py::test_expired_non_adopted_recovery_does_not_conflict_on_resume -q
```

Result: 20 passed; expired-non-adopted 3 passed. Ruff check/format
clean on the two Python files.

No OpenCode live rerun. No `run-opencode.sh`. Export/CLI/product
capabilities unchanged. Dirty `production_host.py` /
`production_worker.py` hunks were not staged.

### RED (cancel-requested prepared)

Independent review of `c0b6393`: a `cancel_requested` prepared-undispatched
activity with a valid lease was dispatched by `resume_running` instead of
staying at `activity_recovery`.

Added `test_cancel_requested_prepared_stays_activity_recovery`. Command:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py::test_cancel_requested_prepared_stays_activity_recovery -q
```

Result: 1 failed. `run_until_blocked` returned `succeeded` and host
`execute` ran.

### GREEN (cancel-requested prepared)

`resume_running` now also `continue`s when `activity.cancel_requested`,
so recover remains the only cancel/reconcile driver. No provider
dispatch for a cancel-requested prepared activity.

Command:

```bash
uv run pytest packages/graph-engine/tests/runtime/test_activity_recovery.py -q
```

Result: 21 passed, including unchanged
`test_expired_non_adopted_recovery_does_not_conflict_on_resume`.
Ruff check/format clean. Status remains `BLOCKED`; not rewritten as
`achieved`. No live rerun. Dirty `production_host.py` hunks not staged.
