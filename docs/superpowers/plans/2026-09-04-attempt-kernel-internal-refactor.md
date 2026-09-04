# Attempt Kernel Internal Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `AssuranceAttemptKernel.execute_or_recover` read as one linear Attempt transaction while preserving public protocols, durable bytes, recovery decisions, and external side-effect counts.

**Architecture:** Keep `AssuranceAttemptKernel` as the sole graph-facing transaction coordinator. Reuse the existing journal, resource arbiter, workspace, activity, Validator, and Effect state machines. Isolate validation through promotion/recovery behind one Kernel-private commit method; do not pre-create a collaborator, context bag, protocol, or module for a single caller.

**Tech Stack:** Python 3.11, Pydantic v2, asyncio, LangGraph 1.2.11, SQLite production ports, uv, pytest, Ruff, Pyright, import-linter.

**Spec:** `docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md`

**Amendment:** `docs/superpowers/specs/2026-09-04-checkpoint-r-removal-design.md` removes the former
protected live-provider gate. This plan uses focused deterministic suites plus ordinary repository CI.

**Prerequisite plan:** `docs/superpowers/plans/2026-09-03-attempt-runtime-production-closure.md`

## Global Constraints

- Work in `/Users/lvqingquan/agent/assurance-agent/.worktrees/attempt-kernel-internal-refactor` on `codex/attempt-kernel-internal-refactor`.
- Start I0 only after the Checkpoint R removal tranche is separately committed and the worktree is
  clean. Record the Phase I base SHA, focused test count, and collected-node digest in
  `.superpowers/sdd/progress.md`; no protected-run URL or candidate manifest exists.
- Do not stage, discard, amend, or silently absorb pre-existing worktree changes into an I0-I2
  commit. Resolve them as their own reviewed tranche before recording the Phase I base.
- If the accepted SHA changes Attempt production code, ports, events, Effect settlement, or workspace promotion relative to the planning audit, rerun the five design-note questions and revise the code-shape decision. Test-only changes require refreshing caller/test inventory and counts, not inventing a new architecture.
- Keep `AttemptKernelPort` unchanged. Its concrete parameter remains named `contract` because keyword callers use it.
- Preserve the happy-path trace exactly:

  `adopt_or_create → authorize_resources → begin_workspace → execute → validate_output → seal_candidate → run_validators → durable_prepare → promote → settle_effects → record_terminal → release_resources → record_release_proof`.

- Preserve every Attempt event and field, journal schema version, record digest, `AttemptResolution`, `ProductLock`, `GraphRevision`, checkpoint marker, and release proof.
- Preserve the existing `PendingTaskResult` and `IndeterminateTaskResult` LangGraph interrupt mapping and replay behavior.
- Preserve the transaction order: validate output/intents → atomically journal observation/intents → seal → ordered Validators → durable prepare → promote/recover → settle persisted Effects → terminal result → resource release → release proof.
- Preserve all existing crash-cut names and their positions relative to durable writes and irreversible actions, including the concrete `transaction_cut` propagation to `during_multi_file_promotion`.
- Do not add a LangGraph node, scheduler, second transaction coordinator, public port, service locator, dependency bag, factory, feature flag, compatibility facade, or historical-data shim.
- Do not wrap `AttemptJournalPort`, `ResourceArbiterPort`, `WorkspaceProvider`, `AttemptEffectSettler`, or their stores merely to rename a responsibility.
- Do not fix unrelated Phase P debt: observer mutability, the absent public `reconcile` member on `AttemptExecutor`, remaining default-fence helpers, deterministic Task error coercion, or the broad evidence re-export surface in `attempts/activity.py`.
- This is a behavior-preserving refactor. Characterize green behavior before moving code; do not manufacture a failing structural test solely to justify an extraction.
- Use `uv run` for Python commands. Each implementation commit ends with focused regression, byte-oracle comparison, review, and an explicit staged-file check.

## Current Code-Shape Decision

The accepted implementation starts with the smallest shape that satisfies the design note:

1. Existing ports already own journal durability, authorization/fencing, workspace state, activity recovery, and Effect settlement.
2. `AssuranceAttemptKernel._commit_or_recover` owns validated observation through promotion/recovery. It uses the Kernel's existing dependencies and `_assert_fence`; it does not receive a dependency bag or callback back into the Kernel.
3. Two private, non-serialized control values cross that seam:

   ```python
   @dataclass(frozen=True, slots=True)
   class _CommitRejected:
       snapshot: AttemptSnapshot
       resolution: RejectedTaskResult | PermanentTaskFailure
       terminal_output: JSONValue


   @dataclass(frozen=True, slots=True)
   class _PromotedCommit:
       snapshot: AttemptSnapshot
       output: BaseModel
       receipt: PromotionReceipt
   ```

