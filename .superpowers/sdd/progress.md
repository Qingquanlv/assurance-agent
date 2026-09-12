# SDD Progress — Attempt Runtime Production Closure

**Plan:** docs/superpowers/plans/2026-09-03-attempt-runtime-production-closure.md
**Branch:** codex/attempt-kernel-internal-refactor
**Worktree:** /Users/lvqingquan/agent/assurance-agent/.worktrees/attempt-kernel-internal-refactor
**Start HEAD:** bed59e69

## Tasks
Task P1: complete (commits bed59e69..c3f882e7, focused checks + smoke green)
- Extra: two commits instead of one (add aborted after package delete)
- Residual for P13: phase4/phase6/architecture Cursor inventory strings; tests/agent_runtime/test_fixture_rebinding.py still imports agent_runtime_cursor
Task P2: complete (commits c3f882e7..67b36f15, review clean after 67b36f15)
- Important fixed: identity symlink reject; current-only revision bindings; leftover status helpers removed; dual-runtime test imports updated
Task P3: complete (commits 67b36f15..fa6394fd, review clean after fa6394fd)
- Important fixed: required RegistryProjections.attempt_contracts; authenticate_composition_lock isinstance ProductLock
- Minor for whole-branch: required-field tests validate ProductLock via incomplete documents rather than RegistryDigests/RegistryProjections directly
Task P4: complete (commits fa6394fd..fea742c7, review clean)
- Minor for whole-branch: schema mapping helpers hardcode contribution version "1"; auto_fix_plan JSON items stay untyped until normalized_auto_fix_edits
Task P5: complete (commits fea742c7..87e37a20, review clean after 87e37a20)
- Critical/Important fixed: project current healing apply/approval/allocation *_digest keys; reject former *_sha256
Task P6: complete (commits 87e37a20..24ba70cc, review clean)
- Minor for whole-branch: observers field still assignable after seal; ensure_durable marker unread by load; compile_roots fencing_token default 1
Task P7: complete (commits 24ba70cc..baecc99a, review clean after baecc99a)
- Important fixed: apply observe integrity permanent; StaleFencingToken not transient
Task P8: complete (commits baecc99a..030875ec, review clean)
- Minor for whole-branch: AttemptExecutor.reconcile not on Protocol; some non-kernel fakes still return bare OutputT
Task P9: complete (commits 030875ec..e91efd6b, review clean after e91efd6b)
- Important fixed: Step 5 mismatch negatives raise on mutation/receipt; agent_runtime fakes use current bound identities
Task P10: complete (commits e91efd6b..0aa0ea57, review clean after 0aa0ea57)
- Important fixed: Agent phases return PermanentTaskFailure / typed ExecutorStepResult
- Deferred to P11: phase delta persistence into source host receipt
- Minor: success-shaped invalid handler ValidationError still raises
Task P11: complete (commit after 0aa0ea57, focused checks green)
- Mutating Application methods bind execution under the live runner lease
- ProductRuntimePorts compiles the 14-root artifact only after bind(lease)
- Status stays read-only and does not acquire a fence
- Remaining: host-receipt persistence of Agent phase deltas (P10 leftover)

# SDD Progress — Attempt Kernel Internal Refactor

**Plan:** docs/superpowers/plans/2026-09-04-attempt-kernel-internal-refactor.md
**Branch:** codex/attempt-kernel-internal-refactor
**Worktree:** /Users/lvqingquan/agent/assurance-agent/.worktrees/attempt-kernel-internal-refactor

## Execution Gate
- Dirty test tranche committed separately: `afd1e604` then pyright follow-up `9cc780a6`
- Deleted `tests/product/test_cli_sqlite_system_interrupt.py`; replacement is lifecycle reopen plus premature wakeup resume fail-closed in `test_non_agent_root_survives_reopen_status_lock_resume_and_publication`
- Pre-removal credential-free baseline on `9cc780a694b5f53a8191ee42324bd90695719c6b`: ruff / format / pyright / lint-imports green; full deterministic suite = 3862 passed, 23 skipped, 631.07s; three smoke scripts OK
- 2026-09-04 decision: the former protected live-provider gate and queued run https://github.com/Qingquanlv/assurance-agent/actions/runs/33875311180 no longer block Phase I. I0 starts only after the gate-removal tranche is committed and its focused/full repository checks plus three smoke scripts are green.
- Phase I accepted base: `b28125c2aba2b743eec5b279be434f68589542ba` (`phase-i-phase-p-base`); protected run URL: not applicable — gate removed 2026-09-04.
- Accepted-base characterization: 229 focused tests passed; collected-node digest `20a64058af5b5588a90f361ebac6962c0a45367bc913f23aeb0976602262f95b`.

## Phase I
- Task I0: complete (`b8c532cd`; Phase P journal-byte oracle is binding).
- Task I1: complete (graph execution uses only `AttemptKernelPort.execute_or_recover`; focused graph-boundary regression 68 passed, Ruff clean, repository-configured Pyright clean).
- I1 verification note: the plan's package-path Pyright invocation overrides the root include set and reports 445 pre-existing errors from excluded framework tests; the repository gate `uv run pyright` reports 0 errors.
- Task I2: complete (pre- and post-extraction focused characterization 105 passed; complete Attempt regression 234 passed; graph-engine Ruff, repository-configured Pyright, and import contracts clean; I0 journal-byte oracle unchanged).
- I2 depth/deletion review: accepted. `_run` now exposes one commit call followed by Effect settlement and terminal/release coordination; `_commit_or_recover` directly owns validation through promotion/recovery, replacing the former validation, prepare-recovery, and promotion helper branches. Only the two private result dataclasses were added; no collaborator, protocol, dependency bag, wrapper, second coordinator, public contract, or persisted shape was introduced.

