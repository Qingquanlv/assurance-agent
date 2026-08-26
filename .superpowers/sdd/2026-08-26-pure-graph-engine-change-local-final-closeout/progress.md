# SDD ledger — plan: docs/superpowers/plans/2026-08-26-pure-graph-engine-change-local-final-closeout.md

## Setup

- Worktree: `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`
- Branch: `codex/pure-graph-engine-phase3-spec`
- Linked-worktree proof: git dir `/Users/lvqingquan/agent/assurance-agent/.git/worktrees/pure-graph-engine-phase3-spec`; common dir `/Users/lvqingquan/agent/assurance-agent/.git`; no superproject.
- Pre-existing state: the worktree contains a large dirty Phase 4/5 implementation, benchmark evidence, and untracked results. Those files are frozen as user/pre-existing work and must not be staged by Task 1.
- Plan-owned workspace: `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/`.

## Pre-flight plan scan

### Per-task internal consistency

| Task | Tests against implementation/files | Finding |
|---|---|---|
| 1 | Admission checker authenticates the opaque Change-local acceptance and freezes residuals | Blocked before RED: upstream acceptance says `not accepted`; plan artifact directory also uses a stale `remaining-phases-final-closeout` name instead of the basename-derived SDD workspace. |
| 2 | Behavioral tests reconcile six Phase 2 tail commits without block cherry-pick | Internally consistent; depends on Task 1 admission. |
| 3 | OpenCode live validator consumes accepted Change-local result format | Internally consistent; cannot start until Task 1 admission. |
| 4 | Phase 5 security/fault gates consume Task 3 evidence and existing product/runtime tests | Internally consistent; Cursor is unit/package-only. |
| 5 | Handoff consumes Tasks 3–4 and records Cursor live deferral | Internally consistent; must not mark Phase 5 Task 25 complete. |
| 6 | One-shot legacy audit/cleanup consumes the handoff | Internally consistent after excluding new Change-local state; destructive execution remains a separate approval-sensitive step. |
| 7 | Product takes sole `aa` ownership while legacy packages still exist | Internally consistent; Tasks 8–9 consume the temporary workspace state. |
| 8 | Deletes `assurance_agent` only after replacement evidence | Internally consistent; destructive source deletion is gated by Tasks 1–7. |
| 9 | Deletes `assurance-kernel` after ownership transfer | Internally consistent; destructive source deletion is gated by Task 8. |
| 10 | No-legacy and final wheel smoke run after both deletions | Internally consistent; keeps Cursor non-live install matrix only. |
| 11 | Renames final tests/benchmark and removes legacy comparison | Internally consistent; consumes final Change-local benchmark contract without reimplementing it. |
| 12 | Documentation and black-box export acceptance consume existing implementation | Internally consistent; no export implementation files are listed. |
| 13 | Final OpenCode live uses post-cutover source and a fresh session | Internally consistent; provider failures remain external blockers. |
| 14 | Final evidence consumes all earlier artifacts and allows approved Cursor deferral | Internally consistent; cannot resolve Task 1 upstream blocker itself. |

### Shared files and interfaces

