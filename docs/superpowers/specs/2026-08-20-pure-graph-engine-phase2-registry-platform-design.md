# Pure Graph Engine Phase 2: Registry Platform and Frozen Composition

**Status:** approved in conversation on 2026-08-20

## 1. Context

Phase 1 created an independent, business-neutral `graph-engine` wheel and two
toy products. It proved structural graph compilation, deterministic planning,
leased task execution, immutable workspaces, crash recovery, explicit product
selection, and a minimal wheel-plugin seam. It did not migrate `aa`, OpenCode,
Cursor, or any Assurance capability.

Phase 2 turns that minimal seam into a production-grade composition platform.
It implements explicit wheel and declarative sources, fixed typed registries,
dependency validation, source and implementation hashing, an immutable
`InvocationLock`, plugin conformance, and the complete durable-effect lifecycle.

Phase 2 remains business-neutral. It does not move any real agent adapter or
Assurance code. Those remain Phase 3 and Phase 4 work respectively.

This design refines Phase 2 of
`2026-08-20-pure-graph-engine-plugin-architecture-design.md`. Where the Phase 1
prototype has a narrower interface, this design replaces it directly. No
compatibility aliases or shims are retained.

## 2. Goals

- Provide one deterministic composition path for explicitly selected product,
  wheel-plugin, and declarative-plugin sources.
- Build five fixed, immutable typed registries: Source, Capability, Schema,
  Resource, and Effect.
- Let plugins declare frozen contributions without receiving or mutating a
  registry.
- Validate the complete dependency graph without downloading, discovering, or
  selecting plugin versions.
- Snapshot and hash every selected implementation and declarative resource.
- Freeze the exact product, sources, registries, bindings, configuration, and
  compiled graph into one `FrozenComposition` and `InvocationLock` before the
  first ledger event.
- Reject any invocation drift on resume.
- Implement durable effects end to end: committed intent, stable idempotency,
  apply, receipt, retry, reconcile, and crash recovery.
- Supply reusable wheel and declarative-plugin conformance suites.

## 3. Non-goals

- No OpenCode, Cursor, model routing, prompt execution, or session behavior.
- No Assurance graph, operation, skill, schema, validator, effect, artifact, or
  business model.
- No `aa` CLI cutover and no old-runtime deletion.
- No automatic package installation, package-index access, ambient directory
  scanning, environment-variable discovery, or version selection.
- No compatibility import for the Phase 1 prototype interfaces.
- No sixth or dynamically defined registry kind.
- No executable Python, shell, command arrays, imports, or host-language
  expressions in declarative plugins.
- No hostile-code sandbox for trusted Python wheels.
- No cross-language or out-of-process plugin protocol.
- No parallel effect execution in Phase 2. Effects settle in canonical order.

## 4. Architectural decision

Phase 2 uses a generic registry platform. The registry kinds are fixed engine
mechanisms; their entries are product/plugin data. A plugin can add entries to
the five registries but cannot add a new registry kind or control registry
construction.

The only composition entry point is:

```python
class RegistryPlatform:
    def resolve(self, request: ResolutionRequest) -> FrozenComposition: ...
```

`ResolutionRequest` contains a single explicit product source and the complete
set of explicit plugin sources. `resolve()` snapshots the sources, validates
identities and dependencies, asks trusted wheel providers for frozen
contributions, parses declarative contributions, constructs all registries,
validates bindings and graphs, computes every digest, compiles the workflow,
and returns one immutable value.

The platform never exposes a mutable registry or registration callback.
`Engine.start()` accepts only a `FrozenComposition`; it cannot assemble or
discover a product itself.

## 5. Module shape

Phase 2 adds the following deep modules inside the engine wheel:

```text
graph_engine/
  composition/
    __init__.py       # narrow public composition interface
    models.py         # frozen request/source/registry/lock models
    sources.py        # wheel, editable-wheel, product-file, config-tree snapshots
    dependencies.py   # exact dependency validation and canonical topology
    registries.py     # five typed immutable registry builders/views
    resolver.py       # RegistryPlatform implementation
    lock.py           # InvocationLock canonical encoding and equality
    conformance.py    # reusable plugin/config contract suite helpers
  runtime/
    effects.py        # effect ledger projection, apply/reconcile state machine
```

Callers and plugin authors use models exported from `composition.__init__` and
`plugin_api`. Source traversal, dependency ordering, registry construction,
hash algorithms, and lock serialization remain implementation details.

