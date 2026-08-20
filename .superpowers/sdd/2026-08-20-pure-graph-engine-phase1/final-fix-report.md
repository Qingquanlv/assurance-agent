# Phase 1 final-review fix report

Date: 2026-08-20

Status: PASS

Implementation commit:

- `613436d68eaef4ecb63819d664ea911e6c07dd20` — `fix(graph-engine): close final review gaps`

This was the single final-review implementation wave. It addresses all three
Important findings and the one Minor finding in `final-review.md`. It does not
add Phase 2 graph-budget APIs, alter the product-agnostic fold contract, open a
SUT plugin boundary, or touch the unrelated benchmark scaffold. Neither
`progress.md` nor `final-review.md` was modified.

## Finding 1 — subgraph dependency DAG and iterative failure propagation

### RED

Commands:

```text
uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py -q -k 'recursive_subgraph_dependency'
uv run pytest packages/graph-engine/tests/runtime/test_planner.py -q -k 'deep_acyclic_child_failure'
```

Observed before the implementation:

```text
2 failed
E Failed: DID NOT RAISE <class 'graph_engine.graph.compiler.CompileError'>

1 failed
E RecursionError: maximum recursion depth exceeded
```

The existing reachable in-graph cycle tests remained the control: graph-local
cycles are legal and continue to compile and execute under `max_activations`.

### GREEN

Implementation:

- The compiler now builds a separate graph-to-graph dependency adjacency from
  `subgraph` calls after validating individual graphs.
- The existing iterative SCC implementation is reused to reject self-recursive
  and mutually recursive graph components with deterministic declaration-order
  diagnostics through typed `CompileError`.
- Node cycles inside a graph are untouched.
- `_propagate_graph_failure` now walks parent graph instances iteratively, so a
  finite acyclic nesting chain deeper than Python's recursion limit fails
  deterministically instead of raising `RecursionError`.

Commands and final output:

```text
uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py -q -k 'recursive_subgraph_dependency or reachable_cycle'
....                                                                     [100%]
4 passed, 95 deselected in 0.61s

uv run pytest packages/graph-engine/tests/runtime/test_planner.py -q -k 'deep_acyclic_child_failure or parallel_successes_before_deterministic_all_join'
..                                                                       [100%]
2 passed, 43 deselected in 15.41s
```

## Finding 2 — exact compiled projection/event-history validation

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q -k 'omitted_fanout or changed_canonical_edge_token or token_from_false_condition or extra_duplicate_edge_token or non_earliest_available_token or non_earliest_token_for_all_join'
```

Observed before the implementation:

```text
FFFFFFF                                                                  [100%]
7 failed, 95 deselected
E Failed: DID NOT RAISE <class 'graph_engine.runtime.engine.EngineError'>
```

Each adversarial ledger was self-digested and accepted by the product-agnostic
fold before `Engine.open`, demonstrating that projection-only validation did
not prove the compiled routing history.

### GREEN

Implementation:

- `validate_event_history` replays authoritative ordered envelopes at
  `Engine.start` and every `Engine.open` validation boundary.
- The invocation bootstrap is exact: root graph, canonical start token ID,
  target, and payload must match the compiled entrypoint.
- At every stable boundary, planner-owned events are regenerated through the
  same pure `plan_next` used during execution and compared as one exact event
  batch. This proves condition evaluation, the complete matching edge set,
  canonical edge-token IDs, exact source-output payloads, deterministic token
  availability and selection (including all-join predecessor order), graph/end
  settlement, terminal status, and terminal output.
- Scheduler-owned transitions remain explicit and atomic: task start plus
  lease, success plus `HeadAdvanced`, attempt outcome, heartbeat, and interrupt
  resume plus exact `NodeCompleted` output.
- Planner settlement may be deferred only while another persisted attempt is
  still running and the next event is an external outcome/heartbeat. A focused
  valid parallel-success/all-join regression protects this scheduler-wave
  ordering without weakening routing checks.
- A final replayed projection equality check proves the supplied projection is
  exactly the fold of the validated causal history.
- The ledger remains authoritative; the fold remains product-agnostic; no
  planning side effects or projection product coupling were introduced.

Adversarial cases covered:

1. omitted fan-out branch;
2. changed canonical edge-token ID;
3. changed edge-token payload;
4. token emitted for a false condition;
5. extra duplicate edge token;
6. non-earliest one-token consumption; and
7. non-earliest all-join predecessor token.

Valid histories covered by fresh replay include branching, all-join,
subgraphs, retry, interrupt/resume, deterministic end/output, and parallel
wave outcome deferral. Toy B now performs a fresh terminal `Engine.open` after
its branch/join/subgraph/retry/interrupt lifecycle.

Commands and final output:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q -k 'omitted_fanout or changed_canonical_edge_token or token_from_false_condition or extra_duplicate_edge_token or non_earliest_available_token or non_earliest_token_for_all_join'
.......                                                                  [100%]
7 passed, 97 deselected in 0.49s

uv run pytest packages/graph-engine/tests/runtime/test_planner.py -q -k 'parallel_successes_before_deterministic_all_join'
.                                                                        [100%]
1 passed, 44 deselected in 0.16s
```

## Finding 3 — post-success HEAD journal clear

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q -k 'post_success_journal_clear_fault'
```

Observed before the implementation:

```text
FF                                                                       [100%]
2 failed, 101 deselected

unlink cut:
E Failed: DID NOT RAISE <class 'graph_engine.runtime.engine.EnginePublicationIndeterminate'>

