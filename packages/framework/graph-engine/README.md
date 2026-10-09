# graph-engine

`graph-engine` is a business-neutral runtime for graphs supplied by explicitly
selected, installed Python wheels. It has no default product, capability bundle,
entrypoint or workspace. Python wheels own `StateGraph` topology and Attempt
contracts. Project `.aa/` contains closed organization data only. The engine
does not load executable plugins, graphs, handlers, schemas, validators, or
runtime bindings from the system under test.

## Composition and boot

`RegistryPlatform.resolve(ResolutionRequest(...))` captures the selected product
and plugin sources, validates dependency versions and contributed definitions,
and produces a `FrozenComposition` with an authenticated `ProductLock`.
Products come from installed wheels or explicitly selected editable wheel
sources. `ConfigTreePluginSource` supplies data-only organization configuration;
it cannot define Attempt contracts or executable handlers. YAML product-file
sources are not supported.

Plugins publish a `PluginDescriptor` and a matching immutable
`PluginContribution`. A contribution can declare handlers, commit validators,
schemas, resources, bindings and Attempt contract references. Registry assembly
checks ownership, uniqueness and references against the selected contributions.

A binding in another plugin's namespace must explicitly link its capability ID
to the same declared Attempt contract ID. The contract must be present in the
selected contributions. Owned aliases can also reference a selected contract.
The binding and that contract can share an ID across contributions; duplicate
bindings and unrelated category collisions are rejected. These rules use the
published declarations, not a list of product-specific names.

`GraphEngineBoot` authenticates the selected graph factories and binds contract
resolution, Attempt execution and checkpointing into their build contexts.
Factories may use the `Flow` API or native LangGraph `StateGraph` builders.
`Flow.compile(context)` checks a flow and compiles it into LangGraph nodes,
routes, subgraphs and interrupts. Native builders can use `add_attempt_node`,
`add_attempt_edge`, `add_route`, and `human_gate` from `graph_engine.stategraph`.
The separate `AttemptGraph` subclass has been removed.

## Execution

`AssuranceApplication` starts and resumes a named entrypoint under an invocation
lease, checks its graph revision and returns normalized invocation status.
LangGraph chooses the graph node. An Attempt node then follows this call chain:

```text
Flow or StateGraph node
  → AttemptNodeFactory
  → AssuranceAttemptKernel
  → AttemptRuntime
  → phase handler
  → contract executor
```

The runtime loads a complete `AttemptCheckpoint` and dispatches the handler for
its saved phase: `authorize`, `execute`, `reconcile`, `commit`, `terminate`,
`release`, or `done`. Handlers own the durable boundaries. The runtime reloads
saved progress before dispatching again and returns when execution must wait.

The commit handler validates output, runs commit validators, seals authorized
writes and promotes them through the workspace provider. Validation protocols
and data types are public plugin interfaces; execution and conversion to
Attempt failure results belong to the commit handler.

Business handlers use `TaskRequest`, `TaskContext`, and `TaskOutcome`. Where
subprocess execution is selected, `attempts/execution_host` manages the worker,
its deadline, resource cleanup and authenticated receipts. These facilities
serve reviewed wheel code; the module CLI also has an explicit in-process
host for the installed demonstration products.

See [the Attempt module map](ATTEMPTS.md) for file-level responsibilities.

## Persistence

`persistence` contains storage protocols, memory implementations, revision and
fencing checks, and the LangGraph checkpoint adapter. The product package
supplies production SQLite implementations.

Two kinds of progress remain distinct:

- LangGraph checkpoints preserve graph state and pending writes. The anchored
  checkpointer records their durable confirmation and delivers interrupt
  notices through its outbox.
- Attempt checkpoints preserve one Attempt's phase, activity, output, promotion,
  terminal result and release state. Updates use compare-and-swap revisions and
  fencing checks.

The old `graph_engine.evidence` event ledger and event-folded checkpoints have
been removed. `persistence/journal.py` still defines the active LangGraph
checkpoint anchor records; it is not the deleted event-ledger mechanism.
Retro evidence is supplied through the read-only runtime evidence port.

Old nonempty Attempt journal databases require their original version to stop
existing runs and a fresh isolated run on rebuilt wheels; they are not migrated.

## Explicit module CLI

The module CLI emits JSON and requires explicit installed product and plugin
selections. From the repository workspace:

```bash
uv run python -m graph_engine compile \
  --product-dist graph-engine-toy-a \
  --product-entrypoint toy-a \
  --plugin-dist graph-engine-toy-a \
  --plugin-entrypoint toy-a

uv run python -m graph_engine run \
  --product-dist graph-engine-toy-a \
  --product-entrypoint toy-a \
  --plugin-dist graph-engine-toy-a \
  --plugin-entrypoint toy-a \
  --entrypoint hello \
  --invocation-id smoke \
  --root /tmp/graph-engine-smoke
```

The `aa` command belongs to `assurance-product`, not this framework wheel.

## Validation

From the repository root:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

The smoke scripts build committed `HEAD` and exercise isolated installations.
Commit the source under test before running them; the capability smoke script
rejects a dirty tracked worktree.
