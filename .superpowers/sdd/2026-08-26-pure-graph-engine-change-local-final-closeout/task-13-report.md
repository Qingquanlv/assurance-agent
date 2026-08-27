# Task 13 Report — Run the Final OpenCode Provider-live Benchmark

## Status: BLOCKED

A fresh post-cutover OpenCode live run at HEAD `0f034de` authenticated
the pinned provider and completed intake through case-review. Operator
resume approved `human-review`. The prepared-undispatched recovery fix
held: generation execute prepared and dispatched. The same isolated
`aa resume` then reached `generation-api-plan` execute and terminated
`failed` with `task_failed:execute:invalid_output`. The change is not
`achieved`. Publish was not invoked. No engine-fix campaign was started.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Committed HEAD: `0f034de8e3c59c43a64b4949f78335d1e80419b5`
- Recovery-fix ancestors: `c0b6393` (prepared undispatched dispatch),
  `0f034de` (cancel-requested skip). Review clean.
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
  overrides were present. Resume bound the same empty env source the
  harness used (`TOKEN_SET=yes`, `TOKEN_LEN=0`).

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

Fresh invocation (not reused; not `054218-9a2df6f6`):

- Result dir: `benchmark/assurance-product/results/opencode-20260827-063450-3e30e331/`
- Change id: `BENCH-opencode-ret-dept-management-20260827-063450-3e30e331`
- SUT: `/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin`
- Started: `2026-08-27T06:34:50Z`
- Harness ended: `2026-08-27T06:45:45Z`
- Process exit: `30`

Harness `evidence.json` SHA-256:
`59be4a82cd2579122fb76c45e240e441cc4674a25bb499ab25ed9d30c811e177`

Isolated wheels actually used by this run (SHA-256 of each `.whl`):

| distribution | wheel SHA-256 |
|---|---|
| graph-engine | `01cad6c56f7499a5b215c1bcf1192f67f459c2c92594929da2df492743c7b538` |
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

`graph-engine` is the only isolated wheel that changed versus the
`054218-9a2df6f6` run, as expected after `c0b6393` / `0f034de`.

## Terminal business projection after harness stop

Observed `status.json` after `run-opencode.sh` finished:

- Invocation status: `interrupted`
- Change state: `interrupted`
- Publication: `not_ready`
- Pending interrupt: `human-review` / `needs_human_review` / `approve|reject`
- Succeeded logical finalizers: `intake`, `explore`, `case-design`,
  `case-review`
- Interrupted node: `human-review`
- Adapter activities: 4 opaque ids, first
  `c386eec41ab04ddbcc9c021fe0dc26ab6e3b78397c71cb5cef3824fcbe76a1aa`

Promoted artifacts under the fresh change (no prior change reused):

- `explore/exploration.json`
- `cases/system/dept/case.yaml`
- `proposal.md`
- `trace/minimum-coverage-matrix.json`
- `review/case-review.json`

Case-review decision (redacted): `needs_human_review`, `risk_level=high`,
`auto_fix_allowed=false`. Finding `CR-PRODUCT-SOURCE-UNAVAILABLE`.
This is the designed graph interrupt, not quota or network failure.

## Continuation (same new change)

Isolated CLI (not worktree `.venv`):
`benchmark/assurance-product/results/opencode-20260827-063450-3e30e331/venv/bin/aa`

Flags reconstructed from that result dir:

- `--product assurance-opencode`
- `--binding-dist assurance-product-bindings-10b8baed00795536`
- `--binding-entrypoint deployment`
- `--binding-declaration assurance_product_bindings_10b8baed00795536/assurance-deployment-plugin.json`
- `--config-tree` that result dir's `config-tree`
- `--secret opencode.token=env:AA_NEXT_OPENCODE_TOKEN` with the empty
  env value the harness bound
- `--project-dir` `/Users/lvqingquan/agent/assurance-agent/benchmark/vue-fastapi-admin`
- `--change` / `--invocation-id` `BENCH-opencode-ret-dept-management-20260827-063450-3e30e331`

`aa resume --json --action approve --reason "closeout operator approval after case-review requested human review"`
started `2026-08-27T06:47:44Z` and itself called `run_until_blocked`.

Ledger after resume (exact activity state):