4. Activity selection remains the cohesive Kernel-private `_execute_or_adopt` method. Journal identity, authorization, Effect handoff, terminal publication, and release remain Kernel methods because they coordinate existing authorities.
5. `AttemptNodeFactory` persists the LangGraph system-interrupt event that it creates through its already-bound authoritative journal.

The initial private entry point is:

```python
async def _commit_or_recover(
    self,
    contract: ResolvedAttemptContract[Any, Any],
    validated_input: BaseModel,
    scope: AuthorizedAttemptScope,
    claims: ResourceClaims,
    snapshot: AttemptSnapshot,
    step: ExecutedAttemptResult[Any],
    *,
    trace: list[str],
    cut: Callable[[str], None],
) -> _CommitRejected | _PromotedCommit:
```

Do not add `AttemptCommitter`, `commit.py`, or `test_commit.py` in this plan. A one-call, run-local object would move the same wide state behind a constructor without reducing the interface. If the reviewed private method still fails the design note's depth test, stop and revise the plan from measured code rather than introducing an intermediate wrapper.

## Inherited Phase P Coverage

The complete pre- and post-refactor Attempt suite inherits all Phase P recovery coverage. Phase I
does not manually duplicate the 21-row production-closure matrix.

The following moved commit seams receive explicit assertions:

| Commit seam | Required focused proof |
| --- | --- |
| invalid output/intents before canonical observation | no `ActivityTerminalObserved` or `EffectIntentRecorded` is appended |
| output and all intents observed | one atomic journal revision with unchanged bytes |
| Validator rejection/failure | seal occurred; no `CommitPrepared`; validated output is retained only for Validator-returned failure |
| observation durable, before seal | replay resumes without redispatch and produces the same result |
| `CommitPrepared` durable, before promotion | prepared workspace is reconstructed and promotion is not duplicated |
| multi-file promotion publication uncertain | the existing workspace recovery path and cut remain intact |
| `WorkspacePromoted` already durable | receipt is reconstructed, promotion is not repeated, and persisted Effects receive it |
| durable prepare and promotion fences | a stale runner cannot perform either irreversible action |

Resource acquisition, OpenCode admission, terminal release, checkpoint recovery, and all other
Phase P rows remain covered by the unchanged complete recovery suite and ordinary repository gate.

## Execution Gate

Complete this gate before I0. It changes no source files.

- [ ] Finish the Checkpoint R removal tranche and any pre-existing worktree change outside Phase I;
  review and commit each separately.
- [ ] Verify the local worktree is clean and record its HEAD as the Phase I base:

```bash
git status --short
phase_i_base_sha="$(git rev-parse HEAD)"
test -n "$phase_i_base_sha"
test -z "$(git status --porcelain)"
```

Expected: status is empty and the base SHA names the separately reviewed Phase P plus gate-removal
state. Do not use a dirty-tree HEAD as the Phase I base.
- [ ] Run and record the accepted-base characterization:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_runner_lease.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py \
  packages/framework/graph-engine/tests/effects/test_effect_state.py
set -o pipefail
uv run pytest --collect-only -q \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_runner_lease.py \
  | rg '::' \
  | LC_ALL=C sort \
  | shasum -a 256
```

Record the actual passed count and a digest of collected node IDs. Do not hard-code the earlier `213 passed` after the prerequisite test inventory changes.

- [ ] Pin the accepted base once:

```bash
test -z "$(git tag --list phase-i-phase-p-base)"
git tag phase-i-phase-p-base "$(git rev-parse HEAD)"
```

Keep the tag local. Protected CI remains the authority.

---

### Task I0: Freeze the canonical journal oracle

**Files:**

- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel.py`
- Create: `packages/framework/graph-engine/tests/attempts/attempt-kernel-phase-p.golden.json`
- Modify: `docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md`
- Modify: `.superpowers/sdd/progress.md`
- Track: `docs/superpowers/plans/2026-09-04-attempt-kernel-internal-refactor.md`

**Produces:** An immutable, accepted-base byte oracle covering fresh execution and prepared-state replay.

