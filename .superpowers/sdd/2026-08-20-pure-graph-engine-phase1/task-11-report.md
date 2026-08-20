# Task 11 report — Toy product B

## Scope

Added the independently packaged `graph-engine-toy-b` workspace wheel with
explicit `toy-b` product and plugin entry points. The product resolves only the
`toy.b` plugin and registers four handlers: `toy.b.seed`, `toy.b.left`,
`toy.b.child`, and `toy.b.combine`.

The root graph executes `seed`, fans out to the retrying `left` task and a child
subgraph, waits at an `all` join, executes `combine`, and interrupts at
`review` before ending. The child graph executes `child` and returns its
canonical output to the parent. The parallel sibling declarations claim
`left.txt` and `child.txt` disjointly; the nested child task repeats the
`child.txt` write claim at its execution boundary.

No engine default, default graph, default plugin, global capability registry,
or production in-process execution host was added. Loading and resolution stay
explicit and invocation-scoped. Integration execution uses a clearly named,
unconfined test-only host and context-manages every `Engine`, invocation handle,
resumed handle, and `SnapshotStore`.

## RED / GREEN

### Initial RED

Created `packages/graph-engine/tests/integration/test_toy_b.py` before the Toy B
wheel existed and ran:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py -v
# 2 failed; both failed at load_product_entrypoint("toy-b") with:
# ProductResolutionError: no product entry point for toy-b
```

This was the expected missing-product failure, not a collection or test-fixture
error.

### GREEN

Added the independent wheel, root workspace/dev/source wiring, product
manifest, plugin descriptor/runtime, and handlers. The first executable run
found one test-representation mismatch: frozen join output stores arrays as
tuples and objects as mapping proxies. The assertion was corrected to compare
the projection's JSON representation without weakening the expected literal.

Final focused GREEN:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py -v
# 2 passed in 0.89s
```

The executable assertions prove:

- exact left attempt history `(1, 2)`, first failure kind `transient`, and
  second-attempt success;
- exact `left.txt` and `child.txt` claims at the parallel root siblings, the
  nested child task claim, and their disjointness;
- canonical child graph instance ID, child graph output, and parent subgraph
  return `{"child": true}`;
- one two-token `all` join with literal left/child outputs;
- `combine` output, interrupt reason, and actions `("approve", "reject")`;
- one successful `approve` resume with reviewer payload and rejection of a
  second resume as `no pending interrupt`;
- terminal output and exact final `left.txt` / `child.txt` bytes;
- two separate engine roots with equal compiled digest, event kind/stable-ID
  sequence, final tree ID, and terminal output; and
- different Toy A/B product digests with mutually isolated registries.

### Review correction RED / GREEN

The spec review noticed that the first test revision compared `left` only with
the nested child task, so a regression in the root sibling subgraph claim could
escape. Added an assertion for `root/child-subgraph`, mutated its product claim
to `wrong.txt`, and ran:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py::test_toy_b_recovers_then_interrupts_and_resumes -v
# 1 failed: ('wrong.txt',) != ('child.txt',)
```

After restoring the required product claim:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py -v
# 2 passed in 0.90s
```

## Verification

All commands ran in the Task 11 worktree after the final product change.

```console
uv sync --dev
# Resolved 44 packages; audited 42 packages; exit 0.

uv run pytest packages/graph-engine/tests/integration -v
# 4 passed in 0.76s.

uv run pytest packages/graph-engine/tests/runtime -q
# 340 passed, 1 skipped in 4.90s.

uv run pytest packages/graph-engine/tests -q
# 556 passed, 1 skipped in 5.85s.

uv run python -c "import sys; import graph_engine; import graph_engine_toy_a; import graph_engine_toy_b; ..."
# Loaded and resolved both entry-point products, asserted assurance_agent was
# absent from sys.modules, and asserted disjoint registries; exit 0.

uv run ruff check .
# All checks passed.

uv run pyright
# 0 errors, 0 warnings, 0 informations.

uv run ruff format --check examples/graph-engine-toy-b packages/graph-engine/tests/integration/test_toy_b.py
# 4 files already formatted.

git diff --check
# exit 0.
```

The review-only sibling-claim assertion was then independently rechecked with
the focused integration, scoped Ruff, scoped format, scoped Pyright, and
whitespace gates; all passed.

