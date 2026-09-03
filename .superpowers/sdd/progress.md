# SDD Progress — Python-native LangGraph Migration

**Plan:** docs/superpowers/plans/2026-08-31-python-native-langgraph-migration.md
**Branch:** feat/python-native-langgraph-migration
**Worktree:** /Users/lvqingquan/agent/assurance-agent/.worktrees/python-native-langgraph-migration
**Integration-base:** a622d116
**Start HEAD:** a622d116

## Operator decisions
- improvement-evaluate is effectful; kind `assurance.improvement.effect.delivery.v1`; `memory_eval` is payload discriminator. "Eval 纯函数" = offline benchmark comparator only.
- Production validators stay 25 registered / 0 bound. Test-only dual-runtime clone binds `assurance.execution.validator.evidence.v1`; accept/reject both sides; never enters wheel/lock/production counts.
- min_matches: `generation/fanout` is the only four-way Send; Intake `case-design/prepare` and `repair-prepare` absorb into Composite internal dataflow only.
- Preflight pins 7 exact `(graph_id, join:any)` anchors, not counts.
- Feature join:any current-trigger failure hard-stops migration with zero waivers; production stays on full legacy-v2; no Product cutover/drain/Runtime deletion.

## Preflight
- Integration-base commit: 0f137fbd
- Prerequisite test alignment: a622d116
- Six workflow-module suites: 122 passed

## Tasks
Foundation 1: complete (commits a622d116..45efd182, review clean)
- ⚠️ RED run not in git (controller: test is brief-verbatim; pre-pin deps lack langgraph)
- ⚠️ live lint-imports/sync (controller: lock has langgraph 1.2.11, checkpoint 4.2.0, sqlite 3.1.1)
- Minor for whole-branch: Product allowed third-party set lists `langgraph` only; checkpoint sqlite modules needed in a later Product task

Attempt 1: complete (commits 45efd182..9d8be762, review clean)
- ⚠️ TDD red/green not separately committed (controller: types and tests match brief)
- Minor for whole-branch: no test that two distinct trigger values yield distinct keys
- Minor for whole-branch: `AttemptResolution` union drops `CommittedTaskResult[OutputT]` generic

Foundation 2: complete (commits 9d8be762..d88a74ae, review clean)
- ⚠️ TDD order not in git (controller: collection-fail RED matches brief)
- Minor for whole-branch: InvocationLock.model_dump_json remapped to canonical_bytes+newline
- Minor for whole-branch: ProductLock not exported from graph_engine.composition

Foundation 3: complete (commits d88a74ae..f65265c4, review clean after fix f65265c4)
- Important fixed: preload eviction now inside restore try
- Minor for whole-branch: attempt_contracts mismatch not fail-closed like handlers; source-less manifest symbol check; fixture in production module

Foundation 4: complete (commits f65265c4..43a0a6c2, review clean)
- Minor: MAX_ACTIVE_GENERATIONS=8 unnamed in spec; test-double fence is loose

Foundation 5: complete (commits 43a0a6c2..42399074, review clean)
- Extra: tests/conftest.py asyncio hook (not in brief)
- Minor: lease-loss and thread_id equality under-driven through saver

Foundation 6: complete (commits 42399074..38b71ba7, review clean)
- Minor: lease optional until Application; no cross-process flock test

Foundation 7: complete (commits 38b71ba7..55f74b59, review clean)
- Minor: Kernel/secret/workspace ports are empty Protocols until Attempt Task 8 / F9
- Minor: status helpers take typed envelopes, not raw LangGraph StateSnapshot

Attempt 2: in progress

Attempt 2: complete (commits 55f74b59..cb9b5a2c, review clean)
- Minor: duplicate-validator test accepts extra exception types; foreign-validator closure untested

Attempt 3: in progress

Attempt 3: complete (commits cb9b5a2c..2b89d0b2, review clean)
- Feature 4-field constructors expected-broken until Task 4

Attempt 4: in progress

Attempt 4: complete (commits 2b89d0b2..a48fba9e, review clean)

Attempt 5: in progress

Attempt 5: complete (commits a48fba9e..59a1b397, review clean)

Attempt 6: in progress

Attempt 6: complete (commits 59a1b397..29b10f0f, review clean)

Foundation 8: in progress

Foundation 8: complete (commits 29b10f0f..48833598, review clean after 48833598)

Foundation 9: in progress

Foundation 9: complete (commits 48833598..7d3516f6, review clean after 7d3516f6)

