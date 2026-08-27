# Remaining Phases Final Closeout — Acceptance

Recorded 2026-08-27 from worktree
`/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`
on branch `codex/pure-graph-engine-phase3-spec` at committed HEAD
`9c86baeff7c5457a332bc0652b21a8f04a5c11ce`. The worktree stayed dirty
(84 `git status --short` lines, SHA-256
`297a22feb406b3033df5f3a545182bc863e7133e26d7c2c80b639e957c2e1724`).
User/other-work files were not cleaned, reset, or staged.

**Verdict: accepted_with_waivers.** The evidence pack is published and
every residual is resolved. The cutover is **not fully accepted**:
OpenCode live is deferred after the blocked record `063450-3e30e331`
(`generation_execute_invalid_output`), Cursor live stays deferred, and
required Step 2 gates exited non-zero. Frozen Phase 5 handoff remains
incomplete and was not rewritten as complete.

Step 4 whole-branch review and Step 5 rerun-after-review are pending
controller work. They are not part of this evidence commit.

## Bound sources

| Binding | Digest |
|---|---|
| Source commit | `9c86baeff7c5457a332bc0652b21a8f04a5c11ce` |
| Change-local admission | `51d934863618b2fa8905ea0a7e05f4007429f6d9d2566dbce7a0e7cb16d863d6` |
| Phase 2 equivalence | `76f259bba648c231d1f76ab94e64fb60b87b91ab643564d9bf456316d43801f3` |
| Phase 5 handoff file | `0faddcdd54fd46a2b677047dbd89c8e0d562076e479e1f5e5848ce5e21d1bd8e` |
| Phase 5 handoff canonical | `d1a91e5096a34960c2e18fde520307ca4566c46d6abe27534e0eea51b5282d5e` |
| Cleanup report file | `f4eafd858a2a6f305c528110fd38e98c1feb6172c52925d86fbc7e85e2422711` |
| Cleanup report digest | `7a86d864c4662723042180692b85949eed0bba0e180d6c7680375ae22c6bff0b` |
| Final OpenCode record | `34426196706b3f86b5c0f050ac2ae07e9e8203a99dc543bd1e9da8c458341f66` |
| Residual ledger | `a3d2e05d04dfb77b4a30b16ba96964d73457ac29ca263c60065bbce2cd7794b3` |

## Completion definition (truthful)

1. Change-local plan remains an opaque prerequisite. This closeout did
   not rewrite its `not accepted` verdict. Admission stays
   `accepted_with_waivers`.
2. Phase 2 tail invariants are bound by `phase2-equivalence.json`
   (`equivalent` / `ported`). Task 2 review is clean.
3. Phase 5 security/fault handoff is published and frozen. OpenCode
   live is **not** closed. Cursor live and dual-provider Eval stay
   `deferred_out_of_scope`.
4. Phase 6 cleanup completed against empty `project_roots` and did not
   delete Change-local result files.
5. Installed `aa` is owned by assurance-product. Deleted-package
   absence is a Task 8/9/10 claim; this task did not reintroduce them.
6. Final tests/benchmark/docs use product names. Legacy comparison
   fixtures were removed in Task 11.
7. Final OpenCode live from post-cutover source **did not** reach
   `achieved`. Latest record: `063450-3e30e331`,
   `generation_execute_invalid_output`. User instruction `先完成task14`
   deferred further live. Cursor live is not required this round.
8. Repository/type/import/security/packaging/installed-wheel/no-legacy
   gates are **not** all green. See Step 2 table.

## Step 2 gates (this run)

| Command | Exit |
|---|---|
| `uv run ruff check .` | 1 |
| `uv run ruff format --check .` | 1 |
| `uv run pyright` | 1 |
| `uv run lint-imports` | 0 |
| `uv run pytest` | 2 |
| `bash scripts/graph_engine_smoke_test.sh` | 0 |
| `bash scripts/assurance_product_wheel_smoke_test.sh` | 1 |
| `uv run python scripts/check_no_legacy.py --scope repository` (first) | 1 |
| same no-legacy after exact SDD allowlist | 0 |
| `uv run python scripts/check_remaining_phase_admission.py --admission .superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/admission.json` | 0 |

## Waivers

- OpenCode live / Phase 3 live / Phase 5 Task 24 / Phase 6 Task 15 /
  this plan Tasks 3 and 13: `deferred_out_of_scope` because the user
  said `先完成task14` and the latest live is blocked
  `063450-3e30e331` (`generation_execute_invalid_output`). Not
  `verified_complete`.
- Cursor live and dual-provider external Eval:
  `deferred_out_of_scope` (Global Constraints).
- Product-wheel smoke `MinimumCoverageMatrixAuthoring` import miss:
  recorded return-to-owner residue from Tasks 4/10. Not patched.
- Combined-suite Cursor isolation failures and two OpenCode fault
  gaps: recorded in the frozen Phase 5 handoff. Not patched.
- Dirty-tree / leftover unit and host hunks: not staged, not cleaned.
- First no-legacy hits were exact gitignored SDD review/report/brief
  files; those exact paths were allowlisted. No wildcards.

## Explicit non-claims

- Task 13 is not achieved and was not rewritten as publishable live.
- Frozen Phase 5 `phase6-handoff.json` still records OpenCode
  `incomplete` / `carried_forward`. That document was not edited.
- Step 4/5 of Task 14 are not done.
