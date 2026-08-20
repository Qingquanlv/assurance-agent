# Pure Graph Engine Phase 1 — final linear replay report

Date: 2026-08-20

Status: PASS

Implementation commit:

- `69a6c52d4e52191f051f7b72064628982b57a20c` —
  `fix(graph-engine): replay event history incrementally`

This focused fix closes the sole residual Important issue in `final-review.md`.
It makes exact event-history validation advance a single product-agnostic fold
cursor instead of repeatedly folding complete ledger prefixes. It does not add
a heartbeat/history budget, a Phase 2 API, product coupling, or a new extension
boundary. Neither `progress.md` nor `final-review.md` was modified.

## Root cause and deterministic evidence

`Engine.open()` acquires the invocation runner claim before reading and
validating the ledger. For a running invocation it validates once before lease
reclamation and once again after reclamation. The prior
`validate_event_history()` implementation did this after every accepted
planner or external transition:

```text
prefix = envelopes[:cursor]
current = fold_events(prefix)
```

A `TaskLeaseHeartbeat` is a valid one-envelope external transition. Therefore,
for `h` heartbeats the old validator applied prefixes of increasing length and
performed a triangular number of envelope applications while the claim stayed
held.

The regression constructs a real 71-envelope ledger through `Engine.start()`,
the pure planner, and `Ledger.append_batch()`. It contains the canonical
bootstrap, canonical task activation, atomic task-start-plus-lease, and 64
valid `TaskLeaseHeartbeat` events. The final projection is fold-valid,
workflow-valid, running, and has a non-expired lease. The test instruments the
real `EventEnvelope.has_valid_digest()` application boundary, so it counts
actual envelope passes and does not use wall-clock timing.

For this exact history, one old history validation applied:

```text
3 + 5 + 7 + sum(8..71) = 2,543 envelopes
```

`Engine.open()` ran that validation twice. Seven other required linear passes
accounted for `7 * 71 = 497` applications, producing the measured baseline:

```text
2 * 2,543 + 497 = 5,583 envelope applications
```

The deterministic linear ceiling is `12 * n`, or 852 for `n = 71`.

## RED

Heartbeat-heavy mandatory-open regression:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py::test_open_applies_heartbeat_heavy_history_linearly -q
F                                                                        [100%]
E       AssertionError: assert 5583 <= (12 * 71)
1 failed in 0.53s
```

Incremental-fold API and partition equivalence before implementation:

```text
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py::test_incremental_fold_matches_one_shot_across_batch_partitions -q
FFF                                                                      [100%]
E       AssertionError: runtime models must provide an immutable incremental FoldCursor
3 failed in 0.18s
```

The first RED proves the reported triangular behavior independently of the new
API. The second RED specifies the missing pure cursor contract.

## Implementation

### One product-agnostic fold state machine

`graph_engine.runtime.models.FoldCursor` is a frozen projection model with:

```text
projection: InvocationProjection
next_seq: int
advance(envelopes: tuple[EventEnvelope, ...]) -> FoldCursor
```

`advance()` is pure. It applies each supplied envelope once to local immutable
state, enforces the cursor's exact next sequence, verifies every envelope
digest, runs the existing event transition invariants, and strictly revalidates
the resulting projection at the batch boundary. If any later envelope in a
batch is invalid, the source cursor remains unchanged; no partial result is
published.

Complete `fold_events()` now starts an empty `FoldCursor` and delegates to the
same implementation. There is no duplicate event-state machine and no compiled
workflow or product data in the fold layer. `FoldCursor` is exported from the
runtime package as a general product-agnostic primitive.

### Single-pass exact history validation

`validate_event_history()` now retains one `FoldCursor` for the entire replay.
It still calls `plan_next()` at the same stable boundaries and still:

- proves the exact canonical bootstrap;
- regenerates and compares complete planner-owned batches;
- validates external task-start/lease, success/HEAD, outcome, heartbeat, and
  interrupt-resume/completion boundaries;
- permits the same valid parallel-wave settlement deferral; and
- performs the same exact final projection equality proof.

After a full planner batch or validated external atomic transition is known,
the validator advances the cursor through exactly that batch once. It no
longer constructs or folds an increasing complete prefix.

## GREEN and focused behavior

Incremental fold equivalence, boundary rejection, and no-partial-advance:

```text
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -q -k 'incremental_fold'
.....                                                                    [100%]
5 passed, 66 deselected in 0.15s
```

The five cases cover one batch, thirteen one-envelope batches, a mixed
partition, a wrong starting sequence, and a bad digest after one valid envelope
in the same attempted batch. Every valid partition equals one-shot
`fold_events()` exactly.

Heartbeat-heavy `Engine.open()` diagnostic:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py::test_open_applies_heartbeat_heavy_history_linearly -q -s
heartbeat replay processed envelopes: 639
.
1 passed in 0.37s
```