- [x] **Step 1: Clarify the design note's caller and observation wording.**

Qualify the two unbounded “all callers” statements as “all graph execution callers.” State that dependency assembly may bind the same authoritative `AttemptJournalPort` to the Kernel and `AttemptNodeFactory`, but graph code may not call another concrete Kernel method.

Clarify the responsibility table:

- activity owns external execute/adopt/reconcile and observation of the source host's terminal receipt;
- commit owns final output/intent validation and the canonical `ActivityTerminalObserved + EffectIntentRecorded` durability fallback.

This changes no production API.

- [x] **Step 2: Add one deterministic effectful byte characterization.**

In `test_kernel.py`, reuse the fixed Attempt identity and Effect registry. Use a workspace double whose sealed, prepared, promotion, source, and Effect receipts contain only fixed canonical values.

The test runs the same scenario:

1. once from a fresh journal;
2. once by crashing at `after_prepare_before_promotion` and replaying.

Serialize every journal record exactly as persisted:

```python
canonical_json_bytes(
    [
        {
            "schema_version": ATTEMPT_JOURNAL_SCHEMA_VERSION,
            "record_digest": record.record_digest,
            "payload": record.canonical_projection(),
        }
        for record in journal.records(attempt_key)
    ]
)
```

Assert fresh bytes equal replay bytes and then equal the checked-in sidecar. Do not scrub paths or recompute expected bytes from candidate code.

- [x] **Step 3: Capture the sidecar from the accepted base only.**

Before changing production code, temporarily print the already-equal fresh/replay bytes, run the single test once, add those exact bytes to `attempt-kernel-phase-p.golden.json`, and remove the print branch. The committed test only reads the sidecar.

```bash
test "$(git rev-parse HEAD)" = "$(git rev-parse phase-i-phase-p-base)"
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel.py::test_effectful_attempt_journal_bytes_match_phase_p_golden
git diff --exit-code HEAD -- packages/framework/graph-engine/graph_engine
```

- [x] **Step 4: Run focused regression and commit I0.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py
uv run ruff format packages/framework/graph-engine/tests/attempts/test_kernel.py
uv run ruff check packages/framework/graph-engine/tests/attempts/test_kernel.py
git diff --exit-code phase-i-phase-p-base -- packages/framework/graph-engine/graph_engine
git add \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/attempt-kernel-phase-p.golden.json \
  docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md \
  .superpowers/sdd/progress.md
git add -f docs/superpowers/plans/2026-09-04-attempt-kernel-internal-refactor.md
git diff --cached --name-only
git commit -m "test: freeze phase p attempt journal bytes"
```

Expected staged set: only the five I0 artifacts. Record the accepted base SHA, run URL, test count, and collected-node digest in progress before committing.

### Task I1: Make graph execution depend on one Kernel method

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/node_factory.py`
- Modify: `packages/framework/graph-engine/graph_engine/testing/graph_harness.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_node_factory.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_graph_delivery.py`
- Modify: `.superpowers/sdd/progress.md`

**Consumes:** The accepted-base journal sidecar from I0.

**Produces:** Graph execution that calls only `AttemptKernelPort.execute_or_recover`.

- [ ] **Step 1: Add the behavioral caller characterization.**

In `test_node_factory.py`, use a scripted Kernel that implements only:

```python
async def execute_or_recover(
    self,
    attempt_key: AttemptKey,
    contract: ResolvedAttemptContract[Any, Any],
    validated_input: BaseModel,
    context: AttemptExecutionContext,
) -> AttemptResolution:
    ...
```

For a pending result, assert that `AttemptNodeFactory` appends exactly one `SystemInterruptIssued` through its injected journal and raises the unchanged LangGraph interrupt payload. Do not inspect `AssuranceAttemptKernel.__dict__` or assert a particular set of private/public-looking method names.

