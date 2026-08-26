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
