# Pure Graph Engine Phase 5: Assurance Product Assembly and Behavioral Comparison

**Date:** 2026-08-22

**Status:** Proposed design; implementation not started

**Depends on:** Phase 1 pure graph engine, Phase 2 registry platform and frozen composition, Phase 3 agent-runtime adapter work, Phase 4 six-wheel Assurance capability extraction

**Feeds:** Phase 6 hard cut, old-invocation disposition, legacy-runtime deletion, and `aa` default switch

## 1. Decision summary

Phase 5 creates one installed `assurance-product` distribution that assembles the six Phase 4 capability wheels into a complete product without moving business implementation back into the product package.

The product distribution exposes two explicit `graph_engine.products` entry points:

- `assurance-opencode`, which selects `runtime.opencode`; and
- `assurance-cursor`, which selects `runtime.cursor`.

Both entry points use the same canonical Assurance workflow and the same six capability wheels. They differ only in their authenticated product source identity, selected runtime adapter, deployment binding wheel, and resulting invocation lock.

Phase 5 also adds a temporary parallel command, `aa-next`. It resolves one exact product entry point, one explicit installed deployment binding wheel, one explicit data-only project configuration tree, the exact installed capability/adapter sources, and one production task host. It imports a stable SUT snapshot into a new invocation, runs or resumes that invocation, and materializes the final engine HEAD into a separate result tree. It does not replace, wrap, call, or alter the current `aa` runtime.

The old and new products run independently against cloned initial workspaces. A comparison harness evaluates semantic results: terminal status, committed business artifacts, gate decisions, durable effects, coverage behavior, reports, Retro/Improvement behavior, and externally visible outputs. It never requires event IDs, provider transcripts, or internal file layouts to be identical.

Phase 5 is not merely a product YAML task. Current prototype facts make four generic increments mandatory before a real Assurance product can run:

1. a canonical invocation root input and authenticated initial workspace seed;
2. closed declarative task-input projection for product graph data flow;
3. a production, lock-pinned `TaskExecutionHost` plus exact secret authorization; and
4. installed adapter configuration and a real confined Cursor process host.

These increments remain business-neutral and live in `graph-engine` or the owning Phase 3 adapter. They do not become Assurance hooks.

Phase 5 does **not** switch the default `aa` command, resume a legacy invocation in the new engine, or delete legacy code. Those actions remain one explicit Phase 6 hard cut.

## 2. Current facts and blocking gaps

### 2.1 What Phase 4 supplies

Phase 4 supplies six authenticated wheels:

- `assurance-intake==0.1.0`;
- `assurance-generation==0.1.0`;
- `assurance-execution==0.1.0`;
- `assurance-healing==0.1.0`;
- `assurance-quality==0.1.0`; and
- `assurance-improvement==0.1.0`.

They expose complete task-handler, validator, schema, resource, effect, and business-contract contributions. Their wheel and descriptor dependency graph is acyclic. They do not import `assurance_agent`, `assurance_kernel`, or `ProductHooks`.

Every agent-backed business capability is split into provider-neutral prepare and finalize handlers. The Phase 4 handoff enumerates 33 prepare IDs requiring Phase 5 composition.

The wheels intentionally contribute no production capability bindings, no product manifest, and no full graph.

### 2.2 What Phase 3 supplies

Phase 3 supplies:

- frozen `AgentRunRequest` and `AgentRunResult` values;
- `runtime.opencode.execute`;
- `runtime.cursor.execute`;
- generic recoverable task-activity events and fold;
- reconcile-before-reclaim behavior;
- opaque provider references;
- terminal receipt models; and
- deterministic adapter fault matrices.

OpenCode remains authoritative for session history, messages, reasoning, tools, model history, token use, and cost. The graph ledger stores only generic activity identity, bounded evidence, canonical results, and recovery authority.

### 2.3 Phase 3 residuals are Phase 5 blockers

The accepted Phase 3 review records these unresolved production facts:

- no production `TaskExecutionHost` exists;
- installed adapter plugins contribute handlers with `config=None`;
- scheduler host calls authorize no secret handles;
- `authorized_secret_port` is not a complete production lifecycle;
- Cursor has only a `ConfinedProcessHost` protocol and test fake;
- installed composed handlers do not complete a live engine run; and
- provider-live scripts fail closed instead of reaching graph-ledger success.

Phase 5 must close these residuals. A Phase 5 implementation may not call configured adapter objects directly from a test harness and claim product success.

### 2.4 Phase 1 runtime lacks product invocation data

The current `Engine.start()` accepts only composition, entrypoint, and invocation ID. Its root graph input is always `null` and its initial snapshot tree is empty.

That is sufficient for toy graphs but not for an Assurance run, which needs:

- a change/request input;
- run mode and selected test families;
- exact policy and capability-catalog digests;
- a stable initial SUT tree; and
- a supported way to inspect or materialize the committed final tree.

Phase 5 must add those generic runtime capabilities before assembling a full product.

### 2.5 Current graph input is not sufficient for Phase 4 handlers

The current planner sends every task:

```json
{
  "config": {},
  "tokens": []
}
```

Phase 4 handlers accept typed business inputs such as `CaseDesignInputV1`, `PlanInputV1`, `AgentRunRequest`, and `AgentFinalizeInputV1`. A real product graph therefore needs deterministic data projection. Product-specific glue handlers are rejected because they would move business wiring into a shallow product package and reproduce an orchestration catalog.

## 3. Goals

Phase 5 must:

1. close the generic runtime and adapter residuals required by a real product;
2. create one independently buildable `assurance-product` distribution;
3. expose exact OpenCode and Cursor product entry points with no adapter fallback;
4. assemble all six capability wheels through a canonical product graph;
5. define an explicit authenticated deployment binding wheel plus strict data-only project configuration;
6. bind every Phase 4 agent capability to exactly one prepare/execute/finalize triplet;
7. pin endpoint or executable identity, secret-handle names, permission profile, model, worker profile, request policy, and limits;
8. add a parallel new-runtime command without changing current `aa` behavior;
9. support new invocation start, crash recovery, interrupt/resume, replay, and result materialization;
10. prove every public product entrypoint compiles and runs through installed wheels;
11. compare representative old/new workflows using semantic business projections;
12. run one complete single-item OpenCode benchmark and one complete single-item Cursor benchmark through the new product;
13. execute coverage repair, report, Retro, Improvement, archive, issue, retry, interrupt, STOP, and failure paths;
14. produce a mechanically checked Phase 6 cutover and deletion handoff; and
15. leave the old runtime untouched and independently runnable until Phase 6.

## 4. Non-goals

Phase 5 does not:

- make `aa-next` the default `aa` command;
- change existing `aa` command output or persistence;
- resume or translate a legacy `events.jsonl` invocation in `graph-engine`;
- read a legacy checkpoint, driver pointer, or workflow state as new-engine authority;
- delete `assurance-kernel`, `assurance-agent`, `ProductHooks`, legacy skills, or legacy graphs;
- add forwarding imports between old and new implementations;
- run old and new products against the same mutable workspace;
- write both runtimes into one ledger or invocation directory;
- compare OpenCode `SessionEvent` history with graph events;
- create a provider-independent session store;
- invent durable Cursor session adoption;
- add an Assurance model, gate, prompt, session, or product hook to `graph-engine`;
- allow a SUT to load executable Python, shell, templates, handlers, validators, or effects from configuration;
- add a new public plugin registry kind;
- add a shared production `assurance-common` or `assurance-contracts` wheel;
- move benchmark scorers or evaluation datasets into runtime wheels;
- guarantee byte-identical model-authored prose or generated code between independent runs;
- apply the new engine HEAD in place to the user's original SUT; or
- waive a provider-live release gate because a local prerequisite is unavailable.

## 5. Considered assembly approaches

### 5.1 Chosen: independent product plus external comparison

`assurance-product` is a small composition root. `aa-next` runs it in an independent engine root and materializes results into a separate tree. The comparison harness owns old/new orchestration but neither runtime imports the other.

This preserves locality:

- product topology and source selection live in one product module;
- business behavior stays in six capability modules;
- provider behavior stays in two adapter modules;
- generic durability stays in the engine; and
- equivalence policy stays in the external comparison harness.

The Phase 6 deletion test is strong: removing the legacy runtime does not move comparison or compatibility code into the new product.

### 5.2 Rejected: shadow execution inside current `aa`

