# Foundation Task 9 Report: Implement the engine-neutral Assurance application lifecycle

## Status

DONE_WITH_CONCERNS

## What I implemented

Revision-pinned LangGraph Application lifecycle. `graph-engine` does not import adapters, capabilities, or product. `aa` CLI and YAML Runtime are untouched.

- `AssuranceApplication.start/start_and_run/run/resume/status` are async. `thread_id == invocation_id`. Mutating calls acquire/release `InvocationRunnerLeasePort` in `try/finally`. `status` is read-only.
- `start` writes the initial checkpoint with `await graph.aupdate_state(..., as_node=START)` and does not execute a business node. `run`/`resume` continue with `await graph.ainvoke(None | Command(...), ...)`.
- Runnable config uses top-level `recursion_limit` plus the exact configurable keys `{thread_id, assurance_revision_id, assurance_product_lock_digest, assurance_root_input_digest, assurance_fencing_token, assurance_initial_checkpoint}`.
- Revision is checked against the installed `BootArtifact` before any checkpoint read or invocation. A later artifact with a different `revision_id` raises `RevisionMismatch` reporting the pinned required deployment. Next-node/status is taken only from the compiled graph snapshot (or a declared `TerminalEnvelope` in graph state), never from the journal.
- One pending human interrupt accepts a schema-validated scalar (`approve|reject|rework`). Two pending interrupts require `{interrupt_id: validated_value}`; a scalar raises `AmbiguousResume`. A system interrupt accepts only a wakeup/reconciliation envelope (`PendingTaskResult` / `IndeterminateTaskResult`) and rejects a human action via `InvalidResume`.
- Concurrent `run` produces one owner and one `RunnerConflict`. `GraphRecursionError` normalizes to `InvocationStatus(status="failed", reason="graph_recursion_limit")`. A declared business-budget terminal stays `completed` / `round_budget_exhausted`.

## What I tested and test results

| Command | Result |
|---|---|
| Step 4 RED: the three new suites | 3 collection errors: missing `AssuranceApplication` / `AmbiguousResume` |
| Same three suites (GREEN) | 11 passed |
| Step 5: `uv run pytest -q packages/framework/graph-engine/tests/application` | 23 passed |
| `uv run pyright packages/framework/graph-engine/graph_engine/application` | 0 errors, 0 warnings |
| `uv run ruff check` / `ruff format --check` on the committed paths | passed / formatted |
| `uv run lint-imports` (extra) | 14 kept, 0 broken |

## TDD Evidence

### RED

Command:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/application/test_application_lifecycle.py \
  packages/framework/graph-engine/tests/application/test_application_interrupts.py \
  packages/framework/graph-engine/tests/application/test_application_concurrency.py
```

Failing output:

```
==================================== ERRORS ====================================
_ ERROR collecting .../test_application_lifecycle.py _
E   ImportError: cannot import name 'AssuranceApplication' from 'graph_engine.application'
_ ERROR collecting .../test_application_interrupts.py _
E   ImportError: cannot import name 'AmbiguousResume' from 'graph_engine.application'
_ ERROR collecting .../test_application_concurrency.py _
E   ImportError: cannot import name 'AssuranceApplication' from 'graph_engine.application'
=========================== short test summary info ============================
ERROR packages/framework/graph-engine/tests/application/test_application_lifecycle.py
ERROR packages/framework/graph-engine/tests/application/test_application_interrupts.py
ERROR packages/framework/graph-engine/tests/application/test_application_concurrency.py
!!!!!!!!!!!!!!!!!!! Interrupted: 3 errors during collection !!!!!!!!!!!!!!!!!!!!
3 errors in 0.47s
```

Why expected: tests were added before `graph_engine.application.application` existed. The brief names missing `AssuranceApplication`.

### GREEN

Command:

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/application/test_application_lifecycle.py \
  packages/framework/graph-engine/tests/application/test_application_interrupts.py \
  packages/framework/graph-engine/tests/application/test_application_concurrency.py
```