# SDD Progress — QA Flat Workspace

**Plan:** docs/superpowers/plans/2026-09-11-qa-flat-workspace.md
**Branch:** benchmark-regression
**Worktree:** /Users/lvqingquan/agent/assurance-agent (in-place; no extra worktree)
**Start HEAD:** 5bddf10523c47600a5022e4e076c5ead5fe686af

## Tasks

Task 1: complete (commits 5bddf105..465a75cc, review clean after 465a75cc)
Task 2: complete (commits 465a75cc..2341e709, review clean)
- Minor for whole-branch: improvement archive-receipt hardcoded (qa_join forbids archive/ substring); intake resolve reads widened to qa/; leftover qa/changes fixtures in some contract tests
Task 3: complete (commits 2341e709..66f2315d, codegen/plan/review writes approved; leftover reviewer-skill + resolve_inputs qa/changes deferred to Task 7)
- Minor: _change_root still builds qa/changes for identity; planning fallback write-root label; healing dual-prefix source_refs; optional tests/** skill reads
Task 4: complete (commits 66f2315d..e90c1e64, review clean)
- Minor: agent_skills fallback write-root label; no explicit qa/tests-vs-fixtures conflict test
Task 5: complete (commits e90c1e64..689eeef3)
- Minor: export/archive compile-remap to qa_root (archive would relocate qa/); intake prepare still emits qa/changes allowed_outputs; status mismatch test deferred to Task 7
Task 5: complete (commits e90c1e64..689eeef3, review clean)
- Minor: unused require_descendant; _workspace_at change_id=qa_root.name coincidence
Task 6: complete (commits 689eeef3..a6b1e72e, review clean after a6b1e72e)
- Minor: DELIVERY_FLOW is a prefix of the old sentence; benchmark fakes still model aa export
Task 7: complete (commits a6b1e72e..a16a22e6, review clean after a16a22e6)
- Minor: product-input-v1.json weaker than model; goldens/public-closure.json unused qa/changes; _SKIP_PARTS unused
Task 8: complete (commits a16a22e6..a15411a2, review clean)
- Minor: full tests/product not run; docs/usage.md gitignored; workspace metapackage still 0.2.0
Task 9: blocked (scan + ruff green; focused pytest 54 pre-existing failures)
- Scan of `packages tests` has no `qa/changes` / `qa/archive`
- `docs/usage.md` rewritten locally but gitignored
- Red is Task 7–8 rewrite residue (allowlist vs `qa/.qa.yaml`, exact prefixes, `qa/tests/` validators, export/phase5/cursor)
Task 9: complete (commits a15411a2..05ac59e8, review clean after 05ac59e8; 2483 passed / 1 leftover cursor snapshot)
- Minor roll-up: DELIVERY_FLOW prefix; product-input-v1.json weaker; goldens; _change_root leftover; retro path-part fallback; docs/usage.md gitignored; workspace metapackage 0.2.0

# SDD Progress — Init Test Runtime

**Plan:** docs/superpowers/plans/2026-09-12-init-test-runtime.md
**Branch:** benchmark-regression
**Worktree:** /Users/lvqingquan/agent/assurance-agent (in-place; no extra worktree)
**Start HEAD:** fe64801e9f9ddbc39e952b0323bc716e1a5f338a
**Commit policy:** do not commit unless the user asks

## Tasks
Task 1: complete (uncommitted; review clean)
- Minor for whole-branch: collector does not dedupe duplicate symbols; notes interpolated into docstrings without escaping; digest-mismatch test omits failure.kind
Task 2: complete (uncommitted; review clean)
- Minor for whole-branch: test name still says “two tasks”; init_runtime import order; GENERATION_GRAPH_CONTRACT_IDS omits new contract
Task 3: complete (uncommitted; review clean)
- Deferred to Task 5: product GenerationGraphs constructors + IMPLEMENTED_BUNDLE_FIELDS
- Minor: receipts not on GenerationState; PEP8 blank line; sibling import ignore; redundant status writes
Task 4: complete (uncommitted; review clean)
- Deferred: factory still 12/14; stale “14” in unlisted tests; composition 46 vs 47
Task 5: complete (uncommitted; review clean)
- Deferred to Task 7: remaining `46` asserts; stale “twelve thin roots” names; init not in `_THIN_EXPORTS`
Task 6: complete (uncommitted; review clean)
- Minor: no test for evidence receipt/null semantics
Task 7: complete (uncommitted; focused gate 142 passed)
- Minor: pyright jsonschema warning on harness template; init not in representative thin-root invoke list
Final review: Important fixed (full init count + fail-closed test, 23 passed)
- Remaining minors: collector/notes escaping; digest-mismatch kind; GenerationState receipts; evidence receipt test; jsonschema warning; identifier check; reads omit .aa; route_init not exclusive
