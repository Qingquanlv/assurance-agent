# Task 6 Blocker — Candidate Validation / Precommit Validators

**Status:** BLOCKED  
**Worktree tip:** `23cecbb`  
**Plan:** `docs/superpowers/plans/2026-08-01-four-layer-assurance-verification.md` § Task 6  
**Design:** `docs/superpowers/specs/2026-07-31-four-layer-assurance-verification-design.md` D14 / D16 / §5.3

No implementation changes were made for Task 6. Cursor-loop-helpers left alone. No git add/commit.

## Why blocked

Task 6 Step 3 requires `codegen_fix_candidate/v1` to enforce **proposal and fixer-authority path subsets**, baseline-before digests from authority, and stale-authority rejection. Task 6 Interfaces also list both validators as deliverables, and Step 4 requires scheduler no-commit coverage for **each** validator.

That conflicts with the binding Task 1 resolution:

- `.superpowers/sdd/task-1-resolution.md` — **Minimal now**
- Deferred to Task 8: `FixerAuthorityV1`, apply-summary models, safety fragments
- Explicit policy: *Do not invent fields now. Do not stub empty classes for deferred items.*

Current tree state:

| Needed by Task 6 Step 3 | Present? |
|---|---|
| `CodegenFixApplyIntentV1` / API+E2E variants | Yes (`healing_codegen.py`) |
| `FixerProposalApprovalReceiptV1` | Yes |
| Strict `FixerAuthorityV1` (+ path/target bindings) | **No** (stripped after `e9cd9f3`) |
| Strict proposal model with editable target paths | **No** (`FixProposal` / `FixProposalItem` remain `extra="allow"` and expose only `target`/`eligible`, not path lists) |

Design §5.3 describes `healing/fixer-authority.json` in prose (codegen attempt, manifest/summary digests, write-set ID, execution batch, normalized path authority with generated/updated/reused + digest rules) but **never locks a Python class body**. The only concrete field set in history is the invented `FixerAuthorityV1` from `e9cd9f3`, which Task 1 deliberately removed.

Implementing Step 3/4 as written would require either:

1. inventing / restoring `FixerAuthorityV1` (and likely a strict proposal path surface) inside Task 6 — violates Task 1 resolution and Task 6 file list (does not include `healing_codegen.py`), or
2. omitting/stubbing authority checks — fails the plan’s Step 3 mutation table and “both validators” interface.

## Also unresolved (secondary; not sole reason)

These would need answers even after authority is unblocked:

1. **Proposal path authority surface** — design says proposal target paths ⊆ fixer-authority paths. Current `FixProposalItem` has no paths. Should Task 6 consume a new strict proposal model, parse legacy proposal payloads ad hoc, or wait for Task 8/15 registry activation?
2. **`PrecommitValidationContext` resolution sources** — plan locks the wire shape (`policy_object_id`, `policy_digest`, `gate_attempt_id`, `interrupt_id`, `definition_semantics`, …) but not the exact scheduler projection/CAS lookups that populate them for a dark-shipped test contract before Tasks 9/10/15 bind commit-safety / definition semantics.
3. **Diff-safety predicates** for codegen_fix (product edit, unrelated test, assertion expected-value weakening, skip/xfail) — design requires code-owned before/after checks; no shared helper module is named in Task 6 Files. Acceptable to place them under `precommit.py` for now, or wait for a later shared safety helper?

`generated_files_candidate/v1` + mapping extraction (`generated_entries.py`) look implementable from D16 + plan Step 2 alone, but the plan’s Task 6 is explicitly both validators; shipping only the generated-files half would still leave Steps 3–4 incomplete.

## Decisions needed from controller

Choose one:

**A. Pull authority forward into Task 6**  
- Restore/lock `FixerAuthorityV1` (+ path/target models) now — prefer the `e9cd9f3` bodies unless design amends fields.  
- Lock how proposal paths are read for subset checks.  
- Amend Task 6 Files to include `assurance_agent/artifacts/models/healing_codegen.py` (and tests) if models change.  
- Clarify that Task 8 then consumes the already-locked authority rather than inventing it.

**B. Narrow Task 6 to generated-files only**  
- Deliver `generated_files_candidate/v1`, receipt/context, registry with one ID (or register `codegen_fix_candidate/v1` as closed but unimplemented / load-rejected until Task 8).  
- Move Step 3 codegen_fix mutation suite + its scheduler no-commit cases to Task 8.  
- Update plan Interfaces / Steps accordingly.

**C. Task 6 implements only the generic seam**  
- Context/receipt models, closed registry IDs, contract field + unknown-ID rejection, scheduler hook, fold/verify receipt identity.  
- Both validator algorithms deferred: generated-files → later D16 task / Task 8, fixer → Task 8.  
- Would contradict current Step 2–4 test obligations unless the plan is rewritten.

## Recommended

**A** if healing precommit must land before Task 8 operations; **B** if the dark-ship priority is codegen authority before commit and fixer validation can wait for authority models.

## Left alone

- `benchmark/vue-fastapi-admin/benchmark/cursor-loop-helpers.sh`
- `tests/unit/benchmark/test_cursor_loop_helpers.py`
- No Task 6 production/test code written
- Stale `.superpowers/sdd/task-6-brief.md` / `task-6-report.md` from an older plan left untouched (they describe evidence digests, not this Task 6)
