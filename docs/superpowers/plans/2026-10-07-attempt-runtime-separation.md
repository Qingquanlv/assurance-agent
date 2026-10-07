# Attempt Runtime Separation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for implementation and independent review. The user already approved completing the original refactor; proceed within that scope without another approval gate.

**Goal:** Complete the omitted internal separation of Attempt state progression and domain transactions.

**Architecture:** A small sequential Attempt runtime chooses explicit actions from journal-derived state and call-local prerequisites. Cohesive domain handlers own writes and proofs. The existing kernel becomes the compatible composition facade. LangGraph still owns business topology and production interruptions still regenerate nodes.

**Tech Stack:** Python 3.11, uv, Pydantic, existing journal and resource ports. No dependencies added.

**Spec:** `docs/superpowers/specs/2026-10-07-attempt-runtime-separation-design.md`.

## Global Constraints

- Current worktree `/Users/lvqingquan/.codex/worktrees/frame-optimized/assurance-agent`, follow-up branch `codex/attempt-runtime-separation`, baseline `a4b95763` (merged main; identical tree to `f859103c91df9be678fc9cc3143f31dbedf97665`).
- Use Sol as previously requested. Do not merge or create another worktree. Preserve existing API, journal bytes, cuts, fencing, stop and generation behavior.
- Journal is sole durable truth. No Effects, second scheduler, persisted checkpoint or broad generic replay framework.
- Keep failed/unknown results distinct. No new catch-all exception conversion.

## Review Focus

- Recovery at prepared/promoted phase still obtains live authority and validates staged bytes.
- Waiting and phase-unchanged journal writes do not spin, duplicate dispatch or allocate another generation.
- Terminal replay completes and verifies release before returning, with artifact drift checks retained.
- Exceptions after file promotion propagate without claiming uncommitted failure.
- Reusing a kernel across calls does not share invocation state; mutable public port replacement used by existing tests still works.

### Task 1: Separate Attempt runtime and domain protocols

**Files:** Modify `packages/framework/graph-engine/graph_engine/attempts/kernel.py`; create private cohesive `runtime.py`, `handlers.py`, `commit.py` and an errors module only if needed to avoid cycles; add `tests/attempts/test_runtime.py`; use existing `phase.py`; add a narrow `.importlinter` contract if appropriate. Implementer may choose fewer files or equivalent focused names after inspecting imports.

**Interfaces:** Preserve `AssuranceAttemptKernel` constructor, public ports, `execute_or_recover(...)`, exported errors. Internal decisions/results are typed; runtime uses a small handler protocol and pure action selection. No business graph caller changes.

- [x] Read the spec, original kernel, phase projection, node factory and characterization/recovery tests. Record existing trace and transaction boundaries.
- [x] Add failing tests of pure routing for all lifecycle phases, including three active activity states. Add recording-handler tests for pending and unknown-result early exits, terminal replay, successful ordered actions and post-promotion exceptions. Reuse existing result models and journal snapshot fixtures. Run `uv run pytest -q packages/framework/graph-engine/tests/attempts/test_runtime.py` and capture the initial missing-runtime failure.
- [x] Extract the runtime and action-selection function. Use an exhaustive typed action/result contract. Keep finite progression; initialization prerequisites cannot be inferred to exist just because a durable phase is advanced.
- [x] Move the existing authorization/activity, commit, terminal/release protocols into cohesive handlers without changing append/cut/barrier order. Compose them in the kernel per call; do not copy the old monolithic coordinator into another file.
- [x] Run new routing/driver tests and existing attempts, persistence and stategraph regressions. Run Ruff and Pyright. Resolve failures against original behavior rather than changing golden data.
- [x] Self-review the diff and write a report describing exact interfaces, test evidence and original-goal coverage. Leave commit/publication to the controller.

### Task 2: Review, integrate and verify

**Files:** Runtime separation files as needed for findings; this design/plan; prior single-worker spec addendum; README architecture explanation.

**Interfaces:** Verify actual production facade delegates to tested runtime and domain handlers. No old monolithic execution path remains.

- [x] Independent task review checks spec coverage and code quality against baseline. Fix actionable issues and rerun affected checks.
- [x] Document the actual routing and handler ownership, retained retry/repair/resume/reconcile definitions, and journal-only truth. Explicitly state what Pi ideas are adopted and what is not copied.
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, `uv run python scripts/build_wheels.py --check`, `uv run pytest -q --tb=short -p no:cacheprovider`, and `git diff --check`.
- [x] Run all three repository wheel smoke scripts against the exact final source; scripts archive HEAD, so use an isolated source snapshot until changes are committed. Record manifest and logs.
- [x] Final review verifies every acceptance item and current source. Prepare the verified follow-up for publication under prior MR authorization; the preceding MR was already merged. Do not merge this follow-up. Record actual check results and any material limitation.

## Implementation and local verification (2026-10-07)

The production kernel now composes the bounded runtime and per-call domain handlers. Pure action selection owns dispatch/reconcile/adoption decisions. Handlers own their transaction writes, cut points and durability barriers. Journal formats, graph-facing signatures, product regeneration, retry budgets and Flow repair ownership remain unchanged.

- New runtime tests: 36 passed. Focused attempts/persistence/stategraph: 345 passed.
- Final full suite: 4842 passed, 18 skipped in 664.22s (0:11:04).
- Ruff and formatting pass; Pyright has zero errors and one pre-existing jsonschema source warning; 32 import contracts pass; generated declarations and whitespace checks pass.
- All three wheel smokes pass against a source snapshot identical to the final production source and tests.
- Independent task and final reviews have no remaining actionable findings. The task review's action-label/operation mismatch was fixed and covered by real-kernel null-output recovery and strict-handler tests. Existing low-level null-output replay behavior is intentionally preserved.

This completes the originally omitted runtime/domain separation. The work is prepared as a focused follow-up to the merged single-worker/Effect-removal change; merge and deployment are not part of this task.