- [ ] **Step 2: Run the new caller test before implementation.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_node_factory.py
```

Expected: the one-method scripted Kernel cannot yet persist the interrupt because NodeFactory still looks for the concrete helper.

- [ ] **Step 3: Move interrupt persistence to NodeFactory and remove leaked helpers.**

Append `SystemInterruptIssued` through `AttemptNodeFactory._journal` using the current snapshot revision and fencing token. Preserve generation, ordinal, envelope digest, marker anchoring, and replay behavior.

Then:

- rename concrete `adopt_or_create` to `_adopt_or_create`;
- delete `record_system_interrupt_issued` and `observe_activity_completion`;
- remove those helpers from scripted Kernels/wrappers;
- load snapshots from fixture-owned journals in tests;
- exercise newer-fence adoption through the normal `execute_or_recover` path.

Do not add an interrupt service, journal facade, forwarding alias, or compatibility shim.

- [ ] **Step 4: Run the graph-boundary regression and commit I1.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_node_factory.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py \
  packages/capabilities/assurance-improvement/tests/test_graph_delivery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py::test_effectful_attempt_journal_bytes_match_phase_p_golden
uv run ruff format \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/attempts/node_factory.py \
  packages/framework/graph-engine/graph_engine/testing/graph_harness.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_node_factory.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py \
  packages/capabilities/assurance-improvement/tests/test_graph_delivery.py
uv run ruff check packages/framework/graph-engine packages/capabilities/assurance-improvement
uv run pyright packages/framework/graph-engine packages/capabilities/assurance-improvement
git add \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/attempts/node_factory.py \
  packages/framework/graph-engine/graph_engine/testing/graph_harness.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_node_factory.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py \
  packages/capabilities/assurance-improvement/tests/test_graph_delivery.py \
  .superpowers/sdd/progress.md
git diff --cached --name-only
git commit -m "refactor: keep one graph-facing attempt kernel method"
```

Expected: serialized interrupt behavior and journal golden are unchanged, and no pre-existing dirty tranche is mixed into I1.

### Task I2: Linearize the commit responsibility

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/effects/apply.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_effects.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_validator_context.py`
- Modify: `docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md`
- Modify: `.superpowers/sdd/progress.md`

**Consumes:** `_execute_or_adopt`, the existing ports/registries, and the I0 byte oracle.

**Produces:** `_commit_or_recover(...) -> _CommitRejected | _PromotedCommit`, with no new public or serialized type.

- [ ] **Step 1: Complete focused green characterization before moving code.**

Retain existing tests for:

- output and Effect intents sharing one journal revision;
- invalid output never becoming a terminal observation;
- prepared and promoted replay without duplicate mutation;
- every irreversible fence;
- promotion-to-Effect ordering and replay;
- exact happy trace and journal bytes.

Add a parameterized assertion in `test_kernel_effects.py` for the existing exact errors:

```text
'unknown effect kind: assurance.unknown.effect.v1'
effect intent schema is not registered: missing.intent.schema.v1
missing required property: remote_id
```

Extend the permanent-Validator replay test so `AttemptTerminated.output` is the validated output before and after replay. Pre-Validator output/intent validation failure must retain `output=None`.

Run these characterizations before refactoring:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py
```

- [ ] **Step 2: Move process-local Effect-intent discovery to the commit responsibility.**

Re-run the repository import inventory. If anything except Kernel imports `recorded_effect_intent_events`, stop and update the plan from the actual callers. Otherwise move it from `effects/apply.py` into `attempts/kernel.py` as a private helper. Remove it from the defining submodule's `__all__`; do not leave an alias.

`AttemptEffectSettler` must consume only persisted `snapshot.effects`. It retains handler-facing `EffectIntent`, `SchemaRegistry`, and receipt Schema validation, but no longer constructs journal intent events or validates process-local intent payloads.

- [ ] **Step 3: Introduce the two private outcomes and the commit method.**

Add `_CommitRejected`, `_PromotedCommit`, and `_commit_or_recover` to `kernel.py`. Move into the method:

- final output-model validation;
- Effect-intent kind and Schema validation;
- atomic canonical observation/intent append;
- seal and ordered Validators;
- durable `CommitPrepared`;
- prepared-workspace reconstruction and digest-drift check;
- promotion and authenticated promotion-receipt reconstruction.

The method directly owns the moved sequence. It must not dispatch/reconcile an Activity, apply an Effect, publish `AttemptTerminated`, or release resources.

Preserve:

- every trace entry and crash-cut position;
- `journal.ensure_durable` immediately after `CommitPrepared`;
- fence checks before prepare and promotion;
- the exact `try/finally` propagation of `cut` through the private multi-file-promotion hook;
- promotion recovery before any new `workspace.promote` call.

- [ ] **Step 4: Make `_run` a linear coordinator.**

After `_execute_or_adopt`, call `_commit_or_recover`.

- For `_CommitRejected`, pass its advanced snapshot, resolution, and `terminal_output` to the existing fail-closed terminal/release path.
- For `_PromotedCommit`, assert the Effect fence and pass its persisted snapshot/output/receipt to `AttemptEffectSettler`.