Calling the new engine from the legacy driver would create one composition root that understands two ledgers, two workspace models, and two recovery protocols. A crash between the two writes would make comparison itself stateful and ambiguous.

It would also leave a bridge that Phase 6 must untangle. Phase 5 therefore uses an external harness and cloned inputs.

### 5.3 Rejected: immediate Phase 5 default cutover

Immediate cutover would combine product assembly, live-provider completion, behavioral proof, old-invocation disposition, CLI replacement, and deletion in one unverifiable change. It contradicts the approved six-phase architecture.

### 5.4 Rejected: product-owned business wiring handlers

Handlers that rename fields, inspect QA artifacts, evaluate gates, or reinterpret capability results would make `assurance-product` another business monolith. Generic declarative input projection belongs in the engine. Business normalization belongs in the six semantic owners.

## 6. Terms

**Product wheel** is the installed `assurance-product` distribution. Its interface is two product entry points and the `aa-next` command. It contributes no Assurance business handler.

**Product entry point** is one exact `graph_engine.products` source. It fixes one adapter requirement and one static product declaration.

**Deployment binding wheel** is one explicitly installed, source-authenticated plugin wheel generated from a closed deployment manifest by a trusted builder. It contributes the 99 agent aliases plus exact adapter, model, permission, request-policy, worker, endpoint or executable, and secret-handle bindings. It contains no organization-authored executable code.

**Project configuration tree** is one explicitly supplied, strict, non-executable `ConfigTreePluginSource`. It contributes only business data such as policy, knowledge, catalog, and node configuration. It contains no capability binding, model route, endpoint, executable, secret handle, or permission grant. It is part of the frozen composition.

**Agent triplet** is one prepare alias, one execute alias, and one finalize alias associated with a Phase 4 logical agent capability.

**Invocation seed** is the canonical root input plus the stable initial workspace tree imported before the invocation bootstrap becomes authoritative.

**Result tree** is an immutable materialization of the final engine HEAD into a fresh destination. It is not an in-place SUT update.

**Behavioral projection** is a canonical comparison value derived from one runtime's terminal state, business artifacts, gate decisions, effect receipts, and selected external observations.

**Comparison case** is one immutable initial SUT fixture, request, policy, adapter/model configuration, expected semantic predicates, and separate old/new run roots.

**Hard cut** is the Phase 6 change that switches `aa` and deletes the legacy runtime without a compatibility bridge.

## 7. Repository shape

Phase 5 adds:

```text
packages/
  assurance-product/
    pyproject.toml
    assurance_product/
      __init__.py
      cli.py
      product.py
      source_catalog.py
      product-declaration-opencode.json
      product-declaration-cursor.json
      resources/
        workflow.yaml
        source-catalog.json
        product-input.v1.schema.json
        deployment-bindings.v1.schema.json
        project-config.v1.schema.json
        workflow-status.v1.schema.json
        comparison-projection.v1.schema.json
        aa-workflow/
          SKILL.md
          FALLBACK-RUNBOOK.md
    tests/

examples/
  assurance-product-deployment/
    opencode/
      deployment-bindings.json
    cursor/
      deployment-bindings.json
  assurance-product-config/
    plugin.yaml
    resources/
      product-policy.json
      data-knowledge.json
      capability-catalog.json

tests/
  phase5/
    conformance/
    fault_matrix/
    graphs/
    comparison/
    fixtures/

benchmark/
  assurance-product-phase5/
    manifests/
    run-opencode.sh
    run-cursor.sh
    compare.py

scripts/
  build_assurance_deployment_bindings.py
  assurance_product_wheel_smoke_test.sh
```

The package may use additional private files, but it must not add:

- `assurance_product.operations`;
- `assurance_product.validators`;
- `assurance_product.effects`;
- an Assurance-wide artifact model package;
- a seventh business plugin entry point; or
- an old-runtime adapter.

## 8. Dependency and import rules

The product distribution depends on:

- `graph-engine`;
- all six Phase 4 capability wheels; and
- `agent-runtime-contracts`.

Adapter wheels are explicit extras:

- `assurance-product[opencode]` installs `agent-runtime-opencode`; and
- `assurance-product[cursor]` installs `agent-runtime-cursor`.

There is no default adapter extra and no fallback from one adapter to the other.

Allowed imports:

```text
assurance_product
  -> graph_engine public composition/runtime interfaces
  -> agent_runtime_contracts value contracts when validating product input
  X assurance_intake.operations
  X assurance_generation.operations
  X assurance_execution.operations
  X assurance_healing.operations
  X assurance_quality.operations
  X assurance_improvement.operations
  X assurance_agent
  X assurance_kernel
```

The product source catalog contains installed distribution coordinates, entry-point names, declaration paths, and exact supported version constraints. It contains no imported provider object and does not scan arbitrary entry points.

The six capability wheels retain their Phase 4 dependency graph. No wheel gains a dependency on `assurance-product` or either runtime adapter.

## 9. Phase 5 generic runtime amendments

Phase 5 explicitly approves four narrow, business-neutral amendments to earlier phase interfaces. No other Phase 1–3 public-interface change is allowed under this specification.

### 9.1 Canonical invocation seed

`graph-engine` adds these public frozen values exactly:

```python
@dataclass(frozen=True)
class SeedFile:
    path: str
    sha256: str
    content: bytes

@dataclass(frozen=True)
class WorkspaceSeed:
    schema_version: Literal["1"]
    tree_id: str
    files: tuple[SeedFile, ...]

@dataclass(frozen=True)
class InvocationSeed:
    schema_version: Literal["1"]
    root_input: JSONValue
    root_input_digest: str
    workspace: WorkspaceSeed
```

`SeedFile.path` is a canonical relative POSIX path. `WorkspaceSeed.files` is strictly path-sorted, has no duplicate path, and every content digest and `tree_id` is recomputed at construction. Callers can:

1. capture a source tree under a closed policy;
2. obtain one canonical tree ID;
3. pass the seed to `Engine.start()`; and
4. reuse the same seed idempotently after an initialization cut.

Source capture is descriptor-relative and stable:

- no symlink following;
- regular single-link files only;
- canonical relative POSIX paths;
- bounded file count and total bytes;
- deterministic ignore policy;
- before/after name-set and identity rescan; and
- fail-closed behavior on drift.

The product policy excludes:

- VCS metadata;
- existing engine/legacy invocation stores;
- build caches;
- virtual environments;
- benchmark output roots; and
- credential files.

The exact included path list and every file digest become the seed tree identity.

### 9.2 Versioned bootstrap with root input and initial HEAD

The invocation start intent and authoritative bootstrap bind:

- invocation ID;
- lock digest;
- entrypoint;
- runtime-authorization digest;
- root-input digest;
- initial tree ID; and
- runtime event-schema version.

Runtime event schema version `2` extends `InvocationStarted` with exact `runtime_authorization_digest`, `root_input_digest`, `initial_tree_id`, and `event_schema_version="2"` fields. The canonical bootstrap remains one atomic three-event batch:

1. the extended `InvocationStarted`;
2. root `GraphStarted` whose `input` equals the exact frozen root input; and
3. the canonical start `TokenOffered` whose payload equals the same root input.

Fold sets the initial authoritative `head_tree_id` from `InvocationStarted.initial_tree_id` before planning later events. The engine never represents a non-empty initial tree as an implicit empty HEAD, and no alternate bootstrap sequence is accepted.

Prototype Phase 1–4 graph-engine invocation directories are not production data. The Phase 5 runtime version rejects their old bootstrap rather than adding an upgrade reader. This is not a legacy `aa` migration path.

### 9.3 Declarative input projection

`NodeDef` gains one optional, closed `input_projection` form. When absent, current `{config, tokens}` input remains available to toy and low-level graphs. When present, the engine produces exactly the declared JSON value.

The projection language supports only:

- a literal canonical JSON value;
- one RFC 6901 pointer into root input;
- one pointer into static node config;
- one pointer into the token emitted by a named direct predecessor;
- the complete token from a named direct predecessor;
- a sorted tuple of all tokens for an `all` join; and
- construction of one JSON object or tuple from those sources.

It does not support:

- arbitrary expressions;
- Python, shell, imports, templates, or interpolation;
- filesystem, environment, clock, network, or secret access;
- a pointer into a non-predecessor activation;
- implicit first/last token selection;
- missing-value fallback; or
- mutation of an upstream value.