| Producer ↔ consumer | Shared surface | Finding |
|---|---|---|
| 1 ↔ 5 | `residual-disposition.json`, Change-local admission digest | Task 5 may advance only records admitted by Task 1. |
| 1 ↔ 8 | `residual-disposition.json`, Phase 4 inventory admission | Task 8 cannot delete before Task 1 authenticates the inventory source. |
| 1 ↔ 9 | `residual-disposition.json`, legacy ownership evidence | Task 9 consumes but cannot weaken Task 1 dispositions. |
| 1 ↔ 14 | admission and final residual ledger | Task 14 must authenticate the same admitted source and approved deferrals. |
| 3 ↔ 4 | OpenCode provider evidence | Task 4 repository gate requires a real Task 3 admission. |
| 3 ↔ 5 | Phase 5 Task 24 evidence | Task 5 handoff binds the exact Task 3 digest. |
| 3 ↔ 11 | live manifest and benchmark paths | Task 11 renames the accepted harness without changing its result contract. |
| 4 ↔ 5 | Phase 5 security/fault gate report | Task 5 rejects incomplete or mutable gate evidence. |
| 4 ↔ 10 | `scripts/assurance_product_wheel_smoke_test.sh` | Task 10 finalizes the smoke only after Task 4 proves current behavior. |
| 5 ↔ 6 | Phase 5 handoff project roots and evidence | Task 6 scans only the roots frozen by Task 5. |
| 5 ↔ 14 | handoff digest | Final evidence must bind the exact handoff. |
| 7 ↔ 8 | `pyproject.toml`, `uv.lock`, `.importlinter` | Task 8 removes legacy agent entries after Task 7 transfers command ownership. |
| 7 ↔ 9 | `pyproject.toml`, `uv.lock`, `.importlinter` | Task 9 removes the remaining kernel entries without restoring root package ownership. |
| 8 ↔ 9 | residual ledger and workspace metadata | Task 9 starts only after Task 8 replacement tests and deletion proof pass. |
| 8 ↔ 10 | deleted agent package evidence | Task 10 converts Task 8 absence into a repository/wheel gate. |
| 9 ↔ 10 | deleted kernel package evidence | Task 10 converts Task 9 absence into a repository/wheel gate. |
| 10 ↔ 11 | CI and final wheel/package surface | Task 11 updates renamed test/benchmark paths without weakening no-legacy checks. |
| 10 ↔ 12 | no-legacy documentation gate | Task 12 current docs must pass Task 10's repository policy. |
| 11 ↔ 12 | final benchmark/docs paths | Task 12 documents only the paths produced by Task 11. |
| 11 ↔ 13 | final OpenCode script and manifest | Task 13 executes the exact Task 11 final harness. |
| 12 ↔ 13 | export acceptance | Task 13 live evidence must satisfy Task 12's black-box contract. |
| 13 ↔ 14 | final OpenCode evidence | Task 14 authenticates the final live source and evidence digest. |

## Rulings

- Ruling: treat `docs/superpowers/plans/2026-08-26-change-local-assurance-workspace.md` as an opaque prerequisite and never rewrite its `not accepted` verdict; on 2026-08-26 the user explicitly authorized the enumerated gaps to be deferred so this plan may admit the baseline as `accepted_with_waivers` — this preserves truthful evidence while unblocking the requested closeout — cost if wrong: a waived Change-local defect may surface in Tasks 3、4、12、13 or 14 and require returning to the separate plan.
- Ruling: use the basename-derived SDD workspace `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/`; the `remaining-phases-final-closeout` artifact paths inside the plan are a naming defect and must be corrected before Task 1 dispatch — the SDD skill requires a single plan-owned workspace — cost if wrong: task briefs/reports would split across two recovery ledgers.
- Resolution: corrected every plan-owned artifact path to the basename-derived workspace before any dispatch.
- Ruling: preserve every pre-existing dirty file and untracked benchmark result; no cleaning, resetting, broad staging, or baseline repair is authorized under Task 1 — cost if wrong: later review ranges may accidentally attribute user work to this plan.

## Task status

- Task 1: admission evidence and checker implemented; focused RED/GREEN evidence is recorded below.
- Blocking evidence: `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/acceptance.md` says `Verdict: not accepted`; `.superpowers/sdd/2026-08-26-change-local-assurance-workspace/task-17-report.md` says `Status: PARTIAL`.
- Non-external failures recorded by the prerequisite: capability/Phase 5 failures, incomplete full repository gates, and incomplete wheel smokes.
- Live evidence recorded by the prerequisite: OpenCode workflow did not start; no change directory, provider session, publish receipt, or export evidence was produced.
- Skipped prerequisite surface: OpenChamber verification was skipped.
- The amended interface forbids emitting `complete` for the authenticated upstream `not accepted` verdict.

## Task 1 execution baseline and verification

- Baseline committed HEAD: `e7129433fd66aeb05ddc843030703eb5cb3ae5ef`.
- Baseline `git status --short`: 79 lines, SHA-256
  `94660d908c1cb7ef4d90283259c46eb959deebf912ff69083aac143b344742a7`.
  It is pre-existing user/other-work state and remains unstaged.