Deleting `RegistryPlatform` would scatter source normalization, dependency
validation, collision policy, binding validation, graph compilation, and lock
construction across every CLI/product caller. Deleting the effect runtime
would scatter idempotency and recovery into every plugin. Both modules
therefore provide real depth and locality.

## 6. Resolution request and explicitness

### 6.1 Source forms

The closed Phase 2 source-kind set is:

- `WheelProductSource`: one named `graph_engine.products` entry point from one
  explicitly named installed distribution;
- `ProductFileSource`: one explicitly named, strict declarative product file;
- `WheelPluginSource`: one named `graph_engine.plugins` entry point from one
  explicitly named installed distribution;
- `EditableWheelPluginSource`: one wheel entry point plus an explicit physical
  source root and closed relative file list; and
- `ConfigTreePluginSource`: one explicit directory rooted at `plugin.yaml`.

The resolver does not infer the owning distribution from an entry-point name
alone when the request can name it. Distribution name, entry-point group, and
entry-point name are all part of a wheel source identity.

### 6.2 Complete explicit source set

The request must include exactly one source for every product requirement and
every transitive plugin dependency. Extra unreferenced sources are rejected.
Missing sources are rejected. Two sources claiming the same plugin ID are
rejected even if their bytes match.

Configuration paths declared relative to a product file resolve against that
file's physical directory. CLI-added paths are explicit additions to the
effective product manifest and are therefore included in its canonical bytes
and lock digest.

### 6.3 No version selection

Product and plugin descriptors use PEP 440 specifiers. Every explicit source
has one exact normalized version. The dependency module only:

1. validates the exact selected version against every applicable specifier;
2. computes the dependency closure;
3. detects missing and extra sources;
4. rejects cycles and incompatible constraints; and
5. emits a canonical topological order with qualified plugin ID as the stable
   tie-breaker.

It does not choose among candidates, prefer a highest version, backtrack,
download, or fall back.

## 7. Source snapshots and implementation hashing

The source-digest algorithm has a versioned engine-level identifier. A source
snapshot contains its normalized identity, exact version, canonical relative
file list, per-file digest, aggregate digest, and frozen bytes or already-bound
trusted implementation objects as appropriate.

All paths are segment-normalized relative paths. Snapshot code rejects path
escape, duplicate normalized paths, symlinks, special files, disappearing
files, name-set changes during enumeration, and byte/stat changes during
capture. Editable and declarative trees also reject multiply linked regular
files. Directory access uses descriptor-relative operations and stable rescans
rather than check-then-open absolute paths.

### 7.1 Installed wheels

An installed wheel source identity consists of normalized distribution name,
normalized version, entry-point group, and entry-point name.

The snapshot reads the distribution's installed `RECORD`, rejects missing
listed files, validates every available `RECORD` hash, then computes the engine
digest from canonical relative paths and actual installed bytes. The `RECORD`
bytes and relevant distribution metadata are themselves included. Cache files
such as `__pycache__` and `.pyc` are not source entries.

The provider is loaded only after its source snapshot is complete. Resolution
retains the bound trusted objects; execution does not re-import the provider or
re-read its module paths.

### 7.2 Editable wheel sources

Editable sources are never inferred. The request supplies a physical source
root and an exact relative file tuple. Globs and implicit repository scans are
not allowed. The tuple must include the package implementation, descriptor
resources, and relevant packaging metadata. Missing, duplicate, out-of-root,
or symlinked files are rejected.

The exact root identity, file tuple, paths, and bytes are locked. Changing the
file list is drift even when the aggregate implementation behavior appears
unchanged.

### 7.3 Declarative configuration trees

A configuration-plugin identity includes its physical normalized root,
qualified plugin ID, and exact plugin version.

`plugin.yaml` must declare every file in the directory tree. Undeclared files
and declared-but-missing files are rejected. Directories may contain only the
closed safe data media types needed for graph YAML, JSON data/schema,
Markdown/text prompts and role instructions, and other explicitly registered
opaque resources. Python, shell, binary executables, executable command
declarations, executable POSIX mode bits, symlinks, devices, and arbitrary
template execution are rejected.

The parser uses strict, extra-forbidden models. It rejects unknown
executable-looking keys rather than ignoring them. Every declared resource is
read into frozen bytes and is never re-read by path during that resolution.

### 7.4 Engine implementation digest

