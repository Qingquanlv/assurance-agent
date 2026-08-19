# Pure Graph Engine and Plugin Architecture

**Status:** approved in conversation on 2026-08-20

## Problem

The current `assurance-kernel` distribution is physically separate from the
`assurance-agent` product, but it is not a business-neutral graph engine. It
still contains Assurance artifact models, evidence rules, verification rules,
knowledge models, conformance checks, model routing, and business-shaped
`ProductHooks` for healing, issues, improvements, and retro behavior. The
current package split prevents a direct kernel-to-product import, but it does
not provide a clean product/plugin seam.

The desired end state is stricter:

- the graph engine contains no default graph and no Assurance business meaning;
- a product is only a frozen composition of graphs, plugins, configuration, and
  entrypoints;
- executable behavior comes from explicitly installed and trusted Python wheel
  plugins;
- declarative plugins may be loaded dynamically from explicitly listed
  configuration paths, but cannot contain executable Python or shell commands;
- operations, skills, validators, schemas, effects, adapters, and business
  models belong to capability plugins rather than the engine;
- an invocation never hot-reloads its product or plugins; and
- the migration is a hard cut with no compatibility import shims and no resume
  compatibility for invocations created by the old runtime.

## Goals

- Create a deep `graph-engine` module whose small interface hides graph
  compilation, scheduling, persistence, recovery, concurrency, and plugin
  dispatch.
- Make the product registration hook return immutable data instead of mutating
  process-global registries.
- Support capability-bundle plugins delivered as Python wheels, declarative
  configuration, or both.
- Give Python operations and agent-backed skills one engine-level execution
  interface.
- Resolve, validate, compile, and pin the complete product/plugin bundle before
  the first runtime event is written.
- Preserve deterministic replay, bounded retries, durable effects, workspace
  commit safety, subgraphs, joins, interrupts, leases, and resource conflict
  control without embedding product semantics.
- Make plugin ownership and dependency direction mechanically enforceable.

## Non-goals

- No backward-compatible `assurance_kernel.*` or `assurance_agent.workflow.*`
  shims for the new engine.
- No resume path for an invocation created by the old runtime.
- No automatic plugin installation from PyPI or another network source.
- No implicit scanning of `.aa/plugins`, the current directory, environment
  variables, or all installed entry points to alter a product.
- No executable code in declarative configuration.
- No process or container sandbox for Python wheel plugins. Installed wheel
  plugins are trusted code.
- No out-of-process or cross-language plugin protocol in this version.
- No attempt to preserve internal event identifiers byte-for-byte across the
  hard cut.

## Design principles

1. **Mechanism stays in the engine; policy stays in plugins.** Scheduling a
   retry is mechanism. Deciding that a coverage gap requires case expansion is
   policy.
2. **One product hook, several generic plugin interfaces.** The engine must not
   grow `healing_*`, `issue_*`, or similarly business-named hooks.
3. **Explicit composition.** A product's effective plugin set is fully listed
   by its manifest and explicit CLI additions. Discovery may locate a requested
   package, but it may not silently enable one.
4. **Freeze before execution.** Product, code, configuration, capability table,
   schemas, and compiled graphs are pinned as one invocation bundle.
5. **Conflict is an error, not precedence.** Duplicate plugin IDs, capability
   IDs, artifact types, effect kinds, or entrypoints fail product resolution.
6. **Capability bundles are deep modules.** A plugin owns a coherent business
   capability and may provide multiple handlers, skills, schemas, validators,
   and effects. It is not split into one wheel per function.
7. **No global registration side effects.** Importing a product or plugin may
   not modify a global registry. Resolution is explicit and invocation-scoped.

## Distribution architecture

### `graph-engine`

Distribution name: `graph-engine`. Import package: `graph_engine`.

It contains only:

- structural graph schema and deterministic compiler;
- planner, scheduler, resource conflict selection, retry, timeout, and leases;
- ledger events, pure ledger fold, checkpointing, replay, and crash recovery;
- generic structural nodes: task, subgraph, join, gate expression, interrupt,
  and end;
- invocation-scoped capability registry and handler dispatch;
- task workspace, candidate write-set, atomic commit, and generic commit
  validation pipeline;
- durable effect intent/receipt/idempotency mechanism;
- product and plugin manifest parsing, dependency resolution, and pinning;
- generic JSON-compatible task input/output and typed engine errors; and
- engine-owned descriptor schemas and conformance tests.