- Historical Change-local digests: plan
  `8f7f94fb0375840606a83838f4f3a17f13ae352794767467f1e8373b2deb7c8c`,
  spec `ba0ba1a2a026ab39f771fc0337b6944f5ade16b21031f4375c5b07e07c99e9be`,
  acceptance `f33bb3059ba647a55d42dc5754d83eb05040d838f3b533c5302e13592787c804`.
- Upstream source commit: `8d8d1ba0a755398ae1edccd6c55cff986c9e6729`, an ancestor of the baseline HEAD.
- RED: `uv run pytest tests/phase6/test_remaining_phase_admission.py -q` failed
  with the expected missing `admission.json` and `residual-disposition.json` artifacts.
- GREEN: the same focused test command passed `4 passed`; the checker printed
  `remaining-phase admission accepted_with_waivers`.
- Waivers approved on 2026-08-26 carry repository/type/package gates to Tasks
  4/14, OpenCode live to Tasks 3/13, export/idempotency proof to Tasks 12/13,
  dirty-tree closeout to Tasks 4/14, and stale acceptance refresh to Task 14.
  OpenChamber direct discovery plus graph inventory, unused CLI flag, and skill
  wording are `deferred_out_of_scope`.
- Task 1: fix round 1/5 (3 addressed, 0 open — duplicate-waiver cardinality,
  fail-closed P1/P2 recognition, and negative admission coverage; commits
  `6d32686..6c4a6d7`).
- Task 1: complete (commits `e712943..6c4a6d7`, review clean).
- Task 2: Ruling: historical Toy A effect business behavior no longer exists in
  the pure graph-engine example, so the retained crash invariant is authenticated
  at the product-neutral durable-effect seam and paired with the isolated
  graph-engine wheel smoke; do not restore Toy A business effects merely to
  reproduce the old smoke script — cost if wrong: an isolated installed Toy A
  process could expose a recovery integration defect not covered by the generic
  effect test and current smoke.
- Task 2: minor (deferred): runtime and composition currently duplicate the
  closed JSON-Schema keyword validator; final whole-branch review must decide
  whether a dependency-neutral shared module is warranted.
- Task 2: fix round 1/5 (1 addressed, 0 open — removed unrequested `$schema`
  dialect restriction and added standard-dialect regression; commits
  `e1960ec..b075512`).
- Task 2: complete (commits `7225c06..b075512`, review clean; 1 deferred minor).
- Task 3: Ruling: take ownership of the existing dirty
  `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/opencode-benchmark.md`
  hunk because it is the immediately preceding failed OpenCode evidence that
  Task 3 must retain and supersede; preserve it verbatim and append authenticated
  Task 3 evidence rather than dropping or restaging unrelated files — cost if
  wrong: the Task 3 commit also freezes an earlier Task 17 diagnostic note that
  its original author intended to keep uncommitted.
- Task 2 reopened: the first fresh Task 3 live run reached composition and
  failed before provider contact with `invalid schema content:
  assurance.intake.schema.case-authoring.v1`. Task 2's closed-schema invariant
  was placed at the generic `SchemaEntry` seam, but the closed vocabulary is a
  durable-effect runtime constraint; ordinary contributed agent/result schemas
  require standard keywords such as `$defs`, `$ref`, `anyOf`, and `minLength`.
  Move the fail-closed check to schemas referenced by effect intent/receipt and
  retain generic schema JSON authentication — cost if wrong: an unsupported
  effect schema could reach runtime, or product composition could remain
  blocked before provider admission.
- Task 2 reopened ruling: after moving validation to effect use, the installed
  healing/improvement effect schemas themselves require a finite set of normal
  JSON-Schema applicator/assertion keywords. Preserve those product contracts
  and extend the closed interpreter only to the exact installed effect-schema
  vocabulary, with local-reference safety and unknown-keyword rejection; do not
  weaken schemas to fit the old tiny subset — cost if wrong: incomplete keyword
  semantics could admit invalid durable-effect payloads or an unnecessarily
  broad vocabulary could make future unsupported schemas appear executable.
- Task 2 reopened fix round 2/5: independent review of `4addd93` found two
  Important fail-close gaps: unhashable/non-text `type` raises `TypeError`
  instead of a schema `ValueError`, and explicitly present `null` keyword values
  are treated as absent for multiple structural/assertion keywords (including a
  null `$schema` dialect). Both remain open pending an implementation fix and
  rereview.