Keep `_authorize`, `_execute_or_adopt`, `_settle_effects`, `_terminate`, `_complete_terminal_release`, `_fail_closed`, and `_assert_fence` in Kernel. Merge only helpers made trivial by the move.

- [ ] **Step 5: Run focused and complete Attempt regression.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_runner_lease.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py \
  packages/framework/graph-engine/tests/effects/test_effect_state.py
uv run ruff format \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/effects/apply.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py
uv run ruff check packages/framework/graph-engine
uv run pyright packages/framework/graph-engine
uv run lint-imports
```

Expected: all inherited crash cuts still collect and pass; the checked-in byte oracle is unchanged; prompt, promotion, Effect, terminal, and release counts are unchanged.

- [ ] **Step 6: Perform the depth/deletion review and record the actual shape.**

Review specification compliance and code quality before committing:

- `_run` must expose the transaction sequence without duplicating commit branches;
- `_commit_or_recover` must directly hide meaningful validation-to-promotion complexity rather than forward to old helpers;
- no dependency bag, new protocol, wrapper, or second coordinator may appear;
- behavior tests, not AST/import-name blacklists, prove negative ownership;
- existing `lint-imports` remains the dependency-direction gate.

If the private method only lengthens the call graph, fold it back and stop to revise the plan; do not replace it with a one-method class.

Update the design note with an `Implemented shape` section recording the accepted Phase P SHA, actual private shape, retained Kernel responsibilities, rejected class/wrapper alternatives, and unchanged public/persisted contracts. Update progress with focused results and review outcome.

- [ ] **Step 7: Inspect the staged set and commit I2.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/effects/apply.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md \
  .superpowers/sdd/progress.md
git diff --cached --name-only
git commit -m "refactor: linearize attempt commit protocol"
```

Expected staged set: only I2 implementation, focused tests, reviewed design note, and progress. No standalone cleanup commit or AST architecture-test file is created.

### Task I3: Qualify the exact refactor candidate

**Files:** No planned source changes. Any review fix creates a new candidate and restarts I3.

- [ ] **Step 1: Freeze one clean candidate.**

```bash
git status --short
git diff --check
test -z "$(git status --porcelain)"
candidate_sha="$(git rev-parse HEAD)"
```

Record `candidate_sha` in external task/PR metadata. Do not edit source, tests, docs, locks, or workflows after this point.

- [ ] **Step 2: Run each credential-free gate once.**

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -v
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
git diff --exit-code phase-i-phase-p-base -- \
  packages/framework/graph-engine/graph_engine/attempts/events.py \
  packages/framework/graph-engine/graph_engine/attempts/resolutions.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py \
  packages/framework/graph-engine/graph_engine/application/runtime_context.py \
  packages/framework/graph-engine/graph_engine/composition/lock.py \
  packages/framework/graph-engine/graph_engine/boot/graph_revision.py \
  packages/products/assurance-product/assurance_product/revision_registry.py
test "$(git rev-parse HEAD)" = "$candidate_sha"
test -z "$(git status --porcelain)"
```

The full pytest run already includes the journal golden, Raw Agent, recovery, and lifecycle rows; do
not duplicate them in a second named gate.

## Completion Criteria

- [ ] The Checkpoint R removal tranche and all pre-existing changes are separately resolved, and
  Phase I starts from their clean SHA.
- [ ] Graph execution callers use only `execute_or_recover`; `AttemptKernelPort` is unchanged.
- [ ] `_run` reads as identity/replay → authorization → workspace → activity → commit → Effects → terminal/release.
- [ ] The private commit seam owns validation through promotion/recovery and owns no Activity dispatch, Effect application, terminal publication, or release.
- [ ] `AttemptEffectSettler` consumes persisted intents and remains the only Effect apply/reconcile owner.
- [ ] No new collaborator, public protocol, serialized schema, LangGraph node, service locator, compatibility layer, or second coordinator exists.
- [ ] Canonical journal bytes, event batches/order/digests, happy trace, resolution mapping, interrupt payloads, and all crash outcomes are unchanged.
- [ ] Replay still enforces one prompt, one promotion, one Effect settlement identity, one terminal result, and durable release proof.
- [ ] Focused tests, complete Attempt characterization, full repository gates, and all three smoke
  tests pass for the clean candidate SHA.