It contains none of the following:

- a default graph, default operation, default skill, or default product;
- Assurance artifact, case, test, coverage, healing, issue, improvement, or
  retro models;
- OpenCode, Cursor, model provider, prompt, persona, or model-routing logic;
- Assurance conformance, plan review, precommit, or evidence rules;
- business artifact registries or business durable-effect implementations; or
- business-specific product hooks.

The engine wheel must be useful with only a toy product and toy plugin. It must
not require `assurance-agent` or any Assurance plugin to compile and run a graph.

### Repository layout

The monorepo uses one directory per independently buildable distribution:

```text
packages/
  graph-engine/
  agent-runtime-opencode/
  agent-runtime-cursor/
  assurance-intake/
  assurance-generation/
  assurance-execution/
  assurance-healing/
  assurance-quality/
  assurance-improvement/
  assurance-product/
examples/
  graph-engine-toy-a/
  graph-engine-toy-b/
```

No distribution shares an import package directory with another distribution.
Tests may use workspace editable installs, but wheel-isolation tests build and
install each declared combination into a clean environment.

### Agent runtime plugins

Provider adapters are plugins, not engine features. Initial distributions are:

- `agent-runtime-opencode`, providing a capability such as
  `agent.opencode.execute`; and
- `agent-runtime-cursor`, providing a capability such as
  `agent.cursor.execute`.

They satisfy the same task-handler contract. Skills bind to one of these
capabilities through declarative configuration. The graph engine does not know
that the handler invokes an LLM, how models are selected, or how sessions are
managed.

### Assurance capability plugins

The initial capability bundles are:

- `assurance-intake`: explore, case authoring, case review, and their artifacts;
- `assurance-generation`: API/E2E/Fuzz/Performance planning, review, and codegen;
- `assurance-execution`: test selection, execution, closed mappings, and raw
  execution evidence;
- `assurance-healing`: diagnosis, proposal, fix safety, healing effects, and
  repair status;
- `assurance-quality`: trace, issue analysis, coverage, metrics, inspection, and
  report generation; and
- `assurance-improvement`: retro, improvement review, and delivery.

These are initial ownership seams, not permission for circular dependencies.
Each plugin declares its dependencies and owns all of its operation code,
skills, business schemas, artifact types, validators, and effect handlers.

### `assurance-product`

The product distribution owns:

- the Assurance `ProductManifest` provider;
- the list and version constraints of enabled plugins;
- product-level graph entrypoints and graph configuration;
- product policies and plugin configuration values; and
- the `aa` CLI composition root.

It does not re-export engine internals and does not contain duplicate
implementations copied from plugins.

## Product registration interface

The engine exposes one product seam:

```python
class ProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...
```

Python products are located through the entry-point group
`graph_engine.products`. Declarative products may be loaded from an explicit
`--product-file`. Both forms produce the same frozen `ProductManifest` model.

`ProductManifest` contains at least:

- `product_id` and `product_version`;
- an engine API version constraint;
- an ordered list of explicit plugin requirements;
- public entrypoint names mapped to graph IDs;
- product configuration values namespaced by plugin; and
- optional, explicit CLI-supplied configuration plugin paths incorporated into
  the effective manifest before hashing.

Calling `manifest()` has no registration side effects. The engine resolves the
returned data into a new invocation-scoped registry. There is no
`current_product_hooks()` process global.

## Plugin sources

### Python wheel plugin

A Python wheel exposes a provider through `graph_engine.plugins`:

```python
class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...
    def bind(self, ports: EnginePorts) -> PluginRuntime: ...
```

`descriptor()` is deterministic and side-effect free. `bind()` returns only the
implementations declared by the descriptor. It may use the stable, generic
ports supplied by the engine; it may not access an engine global registry.

Wheel entry-point discovery is inventory only. The resolver loads only the
distribution and entry-point ID explicitly selected by the effective product
manifest. Merely installing a wheel cannot alter an invocation.

### Declarative configuration plugin

A configuration plugin is a directory rooted at `plugin.yaml`. It may contain:

- graph definitions;
- skills, prompts, and role instructions;
- JSON Schema or equivalent data contracts;
- execution contracts and resource declarations;
- bindings from product capability IDs to wheel-provided handler IDs; and
- plugin configuration schemas and defaults.

It may not contain or declare:

- Python module/function imports;
- shell commands or arbitrary command arrays;
- executable scripts;
- dynamic template expressions that execute host language code; or
- network installation instructions.