## Self-review

- The first-attempt failure lives in the independently packaged Toy B handler,
  keyed by the engine-provided attempt number. Only `left` uses the
  two-attempt transient retry policy; other tasks are single-attempt.
- Root fan-out, child return, all-join, interrupt, resume, and finalization are
  exercised through the public engine API and real ledger/workspace behavior.
- Replay comparison excludes clock-bearing event digests and compares the
  required event kind/stable-ID sequence instead; all canonical invocation,
  graph, token, activation, task, and interrupt IDs are represented where the
  corresponding event carries them.
- The two-axis review's only spec finding was the missing root sibling claim
  assertion; commit `f46c43e` addresses it with a mutation-proven regression
  test.
- A standards reviewer flagged the separate plugin entry point under the
  assurance-product AGENTS.md rule. Task 11 explicitly requires preserving the
  stable Task 10 `graph_engine.plugins` interface for this pure graph-engine
  product, so the task-specific requirement overrides that generic product
  packaging guidance.
- Two duplication observations were left unchanged intentionally: descriptor
  declarations and runtime bindings are separate protocol contracts, while a
  module-level handler map would look like the forbidden global registry; the
  repeated integration setup keeps each entry-point boundary explicit and is
  small.
- `progress.md` was not modified.

## Review correction — exhaustive replay event IDs

The independent review found that the replay projection preserved only six
singular ID attribute values. It omitted the child graph's
`parent_graph_instance_id`, `parent_node_id`, and `parent_activation_id`, the
`graph_id` itself, and the complete tuple-valued `NodeActivated.token_ids`.
Because it also discarded field names, equal values in different ID roles were
not distinguishable in the replay comparison.

### RED

Added a literal projection assertion containing a child `GraphStarted` and a
two-token `NodeActivated`, then ran it against the original helper:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py::test_event_id_projection_preserves_parent_and_complete_token_bindings -v
# 1 failed: graph_started projected only graph_instance_id; parent bindings,
# graph_id, node_id, and the complete token_ids tuple were absent.
```

### GREEN

Replaced the attribute-presence search with an explicit exhaustive schema for
all 19 runtime event kinds. Each projected event now contains its ledger
sequence, kind, and ordered `(field_name, value)` pairs. Singular values,
nullable parent IDs, and tuple-valued IDs retain their complete shape and field
identity. Clock/timing fields and content digests remain excluded.

Added a schema coverage test that derives every `_id` and `_ids` field from
every model in the `RuntimeEvent` union and requires exact equality with the
projection schema. A new event kind or ID-bearing field therefore fails loudly
until the replay projection deliberately covers it.

Focused GREEN:

```console
uv run pytest packages/graph-engine/tests/integration/test_toy_b.py::test_event_id_projection_preserves_parent_and_complete_token_bindings packages/graph-engine/tests/integration/test_toy_b.py::test_event_id_projection_covers_every_runtime_event_id_field -v
# 2 passed in 0.13s.

uv run pytest packages/graph-engine/tests/integration/test_toy_b.py -q
# 4 passed in 0.89s.
```

### Correction gates

```console
uv sync --dev
# Resolved 44 packages; audited 42 packages; exit 0.

uv run pytest packages/graph-engine/tests/integration -v
# 6 passed in 1.05s.

uv run pytest packages/graph-engine/tests/runtime -q
# 340 passed, 1 skipped in 4.71s.

uv run pytest packages/graph-engine/tests -q
# 558 passed, 1 skipped in 5.82s.

uv run python -c "import sys; import graph_engine; import graph_engine_toy_a; import graph_engine_toy_b; ..."
# Both products resolved, assurance_agent stayed absent, and registries were
# disjoint; exit 0.

uv run ruff check .
# All checks passed.

uv run pyright
# 0 errors, 0 warnings, 0 informations.

uv run ruff format --check examples/graph-engine-toy-b packages/graph-engine/tests/integration/test_toy_b.py
# 4 files already formatted.

git diff --check
# exit 0.
```

## Commits

- `a445974` — `test(graph-engine): prove independent complex toy product`
- `f46c43e` — `test(graph-engine): prove sibling subgraph write claim`
- `f3867e5` — `test(graph-engine): compare complete replay event IDs`

## Concerns

None.