The compiler validates predecessor names, join cardinality, unique field names, canonical pointers, and the exact projection shape. Runtime projection is pure and deterministic. Projection errors fail before handler dispatch as `invalid_input` or compile errors, never as a model retry.

The graph uses projection to pass:

- typed business input into prepare;
- the prepare result into the runtime adapter;
- the runtime result plus root/static context into finalize; and
- named outputs into downstream deterministic capabilities.

No product-owned transformation handler is introduced.

### 9.4 Exact host secret authorization

`CapabilityBindingContribution` gains one generic frozen field:

```python
secret_handles: tuple[str, ...] = ()
```

The field contains non-secret handle names only. It is:

- validated for unique canonical tokens;
- included in static/declarative contribution equality;
- included in registry and invocation-lock canonical projections;
- projected into `TaskHost*Call.authorized_secret_handles`; and
- rejected if the handler's adapter configuration names a handle not in the tuple.

The engine does not inspect provider-specific binding data to discover credentials.

`graph-engine` also adds these runtime-only values:

```python
@dataclass(frozen=True)
class SecretSourceBinding:
    handle: str
    source_kind: Literal["environment", "file"]
    source_locator: str

@dataclass(frozen=True)
class InvocationRuntimeAuthorization:
    schema_version: Literal["1"]
    secret_sources: tuple[SecretSourceBinding, ...]
    digest: str
```

Bindings are strictly handle-sorted and unique. Environment locators are canonical variable names; file locators are normalized absolute paths. The digest authenticates names and locators, never resolved values. `Engine.start()` and `Engine.open()` require the exact authorization, and the start intent/bootstrap pins its digest. Recovery with changed, missing, or extra source bindings rejects before task/effect authority.

These are the only Phase 2 contribution/runtime-authority model amendments in Phase 5.

## 10. Production task execution host

### 10.1 Ownership

`graph-engine` owns one fixed production host implementation because the host is generic runtime reliability, not Assurance product behavior. Products cannot register or replace it.

The existing `TaskExecutionHost` protocol remains the engine interface. The concrete implementation is private to the engine runtime. `aa-next` selects production mode, not a host implementation name. The authenticated engine wheel identity pins the host bytes, and the invocation bootstrap separately pins the host wire-schema version and runtime-authorization digest.

The existing direct-call CLI host remains test/toy-only and continues to refuse recoverable handlers.

### 10.2 Process isolation

Every handler call runs in an authenticated worker process. The parent:

- owns the invocation runner claim;
- resolves the exact frozen capability entry point;
- owns the activity RPC endpoint;
- owns the secret resolver;
- owns the terminal receipt store;
- owns timeout and cancellation policy; and
- never sends a ledger, checkpoint, registry object, store root, or sibling path to the worker.

The worker receives:

- one versioned `TaskHost*Call`;
- one exact attempt-root descriptor;
- the exact resolved capability source identity;
- only declared resource bytes;
- an ephemeral non-enumerable secret channel for authorized handles;
- a capability-limited activity RPC channel; and
- no ambient application configuration.

The worker loads only the already-resolved distribution, entry point, plugin ID, and capability ID. Source drift, entry-point drift, an extra handler, or an unselected module fails before plugin code runs.

### 10.3 Confinement and quiescence

The Phase 5 supported production platforms are Linux and macOS. Each must use a real confinement mechanism:

- Linux cgroup/container or verified parent-death supervisor; and
- macOS authenticated supervisor and process group with parent-liveness and descendant cleanup proof.

Windows production execution is outside the Phase 5 support matrix and must fail closed; it is not approximated with PID-only tracking.

PID-only tracking plus `finally: kill()` is not sufficient.

The host refuses startup if it cannot prove:

- exact attempt cwd;
- descendant inheritance for the selected mechanism;
- bounded stdout/stderr/control channels;
- no sibling workspace access through host-granted descriptors;
- terminal writer/descendant quiescence; and
- cleanup or typed indeterminate outcome after host failure.

Installed capability wheels are trusted code. This is a reliability and namespace boundary, not a hostile-code sandbox claim.

### 10.4 Secret lifecycle

`aa-next` supplies an explicit mapping from locked handle name to one configured secret source. Supported source kinds are closed and built in; Phase 5 initially supports explicitly named environment variables and read-only absolute files.

The source name, not its value, is locked. Secret values:

- enter only the parent host;
- are resolved only for a call whose binding authorizes the handle;
- cross only the ephemeral secret channel;
- are never enumerable by the worker;
- are revoked when the call ends;
- never enter argv;
- enter a Cursor child environment only through the adapter's exact closed environment-name mapping;
- never enter a lock, ledger, checkpoint, receipt, result, evidence digest, comparison projection, or log; and
- are scanned with deterministic canaries in tests.

### 10.5 Terminal receipts and recovery

The production host durably installs exactly one immutable terminal receipt only after:

- handler completion;
- worker exit;
- descendant quiescence;
- workspace writer quiescence;
- canonical response validation; and
- complete secret-channel revocation.

Receipt publication uses the existing engine-owned receipt schema and atomic durability rules. A fresh engine process can promote a valid receipt without contacting the provider. Partial, foreign, duplicate, drifted, or unverifiable receipts fail closed.

The combined invocation lock/bootstrap pins the production host implementation through authenticated engine-wheel identity and pins the exact wire-schema version explicitly; it does not use a hand-maintained list of operation names.

The engine internally binds the host to the exact frozen capability entry, source identity, declared resource bytes, `SnapshotStore`, and runtime authorization. This is a private deep-module seam. Phase 5 does not add a host plugin, public resource port, business callback, or product-selectable executor.

## 11. Adapter completion

### 11.1 Locked adapter binding data

Each runtime execute alias carries exactly one adapter-owned binding model.

OpenCode binding data contains:

- schema version;
- exact endpoint origin;
- TLS identity or CA digest;
- protocol profile;
- project scope;
- request, observation, poll, cancel, and byte limits;
- one secret handle name; and
- adapter configuration digest.

Cursor binding data contains:

- schema version;
- exact absolute executable path;
- executable digest;
- expected version;
- protocol profile;
- closed environment-name allowlist;
- graceful/forced cancel limits;
- output/line/time limits;
- optional secret handle name; and
- adapter configuration digest.

No field contains a candidate model list or fallback. Model selection is already frozen in the `AgentRunRequest` produced by the prepare handler.

### 11.2 Installed handler configuration

The installed OpenCode and Cursor plugin contributions remain the authoritative handler objects. They parse and authenticate their adapter binding data from `TaskRequest.binding_data`.

Constructor-only test configuration is not the production path. A composed handler with no valid locked binding data fails before external dispatch.

This changes adapter implementation, not the public `TaskHandler` or `RecoverableTaskHandler` interface.

### 11.3 Cursor confined process host

`agent-runtime-cursor` adds one real `ConfinedProcessHost` implementation. It uses the production host's supported containment substrate and provides:

- executable authentication;
- shell-free launch;
- exact attempt cwd;
- allowlisted environment construction;
- bounded live stdout/stderr draining;
- progress heartbeats;
- durable process receipt;
- authenticated observe/wait;
- graceful then forced cancellation;
- host boot identity and PID-reuse protection; and
- indeterminate behavior when ownership or terminal proof is lost.

A Cursor child and all descendants must not outlive the authorized host lifecycle.

### 11.4 Provider-live closure

The previous fail-closed Phase 3 live scripts become real engine runs through installed composition, production host, locked config, and typed secrets.

One OpenCode and one Cursor fixture run must:

- reach graph-ledger terminal success;
- commit the expected workspace change;
- emit a valid `AgentRunResult`;
- preserve adapter-specific evidence;
- survive engine restart at specified recovery cuts; and
- replay after provider state is unavailable.

These adapter fixtures remain distinct from the full Assurance benchmark required later in this specification.

## 12. Product distribution

### 12.1 Product identity

The product identity is:

- distribution: `assurance-product`;
- import package: `assurance_product`;
- product ID: `assurance`;
- initial product version: `1.0.0`; and
- engine API: exact version `2.0`.

The wheel exposes:

```toml
[project.entry-points."graph_engine.products"]
assurance-opencode = "assurance_product.product:OpenCodeAssuranceProduct"
assurance-cursor = "assurance_product.product:CursorAssuranceProduct"

[project.scripts]
aa-next = "assurance_product.cli:main"
```

The product exposes no `graph_engine.plugins` entry point.

### 12.2 Product source declarations