Unknown executable-looking fields are rejected rather than ignored. A
configuration plugin can bind `assurance.api.plan` to a trusted handler such as
`agent.opencode.execute`, supplying skill and output-schema resource IDs as
data. It cannot create a new executable handler.

Configuration plugin paths are accepted only when explicitly listed in the
product manifest or supplied explicitly by the CLI. Relative paths resolve
against the manifest source directory. The resolver does not scan ambient
directories or environment variables.

## Plugin descriptor and identifiers

Every `PluginDescriptor` declares:

- `plugin_id`, semantic `plugin_version`, and engine API constraint;
- required plugins and compatible version ranges;
- provided task-handler IDs;
- provided commit-validator IDs;
- provided durable-effect kinds;
- provided resource IDs and their media types/digests; and
- declarative bindings and configuration schema IDs, if any.

All IDs are globally qualified with the owning plugin namespace. Plugins may
not replace another plugin's capability through load order. Aliases are allowed
only when declared as a new capability owned by the aliasing plugin and bound to
an existing handler.

Resolution is deterministic:

1. validate product identity and engine API compatibility;
2. resolve only explicitly requested wheel and configuration sources;
3. validate descriptors and canonical IDs;
4. build and topologically sort the plugin dependency graph;
5. reject missing dependencies, dependency cycles, duplicate IDs, and version
   conflicts;
6. bind wheel implementations and declarative resources;
7. validate every graph capability/resource reference against the assembled
   registry; and
8. compile entrypoint graphs and freeze the invocation bundle.

There is no last-wins merge rule.

## Generic plugin interfaces

### Task handler

Operations and agent-backed skills share one interface:

```python
class TaskHandler(Protocol):
    async def execute(
        self,
        request: TaskRequest,
        context: TaskContext,
    ) -> TaskOutcome: ...
```

`TaskRequest` contains only engine concepts: invocation/task identity,
capability ID, JSON-compatible input, frozen evidence bindings, resource claims,
attempt metadata, and previous typed failure feedback.

`TaskContext` exposes generic ports for the confined task workspace,
heartbeat/cancellation, read-only invocation metadata, and durable-effect
intent creation. It does not expose business repositories or product hooks.

`TaskOutcome` contains a typed status, JSON-compatible outputs, typed failure
information, and durable-effect intents. An arbitrary exception never crosses
the interface; the engine converts it to a typed plugin task failure while
retaining diagnostic evidence.

The graph schema no longer distinguishes `operation:*` from `skill:*`. A task
node references a capability ID. Whether that capability executes local Python,
an agent session, or another trusted adapter is private to its plugin.

### Commit validator

```python
class CommitValidator(Protocol):
    def validate(
        self,
        candidate: CandidateWriteSet,
        context: ValidationContext,
    ) -> ValidationResult: ...
```

The engine owns validator ordering, invocation, receipts, and atomic commit.
Engine invariants, such as declared resource/write-set confinement, always run
and cannot be disabled by a product. Assurance checks for cases, mappings,
tests, healing safety, or evidence belong to Assurance plugins.

A plugin descriptor only makes a business validator available. A task node is
the sole place that selects and orders additional validator IDs. No product
validator is enabled implicitly, and a descriptor cannot silently attach one
to every task.

### Durable effect handler

```python
class DurableEffectHandler(Protocol):
    async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectReceipt: ...
    async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectState: ...
```

The engine validates and commits the intent before invoking the effect. Pending
effects are reconciled on recovery. Effect kind schemas and implementations are
plugin-owned; intent persistence, idempotency keys, retry scheduling, and
receipts are engine-owned.

### Resources and business data

The engine stores and hashes opaque bytes and JSON-compatible values. A plugin
may register JSON Schemas and media types for its resources/artifacts. Business
Pydantic models, registries, semantic validation, evidence projection, and
knowledge logic remain in the owning plugin.

## Graph semantics

The fixed graph language contains structural semantics only:

- `task`: dispatch one registered capability;
- `subgraph`: invoke another compiled graph;
- `join`: wait for structurally declared predecessors;
- `gate`: evaluate a bounded expression over frozen graph state and task
  outputs;
- `interrupt`: persist a generic human/external decision checkpoint; and
- `end`: finish with a typed terminal result.

Retry, timeout, fan-out, budget, resource claims, routing, and state reducers are
generic graph features. A gate may read only frozen state/outputs presented to
it by the graph. Reading and interpreting a business artifact is a plugin task,
not hidden engine behavior.