Foundation 10: complete (commits 7d3516f6..7e61c7cf, review clean after 5aa2ea48 + 7e61c7cf)
- Important fixed: remember_entrypoint refuses pin rewrite; recover+anchor before fence advance; Application.start pin_start refuse-on-rewrite; acquire releases lease if recover fails
- Minor for whole-branch: same-identity second start still writes START; pin_start is two transactions; start_pins opt-in; remember_entrypoint uses store._lock

Attempt 7: complete (commits 7e61c7cf..344520f9, review clean after 344520f9)
- Important fixed: acquire fence-upgrade now retries CAS like adopt
- Minor for whole-branch: release transition not keyed by Attempt key; from_records does not fail-closed

Attempt 8: complete (commits 344520f9..df5ac53e, review clean after 019decb0 + df5ac53e)
- Important fixed: non-commit replay; fail-closed terminate+release; crash-window no-rerun; invalid-output settle on replay; fence before fail-closed terminal
- Extra: application/runtime_context.py filled AttemptKernelPort.execute_or_recover (brief omitted)
- ledger.py unchanged (MemoryAttemptJournal covers CAS)
- Minor for whole-branch: process-global promotion cut; fold drops effect events (A9 must fold them); ensure_durable does not gate load

Attempt 9: complete (commits df5ac53e..dd2fde8f, review clean)
- Extra: events.py fold for effect events (required, brief omitted)
- Minor for whole-branch: kind-set test uses synthetic registry; not_applied re-apply untested; handler exceptions collapsed to unknown; DualSettlementError API unused

Attempt 10: complete (commits dd2fde8f..0ec9a8cf, review clean after 0ec9a8cf)
- Extra: events.py multi-generation active_interrupts fold
- Important fixed: completion via replace_checkpoint_marker_batch; no MemoryAttemptJournal fallback; record_system_interrupt_issued only; AnchoredCheckpointer arecover crash cuts
- Minor for whole-branch: replay payload drops interrupt kind; observer skips invocation when Attempt unopened; boot() accepts port-only kernel with attempt_factory=None; later-reentry assert loose; sibling barrier has no downstream join

Feature 1: complete (commits 0ec9a8cf..39918e42, review clean after 39918e42)
- Important fixed: import scan covers relative/from-pkg; with_test_contract reuses registry; run() prepares anchored memory
- Minor for whole-branch: with_test_contract handler not bound in attempt(); harness recording_context cannot call with_test_contract; compile_subgraph+run does not install checkpointer; journal loaded via exec_module of persistence test file

Feature 2: complete (commits 39918e42..bc9c81db, review clean)
- ⚠️ validator-parity uses a test-built one-node graph (factory only binds shipped IDs)
- Minor for whole-branch: with_test_contract overlay unused on Kernel path; tautological RejectedTaskResult isinstance; reject path no journal prepare-event assert; trigger-kind rerun untested
- Residual: test_contracts.py model_dump_json pre-existing

Feature 3: complete (commits bc9c81db..2ed51803, review clean after c3e3816d + 2ed51803)
- Important fixed: inbox-cursor join; equivalent-graph same-epoch proof; request_rework on prepare; graph-owned rounds
- Minor for whole-branch: select_case_design_retry unbound; factory ID table unused
- Residual: concurrent same-epoch on equivalent join graph (exclusive routes fire one predecessor); test_contracts model_dump_json pre-existing

Feature 4: complete (commits 2ed51803..2852945a, review clean after 2852945a)
- Important/Critical fixed: last budgeted plan-retry joins; per-family current-trigger matrix; root done no api fabrication; Send composition (no ainvoke); codegen-fix fail-closed
- Minor for whole-branch: interrupt payload family default api; same-key family-result LWW; publish_plan_review defaults decision to pass; retry-site interrupt mock-only

Feature 5: complete (commits 2852945a..49b7b22c, review clean after 49b7b22c)
- Important fixed: attempt_failure fail-closes coverage/failure routes despite leftover success
- Residual: Product must not replay prior attempt_failure into a later successful recheck; test_contracts model_dump_json pre-existing

Feature 6: complete (commits 49b7b22c..232640d7, review clean after 5eda7b0f + 232640d7)
- Important/Critical fixed: repair_failure fail-closes on attempt_failure; coverage missing status fail-closes; FixProposal committed success (no status) is repaired; three effect IDs on protocol receipts
- Residual: GraphHarness ReceiptRef has no effect_refs; test_contracts model_dump_json pre-existing