Each product entry point has a separate strict declaration:

- `product-declaration-opencode.json`; and
- `product-declaration-cursor.json`.

Each static declaration equals its live `ProductManifest` exactly. Both include the canonical workflow. Their source identities differ in entry-point name/value and declaration path, so their composition and lock digests differ.

### 12.3 Exact plugin requirements

Both products require all six Phase 4 plugins at exact `0.1.0` versions.

Both products also require:

- `assurance.product.agent==1.0.0`.

Earlier drafts used `assurance.product.bindings`; the authenticatable identity is `assurance.product.agent==1.0.0` because engine ownership prefixes must match the frozen §14.2 alias owner.

The OpenCode product additionally requires:

- `runtime.opencode==0.1.0`.

The Cursor product additionally requires:

- `runtime.cursor==0.1.0`.

One explicit project configuration tree is added as one `ConfigTreePluginSource` by the resolution request. Resolver extension makes its plugin ID/version part of the effective manifest and exact dependency closure.

Neither product requires or resolves the unselected adapter. The selected deployment binding wheel must depend on the same selected adapter and must reject the other adapter before provider import.

### 12.4 Source catalog

`source-catalog.json` maps the product, six capability wheels, and two adapter choices to exact installed coordinates:

- distribution;
- entry-point group;
- entry-point name;
- declaration path; and
- supported version specifier.

The catalog declares one external deployment-binding slot with exact plugin ID `assurance.product.agent`, exact plugin version `1.0.0`, entry-point group `graph_engine.plugins`, and entry-point name `deployment`. Its distribution and declaration coordinates are supplied explicitly to `aa-next`; the catalog contains no ambient discovery rule for that slot.

`aa-next` reads only this authenticated product resource and the exact CLI coordinates. It then constructs an explicit `ResolutionRequest`. It does not discover all installed plugins and choose by convention.

Changing the source catalog changes product source and lock identity.

## 13. Deployment bindings and project configuration

### 13.1 Two separate authorities

Phase 5 uses two different configuration mechanisms because they grant different authority:

| Mechanism | Source kind | May contribute |
|---|---|---|
| Deployment binding wheel | explicit installed `WheelPluginSource` | aliases, adapter binding, model route, permission and request-policy digests, worker profile, endpoint or executable identity, and secret-handle names |
| Project configuration tree | explicit `ConfigTreePluginSource` | business policy, data knowledge, capability catalog, and non-executable node configuration |

These sources are not interchangeable. The Phase 2 declarative loader rejects executable-shaped keys and remains data-only. Phase 5 does not rename `executable`, encode it in an opaque string, or add a loader exception to evade that boundary.

### 13.2 Deployment binding wheel

`aa-next` requires exact installed coordinates:

```text
--binding-dist DIST
--binding-entrypoint deployment
--binding-declaration PACKAGE/assurance-deployment-plugin.json
```

The selected source must resolve to:

- plugin ID `assurance.product.agent`;
- plugin version `1.0.0`;
- entry-point group `graph_engine.plugins`;
- entry-point name `deployment`;
- one exact selected runtime dependency; and
- exact dependencies on all six capability plugins.

The wheel contributes exactly:

- the 99 aliases defined in Section 14;
- 33 exact `AgentBindingDataV1` prepare assignments;
- the selected adapter binding for every execute alias;
- permission-profile and request-policy resources referenced by digest;
- secret-handle **names**, never values; and
- no task handler, validator, schema, effect, workflow, or business decision.

An OpenCode deployment wheel presented to `assurance-cursor`, or a Cursor deployment wheel presented to `assurance-opencode`, fails static source-set/dependency validation before any provider import.

### 13.3 Trusted deterministic binding builder

`aa-next bindings build` is the only supported Phase 5 builder for a deployment wheel. It accepts one closed `DeploymentBindingsV1` document and an empty output directory. It:

1. validates the complete document before writing;
2. rejects unknown fields, missing assignments, fallback lists, secret values, arbitrary code, templates, globs, and undeclared files;
3. renders one repository-owned fixed provider template plus canonical static resources;
4. derives distribution and import-package names from the canonical manifest digest;
5. emits a strict static declaration equal to the live descriptor/contribution;
6. builds byte-identical wheel bytes from the same canonical input and committed builder wheel on both supported build platforms; and
7. emits the manifest digest, wheel digest, distribution, entry-point value, and declaration path.

Generated distributions use `assurance-product-bindings-<digest-prefix>` and generated import packages use `assurance_product_bindings_<digest_prefix>`. The exact full manifest and wheel digests, not the prefix, are security identities.

`DeploymentBindingsV1` contains exactly:

```json
{
  "schema_version": "1",
  "runtime_plugin_id": "runtime.opencode",
  "adapter_binding": {},
  "routes": {
    "one.of.the.33.prepare.ids": {
      "provider_model": "exact-provider-model-id",
      "worker_profile": "exact-worker-profile",
      "permission_profile_id": "exact-resource-id",
      "request_policy_id": "exact-resource-id",
      "limits": {"max_seconds": 900}
    }
  },
  "permission_profiles": {},
  "request_policies": {},
  "secret_handles": ["canonical-handle-name"]
}
```

The Cursor form uses `"runtime_plugin_id": "runtime.cursor"`. `adapter_binding` is validated against the selected adapter's strict schema from Section 11. `routes` has exactly the 33 keys in Section 14. Permission and request-policy identifiers resolve within the same generated contribution and are replaced by canonical digests in the emitted `AgentBindingDataV1`. `secret_handles` contains names only and equals the union of handles referenced by the adapter binding.

The builder does not install the wheel, resolve credentials, contact a provider, infer the current project, or mutate an existing output. The runtime never consumes the raw builder input; it consumes only the explicitly installed and source-authenticated wheel.

### 13.4 Project configuration source

`aa-next` requires:

```text
--config-tree /absolute/path/to/project-config
```

There is no implicit current-directory scan and no fallback to an embedded mutable default.

Exactly one tree is required. The repository ships an example tree, not a silently enabled production configuration. A benchmark manifest pins its exact tree digest.

### 13.5 Closed project files

The tree contains only strict JSON/YAML/Markdown resources declared by `plugin.yaml`. It may contain:

- product policy;
- data knowledge;
- capability catalog;
- node policy values; and
- other schema-declared non-executable business data consumed by the six capability wheels.

It contains no:

- Python or shell;
- import/module/callable field;
- executable template;
- command or installer;
- arbitrary file glob;
- credential value;
- endpoint;
- executable or host path;
- secret handle or secret-source name;
- model route, worker profile, permission grant, or capability alias;
- undeclared resource.

The Phase 2 declarative loader remains the sole parser and rejects executable forms.

### 13.6 Project plugin identity

The canonical plugin ID is `assurance.product.configuration` and initial version is `1.0.0`.

Different projects or organizations may use different source bytes with the same semantic plugin ID/version. Their exact source snapshot and contribution projection differ, so their invocation locks differ.

The config plugin depends on:

- all six capability plugins.

It does not depend on either runtime adapter and does not select one.

### 13.7 Exact routing

Every prepare alias carries existing `AgentBindingDataV1`:

```json
{
  "execution": {
    "provider_model": "one/exact-model-id",
    "worker_profile": "one-exact-worker-profile",
    "permission_profile_digest": "sha256",
    "limits": {"max_seconds": 900}
  },
  "request_policy_digest": "sha256",
  "request_config_digest": "sha256"
}
```

The mapping is keyed by the exact prepare target ID. It has no:

- default key;
- wildcard;
- inheritance;
- candidate list;
- fallback;
- retry-with-another-model; or
- provider-selected routing.

Every one of the 33 Phase 4 prepare IDs appears exactly once. Missing, duplicate, unknown, or extra mappings fail composition.

The `request_config_digest` authenticates the complete effective assignment relevant to the capability: adapter identity/config digest, model, worker profile, permission profile digest, request policy digest, and capability binding ID.

The routing document and 99 aliases are resources and contributions of the deployment binding wheel, never of the project configuration tree.

### 13.8 Policy and knowledge

Product policy and data knowledge are configuration data, not product Python behavior.

Their canonical schemas are owned by the relevant capability wheels. Their configured resource values are contributed by the project configuration tree. Product graph projections pass the exact values/digests to the owning capability inputs.

If a Phase 4 handler needs new business interpretation, that interpretation is added to the semantic owner wheel. The product wheel may validate product-level presence and namespace only; it must not decide QA policy.

