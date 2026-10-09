# Checkpoint R Removal Design

> Historical design: the Attempt event/journal and action-runtime descriptions
> are superseded by [persisted Attempt checkpoints](2026-10-09-attempt-checkpoints-design.md).


> **Status:** implemented 2026-09-04.
>
> **Date:** 2026-09-04.
>
> **Decision:** remove Checkpoint R from the repository and migration program. Do not replace it
> with another named, live-provider, or candidate-evidence gate.

## 1. Purpose

Checkpoint R is release and test infrastructure. No production package, LangGraph graph,
`AssuranceAttemptKernel` transaction, runtime port, or persisted record depends on it. It may
therefore be removed without changing runtime behavior.

The removal deliberately gives up repository-managed certification of a real OpenCode binary,
provider, and model for an exact candidate SHA. The repository will retain deterministic coverage
of the behaviors it owns, but it will not claim that ordinary CI proves external-provider
compatibility.

## 2. Decision

Remove the complete Checkpoint R mechanism:

- the protected GitHub workflow and self-hosted-runner contract;
- the shell orchestrator and candidate evidence manifest;
- the 33-row live OpenCode/provider matrix;
- the live deployment fixture and workspace seeding harness;
- the pytest live marker, collection plugin, and credential preflight;
- Checkpoint R source-shape tests and duplicate focused CI invocation;
- all active plan, specification, README, and execution-ledger dependencies on the gate.

There is no successor named Checkpoint Q, production-certification gate, live marker, candidate
manifest, or equivalent abstraction. The normal repository gate becomes the only code merge
qualification maintained by this repository.

## 3. Scope

### 3.1 Delete

Delete these Checkpoint R assets:

```text
.github/workflows/checkpoint-r.yml
scripts/checkpoint_r.sh
tests/product/checkpoint_r_support.py
tests/product/test_checkpoint_r_live.py
tests/product/test_raw_agent_checkpoint.py
tests/product/fixtures/deployment/checkpoint-r-opencode.yaml
```

Remove the corresponding integration points:

- `checkpoint_r_live` from the pytest marker registry;
- `tests.product.checkpoint_r_support` from pytest plugin loading;
- the `not checkpoint_r_live` test filter from ordinary CI;
- the duplicate focused `test_raw_agent_checkpoint.py` CI step;
- tests that assert that the deleted workflow, script, support module, marker, or fixture exists;
- active documentation that makes Checkpoint R a prerequisite for cutover, Runtime deletion,
  Attempt refactoring, merge, or release.

### 3.2 Preserve

Preserve all production behavior and all independently valuable deterministic tests:

- stable `AttemptKey`, one-prompt admission, fencing, replay, crash recovery, and terminal release;
- local Raw Agent JSON extraction and `AgentResultT` validation;
- workspace authorization, seal, ordered Validators, durable prepare, promotion/recovery, Effects,
  and receipts;
- ProductLock, GraphRevision, semantic binding, restart, interrupt, export, and archive behavior;
- the 14-root runtime reachability classification;
- the exact 35 Agent graph-occurrence inventory;
- the full credential-free repository suite and all wheel smoke tests;
- normal LangGraph checkpointing and Attempt journal recovery. These are unrelated to Checkpoint R.

The 14-root reachability and 35-occurrence assertions move to ordinary Product tests only where no
existing test already proves them. Other Checkpoint R assertions are not copied when an existing
contract, binding, graph revision, prompt-admission, or Capability test already covers the same
invariant.

### 3.3 Leave unchanged

The manual OpenCode benchmark under `benchmark/assurance-product/` may remain as a non-blocking
operator or research tool. A benchmark result is not merge evidence, release evidence, or a
substitute Checkpoint.

No production code is changed solely for this removal. In particular, the following remain
unchanged:

- LangGraph graph definitions and routing;
- `AssuranceAttemptKernel.execute_or_recover(...)` and its transaction order;
- OpenCode adapter runtime behavior;
- Capability contracts and finalizers;
- persistence, workspace, Effect, and Product runtime ports.

## 4. Repository gate after removal

The repository gate consists of the existing standard checks, without a live-test exclusion:

```text
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest -v
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

The design does not duplicate deterministic suites in a second workflow: full `pytest` already
collects them, and the three smoke scripts already run in normal CI.

Focused tests remain useful while implementing a change, but they do not create a second release
authority.

## 5. Documentation migration

Documentation is updated according to its role:

1. Current authoritative specs and plans are rewritten so no executable task, dependency,
   acceptance criterion, or completion claim requires Checkpoint R.
2. The Attempt Kernel refactor spec and plan start from a clean Phase P implementation and ordinary
   repository gate; they require their focused journal-byte, crash, replay, and characterization
   suites, then the full repository gate and smoke tests.
3. Completed historical plans retain factual history but receive an amendment stating that the
   Checkpoint R portion was removed on 2026-09-04 and must not be recreated or treated as active.
4. Raw command output, review diffs, and immutable historical evidence files are not rewritten.
5. `.superpowers/sdd/progress.md` is merged rather than overwritten. Its queued-run blocker becomes
   a dated cancellation record, and later phases no longer wait for `CHECKPOINT_R_OK`.

The migration must not globally replace the word `checkpoint`: ordinary LangGraph checkpoint and
Attempt recovery references remain valid.

## 6. Explicitly accepted loss of assurance

After removal, the repository no longer provides:

- a real 33-contract run through an official OpenCode binary and external provider/model;
- detection of external OpenCode HTTP/SSE or provider behavior drift by a protected live job;
- release-time measurement that the selected model returns valid Raw Agent JSON for every contract;
- an evidence manifest binding candidate SHA, OpenCode binary digest, model, ProductLock,
  GraphRevision, and live results;
- a fail-closed branch-protection check that rejects skipped, xfailed, waived, timed-out, or missing
  live rows.

Fake-server and deterministic tests prove this repository's protocol assumptions and recovery
behavior. They do not prove that an external service currently honors those assumptions. Live
provider compatibility becomes an operator/deployment responsibility outside the repository's
merge and release claims.

## 7. External repository settings

Repository files cannot remove GitHub settings. If they exist, an owner should separately remove:

- required check `Checkpoint R / exact candidate`;
- GitHub Environment `checkpoint-r`;
- `CHECKPOINT_R_*` repository or environment secrets;
- the `checkpoint-r` self-hosted runner label and any dedicated runner;
- queued or obsolete Checkpoint R workflow runs.

These settings must not remain documented as prerequisites. Their removal is administrative cleanup,
not a blocker for the source change unless branch protection prevents merging it.

## 8. Implementation constraints

- Do not introduce a renamed live gate, candidate-evidence registry, certification service, or
  provider-health subsystem.
- Do not modify production transaction semantics to compensate for deleted release evidence.
- Do not duplicate coverage already present in ordinary tests.
- Do not retain dead Checkpoint R helpers for possible future use.
- Preserve unrelated worktree changes, especially the existing edit to
  `.superpowers/sdd/progress.md`, and update that file by a minimal merge.
- Keep the implementation reviewable as one removal program: infrastructure deletion, minimal test
  relocation, CI cleanup, and documentation de-authorization.

## 9. Verification and acceptance

The removal is complete when:

- all six Checkpoint R assets listed in section 3.1 are absent;
- no active workflow, script, pytest marker/plugin, CI command, source-shape test, plan task, spec
  requirement, README instruction, or execution blocker refers to Checkpoint R;
- precise searches for `Checkpoint R`, `CHECKPOINT_R`, `checkpoint-r`, `checkpoint_r_live`,
  `checkpoint_r_support`, and `test_raw_agent_checkpoint` find only explicitly retained immutable
  history, if any;
- the 14-root reachability and 35 Agent-occurrence invariants remain covered in ordinary tests;
- full `pytest` runs without a live-marker exclusion;
- Ruff, formatting, Pyright, import-lint, full pytest, and the three current wheel smoke tests pass;
- no production package or serialized schema changed as part of the removal;
- the Attempt Kernel internal-refactor plan has no protected-run, exact-candidate-manifest, or
  `CHECKPOINT_R_OK` prerequisite.

## 10. Non-goals

This work does not:

- certify another Agent runtime or provider;
- design Structured Artifact output;
- change Product cutover topology or migration semantics except to remove the R dependency;
- weaken runtime fail-closed behavior, local Schema validation, fencing, recovery, or transaction
  guarantees;
- remove normal checkpoint/recovery code or tests;
- cancel external GitHub runs or delete secrets without separate repository-owner authority.