- Task 2 reopened fix round 2 result: `a4495ff` addressed both prior Important
  findings; rereview found one new Important because `re.compile()` can raise
  `OverflowError` for an extreme repetition count, which currently escapes
  direct runtime and effect-registry composition instead of becoming the
  stable closed-schema rejection. Fix round 3/5 is open for that exception
  boundary only.
- Task 2 reopened fix round 3/5: `5910649` normalizes extreme-regex
  `OverflowError` into `ValueError`; direct runtime and effect-registry
  regressions pass, and independent rereview reports all findings addressed
  with no new Critical/Important breakage. Task 2 is complete again at
  `4addd93` + `a4495ff` + `5910649` (469-test broad focused evidence from round
  2 plus 90-test schema/registry evidence from round 3).
- Task 3 preflight ruling: after schema composition passed, the next fresh run
  failed before provider contact because the tracked-but-dirty Phase 5 graph
  inventory is stale for the current `full/retro`, `full/improvement`, and
  `full/achieved` nodes. Take ownership of that generated inventory hunk,
  regenerate the entire document from the compiled workflow, verify exact
  nodes/edges/aliases/entrypoint closures, and commit it before retrying live;
  do not change the canonical workflow to match stale evidence — cost if wrong:
  Task 3 source authentication could bind provider evidence to an uncommitted or
  incomplete graph inventory.
- Task 3 workspace-claim ruling: the next committed-source run reached
  `intake.execute`, but its static `qa/changes` contract caused
  `TaskWorkspaceStore` to scan an unrelated 2026-08-18 change and reject that
  change's legacy runtime symlink. The installed `OutputRouteCatalog` already
  defines exact current-change outputs, so render closed resource templates from
  `/change_id` and those catalog routes for every agent execute alias; never
  delete historical data or teach the business-neutral engine to special-case
  `qa/changes` — cost if wrong: sibling changes can block/corrupt each other, or
  an overly broad current-change claim can race `events.jsonl`/`status.json`.
- Task 3 harness ruling: a non-zero `aa-next run` with no structured run result
  is a deterministic local failure and must terminate the benchmark with
  evidence, not loop until the eight-hour horizon — cost if wrong: a genuinely
  recoverable structured activity result could be mistaken for an unstructured
  process crash, so the fail-fast condition must require absence of a parsed
  run result.
- Task 3 installed-declaration ruling: live `110426-f941c51d` proved the Python
  contract generator and installed wheel declaration had drifted: the
  invocation lock still contained broad execute writes even though the source
  contract tests were green. Treat both generated product declarations as
  Task 3-owned authenticated source, add byte-for-byte generator parity and
  installed-provider resource assertions, and regenerate them through the
  existing writer before retrying live; do not bypass wheel declaration loading
  at runtime — cost if wrong: tests can validate an uninstalled dynamic graph
  while production executes stale unsafe claims.
- Task 3 scope-propagation ruling: execute receives the closed
  `AgentRunRequest`, so the exact resource template must resolve an opaque safe
  scope component from `workspace.scope_id`; it must not infer product identity
  from prompt instructions or parse output paths. Task 3 takes ownership of the
  six pre-existing capability `agent_workspace()` helper hunks because they are
  limited to constructing this same workspace contract (including the already
  required logical write-root normalization) and cannot be staged separately
  from scope propagation; no other capability hunk is admitted — cost if wrong:
  a broader Phase 5 change could be accidentally attributed to live admission,
  or adapters could receive a non-canonical envelope.
- Task 3 installed-declaration fix round 1/5: independent review of `b88bf79`
  found one Important scope-authentication gap in generation codegen: workspace
  scope came from the reviewed plan while allowed outputs came from the business
  input, and plan/business change identity was not required equal. Reject that
  drift during codegen and codegen-fix input validation and cover both paths
  before rereview; no provider retry while the finding remains open.
- Task 3 deterministic preflight correction: `435b75b` replaces every
  assurance-product execute claim with an exact `/change_id` template projected
  from `OutputRouteCatalog`; its sibling-symlink begin/promote regression passes.
  `8b67a91` makes an unstructured non-zero `aa-next run` fail closed without
  status polling and limits run-result parsing to the current invocation.
  Focused Phase 5 suite: `33 passed` (one existing schema-shadow warning); no
  provider-live command was run during this correction.