directory-fsync cut:
E AssertionError: assert ['first', 'second'] == ['first']
```

The predecessor's `TaskAttemptSucceeded` plus `HeadAdvanced` was already
authoritative, but the scheduler's generic error path could continue planning
and execute or mislabel the successor.

### GREEN

Implementation:

- Clearing an already-unlinked journal now still fsyncs the workspace
  directory, so a retry can finish the durability barrier after an unlink-then-
  fsync cut.
- After authoritative success publication, a clear fault first reconciles the
  exact candidate HEAD, retries journal clear while holding the commit lock,
  and returns only after the clear barrier succeeds.
- A persistent clear fault raises `HeadPublicationIndeterminate`; the scheduler
  preserves it and the engine translates it to
  `EnginePublicationIndeterminate`. No successor planning occurs and no
  successor `TaskAttemptFailed` is appended.
- If unlink succeeded but the directory barrier remains unavailable, the
  transaction journal is recreated best-effort before propagating the explicit
  indeterminate result. A fresh open then reconciles authoritative ledger +
  HEAD and clears the journal.
- Exact candidate comparison remains conditional; a newer HEAD is never
  overwritten.

Both unlink and directory-fsync cuts assert predecessor success and committed
tree, the absence of any task failure, absence of successor execution before
recovery, retained recovery journal, and clean fresh-open completion.

Command and final output:

```text
uv run pytest packages/graph-engine/tests/runtime/test_engine.py -q -k 'post_success_journal_clear_fault'
..                                                                       [100%]
2 passed, 102 deselected in 0.51s
```

## Finding 4 — successful terminal reason invariant

### RED

Command:

```text
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py packages/graph-engine/tests/runtime/test_engine.py -q -k 'success_terminal_reason or success_without_terminal_reason or self_digested_success_with_terminal_reason'
```

Observed before the implementation:

```text
F.F                                                                      [100%]
2 failed, 1 passed, 167 deselected
E Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>
```

Both `InvocationFinished(status='succeeded', terminal_reason=...)` and a
successful `RunResult` with a reason were accepted.

### GREEN

Implementation:

- `InvocationFinished` rejects a reason for `status='succeeded'`.
- `fold_events` independently rejects the same forged event even when model
  construction validation is bypassed.
- `InvocationProjection` rejects successful projections with a reason.
- `RunResult` rejects successful results with a reason.
- A raw, self-digested success-with-reason ledger is rejected by
  `Engine.open`; an ordinary success-without-reason remains valid.

Command and final output:

```text
uv run pytest packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py packages/graph-engine/tests/runtime/test_engine.py -q -k 'success_terminal_reason or success_without_terminal_reason or self_digested_success_with_terminal_reason'
...                                                                      [100%]
3 passed, 167 deselected in 0.20s
```

## Final verification

All commands below ran from
`/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase1`.

Focused compiler/planner/engine/workspace/scheduler/ledger suite:

```text
uv run pytest packages/graph-engine/tests/graph/test_schema_and_compiler.py packages/graph-engine/tests/runtime/test_planner.py packages/graph-engine/tests/runtime/test_engine.py packages/graph-engine/tests/runtime/test_workspace.py packages/graph-engine/tests/runtime/test_scheduler.py packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py -q
452 passed, 1 skipped in 21.92s
```

The single skip is the existing filesystem-dependent non-UTF-8-name case.

All graph-engine tests:

```text
uv run pytest packages/graph-engine/tests -q
576 passed, 1 skipped in 22.04s
```

Task 10, Task 11, and architecture boundaries:

```text
uv run pytest packages/graph-engine/tests/integration/test_toy_a.py packages/graph-engine/tests/integration/test_toy_b.py tests/architecture/test_graph_engine_boundaries.py -v
7 passed, 1 warning in 1.03s
```

The warning is the pre-existing assurance-kernel Pydantic warning that field
`schema` shadows a `BaseModel` attribute.

Static and architecture gates:

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

Committed-HEAD offline archive/wheel/install smoke, run after implementation
commit `613436d68eaef4ecb63819d664ea911e6c07dd20`:

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

- Re-read the final review, architecture spec, Phase 1 plan, and relevant task
  reports before changing code.
- Checked that graph-call recursion and graph-local node cycles are separate
  relations; only the former is rejected.
- Checked exact replay against partial planner batches, scheduler atomic pairs,
  crash-stable external boundaries, concurrent wave outcome deferral, terminal
  proof, and final projection equality.
- Checked that journal-clear recovery occurs under the existing commit lock,
  never overwrites a newer HEAD, and cannot enter the scheduler's business-
  failure append path after authoritative success.
- Checked event, fold, projection, and facade result invariants independently so
  validation cannot be bypassed through a preconstructed Pydantic instance.
- Reviewed the staged diff and file list: only graph-engine implementation,
  graph-engine tests/Toy B integration, and this requested report are in scope.
  No benchmark file was touched.

## Residual concerns

- Exact event-history validation intentionally re-folds stable ledger prefixes
  during `Engine.open`. This keeps the fold product-agnostic and avoids a second
  mutable replay state machine. The cost is confined to open/recovery; planner
  batches collapse long structural runs, and current bounded Phase 1 suites
  remain fast. If future ledgers become very large, an independently verified
  incremental fold API would be the appropriate optimization rather than
  weakening exact replay.
- No legacy package compatibility is retained for the newly strengthened event
  invariant, as explicitly permitted for this new Phase 1 package.
- No known correctness concern remains within the requested Phase 1 scope.