`InvocationLock` also pins the engine implementation. Production installs use
the graph-engine wheel metadata and installed-byte algorithm. Editable engine
runs use the closed `graph_engine` package tree and its packaging metadata with
the same canonical path/byte rules. The selected algorithm identifier is part
of the lock.

## 8. Plugin descriptors and frozen contributions

### 8.1 Descriptor

Every plugin descriptor contains:

- qualified `plugin_id` and exact PEP 440 `plugin_version`;
- engine API specifier;
- required plugin IDs and PEP 440 specifiers;
- declared capability, schema, resource, and effect IDs;
- the source identity expected by the provider; and
- descriptor schema version.

Descriptor construction is deterministic and side-effect free.

### 8.2 Contribution

A trusted wheel provider exposes:

```python
class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...
    def contribute(self, ports: RegistryPorts) -> PluginContribution: ...
```

`RegistryPorts` contains only stable, generic value constructors and engine
API information. It exposes no global state, mutable registry, resolver,
workspace, ledger, or product hook.

`PluginContribution` is frozen. It contains typed tuples or immutable mappings
for capability implementations, schema bytes, resource bytes, and effect
implementations. Its declared IDs must exactly equal the descriptor IDs.
Importing a plugin or calling `descriptor()` cannot register anything.

The Phase 1 `bind()` and `PluginRuntime` interface is replaced rather than
wrapped. The Phase 1 toys migrate to `contribute()`.

### 8.3 Declarative contributions

A config tree produces the same data contribution shape without Python
implementations. It may contribute graph definitions, schemas, resources,
configuration defaults, and capability bindings to already selected wheel
handlers. It cannot create a new executable handler, validator, or effect
implementation.

A declarative alias is a new qualified capability ID owned by the config
plugin. It binds to one existing wheel capability plus frozen binding data and
resource IDs. Dispatch preserves the alias ID in `TaskRequest` while also
providing the resolved target ID and frozen binding data. Aliases never replace
the target or another plugin's ID.

## 9. The five fixed registries

All registry entries are keyed by globally qualified IDs. Duplicate ownership
is an error, never a precedence rule. Registry views are immutable and sorted
canonically.

### 9.1 Source Registry

Contains the product and plugin source snapshots, identities, versions,
per-file digests, aggregate digests, and source-kind metadata. It is produced
only by the resolver; plugins cannot contribute source entries.

### 9.2 Capability Registry

Contains task handlers, commit validators, and declarative task bindings. Each
entry has one owner, one kind, exact implementation identity, and any frozen
binding data. A capability cannot occupy multiple kinds.

The Phase 2 canonical task interface is:

```python
class TaskHandler(Protocol):
    async def execute(
        self,
        request: TaskRequest,
        context: TaskContext,
    ) -> TaskOutcome: ...
```

`TaskRequest` remains business-neutral and adds only generic resolved binding,
resource-digest, and prior typed failure data. `TaskContext` exposes the exact
attempt workspace, heartbeat/cancellation, immutable invocation metadata, and
effect-intent value construction. It does not allow immediate external side
effects.

`TaskFailure` gains an engine-level `retryable` flag. Plugin failures default to
their explicitly returned value; engine-generated permanent effect failures set
it to false. The planner never schedules another task attempt for a
non-retryable failure, regardless of the node's remaining numeric retry budget.

Commit validators retain their generic candidate-write-set interface. Engine
invariants always run first. A graph task explicitly selects and orders any
additional validator IDs; descriptors cannot install implicit global
validators.

### 9.3 Schema Registry

Contains strict schema resources, schema media type, owning plugin, bytes
digest, and schema dialect/version. The engine treats schemas as data and uses
registered generic validators; business model classes remain outside the
engine.

### 9.4 Resource Registry

Contains immutable opaque resources such as graphs, skills, prompts, roles,
contracts, and policy data. Each entry records owner, media type, canonical
path or logical name, bytes, and digest. The engine does not interpret business
meaning beyond closed structural graph/config formats.

### 9.5 Effect Registry

Contains each effect kind's owning plugin, intent schema ID, receipt schema ID,
trusted handler, implementation digest, and generic timeout/retry/backoff
policy. A declarative plugin may reference an existing effect kind but cannot
provide its executable implementation.

Registry kinds are an engine API closed set. A plugin cannot introduce a sixth
kind or custom registry lifecycle.