Feature 7: complete (commits 232640d7..caef7cb8, review clean after caef7cb8)
- Extra: effects/delivery.py remaps 64-hex Kernel keys to delivery_effect_key
- Important fixed: production ImprovementDeliveryEffect settlement; _coerce_output subset fallback removed
- Minor for whole-branch: hex key remap tautological for Kernel-shaped keys; nested status/projection unwrap; Kernel tests patch writes + importlib helper

Feature 8: complete (commits caef7cb8..06dc67ea, review clean after ce819d40 + 06dc67ea)
- Important fixed: Retro fail-close; assemble RetroContextV3; evaluate Kernel delivery proofs; apply human-review resume; assemble domain/signal conflict
- Minor for whole-branch: _signal_document defaults missing domain; assemble errors raise instead of failed terminal; Kernel tests importlib + rewrite writes
- Residual: assemble rules duplicated vs operations/retro.py

Feature 9: complete (commits 06dc67ea..939d5647, review clean after 939d5647)
- Important fixed: 5/7-owner lists rejected via Boot; child checkpointer asserted on compiled graphs
- Extra: recording_build_context.py records compiled.checkpointer
- Minor for whole-branch: allowlist tests exec test_boot.py; missing/extra rejection is indirect; plugin factory-discovery asserts tautological
- Residual: repo-wide pyright pre-existing; Generation dataclass still exports four family internals

Product 1: complete (commits 939d5647..e7081b8e, review clean after 82c94d3f + e7081b8e)
- Important fixed: Feature terminal adapter; receipts-only publish; process-stable ProductState digest
- Minor for whole-branch: missing status fail-opens to completed; Improvement {kind,digest} receipts not mapped; replace_receipts is LWW; ProductStateDocument can drift; digest tests reimplement projection; dry/runtime projection compares static table

Product 2: complete (commits e7081b8e..0dd571c8, review clean after 0dd571c8)
- Critical/Important fixed: inbox consume on compiled execute loops; coverage-human source; late assessment fail-close; execute-tail compile/validate; recursion limits on invoke; ProductGraphs placeholder dropped
- Extra: test_stategraph_entrypoints.py placeholder removed
- Minor for whole-branch: AST gate misses aliased invoke; adapt_execute_tail clears case_delta_paths; consume in offer not apply; complete_parallel_generation unused; missing coverage_state defaults satisfied

Product 3: complete (commits 0dd571c8..4f1da7dc, review clean after dfa9c80a + 4f1da7dc)
- Critical fixed: aa run finishes interrupted handshake; provider-schema gate uses advertised binding capability on run/resume
- Important fixed: LangGraph identity bound to checkpoint row fields; no journal synthesis; integrity errors not mapped to failed; resume pending IDs; status reads snapshot/receipts
- Extra: extract_checkpoint_markers unwraps Interrupt.value so issuance anchors are real
- Minor for whole-branch: status hierarchy hard-codes running from snapshot.next; sqlite file: URI unquoted; pending-interrupt assertion is weak

Product 4: complete (commits 4f1da7dc..3f22ca2f, review clean after 3f22ca2f)
- Important fixed: shadow runners bind isolated invocation/workspace; evaluate proofs from two kernels; validator prepare/promote from Engine journal
- Minor for whole-branch: LangGraph bind assert tautological; empty PARITY_RECORDS vacuously passes
- Residual: product-root traces still scenario-mapped; evaluate-inside-apply is evaluate kernel under apply identity

Product 5: complete (commits 9a7ba2af..4a9cd197, review clean after 4a9cd197)
- Intake `test_current_trigger[assurance.intake.workflow.graph.entry/advance-join]` collects (9a7ba2af); nine-site + loop-SCC authorization green
- Partial Wave A only: `improvement-evaluate` / `export` / `apply` / `rollback` → `langgraph-v1`
- Other 10 names stay `legacy-v2` with `schema-capability-red` (not a waiver); OpenCode still `provider_schema=False`
- Important fixed: resume `--resume-file` asserts recorded revision before `ProductRuntimePorts.open`; LangGraph reopen bind uses recorded lock or `required artifact`
- Extra: `models.py` + `test_cli_langgraph_lifecycle.py` staged (switch map and all-legacy assertion live there)
- Minor for whole-branch: reopen coverage is helper-level; existing-binding mismatch untested; missing-bind error repeats lock digest as revision id; official suite not fully re-run after class-identity fix

Product 6: BLOCKED (HEAD 4a9cd197)
- T6 requires all 14 switches → `langgraph-v1` and then drain/delete YAML-backed legacy starts
- 10 Agent-using names remain `legacy-v2` because OpenCode advertises `provider_schema=False`
- Must not fake the capability flag; must not delete compiler/Runtime while those names still need the YAML member