The temporary diagnostic print was removed after recording the measurement.
The committed regression asserts the proportional ceiling. The result is
exactly `9 * 71 = 639` required envelope passes, versus the old 5,583.

External atomic publications and parallel-wave control:

```text
uv run pytest packages/graph-engine/tests/runtime/test_planner.py -q -k 'partial_external_atomic_transition or parallel_successes_before_deterministic_all_join'
...                                                                      [100%]
3 passed, 44 deselected in 0.14s
```

The atomic cases prove that a task start without its lease and a task success
without its `HeadAdvanced` are rejected as partial publications.

All seven exact-routing adversaries plus the linear-open regression:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q -k 'applies_heartbeat_heavy_history_linearly or omitted_fanout or changed_canonical_edge_token or token_from_false_condition or extra_duplicate_edge_token or non_earliest_available_token or non_earliest_token_for_all_join'
........                                                                 [100%]
8 passed, 97 deselected in 0.30s
```

The seven adversarial histories remain rejected: omitted fan-out, changed edge
token ID, changed edge payload, false-condition token, duplicate edge token,
non-earliest one-token consumption, and non-earliest all-join predecessor
consumption.

## Final verification

Focused ledger, planner, engine, and scheduler suite:

```text
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py packages/graph-engine/tests/runtime/test_planner.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_scheduler.py -q
283 passed in 20.89s
```

All graph-engine tests:

```text
uv run pytest packages/graph-engine/tests -q
584 passed, 1 skipped in 24.16s
```

The skip is the existing filesystem-dependent non-UTF-8-name case.

Task 10, Task 11, and architecture boundaries:

```text
uv run pytest packages/graph-engine/tests/integration/test_toy_a.py packages/graph-engine/tests/integration/test_toy_b.py tests/architecture/test_graph_engine_boundaries.py -v
7 passed, 1 warning in 0.69s
```

The warning is the pre-existing assurance-kernel Pydantic warning that field
`schema` shadows a `BaseModel` attribute.

Static, formatting, typing, import architecture, and whitespace gates:

```text
uv run ruff check .
All checks passed!

uv run ruff format --check .
1131 files already formatted

uv run pyright
0 errors, 0 warnings, 0 informations

uv run lint-imports
Analyzed 632 files, 2153 dependencies.
Contracts: 12 kept, 0 broken.

git diff --check
<no output; exit 0>
```

## Committed-HEAD offline smoke

The implementation was committed before the smoke test, so the script archived
and built exact committed HEAD
`69a6c52d4e52191f051f7b72064628982b57a20c` rather than the working tree.

```text
bash scripts/graph_engine_smoke_test.sh
Successfully built graph_engine-0.1.0-py3-none-any.whl
Successfully built graph_engine_toy_a-1.0.0-py3-none-any.whl
Successfully built graph_engine_toy_b-1.0.0-py3-none-any.whl
TOY_A_EVIDENCE={..."status":"succeeded","terminal_reason":null}
TOY_B_EVIDENCE={..."blocked_status":"interrupted",..."terminal_status":"succeeded"}
graph-engine smoke test: OK
```

## Self-review

- Re-read the complete current final review and final-fix report, the complete
  fold, planner, engine, ledger/planner/engine/scheduler tests, and the relevant
  runtime ledger/checkpoint/event code before editing.
- Traced the introducing change at `613436d` and reproduced its exact
  prefix-fold recurrence before implementing a fix.
- Kept every fold transition in one source of truth; complete and incremental
  folds cannot drift semantically.
- Checked cursor immutability, wrong-sequence and digest rejection, mixed batch
  partitions, and no partial advance after a later envelope fails.
- Checked exact planner batch comparison and external atomic validation happen
  before cursor advancement, while stable planning boundaries and final
  projection equality are unchanged.
- Re-ran valid parallel wave, retry, subgraph, join, interrupt/resume, crash,
  scheduler, and full graph-engine histories; all remain green.
- Re-ran all seven adversarial exact-routing cases; all remain rejected.
- Reviewed the implementation diff: changes are limited to the pure runtime
  cursor/replay path and focused tests. No product module, benchmark scaffold,
  Phase 2 budget API, `progress.md`, or `final-review.md` was changed.

## Residual concerns

- `Engine.open()` intentionally retains several independent linear passes for
  ledger integrity, the authoritative fold, exact workflow replay, checkpoint
  verification, and post-reclamation revalidation. The measured heartbeat path
  is nine envelope passes per event. This constant factor could be reduced in a
  future separately reviewed change, but it is bounded linearly and preserves
  independent proofs.
- Planner/projection validation cost still depends on compiled workflow and
  projection size. There is no longer a heartbeat-driven recurrence over prior
  event envelopes; each authoritative envelope enters the history fold cursor
  exactly once per required validation pass.
- No heartbeat or history budget was introduced. That remains a Phase 2 policy
  decision and is not required to recover a valid Phase 1 ledger safely.
- No known correctness or availability concern remains for the reviewed linear
  replay scope.