## 10. Registry construction and compilation

`RegistryPlatform.resolve()` performs these operations in order:

1. strictly parse the resolution request and product source;
2. capture every explicit source snapshot;
3. validate product identity, engine API, and complete source set;
4. validate exact dependency constraints and canonical topology;
5. load wheel providers from already captured sources;
6. compare provider descriptors with selected source identities;
7. obtain and freeze contributions;
8. parse config-tree contributions from frozen bytes;
9. build all five registries and reject every collision;
10. resolve aliases and validate all cross-registry references;
11. validate namespaced product configuration;
12. compile every product graph and entrypoint against the frozen registries;
13. compute canonical registry, configuration, and compiled-workflow digests;
14. construct canonical `InvocationLock` bytes; and
15. return `FrozenComposition`.

Any error returns no composition and creates no invocation root or ledger.

## 11. FrozenComposition and InvocationLock

`FrozenComposition` contains:

- the normalized effective product manifest;
- immutable views of all five registries;
- the compiled workflow and public entrypoints;
- validated product/plugin configuration;
- bound trusted implementations and frozen declarative bytes;
- canonical `InvocationLock` bytes and digest; and
- one overall composition digest.

`InvocationLock` contains at least:

- lock schema version and source-digest algorithm ID;
- engine API version and engine implementation digest;
- canonical effective product manifest and product source digest;
- every selected plugin ID, exact version, source identity, and source digest;
- the exact canonical dependency graph;
- canonical projections and digests of all five registries;
- all referenced graph, schema, skill, prompt, contract, policy, and other
  resource digests;
- validated namespaced configuration and digest;
- capability alias/binding table and digest; and
- compiled workflow and entrypoint digest.

Lock encoding uses the engine canonical JSON encoder. Map ordering, tuple/list
representation, number handling, and Unicode normalization are part of the lock
schema and covered by golden tests.

### 11.1 Start

`Engine.start()` atomically installs the immutable lock before writing the
first runtime event. File data and parent-directory durability are required.
The bootstrap `InvocationStarted` event contains the exact lock digest.

A crash after lock installation but before ledger bootstrap leaves a
recoverable initialization state. A repeated `start()` with the exact same
composition may complete the one canonical bootstrap. A different composition
raises `InvocationDrift` and writes nothing.

### 11.2 Resume/open

The caller re-resolves the same explicit sources and passes the resulting
composition to `Engine.open()`. Open requires byte-for-byte canonical lock
equality and agreement between the lock digest and ledger bootstrap before it
claims or advances the invocation.

Any engine, product, code, dependency, configuration, schema, resource,
binding, or compiled-graph difference raises `InvocationDrift`. The engine does
not repair, upgrade, hot-reload, overwrite the lock, or append a drift event.
New inputs require a new invocation.

A missing or corrupt lock for an existing ledger is corruption and fails
closed. A valid lock with no ledger follows only the idempotent initialization
path described above.

## 12. Durable-effect interface

The effect interface remains generic:

```python
class DurableEffectHandler(Protocol):
    async def apply(
        self,
        intent: EffectIntent,
        idempotency_key: str,
    ) -> EffectApplyResult: ...

    async def reconcile(
        self,
        intent: EffectIntent,
        idempotency_key: str,
    ) -> EffectReconcileResult: ...
```

`EffectReconcileResult` has a closed state set:

- `not_applied`: the engine may call `apply` with the same key;
- `pending`: outcome is still unknown; the engine must retry reconcile only;
- `applied`: includes a schema-valid receipt;
- `permanently_failed`: includes a typed failure.

Arbitrary handler exceptions never prove that an effect was not applied. An
exception after an apply attempt is therefore ambiguous and enters reconcile.

`TaskOutcome` may contain ordered `EffectIntent` values only when its task
status is successful. Failed or stopped outcomes cannot contain intents.
Intent kinds and payloads are validated against the frozen Effect and Schema
registries before workspace publication.

## 13. Durable-effect persistence and state machine

### 13.1 Stable identity

For each task attempt, the engine assigns effect IDs from invocation ID,
activation ID, task attempt number, and declaration index. The idempotency key
is the canonical digest of lock digest, effect ID, effect kind, and canonical
payload digest. It remains identical across crashes and reconcile calls.

Effects from one task settle strictly in declaration order. Phase 2 also
settles effect-pending task attempts in canonical task/effect ID order. It does
not execute effects concurrently.