Product 7–10: BLOCKED on Product 6 (no compiler/Runtime/YAML deletion, no v3-only compile, no switch removal)

## Raw Agent Runtime Closure (supersedes the Product 6 blocker above)
Plan: docs/superpowers/plans/2026-09-02-raw-agent-runtime-closure.md
The `provider_schema=False` blocker recorded under Product 6 is historical: R1 deleted the
capability flag entirely and made local result validation unconditional, so Structured Output is
no longer on the critical path. Per-task detail lives in progress-raw-agent-runtime-closure.md.

R1–R5: complete (ca475d9e..a6fe5401, each review clean), fast-forwarded onto this branch at 2d952a6d

Post-R5 repairs (2d952a6d)
- Live-provider defects in the OpenCode observation: closed-terminal parsing rejected `reasoning`
  parts, and idleness required an explicit status-map record that real OpenCode never writes for a
  finished session. Every live attempt burned its observation horizon on its own success.
- Live Checkpoint R row now takes operator provider config and credentials through explicit
  environment pointers and keeps the server's own state out of the scanned project.
- Six capability packages shipped an identically named `test_graph_factory.py`, so a bare import
  resolved into another capability's copy; each now carries its capability in the module name.

Rename completion (uncommitted at time of writing)
- R1 renamed `ResultContract.extraction_mode` → `delivery_mode` and
  `AgentRunResult.structured_result` → `result_payload` but left ~15 call sites behind, including
  two shipping defects: the Cursor parser fed the old key to `AgentRunResult.model_validate`, so
  every Cursor success was rejected as `invalid_output`; and `plugin_kit.RUNTIME_RESULT_SCHEMA`
  still required the old key while the OpenCode reducer emitted the new one.
- Regenerated the fixture product declaration: both `request_policy_digest` and
  `request_config_digest` cover the whole configuration, so renaming the policy key moved both.
- Scope `tests/agent_runtime` + `tests/phase4` + the two adapter packages went from 3 uncollectable
  modules and 83 failures to 0 collection errors and 15 failures.

Checkpoint R live row: GREEN (29s against the operator's own provider), so Product T5b is unblocked
- The row had been failing as `observation horizon exceeded`, which read like an adapter defect but
  was the model id: the binding row carries the fixture placeholder `opencode/fixture-model`, which
  no real provider resolves, and OpenCode never closes such a session. `OPENCODE_MODEL` is now
  required rather than silently defaulting to that placeholder.
- Operator contract for the row: `AA_CHECKPOINT_R_LIVE=1`, `OPENCODE_MODEL` naming a served model,
  `OPENCODE_SERVER_PASSWORD` (or `OPENCODE_API_KEY`) as the redaction canary,
  `AA_CHECKPOINT_R_PROVIDER_CONFIG`, `AA_CHECKPOINT_R_AUTH_FILE`, and OpenCode >= 1.18.26.
- The loopback server now logs to a file instead of an undrained pipe.

Handed to Product T5b: the provider-schema apparatus is vacuous but still wired in
- `entrypoint_requires_provider_schema` and `_contracts_require_provider_schema` both discard their
  argument and return False, `AgentRuntimeCapabilities` is an empty husk, and
  `negotiate_provider_schema` therefore can never raise, yet `application.py` still calls the gate
  on the run and resume paths. The test that asserted the refusal now fails as DID NOT RAISE and
  has been deleted, since its subject no longer exists.
- Removing the rest belongs to T5b, not here: `test_entrypoint_cutover.py` still requires
  `schema-capability-red` in each cutover record, and that reason is exactly what T5b re-decides
  when it moves the remaining ten entrypoints. Deleting the gate and rewriting those reasons is one
  change, and it is T5b's to make.

Deferred debt (operator decision: record, do not fix now; T5b takes priority)
- benchmark/agent-runtime-phase3/manifest.json pins the pre-R1 request shape. It documents a real
  live run, so it stays stale until the phase3 benchmark is genuinely re-run rather than rewritten.
- 14 phase4 failures pre-date this work and were reproduced at HEAD: 13 share one root cause, the
  fixture workspace never seeds `qa/changes/CH-DEMO-001/requirement.md` so case-review `prepare`
  fails `invalid_input`; the ownership ledger pins paths under the long-renamed `packages/features/`.
- `test_isolated_wheel_import_does_not_load_adapters_or_assurance` is red at HEAD as well.