The engine has no packaged graph. Starting without a selected product fails
with a product-not-selected error. A selected product with no valid entrypoint
fails compilation.

## Invocation bootstrap and pinning

Before the first runtime ledger event, the engine creates an `InvocationLock`
that includes:

- engine API and implementation version;
- effective product manifest bytes and digest;
- selected plugin IDs, versions, source identities, and source digests;
- wheel distribution versions and installed `RECORD`/code digest;
- configuration plugin tree digests;
- resolved dependency graph and capability table digest;
- all referenced graph, skill, prompt, schema, contract, and policy digests;
- compiled graph digest; and
- validated product configuration digest.

Editable wheel installs use a deterministic digest of the explicitly declared
plugin source files; production wheel installs use installed distribution
metadata and file hashes. The source-digest algorithm is part of the engine API
and is itself versioned.

The lock is persisted before scheduling begins. Resume reloads the explicitly
selected sources and requires an exact lock match. A mismatch produces
`InvocationDrift`; the engine does not repair, upgrade, or hot-reload the
running invocation. New product or plugin configuration affects only a new
invocation.

## Failure model

- Duplicate capabilities, missing dependencies, cycles, version conflicts, or
  engine API mismatch fail product resolution and create no invocation.
- Executable or unknown executable-looking fields in a declarative plugin fail
  configuration loading.
- Graph references to unknown capabilities, validators, effects, resources, or
  schemas fail compilation before a runtime ledger is created.
- A handler exception or invalid output becomes a typed task failure and follows
  the graph's declared retry/recovery policy.
- A commit-validator rejection discards the candidate write-set and fails the
  task without modifying the committed workspace.
- A temporary durable-effect failure leaves a committed pending intent and is
  retried/reconciled using its idempotency key.
- Invocation lock drift rejects resume.
- An engine internal exception preserves the last complete checkpoint and must
  not expose a partial write-set or partially folded event batch.

The engine never substitutes an Assurance-specific fallback when a plugin is
missing or fails.

## Trust and security model

Python wheel plugins are explicitly installed trusted code. An in-process
Python plugin can bypass cooperative interfaces, so this architecture does not
claim to contain a malicious wheel. Trust is managed through explicit product
selection, package review, version/digest pinning, SPI conformance, write-set
validation, and audit evidence.

Declarative configuration is treated as untrusted data. It is parsed by strict,
closed schemas; cannot name importable Python functions or arbitrary commands;
and is frozen before execution.

If hostile-code containment becomes a requirement, it is a separate future
design for out-of-process plugin workers. It is not silently approximated in
this in-process design.

## Migration strategy

The migration builds the new system beside the old runtime, then performs one
hard cut. Side-by-side development is not compatibility: no new code imports
the old runtime through shims, and no invocation crosses the cut.

This architecture is too large for one implementation plan. Each phase below
is a separately specified, planned, reviewed, and accepted project. Work starts
with Phase 1 only; a later phase cannot begin until the previous phase's wheel,
import-boundary, conformance, and fault-injection gates pass. The hard-cut phase
is never bundled into an earlier extraction task merely to shorten the schedule.

### Phase 1: Pure engine foundation

- Create the independent `graph-engine` workspace distribution.
- Port only demonstrably generic graph schema, compiler, planner, scheduler,
  ledger, workspace, lease, checkpoint, and runtime mechanisms.
- Remove or replace business-shaped conditions while porting; do not rename the
  existing `assurance-kernel` wholesale.
- Add two toy products and plugins with materially different graphs to prove the
  product/plugin seam is real.

### Phase 2: Plugin SPI and frozen composition

- Implement product/plugin descriptors, explicit source resolution, dependency
  solving, invocation-scoped registry, source hashing, and `InvocationLock`.
- Add plugin conformance tests and import-layering firewalls.
- Implement task-handler, commit-validator, durable-effect, and resource
  interfaces.

### Phase 3: Agent runtime plugins

- Move OpenCode and Cursor execution behind the common task-handler contract.
- Move model routing, prompts, personas, session behavior, and provider-specific
  logic out of the engine.
- Prove the same declarative skill can be rebound to either adapter without an
  engine change.

### Phase 4: Assurance capability extraction

- Extract the six Assurance capability bundles.
- Move their operations, skills, business models, artifact schemas, validators,
  durable effects, and resources together.