### 13.9 Lock closure

The invocation lock independently projects and digests:

- deployment binding wheel source identity and complete contribution;
- each of the 99 alias bindings and secret-handle names;
- every adapter/model/worker/permission/request-policy/endpoint-or-executable assignment;
- project configuration source identity and complete contribution;
- validated product input; and
- the resulting graph and compiled workflow.

Changing either authority produces a different lock. Deleting, adding, or synchronously forging a binding/config projection fails public `FrozenComposition` authentication.

## 14. Canonical agent-triplet bindings

### 14.1 Closed prepare set

The logical agent set is exactly these 33 Phase 4 prepare capabilities:

```text
assurance.intake.case-design.prepare
assurance.intake.case-review.prepare
assurance.intake.explore.prepare
assurance.intake.intake.prepare
assurance.generation.api.codegen-fix.prepare
assurance.generation.api.codegen.prepare
assurance.generation.api.plan-review.prepare
assurance.generation.api.plan.prepare
assurance.generation.e2e.codegen-fix.prepare
assurance.generation.e2e.codegen.prepare
assurance.generation.e2e.plan-review.prepare
assurance.generation.e2e.plan.prepare
assurance.generation.fuzz.codegen.prepare
assurance.generation.fuzz.plan-review.prepare
assurance.generation.fuzz.plan.prepare
assurance.generation.performance.codegen.prepare
assurance.generation.performance.plan-review.prepare
assurance.generation.performance.plan.prepare
assurance.execution.execute.prepare
assurance.execution.run.prepare
assurance.healing.coverage-repair.prepare
assurance.healing.fix-proposal.prepare
assurance.quality.fact-baseline.prepare
assurance.quality.inspect.prepare
assurance.quality.issue-analysis.prepare
assurance.quality.issue-triage.prepare
assurance.quality.report.prepare
assurance.improvement.archive.prepare
assurance.improvement.improvement-review.prepare
assurance.improvement.retro-eval-analysis.prepare
assurance.improvement.retro-issue-analysis.prepare
assurance.improvement.retro-workflow-analysis.prepare
assurance.improvement.retro.prepare
```

The generated deployment wheel derives its binding key set from this repository-owned frozen list and fails if the six installed descriptors expose a different prepare/finalize closure.

### 14.2 Alias derivation

For each Phase 4 prepare ID `P`:

1. remove the terminal `.prepare` to obtain stem `S`;
2. remove the leading `assurance.` from `S` to obtain suffix `K`; and
3. define these aliases:

```text
assurance.product.agent.<K>.prepare
assurance.product.agent.<K>.execute
assurance.product.agent.<K>.finalize
```

The aliases bind as follows:

| Alias | Target | Binding data | Secret handles |
|---|---|---|---|
| `...prepare` | exact Phase 4 `P` | `AgentBindingDataV1` | none |
| `...execute` | selected `runtime.*.execute` | exact adapter binding | exact selected adapter handles |
| `...finalize` | `S + ".finalize"` | exactly `null` for all 33 Phase 4 pairs | none |

All resource IDs referenced by a binding resolve in the frozen registry. A permission or request-policy resource is referenced by exact resource ID and digest where the consumer requires bytes.

The product graph may not reference:

- a direct `runtime.*.execute` capability;
- a direct Phase 4 prepare/finalize capability for an agent-backed step;
- a test-only `test.assurance.bindings.*` alias; or
- an undeclared compatibility alias.

The deployment binding contribution must expose exactly 99 triplet aliases. The deployment manifest, generated descriptor, static declaration, contribution, registry, lock, and graph reference set must agree.

## 15. Product graph

### 15.1 One canonical workflow

Both product entry points use byte-identical canonical `workflow.yaml` content. Adapter selection is not encoded in graph topology.

The workflow declares all registry schema/resource/effect references needed by its nodes. Every reference resolves through the selected six wheels, deployment binding wheel, or project configuration tree.

### 15.2 Public entrypoints

The first product version exposes exactly:

| Entrypoint | Purpose |
|---|---|
| `full` | intake, assurance, report, and optional archive |
| `intake` | intake and case preparation |
| `case` | case-only intake mode |
| `execute` | assurance branches, execution, inspect, healing, report |
| `archive` | archive precheck and archive |
| `retro` | collect, analyze, propose, reconcile |
| `issue-review` | issue review lifecycle |
| `issue-analyze` | issue analysis lifecycle |
| `issue-reconcile` | issue reconciliation lifecycle |
| `improvement-review` | improvement review |
| `improvement-evaluate` | improvement evaluation |
| `improvement-export` | improvement export |
| `improvement-apply` | improvement delivery |
| `improvement-rollback` | improvement rollback |

Names are stable product interface. Adding or removing an entrypoint changes product version and lock identity.

Evaluation remains an external comparison/release harness. It is not made a graph-engine registry or a hidden runtime graph. Retro and Improvement are product entrypoints because their business capabilities were extracted in Phase 4.

### 15.3 Root input

All entrypoints accept the same closed `ProductInputV1`. It contains exactly:

- `schema_version: "1"`;
- `change_id: str` as one canonical non-empty identifier;
- `requirement: str` as normalized UTF-8 text;
- `run_mode: "case" | "implement" | "verify"`;
- `selected_test_families: tuple["api" | "e2e" | "fuzz" | "performance", ...]` in canonical family order with no duplicate;
- `auto_archive: bool`;
- `capability_catalog: ResourceRefV1`;
- `product_policy: ResourceRefV1`;
- `data_knowledge: ResourceRefV1`;
- `allowed_artifact_paths: tuple[str, ...]` as sorted canonical relative POSIX prefixes; and
- `budgets: BusinessBudgetsV1` with exact non-negative `review_rounds`, `coverage_rounds`, `healing_rounds`, and `execution_retries` values.

`ResourceRefV1` contains exactly `resource_id` and `sha256`. Every reference must resolve to the exact frozen project/capability resource before bootstrap. Entrypoint validation applies this closed family rule table:

| Entrypoint | `selected_test_families` |
|---|---|
| `full`, `execute` | non-empty |
| `intake`, `case`, `archive`, `retro` | empty |
| `issue-review`, `issue-analyze`, `issue-reconcile` | empty |
| `improvement-review`, `improvement-evaluate`, `improvement-export`, `improvement-apply`, `improvement-rollback` | empty |

Host paths, endpoint credentials, mutable registry objects, provider session IDs, and legacy checkpoint references are forbidden.

### 15.4 Agent subgraphs

Every agent-backed logical step is represented as:

```text
prepare alias
    -> execute alias
    -> finalize alias
```

Input projection sends:

- typed business data and root/static policy to prepare;
- the complete prepare output to execute; and
- the typed adapter result plus exact root/static finalize context to finalize.

Prepare/finalize retry policies are business/output policies. Execute recovery is governed by task-activity state. An indeterminate external activity cannot be converted into a normal model retry.

### 15.5 Family branches

`execute` and `full` activate selected API, E2E, Fuzz, and Performance branches from one frozen root selection.

For every selected family:

1. plan runs;
2. plan review runs;
3. a failed review takes the explicit bounded fix/review route when allowed;
4. code generation runs;
5. codegen validation runs;
6. any allowed codegen fix is bounded; and
7. branch completion joins before execution.

An unselected family is represented by an explicit deterministic skip route. A selected family cannot disappear because another branch failed early or a resource queue serialized execution.

The all-family join proves the exact selected set. It does not infer completion from whichever tokens happen to arrive.

### 15.6 Coverage loop

After execution and quality projection:

- coverage sufficiency is computed deterministically by `assurance-quality`;
- insufficient but repairable coverage enters `assurance-healing.coverage-repair`;
- repair returns through selection/execution/quality;
- every loop has an explicit business budget and graph `max_activations` bound;
- no-progress and exhausted-budget outcomes are typed;
- business STOP uses `TaskOutcome.stopped`, not an internal graph failure; and
- an engine/runtime failure never masquerades as a normal STOP.

The acceptance suite contains one case that triggers and exits this loop and one that reaches its bounded STOP/failure result.

### 15.7 Issue, report, archive, Retro, and Improvement

The product graph ensures:

- issue analysis and reconciliation execute when their deterministic probe selects them;
- report generation occurs after final quality/healing state and produces all required report artifacts;
- archive runs only after its precheck and durable effect authority;
- Retro collect/analyze/propose/reconcile is an explicit graph path;
- Improvement review/evaluate/export/apply/rollback are separate repeatable entrypoints; and
- Eval inputs may feed Retro through canonical artifacts, but Eval orchestration remains outside the runtime product.