Passing output:

```
...........                                                              [100%]
11 passed in 0.64s
```

Step 5:

```
.......................                                                  [100%]
23 passed in 0.38s
0 errors, 0 warnings, 0 informations
Contracts: 14 kept, 0 broken.
```

`test_start_pins_identity_and_run_uses_same_thread` is brief-verbatim in the body (`invocation_id="inv-1"`, `entrypoint="execute"`, `graph_input={"change_id": "chg-1"}`, `result.status == "completed"`). Start leaves `next` populated and the business node unrun. Status after start is `running`; after run it is `completed`. A second artifact with a different Product-lock digest cannot `run` the same thread. One human interrupt resumes with `"approve"` after schema validation; `"not-an-action"` raises `ValidationError`. Two interrupts reject a scalar with `AmbiguousResume` and accept `{interrupt_id: "approve"}`. A `system_wake` interrupt rejects `"approve"` and accepts `PendingTaskResult(wakeup=...)`. Two concurrent `run` calls yield one `completed` owner and one `RunnerConflict`. Infinite self-edge + `recursion_limit=3` is `failed` / `graph_recursion_limit`. A two-tick business counter returns `completed` / `round_budget_exhausted`.

## Files changed

Committed in `9b01ec41`:

- `packages/framework/graph-engine/graph_engine/application/__init__.py` (modified)
- `packages/framework/graph-engine/graph_engine/application/application.py` (created)
- `packages/framework/graph-engine/tests/application/test_application_lifecycle.py` (created)
- `packages/framework/graph-engine/tests/application/test_application_interrupts.py` (created)
- `packages/framework/graph-engine/tests/application/test_application_concurrency.py` (created)

Not committed (out of scope): `.superpowers/sdd/progress.md`, this report, and the task brief.

## Self-review findings

- `start` uses `as_node=START`; `run` uses `ainvoke(None, ...)`. Lease acquire/release is `try/finally`.
- Configurable key tuple is asserted equal to the brief set before every invoke.
- Status/next comes from `aget_state` / `GraphSnapshotEnvelope` / `TerminalEnvelope`, not the journal.
- `graph-engine` imports stay inside boot/application/attempts/persistence. `lint-imports` still forbids adapters/capabilities/product.
- Commit used the explicit path list from the brief; no `git add .` / `-A`.
- `aa` CLI and YAML Runtime were not modified.

## Issues or concerns

Not blocking.

- Brief-verbatim `test_start_pins_identity_and_run_uses_same_thread(application)` uses bare `artifact` / `context`. Those are pytest fixtures added to the signature so injection works; the body is unchanged.
- Entrypoint is remembered in-process on `AssuranceApplication._started`. A later process reopen (Foundation Task 10) needs a durable way to recover the entrypoint if the artifact has more than one root.
- Entrypoint `recursion_limit` is constructor-injected (`recursion_limits` / default 2048). `BootArtifact` does not carry `EntrypointGraphContract`, so the Application cannot read the Boot-time limit from the artifact.
- Lifecycle tests drive a tiny real `StateGraph` through official `InMemorySaver`. Anchored SQLite handshake remains Task 10.
- Runtime-kind pin is implicit: this Application is LangGraph-only and stamps revision / Product-lock / root-input digests. No extra configurable key was added because the brief lists an exact six-key set. YAML Runtime is not invoked and was not deleted.

## Review fix: disallowed human action names the real value

`_validate_human` now raises `InvalidResume` with the submitted action when it is schema-valid but not in the interrupt's allowed set. It no longer validates a dummy `{"action": "not-an-action"}`.

### TDD

RED: `test_disallowed_human_action_raises_with_actual_value` failed because `resume="rework"` on `approve|reject` raised `ValidationError` for `input_value='not-an-action'`.

GREEN:

```bash
uv run pytest -q packages/framework/graph-engine/tests/application/
```

```
........................                                                 [100%]
24 passed in 0.38s
```