- Task 3 installed-workspace-claim correction: `b88bf79` refreshes both
  declarations only through `write_committed_product_declarations()` and adds
  byte-for-byte parity plus provider-loaded-manifest coverage. To retain the
  closed direct `AgentRunRequest` adapter interface, the execute template now
  resolves from the authenticated safe `workspace.scope_id`, populated from
  each product prepare business `change_id`; it does not inspect prompts or
  outputs. GREEN: 59 AgentWorkspace tests, 5 provider tests, 8 agent-contract
  tests, 7 CLI-compile tests, and 2 packaging tests; Ruff/format/Pyright clean.
  No provider-live command was run.
- Task 3 codegen scope-authentication correction: `d0df49d` requires the
  reviewed plan and validated business `change_id` to match for normal codegen
  and codegen-fix before preparation, then uses the validated business ID as
  `workspace.scope_id`. RED observed both validators accepting mismatch;
  GREEN is 71 focused codegen tests plus Ruff/format and source Pyright clean.
  No provider-live command was run; the full existing codegen test file's
  Pyright still has 12 unchanged assertion errors at lines 158/162.
- Task 3 execute-workspace ruling: live `113601-058d301c` created OpenCode
  session `ses_fc22480e8ffexonOfWBTDvl5Tr`, but failed before prompt admission.
  The prepare request authenticated `.staging/544101…/attempt-1` while the
  execute task authenticated `.staging/83c585…/attempt-1`; the adapter compared
  them before PATCH and collapsed the mismatch into `workspace binding title is
  missing or invalid`. A direct full-length title probe succeeded, ruling out
  provider title length. Rebind the effective agent request from the current
  execute `TaskContext` for both adapters and make the OpenCode PATCH conform to
  the v1.18.4 title-only API; do not weaken the staging boundary or reuse the
  prepare task root — cost if wrong: the model could write to a different task's
  staging area, or replay could authenticate a prompt against the wrong attempt.
- Task 3 execute-workspace correction: `9363fdb` adds one shared effective-run
  rebinder and makes OpenCode and Cursor dispatch, prompt/stdin, receipt,
  reconcile, cancel and terminal reduction use the current authenticated
  execute attempt root while leaving the ledger-bound original `TaskRequest`
  unchanged. OpenCode session PATCH is now title-only and its fake rejects extra
  fields. GREEN: 377 passed, 1 skipped; focused Ruff/format/Pyright clean.
- Task 3 adapter-recovery review fix: independent review found Cursor cancel did
  not compare the receipt request digest before termination and OpenCode did not
  authenticate the PATCH response session ID. `c083442` closes both, adds
  fail-closed drift/wrong-ID regressions, and revalidates the post-PATCH GET
  ID/title/agent. GREEN: 303 passed, 1 skipped; controller rerun of the two
  negative tests passed; independent rereview found no remaining
  Critical/Important issue.
- Task 3 finalize-root ruling: live `122211-ac0d6364` proved adapter execution,
  provider admission and promotion succeeded, but the following deterministic
  `intake.finalize` looked for project-relative predecessor outputs in its own
  new empty `write_root`. A finalize task does not inherit the execute task's
  staging directory; after the execute promotion receipt is durable, prior
  outputs are authoritative under `context.project_root`. Correct the same
  read-root error in Intake, Generation, Execution and Healing finalizers while
  leaving prepare/current-task writes on `context.write_root`; do not copy a
  project base view or reuse predecessor staging. Cost if wrong: later semantic
  validation can reject valid promoted files or accidentally authenticate an
  unpromoted candidate from the wrong attempt.
- Task 3 finalize-root correction: deterministic finalizers now read promoted
  predecessor files from `context.project_root`; a real `TaskWorkspaceStore`
  regression proves execute staging A -> seal/promote -> empty finalize staging
  B succeeds, while a file present only in finalize staging is rejected.
  Related package and Phase 5 suites passed (implementer: 406 tests; controller
  focused rerun: 167 tests), focused Ruff/format/Pyright passed, and independent
  spec/standards reviews found no blocking issue. The existing execution
  finalizer canonical-write boundary remains explicitly outside this correction.