### 15.8 Gate semantics

Business decisions are produced by capability handlers as typed outputs. Graph `gate` nodes only evaluate closed expressions over projected values.

No gate expression:

- reads a file;
- invokes Python;
- calls a plugin;
- reconstructs a capability prefix;
- performs model routing;
- recomputes a mutable decision after it was frozen; or
- invents a default for a missing field.

Needs-fix, pass, reject, needs-human, STOP, and continue routes are explicit and mutually exclusive.

### 15.9 Interrupts and resume

Human decisions use graph interrupt nodes with closed actions. Resume:

- uses the same invocation ID and exact lock;
- authenticates root input and initial tree identity;
- preserves the same attempt workspace for recoverable activity;
- rejects changed graph/config/model/adapter/policy;
- never reads legacy `workflow-state.yaml` as authority; and
- appends one canonical resume transition.

## 16. `aa-next` command

### 16.1 Purpose

`aa-next` is a Phase 5 verification and parallel-use command. It proves the product composition root without changing current `aa`.

### 16.2 Commands

The minimum interface is:

```text
aa-next compile
aa-next bindings build
aa-next start
aa-next run
aa-next status
aa-next resume
aa-next export
aa-next lock show
```

All commands require explicit engine root and invocation identity or read them from an exact non-authoritative pointer created by `aa-next`. The ledger and invocation lock remain authoritative.

`compile`, `start`, and first `run` use the same explicit product, deployment-binding, and project-config source coordinates. `status`, `resume`, `export`, and repeated `run` require those same coordinates and authenticate them against the stored lock; they do not reconstruct composition from an ambient environment.

### 16.3 Start inputs

`aa-next start` requires:

- `--project-dir`;
- `--engine-root`;
- `--invocation-id`;
- `--product assurance-opencode|assurance-cursor`;
- `--binding-dist`;
- `--binding-entrypoint deployment`;
- `--binding-declaration`;
- `--config-tree`;
- `--entrypoint`;
- one canonical input document; and
- one explicit `--secret HANDLE=env:NAME|file:/absolute/path` mapping for every handle required by the selected deployment binding.

It:

1. validates the product input;
2. captures the stable SUT seed;
3. resolves the explicit composition;
4. validates product, deployment-binding, project-config, secret-handle, and entrypoint closure;
5. starts one invocation with the seed;
6. writes no legacy runtime file; and
7. prints invocation ID, lock digest, composition digest, seed tree ID, and root-input digest.

### 16.4 Run and resume

`run` opens or starts, drives until terminal or interrupt, and returns:

- `0` succeeded;
- `20` stopped;
- `30` interrupted/needs human;
- `40` failed, drifted, indeterminate, or invalid.

`resume` accepts only a pending interrupt's declared action and reason. It does not accept a new product/config/input.

`bindings build` requires one closed deployment manifest and one absent or empty output directory. It has no engine root, project directory, provider access, or secret-source argument. Its output must be installed before `compile` or `start`; no run command builds or installs code implicitly.

### 16.5 Export

`export` materializes one authenticated final HEAD into an absent or empty destination. It:

- never follows destination symlinks;
- uses regular single-link files;
- writes through staging;
- fsyncs files/directories;
- atomically publishes the result tree;
- emits a canonical manifest and tree ID; and
- refuses an in-place original-SUT destination in Phase 5.

Running, interrupted, stopped, drifted, indeterminate, and failed invocations do not export a result tree. Phase 5 defines no diagnostic export command.

### 16.6 No ambient behavior

`aa-next` does not:

- scan installed products/plugins;
- infer an adapter;
- infer a model;
- infer a config tree;
- read current `aa` state;
- reuse current `aa` change IDs as invocation IDs without an explicit mapping;
- load code from the SUT;
- read ambient credentials without an explicit handle-to-source mapping; or
- fall back from a drifted source.

### 16.7 Status projection

`aa-next status --json` emits one versioned, read-only projection derived from the authenticated ledger/checkpoint/workspace state. It contains:

- invocation, entrypoint, lock, root-input, initial-tree, and current-HEAD identities;
- terminal class/reason or pending interrupt actions;
- the complete compiled root/subgraph hierarchy;
- every node's inactive, ready, running, retrying, succeeded, failed, stopped, interrupted, or skipped state;
- attempt number, lease state, bounded failure category, and activity reference where present;
- selected family and coverage-loop progress;
- durable effect state; and
- bounded adapter evidence references.

It contains no provider transcript, chain of thought, raw secret, or duplicated OpenCode session history. A UI may combine this workflow projection with provider-owned conversation views, but the workflow projection remains authoritative for product progress.

## 17. Behavioral comparison harness

### 17.1 Ownership

The comparison harness lives outside runtime wheels. It may import test/reporting helpers but neither production runtime imports it.

Benchmark datasets, scorers, fixtures, and evaluation orchestration remain harness assets.

### 17.2 Isolation

For every case the harness creates:

```text
immutable input fixture
  ├── legacy workspace clone + legacy state root
  └── new workspace seed + graph-engine root + result export
```

The clones share only immutable fixture bytes. They do not share:

- ledger;
- checkpoints;
- provider session ID;
- worktree;
- report directory;
- effect store;
- secret material;
- invocation ID namespace; or
- mutable external target.

Effects that would touch an external system use distinct sandbox targets or deterministic fakes. The harness never double-applies one real mutation merely to compare implementations.

### 17.3 Projection

Each side produces a canonical `BehavioralProjectionV1` containing:

- case ID and input digest;
- runtime identity;
- terminal class and reason category;
- selected/activated/completed/skipped family sets;
- typed gate decisions;
- canonical artifact envelopes and digests;
- generated/changed file manifest;
- execution evidence summary;
- coverage/trace/quality metrics;
- issue/healing decisions;
- durable effect intents, idempotency keys, receipts, and observed external projection;
- report presence and semantic fields;
- Retro/Improvement/Archive projections where selected;
- retry/interrupt/STOP counts by semantic role; and
- bounded redacted diagnostics.

It excludes:

- graph event IDs and sequence numbers except internal consistency checks;
- timestamps and durations from equality unless a bound is under test;
- OpenCode session IDs;
- Cursor process/session IDs;
- provider message order beyond adapter evidence;
- raw reasoning/tool transcripts;
- token/cost values;
- secrets; and
- engine-private path names.

### 17.4 Equivalence classes

Comparison fields use one of four declared modes:

| Mode | Meaning |
|---|---|
| exact | byte/canonical-value equality |
| set | exact set equality independent of order |
| predicate | both satisfy the same named semantic predicate |
| intentionally-different | value must differ for a documented identity reason |

Examples:

- terminal class: exact;
- selected test families: set;
- capability-key validity: same predicate;
- model-authored prose: schema and evidence predicate;
- generated source: validators, mapping closure, test pass, and declared-file predicate;
- lock digest: intentionally different from legacy;
- provider evidence digest: intentionally different across adapters;
- report required sections and quality decision: exact/predicate as declared.

No field is silently dropped after a mismatch. Every exclusion is versioned in the comparison schema.

### 17.5 Baseline authority

The existing legacy runtime is a behavioral reference, not an architectural oracle. A known legacy defect is not copied into the new product merely to achieve parity.

When old and new differ:

1. classify whether the legacy behavior is required, buggy, unspecified, or observational noise;
2. cite the governing business contract;
3. fix the semantic owner or comparison projection;
4. add a deterministic regression; and
5. record the disposition.

The product graph cannot weaken a Phase 4 validator to match an invalid legacy output.

## 18. Required comparison matrix

The mandatory comparison matrix contains these 25 cases:

1. full API-only success;
2. full E2E-only success;
3. full Fuzz-only success;
4. full Performance-only success;
5. full all-four-family success;
6. intake review needs-fix then pass;
7. plan review invalid output then bounded retry;
8. codegen validation failure then bounded fix;
9. execution with closed mapping and no stale unmapped test;
10. coverage insufficient, repair, re-execution, then pass;
11. coverage repair no-progress/exhausted result;
12. healing disallowed business STOP;
13. report generation with all required outputs;
14. issue analysis/reconcile path;
15. archive durable effect and replay;
16. Retro collect/analyze/propose/reconcile;
17. Improvement review/evaluate/export/apply;
18. Improvement rollback;
19. human interrupt and exact resume;
20. transient local retry;
21. OpenCode ambiguous create and recovery;
22. Cursor unknown process outcome remaining indeterminate;
23. engine crash after provider terminal receipt;
24. config/model/graph/source drift rejection; and
25. replay after provider state removal.

