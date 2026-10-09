# graph-engine

`graph-engine` is a business-neutral, deterministic graph runtime. The wheel
ships the engine and its structural graph language only: it has no default
product, graph, plugin, entrypoint, workspace, Assurance package, or toy
package. A caller must select an installed product and its exact plugin bundle
for each invocation.

YAML replaces graph and contract text. Python wheels add installed capability.
Project `.aa/` holds organization configuration only. The engine does not load
executable plugins from the system under test.

## Public composition interfaces

`Engine(root, *, clock=None, host=None)` owns invocation ledgers, checkpoints,
leases, and immutable snapshot workspaces. `start()` binds an explicit
`ResolvedProduct`; `run_until_blocked()` deterministically advances it;
`resume()` resolves an explicit interrupt action; and `open()` replays an
existing invocation against the same product digest. Constructing `Engine`
without a `TaskExecutionHost` is fail-closed: task handlers are never called.

`ProductProvider` has one side-effect-free method:

```python
class ProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...
```

The manifest identifies the product, engine API, exact plugin requirements,
entrypoints, and structural workflow. `resolve_product()` creates a fresh,
invocation-scoped capability registry; importing or merely installing a wheel
does not mutate a global registry.

`PluginProvider` declares and contributes one reviewed wheel plugin:

```python
class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...
    def contribute(self, ports: RegistryPorts) -> PluginContribution: ...
```

The descriptor and immutable contribution must expose exactly the same task-handler and
commit-validator IDs. Entry-point discovery is inventory only;
`load_product_entrypoint()` and `load_plugin_entrypoint()` load only the name
the caller supplies.

`TaskHandler` exposes one asynchronous execution method:

```python
class TaskHandler(Protocol):
    async def execute(
        self,
        request: TaskRequest,
        context: TaskContext,
    ) -> TaskOutcome: ...
```

Production execution requires a `TaskExecutionHost` that confines plugin code
to the supplied attempt workspace. Phase 1 trusts reviewed Python wheel
plugins and does not implement hostile-code sandboxing. The module CLI's
`run` command therefore constructs a visibly CLI-local, in-process adapter for
trusted Phase 1 demonstrations only; it is not a default Engine host or a
production confinement boundary.

## Phase 1 graph language

The closed node-kind set is:

- `task`: invoke one registered capability with retry, timeout, resource, and
  optional commit-validator declarations;
- `gate`: choose outgoing edges using the closed expression language;
- `join`: wait for `all` or `any` inbound tokens;
- `subgraph`: invoke another graph in the compiled product;
- `interrupt`: stop at an explicit human/action boundary until `resume()`; and
- `end`: terminate a graph instance.

Phase 1 also includes conditional routing, bounded activations, resource-aware
waves, leases, immutable snapshots, atomic ledger batches, and checkpoints. It
does not expose Phase 2 configuration-plugin syntax, dependency solving,
wheel/source hashing, or an invocation lock.

## Explicit module CLI

The CLI emits one JSON document. There are no implicit product, entrypoint,
workspace, or run-plugin choices:

```bash
python -m graph_engine compile --product toy-a
python -m graph_engine run \
  --product toy-a \
  --plugin toy-a \
  --entrypoint hello \
  --invocation-id smoke \
  --root /tmp/graph-engine-smoke
```

The Phase 1 toy products use the same explicit entry-point name for their
product and sole plugin, so `compile --product toy-a` resolves that named pair.
`run` always requires one or more explicit `--plugin` options and rejects a set
that differs from the product manifest.

## Phase 1 acceptance gate

From the repository root:

```bash
uv run ruff check packages/framework/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run ruff format --check packages/framework/graph-engine examples/graph-engine-toy-a examples/graph-engine-toy-b tests/architecture/test_graph_engine_boundaries.py
uv run pyright packages/framework/graph-engine/graph_engine examples/graph-engine-toy-a examples/graph-engine-toy-b
uv run lint-imports
uv run pytest packages/framework/graph-engine/tests tests/architecture/test_graph_engine_boundaries.py -q
bash scripts/graph_engine_smoke_test.sh
```

The smoke test archives committed `HEAD`, builds all three Phase 1 wheels
offline, inspects their contents and dependencies, and exercises isolated
engine-only, toy-A, and toy-B environments.

Attempt execution stores full `AttemptCheckpoint` records and dispatches their
persisted `AttemptPhase` handler entry. The store enforces CAS revisions and
fencing; handlers own dispatch, output, promotion, terminal and release barriers.
Recovery reconstructs transient authorization/workspace handles without changing
the saved phase. The LangGraph checkpoint-anchor journal remains independent.
Old nonempty Attempt journal databases require their original version to stop
existing runs and a fresh isolated run on rebuilt wheels; they are not migrated.