### 13.2 Prepare commit

After a handler returns a nominal success, the engine:

1. validates output, candidate write-set, validators, effect kinds, and intent
   schemas;
2. commits the candidate workspace through the authenticated HEAD transaction
   journal; and
3. CAS-appends one authoritative ledger batch containing
   `TaskCommitPrepared`, `HeadAdvanced`, and every ordered
   `EffectIntentCommitted` event.

The attempt then has `effect_pending` status. Its output is persisted in
`TaskCommitPrepared`, but no `TaskAttemptSucceeded`, `NodeCompleted`, or
downstream token exists.

The existing HEAD transaction journal is extended so crashes and ambiguous
ledger outcomes reconcile the exact prepared batch without overwriting a newer
HEAD. A task with no effects may include `TaskAttemptSucceeded` in the same
authoritative prepared publication.

### 13.3 Apply and receipt

Before invoking an effect, the engine persists `EffectApplyStarted` with the
effect ID and apply-attempt number. It then calls `apply` using the stable key.

- A definite applied result is schema-validated and persisted as
  `EffectReceiptRecorded`.
- A definite transient result follows the Effect Registry's generic retry,
  timeout, and backoff policy using the same key.
- An exception, process death, timeout with unknown remote outcome, or
  ambiguous receipt append enters reconcile; the engine never blindly calls
  apply again.
- `reconcile(applied)` records or authenticates the receipt.
- `reconcile(not_applied)` permits another apply with the same key.
- `reconcile(pending)` schedules another reconcile.
- A permanent result or exhausted generic policy records a typed,
  non-retryable effect failure.

After all ordered receipts are durable, the engine appends
`TaskAttemptSucceeded`; normal node completion and downstream planning then
continue.

### 13.4 Permanent effect failure

A permanent effect failure publishes a typed, non-retryable
`TaskAttemptFailed`. The engine does not rerun the original TaskHandler and does
not create a new idempotency key. The committed workspace, committed intents,
and any earlier receipts remain immutable audit facts. Downstream nodes do not
start, and normal structural failure propagation terminates the affected graph
and invocation.

This is intentionally different from a precommit validator rejection, which
discards the candidate and leaves HEAD unchanged.

### 13.5 Crash matrix

Recovery must cover at least these cuts:

- before the prepared batch: no effect may be invoked;
- after HEAD installation but before the prepared ledger batch: the HEAD
  journal authenticates, completes, or conditionally rolls back publication;
- after intents are committed but before apply: recovery applies the first
  pending intent;
- after `EffectApplyStarted` but before/during/after apply: recovery reconciles
  before any further apply;
- after external apply but before receipt: recovery reconciles using the same
  key;
- during an ambiguous receipt append: recovery authenticates the exact expected
  event range before deciding;
- after receipt but before the next effect: recovery starts the next ordered
  effect;
- after all receipts but before task success: recovery publishes success
  without calling any effect; and
- after task success: normal replay performs no effect work.

## 14. Failure model

Composition failures occur before the first ledger event:

- `SourceResolutionError`: missing/ambiguous source, unsafe path, unstable
  snapshot, corrupt wheel metadata, or entry-point mismatch;
- `DependencyConflict`: missing/extra dependency, incompatible exact version,
  or cycle;
- `RegistryConflict`: duplicate ID, kind conflict, undeclared implementation,
  or invalid alias;
- `DeclarativePluginRejected`: schema violation, executable form, undeclared
  file, unsafe media/path/mode, or unbound reference;
- `CompositionInvalid`: unresolved graph/config/schema/resource reference or
  invalid compiled workflow; and
- `InvocationDrift`: canonical lock inequality or lock/ledger digest mismatch.

Runtime failures remain typed:

- handler exception or invalid output becomes a task failure;
- validator rejection discards the candidate and fails the task;
- transient/ambiguous effects remain pending and reconcile;
- permanent effects become non-retryable task failures; and
- ledger, HEAD, lock, or receipt publication that cannot be proven present or
  absent raises an explicit indeterminate engine error.

No failure selects a different plugin, source, version, graph, or Assurance
fallback.

## 15. Trust and security

Python wheel providers are explicitly installed trusted code. Phase 2 does not
claim that in-process interfaces contain a malicious wheel. It does guarantee
explicit selection, source identity and digest pinning, contribution
conformance, immutable registries, candidate validation, and auditable effect
protocols.