- seq 180: `interrupt_resumed` action `approve`
- seq 187: `generation` activated
- four family `plan.prepare` tasks promoted
- four `execute` nodes activated
- seq 291: `task_activity_prepared` activity
  `c583ebe382c80e1b92ae33f90ea4504aec42741d971b5accc48dbc80136169b3`
- seq 292: `task_activity_dispatch_started` for that activity
  (dispatch fingerprint present; not the old prepared+undispatched stall)
- later generation executes also prepared and dispatched:
  `2957adc0c8731b29…`, `6725ebca7e8a8745…`

Promoted after resume (in addition to intake artifacts):

- `plans/e2e-plan.md`, `plans/e2e-codegen-plan.md`,
  `plans/e2e-codegen-mapping.json`, `plans/e2e-test-data-plan.md`,
  `plans/m4-review-summary.md`
- `plans/performance-plan.md`, `plans/performance-codegen-plan.md`,
  `plans/performance-codegen-mapping.json`,
  `plans/performance-review-summary.md`

Resume JSON (exit 40) at `2026-08-27T07:01:35Z`:

```json
{"action":"approve","invocation_id":"BENCH-opencode-ret-dept-management-20260827-063450-3e30e331","status":"failed","terminal_reason":"task_failed:execute:invalid_output"}
```

Failing node: `generation-api-plan` / `execute` (graph instance
`9075057953bb…`). Activity `6725ebca7e8a87457b25d803c94c1367c03fc059f3938c0b38760665c89bfd02`
terminal outcome (redacted):

```json
{"failure":{"kind":"invalid_output","message":"$: missing required properties ['case_ids', 'required_capabilities', 'coverage', 'output_files']","retryable":false},"output":null,"status":"failed"}
```

Ledger closed at seq 330 with `invocation_finished` status `failed`.
Sibling e2e/fuzz/performance plan graphs were still projected `running`
when the root failed. Isolated `aa status` at `07:04:09Z` confirmed
invocation `failed`, change `failed`, publication `not_ready`, no
pending interrupt.

A further `aa run` loop was not started: the invocation is finished
failed. That is a new deterministic blocker, not `activity_recovery`.

## Export / publish

Export was not invoked. The change is not `achieved`. Task 12 already
locks `publish_achieved` rejection of non-achieved changes. No publish
receipt exists, and none is invented. Idempotent export and optional
archive were not exercised on this change.

## Secrets and legacy

- Harness `evidence.json` and `run.log` have no credential-looking text.
- Canonical `final-opencode.json` omits session text, provider text, and
  Tree/HEAD/export vocabulary.
- Isolated CLI used was `aa`. Result artifacts were not staged.

## Canonical evidence

Wrote
`.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/final-opencode.json`
bound to source commit `0f034de8e3c59c43a64b4949f78335d1e80419b5` and the
fresh harness evidence digest above.

## Files changed

Updated:

- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/final-opencode.json`
- this Task 13 report

Commit stages only those two SDD files (`git add -f`).

Not staged: leftover Phase 4/5 hunks, `benchmark/**/results/`, `tmp/`,
provider sessions, frozen Phase 5 admission, Task 3 diagnostics,
`validate_live_run.py`, or dirty `production_host.py` /
`production_worker.py` hunks.

## Self-review

- The live command was a new invocation. Prior change ids
  `144256-0fbe82c2`, `141439-41852bc4`, `132110-a9d717c6`,
  `054218-9a2df6f6`, `BENCH-opencode-ret-dept-management-*` from earlier
  days, and `RET-dept-management-20260818-*` were not reused.
- Achieved, publish, and archive are not claimed.
- Operator resume of `human-review`/`approve` was performed. The harness
  was not rewritten to auto-resume.
- The prepared-undispatched stall did not recur after `c0b6393` /
  `0f034de`. No second engine-fix campaign was started for
  `invalid_output`.
- Cursor live was not run.
- Phase 5 admission is not rewritten as complete.

## Issues or concerns

- After operator approve, generation execute dispatched (fix held) and
  then `generation-api-plan` execute failed closed on a structured
  result missing `case_ids`, `required_capabilities`, `coverage`, and
  `output_files`. That is a deterministic product/result-contract
  failure, not quota/network and not `activity_recovery`.
- Isolated wheels were built from the dirty worktree at source commit
  `0f034de8e3c59c43a64b4949f78335d1e80419b5`.
- Case-review independently reported product source unavailable.
- Task 3 Phase 5 live admission remains incomplete and was not rewritten.