The all-four-family case asserts API, E2E, Fuzz, and Performance each reached planning, review, codegen, and execution evidence. A terminal run with a silently absent branch fails.

The report case asserts the report artifacts exist in the final materialized tree. A terminal success without report output fails.

## 19. Provider-live full benchmarks

### 19.1 OpenCode

One pinned existing benchmark item runs through:

- installed `assurance-product[opencode]`;
- explicit installed OpenCode deployment binding wheel;
- explicit data-only project configuration tree;
- exact server profile and endpoint;
- exact model and reasoning/worker profile;
- production host and ephemeral secret channel;
- the `full` entrypoint; and
- all test families selected by the manifest.

It must run to terminal success or the manifest's explicit expected business STOP. Infrastructure failure, indeterminate provider state, absent branch, missing report, or missing coverage decision is failure.

### 19.2 Cursor

A second pinned item runs through:

- installed `assurance-product[cursor]`;
- explicit installed Cursor deployment binding wheel;
- explicit data-only project configuration tree;
- exact executable digest/version;
- exact model and worker profile;
- production outer host and real confined process host;
- the `full` entrypoint; and
- all manifest-selected families.

Unknown process ownership or missing terminal stream is indeterminate and cannot be converted into success or a blind retry.

### 19.3 Model policy

Each live benchmark pins one exact model per logical capability. The manifest records effective routing assignments and digests.

Changing a model may improve completion rate, but it cannot:

- alter graph topology;
- disable validators;
- expand permissions;
- select a fallback;
- repair invalid output outside the declared loop;
- change the comparison schema; or
- waive a failed branch.

### 19.4 Eval, Retro, and Improvement

The external Eval harness evaluates both complete runs. Its outputs are canonical inputs to the selected Retro case.

The new product's `retro` and Improvement entrypoints must actually execute. A release report that merely finds pre-existing files does not count.

## 20. Error and terminal semantics

### 20.1 Error classes

The parallel CLI distinguishes:

- invalid product input;
- composition/source/config drift;
- compile-time graph/reference failure;
- host unavailable;
- adapter preflight failure;
- task invalid input;
- model invalid output;
- business STOP;
- needs human;
- retry exhausted;
- durable effect failure;
- provider/host indeterminate;
- internal engine invariant failure; and
- result export failure.

No class is collapsed into generic successful STOP.

### 20.2 Retry

Normal retry requires:

- a typed retryable failure kind;
- a matching retry policy;
- remaining attempt budget; and
- no live/indeterminate external activity.

Business loops use explicit graph budget and state, not task retry count.

### 20.3 STOP

A business STOP comes only from a capability handler or explicit interrupt decision defined by the product graph. A nested subgraph propagates STOP without changing it to `completed` or `internal`.

Infrastructure, invalid composition, missing artifact, and recovery ambiguity are not business STOP.

### 20.4 Reported terminal success

Success requires:

- graph terminal success;
- no pending activity/effect/interrupt;
- final HEAD matching ledger authority;
- all selected family evidence complete;
- mandatory gate/report artifacts valid; and
- successful result-tree materialization when the command requested export.

## 21. Security and trust model

### 21.1 Trusted code

Installed engine, product, deployment-binding, capability, and adapter wheels are trusted but source-authenticated. The SUT cannot register executable code through project configuration.

### 21.2 Untrusted data

SUT files, raw deployment manifest bytes, product input, model output, project configuration bytes, provider responses, Cursor stream records, and legacy comparison artifacts are untrusted data and validated at their owning seam.

### 21.3 Path safety

Seed capture, attempt workspaces, candidate validation, effects, result export, config trees, and comparison reads use:

- descriptor-relative access;
- no-follow opens;
- regular single-link checks;
- canonical relative paths;
- stable identity rescans;
- exact write sets;
- atomic publication; and
- directory durability barriers.

### 21.4 Drift

Changing any of these rejects open/resume before new ledger or effect authority:

- engine implementation;
- production host implementation/wire;
- product source or declaration;
- product entry point;
- capability, adapter, deployment-binding, or project-config source;
- plugin descriptor/contribution;
- schema/resource/effect/binding projection;
- workflow or compiled graph;
- routing/model/permission/request policy;
- endpoint/executable identity or secret-source locator;
- root input; or
- initial seed identity.

### 21.5 No context duplication

The graph ledger records generic task activity only. It does not copy OpenCode session event history or create a synthetic Cursor session history.

UI and local status projections derive from the graph ledger plus bounded adapter evidence. Full provider conversation UI remains provider-owned.

## 22. Fault matrix

Deterministic cuts cover at least:

### 22.1 Deployment build and composition

- invalid, unknown, missing, duplicate, or fallback routing assignment;
- raw secret value or arbitrary code in a deployment manifest;
- builder cut before/after file and wheel publication;
- generated declaration/live contribution mismatch;
- wrong adapter dependency or unselected adapter source;
- drifted deployment wheel between resolution and provider import;
- binding supplied through a project configuration tree;
- project config containing an executable-shaped or runtime-authority field; and
- missing, extra, or forged one of the 99 aliases.

### 22.2 Seed/bootstrap

- during seed tree capture;
- after seed capture before start;
- during initial tree publication;
- after initial tree durability before start intent;
- during start-intent publication;
- before/after bootstrap append;
- after visible bootstrap before ledger-directory fsync; and
- repeated start with changed root input or seed.

### 22.3 Input projection

- missing root pointer;
- missing predecessor;
- duplicate predecessor token;
- wrong join cardinality;
- token payload schema mismatch;
- non-canonical pointer; and
- projection drift on replay.

### 22.4 Host

- before worker spawn;
- after spawn before dispatch marker;
- during activity RPC;
- after reference bind;
- after handler response before quiescence;
- during terminal receipt publication;
- after receipt durability before parent acknowledgement;
- parent crash with live descendants;
- secret channel disconnect/revocation;
- worker source drift; and
- duplicate/foreign terminal receipt.

### 22.5 OpenCode

- ambiguous session create;
- empty discovery after ambiguous create;
- duplicate metadata matches;
- prompt admission lost response;
- SSE disconnect;
- polling transient idle;
- cancel/result race;
- provider terminal before engine restart; and
- provider state deleted after receipt.

### 22.6 Cursor

- confinement unavailable;
- executable/version drift;
- before/after spawn;
- partial NDJSON;
- output overflow;
- terminal record/exit mismatch;
- unknown process ownership;
- host boot identity change;
- graceful/forced cancel race; and
- descendant cleanup failure.

### 22.7 Effects/export/comparison

- before/after effect intent;
- during effect receipt publication;
- reconcile after lost acknowledgement;
- export file write/rename/directory fsync;
- destination race/symlink/hardlink;
- comparison input drift;
- one side terminal while the other is running; and
- partial comparison report publication.

Every cut specifies authoritative state, legal next action, and whether the outcome is recoverable, terminal, stopped, interrupted, or indeterminate.

## 23. Verification strategy

### 23.1 Engine tests

Tests prove:

- non-null root input and non-empty initial seed;
- exact bootstrap/fold/replay;
- seed/start idempotency;
- closed input projection;
- final HEAD export;
- secret-handle lock projection;
- production host source pin;
- terminal receipt recovery; and
- all fault cuts above.

Toy A/B remain green after explicit event-schema/golden updates. No compatibility reader is added for prototype invocation directories.

### 23.2 Adapter tests

Both adapters pass:

- installed handler configuration;
- production host calls;
- exact secret authorization;
- source/config drift;
- live bounded I/O;
- recovery/cancel;
- credential canary;
- wheel isolation; and
- provider-live fixture execution.

### 23.3 Product tests

Tests resolve the real product through public `RegistryPlatform` from installed/committed wheel sources. They assert:

- exact plugin closure;
- no unselected adapter;
- deterministic deployment-wheel build and installed-source authentication;
- exact separation of deployment authority from data-only project configuration;
- exact 99 aliases;
- all graph references;
- all public entrypoints;
- adapter rebinding with byte-identical workflow;
- model/permission/policy lock identity;
- root input schema;
- coverage/report/Retro/Improvement topology;
- STOP and interrupt propagation; and
- result materialization.