- Replace current business-shaped `ProductHooks` with the generic plugin
  interfaces; do not recreate them as a single catch-all plugin hook.
- Enforce acyclic plugin dependencies through the descriptor graph and import
  contracts.

### Phase 5: Product assembly and behavioral comparison

- Build `assurance-product` from explicit plugin requirements and declarative
  product graphs.
- Compare representative workflows on terminal result, committed business
  artifacts, gate decisions, and externally visible effects.
- Internal event IDs need not match the old runtime, but the new runtime must
  satisfy its own deterministic replay and crash-recovery invariants.

### Phase 6: Hard cut and deletion

- Drain or explicitly terminate all old-runtime invocations and retain an audit
  record of that decision.
- Switch `aa` to `graph-engine` and the new Assurance product manifest.
- In the same cutover change, delete the old `assurance-kernel` runtime and
  duplicate product implementations.
- Do not add compatibility imports or old-invocation resume adapters.

## Current-code ownership map

The current `assurance-kernel` contains roughly 69,000 Python lines and cannot
become the new engine through a package rename. Candidate generic mechanisms
must be extracted selectively from current graph/core modules, including the
compiler, planner, scheduler, runtime, ledger fold, checkpoint, leases, project
locks, workspace, and generic event/state machinery.

The following current areas are product/plugin code and must not enter the pure
engine as-is:

- `artifacts/`, `evidence/`, `knowledge/`, and `verification/` business models
  and rules;
- Assurance orchestration, gate semantics, plan checks, artifact interpretation,
  and decision policy;
- `assurance_conformance`, `healing_conformance`, Assurance invariants,
  codegen manifests, reviewer checks, personas, and model routing;
- business precommit validators and durable-effect implementations;
- driver adapters for OpenCode/Cursor and agent-specific behavior; and
- the current business-shaped `ProductHooks` dataclass.

Generic pipelines may be reimplemented in the engine, but their Assurance
implementations and policies remain in plugins.

## Verification strategy

### Engine tests

- Engine-only installation with a toy product can compile, execute, stop,
  interrupt, retry, recover from a crash, and resume.
- A second toy product with different capabilities and topology runs without an
  engine change.
- Fault injection covers atomic event batches, workspace commit, pending
  durable effects, leases, subgraphs, fan-out, and replay determinism.
- The engine source and wheel contain no default graph, skills, prompts,
  Assurance schemas, or Assurance business modules.
- Import contracts forbid `graph_engine` from importing `assurance_*`,
  `agent_runtime_*`, or any product/plugin package.

### Plugin conformance tests

Every Python plugin must pass one shared suite proving:

- deterministic, side-effect-free descriptors;
- no import-time global registration;
- exact agreement between declared and bound implementations;
- globally qualified, unique capability/effect/validator IDs;
- valid dependency and engine API constraints;
- JSON-compatible task inputs and outputs;
- typed handling of exceptions and invalid outputs; and
- idempotent durable-effect reconciliation where effects are provided.

Every declarative plugin must pass strict-schema tests proving it contains no
executable forms, all bindings resolve, and its tree digest is stable.

### Product and cutover tests

- Every Assurance graph reference resolves only through its declared plugins.
- OpenCode and Cursor pass the same agent-runtime contract suite.
- Wheel-isolation tests install the engine alone, engine plus toy products, and
  the full Assurance product in separate environments.
- Representative end-to-end workflows preserve terminal results, business
  artifacts, safety decisions, and external effects.
- At least one complete single-item benchmark passes through OpenCode and one
  through Cursor before cutover.
- Ruff, formatting, type checking, import layering, full unit/integration tests,
  packaging smoke tests, and crash/replay tests pass.

## Acceptance criteria

- `graph-engine` can be installed and exercised without any Assurance package.
- The engine has no default graph and refuses execution without a selected,
  valid product and entrypoint.
- The only product seam returns `ProductManifest`; there are no business-named
  engine hooks.
- All operations and agent-backed skills resolve through plugin-owned
  capabilities and the generic `TaskHandler` interface.
- All configuration plugin sources are explicit, non-executable, strict, and
  included in `InvocationLock`.
- An invocation rejects any product, plugin, configuration, capability-table,
  or compiled-graph drift on resume.
- Assurance business models, validators, effects, skills, adapters, and graphs
  live outside the engine wheel.
- Plugin dependency and import graphs are acyclic and mechanically checked.
- The hard cut removes the old runtime and duplicate implementations without
  compatibility shims.