Declarative files are untrusted data. Their parsers are closed, non-executable,
path-confined, media-type constrained, and snapshot before use. A config tree
can bind data to a trusted existing capability; it cannot name an importable
function, create a command, or install code.

The runtime host-isolation requirement from Phase 1 remains unchanged: a
production `TaskExecutionHost` must confine task code to the exact attempt
workspace and protect engine store namespaces.

## 16. Conformance suites

### 16.1 Wheel-plugin conformance

One shared suite proves:

- descriptor and contribution determinism across repeated calls and fresh
  processes;
- no import-time global registration;
- exact descriptor/contribution ID agreement;
- qualified unique IDs and valid PEP 440/engine constraints;
- complete dependency declarations;
- frozen JSON-compatible task input/output and binding data;
- capability, schema, resource, and effect references close exactly;
- effect intent/receipt schema validation;
- paired apply/reconcile implementations for every effect kind; and
- source mutation changes the source and composition digests.

### 16.2 Declarative-plugin conformance

One shared suite proves:

- strict extra-forbidden `plugin.yaml` parsing;
- no executable fields or executable files;
- exact file inventory, safe paths, safe media types, and no symlinks;
- deterministic config-tree digest;
- graph/schema/resource/config references close exactly;
- aliases bind only selected wheel capabilities; and
- no declarative contribution creates an executable implementation.

### 16.3 Registry-platform conformance

Tests cover canonical output independent of request ordering, exact source-set
closure, PEP 440 validation without selection, cycle and conflict rejection,
immutable registry views, descriptor/contribution disagreement, alias cycles,
and cross-registry dangling references.

### 16.4 Lock conformance

Golden canonical bytes cover normalization and ordering. Separate drift tests
change exactly one of engine bytes, product bytes, plugin code, plugin version,
config bytes, dependency edges, schema bytes, resource bytes, binding data,
validated configuration, graph bytes, compiled digest, and entrypoints. Every
change must reject open without appending a ledger event.

### 16.5 Effect conformance

The Toy effect adapter records externally observable applications by
idempotency key. Fault injection covers every cut in section 13.5, transient
and permanent results, ambiguous apply, ambiguous receipt publication,
multiple ordered effects, invalid intents/receipts, and process reopen. Tests
prove one observable application per key and no downstream event before all
receipts.

## 17. Phase 2 acceptance

Phase 2 is complete only when all of the following are true:

- one wheel-only Toy composition and one mixed wheel plus declarative-config
  composition resolve, compile, run, crash, reopen, and finish;
- the mixed composition proves data binding to an existing wheel handler
  without executable config;
- at least one Toy task emits multiple ordered durable effects and passes the
  complete crash matrix with exactly-once observable outcomes;
- every lock facet has an isolated drift rejection test;
- the engine wheel still contains no Assurance, OpenCode, Cursor, default
  graph, default plugin, or default product;
- engine-only, wheel-only Toy, and mixed-source Toy wheels/installations pass in
  isolated offline environments;
- Ruff, format, Pyright, import-linter, all graph-engine tests, architecture
  tests, wheel smoke, and fault-injection tests pass; and
- full-repository testing introduces no failure beyond the independently known
  four missing ignored benchmark-scaffold fixtures in
  `tests/unit/benchmark/test_opencode_openai_loop.py`.

Completing these criteria does **not** mean that `aa` runs on the new engine.
It means the generic plugin, frozen-composition, and durable-effect mechanisms
needed by Phase 3 and Phase 4 are production-ready. The project becomes a pure
graph-engine product only after agent adapters, Assurance capabilities, product
assembly, behavioral comparison, hard cut, and old-runtime deletion in Phases
3 through 6.

## 18. Implementation boundary

The Phase 2 implementation plan must modify only the graph-engine package,
Phase 1 Toy packages, graph-engine architecture tests, isolated smoke scripts,
workspace packaging metadata, and this phase's test/evidence files. It must not
move or import real `assurance_agent`, `assurance_kernel`, OpenCode, or Cursor
implementation code.

The plan should be divided into independently reviewed tracer tasks for frozen
models, source snapshots, dependency validation, typed registries, resolver and
lock, wheel/config conformance, effect models/events/fold, effect scheduler and
recovery, Toy integrations, and final isolation/fault gates. A later phase
cannot begin until the Phase 2 wheel, import, conformance, drift, and crash
evidence is reviewed and accepted.