Tests do not construct private registries or replace composed handlers with fakes after resolution.

### 23.4 Wheel isolation

Committed-HEAD smoke environments include:

1. `assurance-product` base without an adapter: wheel/source inspection works and composition resolution fails closed;
2. `assurance-product[opencode]` plus one generated OpenCode deployment wheel and one project config tree;
3. `assurance-product[cursor]` plus one generated Cursor deployment wheel and one project config tree;
4. both adapter extras installed but OpenCode product/binding selected: Cursor remains unselected;
5. both installed but Cursor product/binding selected: OpenCode remains unselected;
6. a product with a missing, foreign, drifted, or extra deployment binding wheel fails before provider import; and
7. wheel-only execution outside the repository and source cwd.

No environment contains `assurance-agent` or `assurance-kernel` unless it is the separate legacy comparison environment.

### 23.5 Repository gates

Required gates are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
bash scripts/packaging_smoke_test.sh
```

Provider-live benchmarks are separate mandatory release gates and are not replaced by deterministic tests.

## 24. Acceptance criteria

Phase 5 is complete only when:

1. Phase 4's six wheels remain independently buildable and isolated;
2. `assurance-product` builds without legacy runtime dependencies;
3. both product entry points have exact static/live declarations;
4. each product resolves only six capability wheels, its selected adapter, one explicit deployment binding wheel, and one explicit project configuration tree;
5. the unselected adapter is absent from the frozen composition;
6. all 99 canonical agent-triplet aliases resolve exactly;
7. every one of 33 routing assignments is exact and has no fallback;
8. the deterministic builder rejects executable input and emits a wheel whose static/live declaration, source digest, and full contribution agree;
9. the project configuration tree contains no binding, model, endpoint, executable, permission, or secret authority;
10. root input and initial SUT tree are authenticated and replayable;
11. graph input projection supplies every Phase 4 handler's declared business contract without product glue handlers;
12. the production host runs installed composed handlers rather than test-constructed replacements;
13. terminal receipts, secrets, descendants, and workspaces pass the full recovery/security matrix;
14. OpenCode and Cursor installed handlers receive exact locked adapter binding data;
15. one Phase 3 provider-live fixture succeeds through each adapter;
16. the complete product graph exposes and runs every public entrypoint;
17. API, E2E, Fuzz, and Performance selected branches cannot be silently omitted;
18. coverage repair, report, issue, healing, archive, Retro, and Improvement paths have execution evidence;
19. business STOP is distinct from engine failure and nested STOP propagates correctly;
20. old/new comparison cases satisfy their declared equivalence modes;
21. one complete OpenCode and one complete Cursor single-item Assurance benchmark reach their expected terminal outcome;
22. post-success replay and comparison no longer require retained provider state;
23. no secret appears in any persisted or reported surface;
24. committed-HEAD wheel isolation and repository gates pass;
25. current `aa` still runs the legacy product by default;
26. no old/new import or persisted-state bridge exists; and
27. the Phase 6 cutover/deletion handoff is complete and mechanically checked.

## 25. Phase 6 handoff

Phase 5 publishes:

- exact `assurance-product` wheel and source digests;
- exact OpenCode/Cursor product entry-point coordinates;
- exact six-wheel and adapter constraints;
- deterministic deployment-binding builder version and template digest;
- exact deployment manifest schema plus representative OpenCode/Cursor generated wheel/source/contribution digests;
- canonical workflow and compiled digest;
- canonical project-config plugin schema and example source digests;
- exact 99 binding IDs and routing-coverage report;
- production host implementation/wire digest;
- product input/bootstrap/export schema versions;
- full comparison matrix and dispositions;
- provider-live benchmark manifests and results;
- list of old invocations and approved disposition mechanism;
- legacy command-to-new command mapping;
- updated Phase 6 deletion inventory;
- evidence that no deleted module is imported by new wheels; and
- a rollback boundary that does not reinstall a compatibility bridge.

Phase 6 then:

1. freezes new legacy starts;
2. drains or explicitly terminates every old invocation with audit evidence;
3. switches `aa` to the `assurance-product` composition root;
4. replaces temporary `aa-next` naming with the final `aa` interface;
5. decides and implements safe in-place result application if required;
6. deletes legacy runtime and duplicate business implementations in the same cut;
7. deletes `ProductHooks`, catalogs, old product entry points, resources, and obsolete tests;
8. removes the comparison-only compatibility baseline where no longer needed; and
9. adds no old-invocation resume adapter or forwarding import.

## 26. Implementation-plan boundaries

The Phase 5 implementation plan must use dependency-ordered tracer tasks:

1. freeze Phase 5 ownership, residual, graph-node, binding, comparison, and Phase 6 handoff ledgers;
2. add generic invocation seed/root-input/bootstrap models with RED/GREEN crash tests;
3. add stable SUT capture and fresh-destination HEAD materialization;
4. add closed declarative input projection and compiler/planner/replay tests;
5. add generic secret-handle binding projection and lock/authentication tests;
6. implement and verify the production engine task host and terminal receipts;
7. implement the real Cursor confined process host;
8. make installed OpenCode/Cursor handlers consume exact locked adapter config and secrets;
9. close both Phase 3 provider-live fixture runs;
10. create `assurance-product` packaging, source catalog, two product providers, and declarations;
11. define the closed deployment manifest and implement the deterministic deployment-binding wheel builder;
12. define the data-only project-config schema and prove it cannot grant runtime authority;
13. resolve and authenticate the exact deployment wheel, project config tree, 33 routes, and 99 bindings;
14. assemble intake/case and reusable agent-triplet subgraphs;
15. assemble four generation branches and exact selected-family join;
16. assemble execution, quality, issue, healing, coverage loop, and report;
17. assemble archive, Retro, and Improvement graphs;
18. expose every public entrypoint and complete graph/reference/topology audits;
19. implement `aa-next bindings build/compile/start/run/status/resume/lock` interfaces;
20. implement safe result export;
21. implement canonical behavioral projection and isolated old/new harness;
22. run deterministic representative comparison matrix and fix high-confidence semantic gaps in their owning modules;
23. run committed-HEAD product/deployment wheel-isolation smoke;
24. run the complete OpenCode single-item benchmark;
25. run the complete Cursor single-item benchmark;
26. complete security/fault/replay/property audits and repository gates; and
27. publish Phase 5 acceptance plus exact Phase 6 cutover/deletion handoff.

Every task:

- begins with a failing public-interface, canonical-event, or behavioral-projection test;
- modifies one generic or semantic owner at a time;
- records RED, GREEN, focused, full, wheel, and live evidence as applicable;
- receives separate Spec and Standards review;
- commits independently;
- preserves existing unrelated worktree changes; and
- does not advance while a Critical or Important finding remains open.

Tasks 2–9 are product prerequisites, not permission to add Assurance behavior to the engine. Tasks 10–20 assemble and expose the product. Tasks 21–27 prove behavior and prepare deletion.

The plan must not include the Phase 6 default switch or legacy deletion.

## 27. Design closure

This specification fixes the Phase 5 seams:

- one product wheel, two explicit adapter-selected product entry points;
- six business wheels remain the only Assurance behavior owners;
- one explicit source-authenticated deployment binding wheel;
- one explicit data-only project configuration source;
- exact 33 routing assignments and 99 triplet aliases;
- no adapter, model, permission, endpoint, executable, or secret fallback;
- canonical root input and initial SUT seed;
- closed declarative graph data projection instead of product glue handlers;
- one generic production task host owned by the engine;
- adapter-owned installed configuration and Cursor confinement;
- OpenCode-owned session history and no duplicate context store;
- a temporary independent `aa-next` command;
- separate result materialization, not in-place SUT mutation;
- external isolated old/new comparison;
- semantic parity rather than internal event identity;
- mandatory full OpenCode and Cursor product benchmarks;
- no old/new imports or persisted-state bridge;
- no default `aa` switch; and
- Phase 6 as the only hard cut and deletion phase.

No implementation decision required for Phase 5 is intentionally left open. A proposal to add business hooks to the engine, executable SUT plugins, an ambient configuration scan, a model fallback, a shared Assurance runtime wheel, a legacy event translator, a dual-write driver, an in-place original-SUT publisher, or a Phase 5 default `aa` switch requires a new design review rather than an implementation-plan deviation.
