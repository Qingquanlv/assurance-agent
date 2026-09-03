# Attempt Kernel Production Closure and Internal Refactor Design

> **Status:** active corrective amendment; implementation has not started.
>
> **Date:** 2026-09-03. The requested historical filename is retained.
>
> **Release status:** the Python/LangGraph topology migration is present in source, but no
> Attempt-bearing Product path is production-complete. Agent roots reach deferred Agent executors,
> and the four earlier T5a roots reach deferred Task executors. The current revision must not be
> represented as having passed Checkpoint R until Phase P in this document is implemented and the
> protected gate passes for the exact release candidate.
>
> **Scope split:** Phase P closes the production transaction and is release-blocking. Phase I only
> reorganizes `AssuranceAttemptKernel` internally and is a separate, non-blocking follow-up.
> Phase P qualifies the `assurance-opencode` Product provider only. The installed
> `assurance-cursor` provider is explicitly not qualified by Checkpoint R.
>
> **Amends:** the completion/status language in
> [Python-native LangGraph Assurance Runtime](./2026-08-31-python-native-langgraph-assurance-design.md),
> [Python-native LangGraph Migration](../plans/2026-08-31-python-native-langgraph-migration.md),
> [Semantic Attempt Kernel](../plans/2026-08-31-semantic-attempt-kernel.md),
> [Feature StateGraph Migration](../plans/2026-08-31-feature-stategraph-migration.md),
> [LangGraph Product Cutover](../plans/2026-08-31-langgraph-product-cutover.md), and
> [Raw Agent Runtime Closure](../plans/2026-09-02-raw-agent-runtime-closure.md), plus every
> current-status, task-sequencing, phase-write-authority, and release-certification claim in
> sections 1, 3-5, 7-8, 10-18, and 20-22 of
> [Raw Agent Runtime Cutover Design](./2026-09-02-raw-agent-runtime-cutover-design.md).
> Those documents remain useful design history, but their 34-Agent/43-Attempt occurrence counts,
> claims that only T5a is selected, claims that T5b-T10 are unexecuted, or claims that the initial
> Checkpoint R is closed do not describe the merged source. The matching completion claims in
> `.superpowers/sdd/progress.md` are also non-authoritative.
> This amendment remains authoritative after the implementation plans are synchronized; plan
> synchronization adds reverse links and removes conflicting status text rather than ending this
> document's authority.
> T5a remains accepted as historical topology work; Phase P reopens its production qualification
> because those four roots still resolve to deferred Task executors.
>
> **Retained authority:** Raw Agent Runtime Cutover Design remains authoritative for strict raw
> result, transport, file-validation, and one-session semantics except where this document corrects
> current status, production-port wiring, occurrence counts, staged prepare/finalize writes,
> durable Effect-intent capture, Product-provider release scope, or Checkpoint R.

## 1. Decision

Do not remove transaction stages to shorten `kernel.py`, and do not restore a second Workflow
Runtime. Keep the merged Python-native LangGraph topology and the permanent Raw Agent model, then
close the missing production adapters around the existing Attempt transaction.

The work is divided into two independently accepted phases:

1. **Phase P — Production Closure:** replace the release-path placeholders, make raw activity,
   workspace, authorization, Effect, and journal recovery durable, and run a real candidate-bound
   Checkpoint R. This phase must complete before the `assurance-opencode` 14-root cutover can be
   released as production-capable.
2. **Phase I — Internal Refactor:** after Phase P is green, optionally extract four private
   collaborators from `AssuranceAttemptKernel`. This phase changes structure, not semantics, and
   cannot delay or retroactively close Phase P.

No new LangGraph node or edge is introduced by either phase. LangGraph remains the only Workflow
control authority; one Attempt node still invokes one transaction.

The provider-neutral Kernel/host/store repairs are shared infrastructure, but they do not certify
an adapter. `assurance-cursor` remains installable and offline-compilable; executable
start/run/resume for that provider fail before Invocation mutation with
`runtime_provider_not_qualified`. Cursor needs its own process-protocol matrix, binding identities,
credentials, and protected release gate (or an explicit removal decision) in a separate spec. It
cannot inherit an OpenCode Checkpoint R result.

## 2. Audited baseline

The baseline for this design is the merged revision `d2a866ea0848460454bb40139de9eb51e0c01301`.
At that revision:

- all 14 public entrypoints are selected as `langgraph-v1`;
- Workflow YAML, the compiler, planner, scheduler, settle loop, and `graph_engine.runtime` package
  have been removed;
- six Python Feature bundles, 14 Product roots, 41 semantic Attempt contracts, 33 Raw Agent
  bindings, 35 Agent occurrences, and 44 total Attempt occurrences are present;
- `AssuranceAttemptKernel`, `AttemptNodeFactory`, the isolated workspace implementation, the
  production host/worker protocol, terminal receipt store, and Effect settler exist in permanent
  modules;
- the OpenCode adapter uses ordinary prompt text and locally validates one assistant JSON object;
  it does not require OpenCode structured output or provider `response_format`.

The topology migration therefore does not need to be replayed. The release path is nevertheless
incomplete:

| Current surface | Production defect |
| --- | --- |
| `assurance_product.runtime_ports.DurableAttemptJournal` | pickles an in-memory object graph and rewrites a whole file; it is not a canonical, transactional CAS journal |
| `ProductRuntimePorts.open` | installs `MemoryResourceAuthorizationStore`, `_UnusedWorkspace`, placeholder secret objects, and omits production Validator/Effect/Schema wiring |
| `assurance_product.runtime_bindings` | freezes `_DeferredPhase` and `_DeferredTaskExecutor`; every real semantic executor raises when driven |
| Agent phase writes | Explore prepare emits a required staged context file, while Execution finalizers write evidence through `project_root`; this contradicts the older read-only-finalizer wording and lacks phase-specific delta enforcement |
| `AssuranceProductApplication` | accepts `InvocationRuntimeAuthorization` but does not pass it into runtime-port construction; its helper context also hard-codes fence `1` |
| production host activity RPC | reopens the former invocation `LedgerTaskActivityPort`, creating a second activity authority instead of using the Attempt journal |
| Effect registrations | construct in-memory Capability stores during static composition; Healing even constructs one unrelated store per handler |
| Effect key semantics | five handlers reject the Kernel's `(AttemptKey, ordinal)` settlement key as if it were a payload-derived business key; delivery carries a one-off compatibility branch |
| effectful deterministic executor | keeps `TaskOutcome.effects` in process-local `_effects`, so a crash can lose intents after output observation |
| `ProductRuntimePorts.test_kernel_resolutions` | allows tests to replace real Kernel execution and repair invalid selected inputs synthetically |
| `assurance_product.revision_registry` | embeds `CHECKPOINT_R_RELEASED_SHA`, synthesizes migration `DrainEvidence`, requires `fixture-model`, and certifies the stale 34-occurrence count inside Product source |
| `tests/product/test_raw_agent_checkpoint.py` | the live row is skip-gated, selects one binding, overrides `fixture-model`, invokes `OpenCodeHandler` directly, and uses an ad hoc `{\"ok\": true}` schema |
| normal CI | runs the focused test file without the live prerequisites, so the only external row may skip while CI stays green |

The existing `attempts.*` host, workspace, receipt, and security code is retained. Phase P wires and
hardens it; it does not create parallel replacements merely because Product composition is
incomplete.

## 3. Goals

- Execute every one of the 41 semantic contracts for `assurance-opencode` through real
  Product-owned runtime ports.
- Preserve one stable graph-facing call:

  ```python
  AssuranceAttemptKernel.execute_or_recover(
      attempt_key,
      resolved_contract,
      validated_input,
      execution_context,
  ) -> AttemptResolution
  ```

- Run each Agent contract as prepare → one OpenCode root session → local result validation →
  deterministic finalize inside one Attempt transaction.
- Resume the same Attempt after process death without knowingly admitting a second prompt,
  promotion, Effect, or terminal result.
- Bind runtime selection, provider/model, handlers, schemas, resources, secrets, and policy to the
  authenticated ProductLock/GraphRevision.
- Keep dry `aa compile` offline while making executable start/run/resume fail closed before graph
  execution when a required production port is unavailable.
- Make Checkpoint R a protected result for the exact source candidate and deployment artifacts,
  with no skip, waiver, fixture model, or direct-adapter shortcut.
- Qualify exactly the `assurance-opencode` provider without implying Cursor qualification.
- Make a later Kernel refactor easier to review without weakening any transaction invariant.

## 4. Non-goals

- No Structured Artifact Pipeline, typed artifact slots, Kernel JSON/YAML materializer, or
  materialization receipt.
- No OpenCode `format.json_schema`, provider-native structured output, or child-session hierarchy.
- No Workflow YAML, GraphDef lowering layer, custom scheduler, legacy runtime fallback, or new
  LangGraph node for prepare/finalize/effects.
- No project-loadable Python, handlers, validators, schemas, Effect implementations, or graph
  factories.
- No new business Validator binding merely to prove port plumbing; production contracts keep their
  declared tuples, currently empty.
- No migration of pickle Attempt state into the new canonical journal.
- No Cursor production qualification, Cursor process-host redesign, or removal of the installed
  `assurance-cursor` entrypoint.
- No line-count target for `kernel.py` and no public API for the private Phase I collaborators.

## 5. Authority and invariants

| Concern | Sole authority |
| --- | --- |
| branch, subgraph, retry between business activations, human interrupt | Python LangGraph Feature/Product graphs |
| Attempt identity, transaction order, recovery decision, final resolution | `AssuranceAttemptKernel` |
| resolution-to-state/interrupt mapping | `AttemptNodeFactory` |
| installed input/result/output models and prepare/finalize business logic | Capability wheel |
| runtime binding and concrete production adapter lifetime | `assurance-product` |
| OpenCode session/message/admission/observation protocol | `agent-runtime-opencode` |
| Raw Agent composition and local `AgentResultT` validation | `agent-runtime-contracts` |
| Attempt and resource record protocols | `graph_engine.persistence` |
| staging, seal, durable prepare, promote/recover | `graph_engine.attempts.workspace` |
| host isolation, secret transport, terminal host receipt | `graph_engine.attempts.*` host modules |
| Effect apply/reconcile state machine | existing `AttemptEffectSettler` plus installed Capability handler |
| release admission | protected external Checkpoint R job |

The following invariants are non-negotiable:

1. one business activation produces one stable `AttemptKey`;
2. one `AttemptKey` authorizes at most one OpenCode root session and one admitted prompt;
3. a stale fencing token cannot dispatch, bind, prepare, promote, apply an Effect, publish a
   terminal result, or release a newer authorization;
4. OpenCode and installed prepare/finalize handlers mutate only their declared portions of the
   authenticated staging workspace; Capability finalizers validate raw file contents; the Kernel
   commits only the sealed aggregate mutation set;
5. external activity success is not Attempt success; output validation, validators, durable
   prepare, promotion, Effects, and terminal durability must still complete;
6. `PendingTaskResult` and `IndeterminateTaskResult` are mapped by `AttemptNodeFactory` to durable
   system interrupts; LangGraph never receives an `AttemptResolution` directly;
7. human interrupt nodes are pure; a system interrupt may originate from the idempotent Attempt
   node and resume it from the beginning;
8. an Effect intent is durable before its source executor state or host receipt can be discarded;
9. Product source cannot claim a release gate passed; only the protected result for the exact
   candidate can do that;
10. a durable terminal event is not returned to LangGraph until the resource store proves release
    and the Attempt journal durably records that proof.

## 6. Phase P target architecture

### 6.1 Compile and execution lifetimes

Compilation and invocation execution have different dependency closures.

`aa compile` authenticates installed Product/Capability metadata, builds the Python graphs, and
emits ProductLock/GraphRevision data. It must not open the invocation database, resolve a secret,
spawn a worker, or contact OpenCode.

Executable start/run/resume first load and authenticate the selected GraphRevision, ProductLock,
root identity, and `InvocationRuntimeAuthorization`, then open `ProductRuntimePorts` for that exact
entrypoint and invocation. Immediately before a graph mutation, `AssuranceApplication` acquires the
runner lease and uses the post-acquire execution factory defined below. Read-only status/lock/export
operations open only the stores they require and do not resolve secrets or contact OpenCode.

The executable composition root becomes conceptually:

```python
async with ProductRuntimePorts.open(
    workspace=workspace,
    composition=composition,
    invocation=invocation_identity,
    authorization=runtime_authorization,
    reachable_contract_ids=reachable_contract_ids,
) as ports:
    factory = ports.execution_factory(
        entrypoint=entrypoint,
        root_input_digest=root_input_digest,
    )
    await application.run(
        invocation_id=invocation_identity.invocation_id,
        execution_factory=factory,
    )
```

Mutating framework calls receive neither an invocation-bound `BootArtifact` nor a completed
`AssuranceRuntimeContext` before the runner lease exists. `AssuranceApplication.start`,
`start_and_run`, `run`, and `resume` instead accept an `InvocationBoundExecutionFactory`. After
`_hold_lease` acquires the authoritative `RunnerLease`, the Application invokes that factory
exactly once inside the lease lifetime:

```python
bound = await execution_factory.bind(
    runner_lease=lease,
)
# bound contains one BootArtifact, AssuranceRuntimeContext, and CheckpointAnchorState
```

`bind` authenticates the invocation and authorization again, constructs `CheckpointAnchorState`
with the exact `lease.fencing_token`, creates the invocation-bound `AnchoredCheckpointer`, compiles
the 14-root `BootArtifact`, and returns its matching `AssuranceRuntimeContext` as one value. It never
accepts a caller-supplied fence. The Application writes that same token into LangGraph configurable
state and rejects disagreement between the lease, checkpointer identity, context, and stored
invocation pin. `AttemptNodeFactory` removes its default fence of `1`: missing or unequal tokens fail
before Kernel entry. The bound artifact, context, and every `AuthorizedAttemptScope` expire with
that lease. The Application completes or safely reconciles the bound checkpointer outbox before it
releases the lease; outer port shutdown performs no invocation mutation.

Read-only status inspection loads the authenticated persisted anchor identity and compiles a
strictly read-only graph/checkpointer view. Its `put`, pending-write, recovery, dispatch, workspace,
Effect, and resource-release operations all fail closed. An outbox that requires recovery is
reported as requiring `aa run`/`aa resume`; status does not acquire a new fence merely to inspect
state.

`reachable_contract_ids` is derived from the authenticated Product graph inventory, not from
untrusted runtime input. The runtime still resolves all 41 contracts because the current
`BootArtifact` compiles all 14 roots together; the set controls which external dependencies and
secret authorizations must pass executable preflight. An entrypoint whose reachable closure
contains no Agent contract does not need a live OpenCode endpoint merely because another Product
root does. Checkpoint R deliberately preflights and executes the full 41-contract closure.

### 6.2 Production composition order

`ProductRuntimePorts.open` owns one invocation-scoped outer lifetime and assembles dependencies in
this order:

```text
authenticate ProductLock / GraphRevision / invocation / runtime authorization
  → open descriptor-pinned ChangeWorkspace binding
  → open shared Product SQLite backend (synchronous=FULL)
  → construct SQLite Attempt journal and resource-authorization store
  → construct Attempt checkpoint observer; install and seal observer set
  → construct TaskWorkspaceStore + TaskWorkspaceProvider
  → construct dedicated activity terminal-receipt store
  → construct runtime secret/network policy
  → construct TaskExecutionHost through the production factory and journal-backed activity bridge
  → construct runtime Effect registry with durable Capability stores
  → resolve all 41 Agent/Task contracts to real executors
  → preflight the selected root's exact reachable external dependencies
  → construct AssuranceAttemptKernel with journal, arbiter, workspace,
    Validators, runtime Effects, Schemas, and GraphRevision
  → expose an InvocationBoundExecutionFactory

after AssuranceApplication acquires the RunnerLease:
  → create the exact-fence CheckpointAnchorState and AnchoredCheckpointer
  → construct AttemptNodeFactory and compile the invocation-bound 14-root BootArtifact
  → create the matching AssuranceRuntimeContext
```

The lease-bound execution closes and recovers its checkpoint/observer outbox before lease release.
Outer shutdown then reverses the port-construction order and explicitly calls
`TaskWorkspaceStore.close()` without performing an invocation write. The runtime-owned 41-row
resolved-contract mapping is immutable for the lifetime and is never written back into
`FrozenComposition`. Root reachability limits external preflight; it does not leave deferred
executors in the other compiled roots.

Static composition contains authenticated contract/binding/factory specifications. It must not
capture invocation-owned executors, SQLite connections, workspaces, secrets, or `_DeferredPhase`
objects.

Before graph execution, Product boot rejects a missing handler, binding, model, policy, schema,
secret authorization, Effect store, Validator ID, or production port required by the selected
root. It also rejects every `assurance-cursor` executable request as unqualified before opening an
Invocation. The following are forbidden on the `assurance-opencode` executable release path:

- `_DeferredPhase`, `_DeferredTaskExecutor`, and `_UnusedWorkspace`;
- `DurableAttemptJournal` backed by pickle;
- `MemoryAttemptJournal` or `MemoryResourceAuthorizationStore`;
- in-memory Healing/Improvement Effect stores;
- `_TolerantAttemptFactory`, `_scripted_input`, `test_kernel_resolutions`, or equivalent hooks;
- `object()` in place of a typed secret/workspace/runtime port;
- `fixture-model` or an environment override that disagrees with the locked runtime binding.

### 6.3 Shared SQLite persistence

Add Product-owned `SqliteAttemptJournal` and `SqliteResourceAuthorizationStore` adapters over the
same SQLite connection and async lock already owned by `AssuranceSqliteBackend`. They remain
separate typed state machines and tables; sharing a transaction substrate does not merge their
semantics with LangGraph checkpoints.

The backend lifecycle must allow this sequence: open/configure connection, create the Attempt
journal, create `AttemptCheckpointObserver`, install the complete observer tuple exactly once, then
seal the backend before any root is compiled. Observer mutation after sealing fails. This removes
the current construction cycle in which `open_sqlite_checkpointer(observers=...)` asks for an
observer before its SQLite Attempt journal exists.

The Attempt journal uses a versioned canonical JSON envelope, never pickle. Required behavior:

- key by `AttemptKey.digest` and monotonically increasing record revision;
- encode the closed event discriminator and explicit event schema version;
- validate the record digest, Attempt identity, expected revision, and fence before fold;
- append under `BEGIN IMMEDIATE` and commit before returning;
- make exact replay idempotent and reject different bytes at the same revision;
- reject gaps, unknown event kinds/versions, non-canonical JSON, corruption, and stale fences;
- make `ensure_durable` a verified committed-revision barrier;
- return the same typed terminal resolution after process restart.

The authorization store durably records acquire/adopt/release and reconstructs active grants after
restart. Same-key replay under the same live fence reuses a grant. Adoption requires the same
`AttemptKey`, a strictly newer live fence, and proof that the previous runner lease is no longer
current. A stale runner cannot release a newer grant.

### 6.4 Authenticated execution scope

After authorization and workspace open, the Kernel creates one private, non-serialized value:

```python
@dataclass(frozen=True, slots=True)
class AuthorizedAttemptScope:
    execution: AttemptExecutionContext
    workspace: TaskWorkspaceBinding
```

Resolved executor calls receive that scope. It is the only way an executor obtains the already
authenticated staging binding; an executor cannot reopen a workspace or derive a path from model
output.

The internal executor result and its source receipt reference are explicit:

```python
@dataclass(frozen=True, slots=True)
class TerminalReceiptRef:
    identity_digest: str
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class ExecutedAttemptResult(Generic[OutputT]):
    output: OutputT
    effects: tuple[EffectIntent, ...]
    source_terminal_receipt: TerminalReceiptRef | None


ExecutorResolution: TypeAlias = (
    RejectedTaskResult
    | PermanentTaskFailure
    | PendingTaskResult
    | IndeterminateTaskResult
)
ExecutorStepResult: TypeAlias = ExecutedAttemptResult[OutputT] | ExecutorResolution


class AttemptExecutor(Protocol[InputT, OutputT]):
    async def execute(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
    ) -> ExecutorStepResult[OutputT]: ...

    async def reconcile(
        self,
        validated_input: InputT,
        scope: AuthorizedAttemptScope,
        snapshot: AttemptSnapshot,
    ) -> ExecutorStepResult[OutputT]: ...
```

Committed resolutions are intentionally absent: only the Kernel can commit. All 33 Agent adapters
and all eight Task adapters implement both methods. A Task adapter's `reconcile` may
deterministically re-execute only when the snapshot proves that no external activity was admitted;
it cannot infer safety from missing process memory.

This result replaces process-local `declared_effects` state. The Kernel validates the output and all
Effect intents, then appends `ActivityTerminalObserved` and the corresponding
`EffectIntentRecorded` events in one CAS batch before continuing. Recovery reads both from the
journal; it never asks an executor object to remember what happened in a previous process.

The four-argument public Kernel call remains unchanged. This design does not add the unrealized
`BoundAttemptDispatch` object described by older planning text.

### 6.5 Real Agent and Task executors

For a qualified `assurance-opencode` Invocation, each of the 33 Agent contracts resolves at open to
one real `ResolvedRawAgentExecutor`. Its flow is:

```text
installed prepare handler(validated input, authorized scope)
  → validated AgentRunRequest using the locked binding
  → execute/adopt/reconcile one OpenCode root session
  → authenticated durable TaskHostTerminalReceipt(raw TaskOutcome)
  → exact assistant JSON + local AgentResultT/schema validation
  → RawFinalizeBundle(read-only raw workspace)
  → installed deterministic finalizer
  → validated OutputT + captured EffectIntent tuple + source receipt reference
```

All finalizers keep the existing closed bundle:

```text
RawFinalizeBundle
  validated_input
  prepared
  agent_result
  run_evidence
  raw_workspace
```

Prepare and finalize may not publish external Effects. They do have deterministic staged-write
authority where an installed contract declares it: for example, prepare emits the Explore context
and Execution finalizers emit canonical evidence. Those bytes are part of the Attempt candidate;
they are not direct project writes or a hidden side channel.

All three Agent phases receive the same authenticated staging-root identity so a prepared
`AgentWorkspaceV1.write_root` remains valid during runtime and recovery. The contract binds three
closed, installed write-claim sets:

- prepare claims only deterministic request/context artifacts;
- runtime claims only the model-produced raw output paths;
- finalize claims only deterministic evidence/result artifacts and cannot overwrite runtime-owned
  raw outputs.

The host measures the staging tree before and after each phase, validates that phase's exact delta
against its own claim set, and persists the measured delta with the phase result. The Kernel's
later seal validates the aggregate against the Attempt-level union and commits the actual bytes.
No phase may self-report or expand its paths at runtime. `ReadOnlyRawWorkspace` becomes a
descriptor-pinned read service with no public raw `Path`; finalizers read runtime-owned bytes only
through that service and write their separate declared outputs through the staging writer/root.

Capability handlers that currently write through `TaskContext.project_root`, including Execution
result evidence, migrate to the authenticated staging writer/root. The host pins and measures the
canonical project view around every phase; any direct project mutation, undeclared staging delta,
link/mode/ownership drift, or overlap with another phase's authority is a permanent pre-commit
failure. Installed Capability handlers remain trusted code under the existing production-host
boundary; these controls do not claim to sandbox a malicious installed wheel.

Checkpoint R executes the real prepare artifacts and finalizer evidence writers, attempts direct
project writes and cross-phase staging writes, and proves that only declared deltas reach the final
seal. Any non-success phase `TaskOutcome` is reduced to the declared typed Attempt failure.

The eight deterministic Task contracts resolve through one Product-owned handler adapter and the
same production host. Their successful `TaskOutcome.output` becomes `OutputT`; their
`TaskOutcome.effects` enters `ExecutedAttemptResult.effects`. They do not call OpenCode, but their
authorization, workspace, commit, Effect, and receipt behavior is identical where declared.

Runtime resolution reads the installed, authenticated `CapabilityBindingEntry` fields—target
capability, binding data, resource IDs, and secret handles—and the matching ProductLock projection.
It does not rebuild production bindings from `_DEFAULT_MODEL`, `_catalog_binding()`, environment
variables, or a phase alias. Provider, fully qualified model, limits, policy, adapter configuration,
and secret-handle set are fixed before execution and enter the binding digest.

For an Agent contract, `ResolvedAttemptContract.contract_digest` is the digest of the complete
`AgentExecutionContract.canonical_projection()`, including prepare/finalize handlers, skill,
`AgentResultT` schema, resources, policies, and output model. It must not fall back to the lossy
`TaskAttemptContract` projection. Deterministic Task contracts continue to use their complete
`TaskAttemptContract` digest. ProductLock, the runtime binding row, and the Attempt journal all
agree on the same digest before dispatch.

A Product-owned phase adapter constructs `TaskRequest` and `TaskContext` from the authorized scope
and authenticated registries. Host identity includes:

- `attempt_key_digest`;
- `authorization_id` and fencing token;
- closed phase discriminator `prepare | runtime | finalize | task`;
- handler/capability ID and source digest;
- workspace identity;
- GraphRevision/ProductLock identity;
- provider/model and policy identities for the runtime phase.

The phase-specific `task_id` is the canonical digest of `(AttemptKey, phase, handler_id)` so the
three host calls cannot collide. Only the runtime phase receives an activity port and secret
handles.

### 6.6 One activity authority

Install `JournalBackedTaskActivityPort` as the only owner of durable activity prepare, dispatch
admission, bind, observe, reconcile, and cancel state. It folds and appends the existing Attempt
journal events. The production host must no longer open `LedgerTaskActivityPort` or any second
invocation ledger for activity RPC.

The Kernel removes its current generic pre-dispatch fingerprint write. The OpenCode handler calls
`mark_dispatch_started()` with the actual provider-specific admission fingerprint, and that one
value is the durable authority; two independently derived fingerprints would drift on replay.

The handler-facing `TaskActivityPort` stays synchronous. The parent already drives the worker in a
dedicated thread. That driver receives the owner event loop; `snapshot`,
`mark_dispatch_started`, and `bind` all submit the corresponding async journal operation with
`asyncio.run_coroutine_threadsafe`, bounded by the remaining host-call deadline. The activity RPC
response is returned only after the journal append commits. Do not create a second synchronous
journal, call `run_until_complete`, or buffer an acknowledgement for later flushing.

The parent constructs this proxy from the authenticated host call and explicitly supplies the
request digest and workspace identity needed by `TaskActivitySnapshot`; those values are not
invented from an `AttemptSnapshot` that does not contain them. Before each activity snapshot
mutation and before accepting/installing a terminal host receipt, the parent verifies the current
runner lease, authorization ID, fencing token, Attempt key, request digest, and workspace identity.

The recovery mapping remains closed:

| Durable activity state | Next action |
| --- | --- |
| no activity / `not_dispatched` | prepare and execute this same Attempt once |
| dispatch fingerprint durable, no proven binding | discover/authenticate the same session or return indeterminate |
| session bound, admission uncertain | reconcile admission; never blindly send again |
| prompt admitted/running | observe the same session or return pending |
| terminal host receipt durable | reuse its exact raw `TaskOutcome`; do not call OpenCode |
| final output/effect batch durable | skip executor and continue/recover commit |

`TaskActivityRpcIdentity`, `TaskHostCallIdentity`, and terminal receipts are versioned to bind the
Attempt key, authorization, fence, phase, workspace, request, and host implementation. Old wire
records are never silently interpreted as the new version.

`graph_engine.attempts.production_host` exposes one narrow factory returning the existing
`TaskExecutionHost` protocol; Product does not import or instantiate the private
`_ProductionTaskExecutionHost` class. The public factory receives the authenticated invocation
root, runtime authorization, installed handler registry and import roots, `TaskWorkspaceStore`,
`JournalBackedTaskActivityPort` factory, and `TerminalReceiptStore`, and returns a fully bound,
immutable host. `TaskExecutionHost` itself remains the four-operation execute/reconcile/cancel/
read-receipts protocol; it gains no post-construction bind method. Product cannot mutate host
dependencies after the factory validates and seals them.

Host terminal receipts use a dedicated authenticated invocation-scoped directory, for example:

```text
qa/changes/<change>/.runtime/activities/<invocation>/receipts/
```

They do not share `InvocationWorkspaceBinding.receipts_root`, because that root also contains
workspace prepare/promotion artifacts and `TerminalReceiptStore` parses every non-hidden child as
a host receipt.

`ActivityTerminalObserved` continues to mean the validated final `OutputT`, not the raw
`TaskOutcome`. Its next event version adds only the source host-receipt identity digest and receipt
digest. The raw outcome remains in the immutable host receipt. Host receipts are retained with the
Invocation/change tree and move with its authenticated lifecycle archive. Attempt execution does
not delete them or introduce a receipt-deletion crash window.

### 6.7 Workspace, secrets, and network

Construct the existing descriptor-pinned `TaskWorkspaceStore` from the three concrete fields of
`ChangeWorkspace.runtime_binding()` and wrap it in `TaskWorkspaceProvider`. Do not pass the binding
object as a single constructor argument.

The existing workspace invariants remain:

- replay opens/authenticates the same Attempt root;
- writes are restricted to resolved `ResourceClaims.writes`;
- traversal, symlink/hardlink substitution, undeclared/missing/extra files, mode drift, size
  violations, and control-tree mutation fail before prepare/promotion;
- seal measures actual bytes and mutations, not a model-reported manifest;
- durable prepare and promote/recover are idempotent and receipt-bound.

`InvocationRuntimeAuthorization` is passed from Product Application into `ProductRuntimePorts` and
authenticated against the selected bindings. Secret bytes resolve only inside an authorized host
call and are revoked afterward. Only handles and digests may enter requests, logs, checkpoints,
journals, and receipts. OpenCode network access is restricted to the locked endpoint/target policy;
redirect or resolved-target drift fails closed.

### 6.8 Validators and Effects

The Kernel receives all installed Validators, but executes only the ordered IDs declared by the
resolved contract. The current production declarations remain 25 registered and zero bound. A
test-only installed contract may bind two Validators to prove ordering, accept, and reject through
the real port; it does not enter production counts or ProductLock.

Reuse the existing `AttemptEffectSettler`. It consumes only the Effect intents already persisted in
the Attempt snapshot and never consults executor process memory.

Static `EffectRegistration` currently embeds a concrete in-memory handler, which cannot be changed
to a durable store by mutating private fields without invalidating contribution authority. Phase P
therefore replaces that field in the existing installed contribution path; it does not add a
parallel untrusted registry:

```python
StoreT = TypeVar("StoreT")


class EffectRuntimeFactory(Protocol[StoreT]):
    def bind(self, store: StoreT) -> DurableEffectHandler: ...


@dataclass(frozen=True)
class EffectRegistration(Generic[StoreT]):
    kind: str
    intent_schema_id: str
    receipt_schema_id: str
    policy: EffectPolicy
    store_contract: EffectStoreContract[StoreT]
    factory: EffectRuntimeFactory[StoreT]
    factory_provenance: ExecutableProvenance
    apply_provenance: ExecutableProvenance
    reconcile_provenance: ExecutableProvenance
```

`EffectStoreContract` is installed, authenticated, immutable metadata rather than a free-form
string. `StoreT` is the Capability-owned `HealingStore` or `ImprovementStore` protocol, never a
generic property bag. `PluginContribution.effects` keeps its single field but now accepts only this
factory-bearing `EffectRegistration`; the concrete `handler` field is removed. `CapabilitySpec`
publishes those registrations instead of constructing in-memory handlers during composition.

`EffectContributionProjection` and ProductLock add the store-contract ID/digest, factory
implementation/digest, and declared apply/reconcile implementation digests. Contribution authority
gets a closed `EFFECT_FACTORY` executable kind and authenticates the exact factory object plus those
method projections. Static `FrozenComposition` exposes an authenticated
`effect_factory_registry`, not a concrete Effect handler registry. Product supplies one
SQLite-backed store per family, validates its adapter against the exact store contract, invokes the
authenticated factory, and verifies the returned handler against the locked apply/reconcile
projections before sealing the concrete invocation-scoped `runtime_effect_registry`. No
`object`-typed, project-provided, or post-lock replacement seam exists. The Kernel receives only
that runtime registry. This is dependency injection among installed wheels, not a SUT plugin
surface.

The two store contracts and six exact Effect kinds are:

| Store contract | Effect kinds |
| --- | --- |
| `assurance.healing.store.v1` | `assurance.healing.effect.allocation.v2`, `assurance.healing.effect.heal-apply.v2`, `assurance.healing.effect.proposal-approved.v1` |
| `assurance.improvement.store.v1` | `assurance.improvement.effect.archive.v1`, `assurance.improvement.effect.delivery.v1`, `assurance.improvement.effect.promotion.v1` |

Both stores share the Product SQLite substrate/lock but retain Capability-owned schemas and state
machines. Phase P separates two identities that current handlers overload:

- `settlement_key = effect_idempotency_key(AttemptKey, effect_ordinal)` is the Kernel's exact
  Attempt replay key and the durable store's primary key;
- `business_key` is derived by the Capability from the authenticated intent payload
  (`operation_id`, `approval_id`, `record_key`, or the corresponding Improvement key) and is the
  external/domain idempotency identity.

All six handlers accept the Kernel settlement key, independently derive and validate the business
key, and call the external operation with the business key. The family store atomically records
`(effect_kind, settlement_key, business_key, intent_digest, payload, receipt)` with a unique
`(effect_kind, business_key)` index. Same settlement plus different intent is corruption. The same
kind/business key from another Attempt may reuse the identical committed receipt but may not
reapply the external operation; different intent under that identity is a permanent conflict. New
receipt schema versions retain
`idempotency_key` as the business key and add the explicit `settlement_key`. The delivery-only
`_kernel_settlement_key` compatibility branch is removed. A committed record is observed or
reconciled after restart, never applied again.

## 7. Normative transaction

The production transaction is:

```text
derive stable AttemptKey                         [AttemptNodeFactory]
  → load/open and authenticate identity          [Attempt journal]
  → finish/verify terminal resource release, if terminal is present
                                                   [Kernel + resource authorization]
  → return durable terminal replay only after release proof
                                                   [Kernel]
  → acquire/reuse/adopt resources                [Resource authorization]
  → open/authenticate isolated workspace         [Workspace provider]
  → execute/adopt/reconcile installed executor   [Agent or Task executor]
      Agent only:
        prepare → one OpenCode session → raw host receipt
        → local AgentResultT validation → deterministic finalize
  → validate OutputT and Effect intents          [Kernel + Schemas]
  → atomically record final output, source receipt link, and Effect intents
                                                   [Attempt journal]
  → seal actual candidate bytes                  [Workspace provider]
  → run ordered declared Validators              [Kernel]
  → durable workspace prepare                    [Workspace provider]
  → atomic promote or recover                    [Workspace provider]
  → apply/reconcile persisted Effects in order   [AttemptEffectSettler]
  → durably record terminal result + receipt     [Attempt journal]
  → adopt old same-Attempt grant if needed, then durably release it
                                                   [Resource authorization]
  → durably record ResourcesReleased proof       [Attempt journal]
  → publish state update or system interrupt     [AttemptNodeFactory]
```

For a deterministic Task, the Agent-only subtrace is absent. The commit tail is unchanged.

The immutable commit order is:

```text
validate output/effects
→ record output/effect intents
→ seal
→ ordered validators
→ durable prepare
→ promote/recover
→ Effects
→ terminal receipt
→ resource-store release
→ ResourcesReleased proof
```

Effect intents are recorded before sealing rather than discovered after promotion. This is not a
new business effect: it closes a durability hole in the current process-local
`declared_effects` implementation.

Terminal and resource release remain two durable authorities, so the Kernel uses a closed recovery
protocol rather than pretending the writes are atomic. `ResourcesReleased` changes meaning to
“the authorization store release has already been proved” and is never batched ahead of that store
write. On a terminal fast path, the Kernel inspects the resource store before returning. If the
same Attempt still owns a grant under an older fence, the current live runner durably adopts that
grant solely for terminal cleanup, releases it, appends `ResourcesReleased`, and calls
`ensure_durable`. If no active grant remains, it appends the missing proof idempotently. A release
marker paired with an active grant is an integrity failure, not a successful replay.

## 8. Recovery and failure semantics

| Crash cut or condition | Required recovery |
| --- | --- |
| before resource grant commits | reevaluate durable claims; never assume ownership |
| grant committed, before workspace open | reuse the same grant and binding under the current fence |
| prior runner dead, newer fence acquired | durably adopt the same Attempt; old runner can no longer mutate or release |
| workspace open, before deterministic prepare completes | rerun prepare against the same binding |
| before OpenCode creation | execute the same admission protocol once |
| session created, before durable bind | discover/authenticate that session or return indeterminate |
| bound, prompt acknowledgement uncertain | reconcile admission; do not resend |
| acknowledged/running | observe the same session; return pending if no terminal result |
| provider terminal, before host receipt | reread the same bound session and install the receipt; do not readmit |
| host receipt durable, before finalize | reuse receipt, rerun deterministic validation/finalize without OpenCode |
| finalize returned, before output/effect batch | rebuild from the same receipt and append one identical batch |
| output/effect batch durable, before seal | replay persisted output/effects; do not invoke executor |
| Validator rejects | terminal rejected; zero durable prepare/promotion/Effects |
| durable prepare, before promotion | recover the authenticated prepared write set |
| promotion publication uncertain | prove committed or return indeterminate; do not restage new bytes |
| promoted, before/during Effect | reconcile the same Effect idempotency key; do not promote again |
| terminal durable, resource grant still active | under the current live fence adopt the same-Attempt grant for cleanup, release it, append durable `ResourcesReleased`, then return terminal |
| resource store released, before `ResourcesReleased` | prove no active same-Attempt grant, append the idempotent release proof, then return terminal |
| `ResourcesReleased` present but resource store active | integrity failure; never return terminal |
| terminal and release proof durable, before LangGraph checkpoint | return the same typed resolution and complete checkpoint anchoring |
| stale fence at any irreversible cut | stop before the cut; never release or commit for a newer runner |

Invalid assistant JSON, local schema failure, invalid files, undeclared writes, and invalid finalizer
output are permanent pre-promotion failures. Resource contention and a proven-running activity are
pending. Unprovable prompt admission, promotion publication, or Effect publication is
indeterminate. A permanent Effect failure after promotion is `CommittedEffectFailure`, never a
rollback fiction.

## 9. Checkpoint R

Checkpoint R is an external protected CI/release result. It is not a runtime service, source-code
constant, status checkbox, or second deployment registry.

Phase P deletes the migration-only `CHECKPOINT_R_RELEASED_SHA`, synthesized `DrainEvidence`,
`fixture-model` release assertion, stale 34-occurrence assertion, test-source scanner, and legacy
deletion authorization path from `revision_registry.py`. That module retains only authenticated
GraphRevision retention/reopen behavior still used at runtime. The protected CI/release system
creates and verifies the result; Product source neither consumes nor manufactures it.

### 9.1 Candidate identity

One result binds the CI-provided candidate SHA to:

- exact Product provider `assurance-opencode`;
- the exact checked-out source and wheel digests deployed from it;
- official OpenCode binary version and digest;
- adapter distribution/source digest;
- exact provider/model for every binding, with no `OPENCODE_MODEL` override;
- 33 Agent contract IDs/digests/result-schema digests;
- 33 `assurance-opencode` runtime binding digests;
- exact 35-Agent-occurrence multiset, identified by
  `(feature factory, graph path, semantic_node_id, contract_id)`;
- all 41 semantic contracts and 44 Attempt occurrences;
- ProductLock, GraphRevision, workspace/security/network policy digests;
- protected workflow and CI run identity.

The result is emitted only after every required row succeeds. Deployment depends on the protected
check. Runtime code never reads or manufactures that artifact.

### 9.2 Required layers

Checkpoint R contains all of the following:

1. **Inventory/authentication:** exact 33/33/35/41/44 identities and digest equality, not only
   counts.
2. **Contract matrix:** all 33 Agent contracts execute through Product runtime assembly, installed
   prepare, exact locked OpenCode binding, local result validation, installed finalizer, and Kernel
   output validation. The occurrence inventory records both repeated Agent contracts
   (`case-design` and `issue-analysis`) and the repeated deterministic `evaluate-memory` contract;
   repeated occurrences do not require duplicate qualification calls for one identical contract
   binding.
3. **Production transaction:** real `ProductRuntimePorts`, `AttemptNodeFactory`, SQLite journal and
   authorization, workspace, host/worker, terminal receipt, Validators, prepare/promote, all six
   Effects, terminal receipt, and checkpoint observer. No scripted Kernel result is permitted.
4. **Recovery/security:** all cuts in section 8, one-prompt proof, fencing, unauthorized write/link/
   traversal/mode/size cases, secret canaries, target policy, interrupt anchoring, and lifecycle
   reopen/status/export/archive.
5. **Raw OpenCode protocol:** create, admission, message-list and single-message reads, terminal
   reduction, restart adoption, repeated read, error, cancellation, and reconcile through the same
   production path. The outbound prompt contains the installed JSON Schema text but no structured
   `format` field.
6. **Negative architecture:** no Structured Artifact path, typed materializer, provider-structured
   capability claim, placeholder port, pickle journal, in-memory production Effect store, or
   legacy Workflow authority.
7. **Repository gate:** lint, format, types, import contracts, full tests, and all wheel smoke tests
   pass for the same candidate.

The repository-gate command set is exact: `uv run ruff check .`,
`uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, `uv run pytest`, the
focused `uv run pytest -q tests/product/test_raw_agent_checkpoint.py`, and all three smoke scripts
(`scripts/graph_engine_smoke_test.sh`, `scripts/assurance_capability_wheel_smoke_test.sh`, and
`scripts/assurance_product_wheel_smoke_test.sh`). Checkpoint R adds its protected live matrix; it
does not replace any command in this set.

Deterministic fault/security tests may use an installed loopback transport. The 33 contract-matrix
rows use the official OpenCode binary and the exact locked provider/model. A direct call to
`OpenCodeHandler`, an ad hoc `{\"ok\": true}` result schema, one representative binding, or a
fixture model does not satisfy the matrix.

### 9.3 CI behavior and invalidation

Local developers may explicitly skip the external matrix when credentials are absent. The
protected Checkpoint R job sets its required environment and fails preflight if the OpenCode
binary, credentials, provider config, model, or any contract row is absent. Skipped, xfailed,
waived, cancelled, timed-out, or partially selected rows make the gate red.

The normal credential-free CI suite remains required but is not renamed “Checkpoint R.” The
protected workflow runs after the final source/documentation candidate is frozen. Any change to
the candidate SHA or to an identity component listed in section 9.1—including wheel, binding,
ProductLock, GraphRevision, OpenCode, provider, model, policy, and workflow identities—invalidates
the complete result. Environment/download caches may be reused; results from another candidate or
identity set cannot.

The current `test_live_opencode_cutover_binding_records_checkpoint_r` is replaced, not merely
unskipped. Its direct one-row adapter probe may remain as an adapter integration test under a
non-gate name, but it cannot certify Product execution.

## 10. Rollout and revision policy

PR #34 already removed the in-repository legacy execution engine and selected 14/14 LangGraph
roots. Phase P does not recreate a selector or roll back topology. Instead:

1. treat `d2a866ea` and descendants without Phase P as non-releasable for every Attempt-bearing
   Product root under either provider, including the four T5a deterministic roots;
2. make executable Product assembly permanently fail closed before graph execution whenever any
   reachable required production port, Agent executor, or Task executor is unresolved;
3. implement Phase P as one reviewed candidate series;
4. freeze the final source and documentation candidate, then run the full repository gate and
   Checkpoint R against that exact candidate;
5. release that exact passing candidate for `assurance-opencode` without a follow-up source or
   documentation edit; keep `assurance-cursor` executable admission red.

The new result qualifies only that remediation candidate. It cannot retroactively prove that the
historical T5b, T5c, T5d, T7, T8, T9, or T10 commits satisfied their missing release gates.

There is no pickle migration. Before opening or writing the canonical SQLite Attempt journal, the
Phase P binary checks the selected Invocation workspace. If `attempts.pkl` exists, it rejects the
Invocation with a machine-readable `previous_runtime_required` error and leaves both formats
untouched. Because the current pickle is change-wide and is never removed on terminal completion,
its presence permanently pins that whole change workspace to the previous deployment; merely
finishing one Invocation does not make the workspace eligible for Phase P. Moving such a workspace
to the new binary requires an independently approved migration or terminal-state quarantine
protocol, both outside this spec. Phase P deploys only to new or otherwise clean workspaces and
neither scans unrelated environments nor deletes, converts, quarantines, or routes legacy state.
The previous pinned deployment remains responsible for status, completion, export, and archive of
pickle-bearing workspaces.

The former T6 “zero resumable legacy Invocations” condition was external deployment state and
cannot be reconstructed from this repository. Phase P tests seed `attempts.pkl` in the selected
workspace and prove fail-closed rejection before any canonical journal write. Operational drain is
a deployment prerequisite owned outside this codebase, not a repository-generated certificate or
a reason to restore dual execution.

## 11. Phase I — private Kernel refactor

Phase I starts only after Phase P and Checkpoint R are green. It receives its own implementation
plan and candidate. The public Kernel signature, Attempt events, resolution mapping, transaction
order, crash behavior, and same-revision replay remain unchanged.

The target is one short coordinator over four private collaborators plus the existing settler:

```text
AssuranceAttemptKernel.execute_or_recover
  ├── AttemptJournalState
  │     identity, load/open/adopt, CAS append, replay, terminal durability
  ├── AuthorizedAttemptLease
  │     acquire/adopt, fence assertions, terminal release
  ├── ActivityProtocol
  │     execute/adopt/reconcile, output/effect observation
  ├── CommitProtocol
  │     output validation, seal, Validators, prepare, promote/recover
  └── AttemptEffectSettler
        persisted intent apply/reconcile and Effect receipts
```

These are private modules, not five service interfaces or LangGraph nodes:

- `AttemptJournalState` wraps one loaded snapshot and the existing journal port; it does not own
  storage or invent events.
- `AuthorizedAttemptLease` is not a lexical context manager, because pending/indeterminate work may
  outlive one process call. Durable terminal semantics control release.
- `ActivityProtocol` consumes the Phase P executor/result contract and activity state; it neither
  seals files nor creates another activity store.
- `CommitProtocol` owns the local transaction from validated observed result through promotion; it
  neither calls OpenCode nor settles Effects.
- `AttemptEffectSettler` remains the existing authority and is not wrapped for symmetry.

The refactor is successful when callers and high-level tests still use only
`execute_or_recover`, and the original Kernel becomes a readable coordinator. A reduction in file
length is incidental.

## 12. Implementation sequence

### Phase P — release repair

1. Update the five affected plans, the base Python/LangGraph design, the Raw design, and the
   progress ledger to reflect the merged 14/14 source state, corrected 35/44 occurrence inventory,
   premature Checkpoint R claim, and this corrective dependency.
2. Add failing Product tests proving real Agent/Task paths reach deferred executors/placeholders;
   add permanent fail-closed executable assembly while leaving offline compile intact.
3. Add the canonical SQLite Attempt journal/resource store and sealed observer assembly.
4. Wire the existing workspace, host/worker, dedicated terminal receipts, runtime authorization,
   lease-bound execution factory, and journal-backed synchronous activity bridge; construct the
   checkpointer, artifact, and context under the same real fence and remove every fence default.
5. Introduce `AuthorizedAttemptScope` and `ExecutedAttemptResult`; add the three installed Agent
   phase write-claim sets, migrate direct project writers to staging, resolve all 41 contracts at
   runtime, and remove deferred/test-only production branches.
6. Persist final output/source receipt/Effect intents atomically; install the runtime Effect factory
   contribution/projection/ProductLock seam and two durable stores; migrate all six handlers to
   settlement-key/business-key semantics; implement terminal-release reconciliation; pass
   Validator/Effect/Schema registries to the Kernel.
7. Run deterministic transaction, crash, security, lifecycle, packaging, and full repository tests.
8. Delete source-manufactured migration drain certification; replace the one-row direct live probe
   with the protected full Checkpoint R workflow and matrix.
9. Freeze the complete source/documentation candidate and run Checkpoint R last. No repository
   commit may write “green” afterward; that would create a new candidate requiring a new run.

### Phase I — separate follow-up

1. Freeze the then-current public transaction trace with characterization tests.
2. Extract the four private collaborators without schema or behavior changes.
3. Run the full repository gate and a fresh Checkpoint R for the refactor candidate.

## 13. Expected file ownership

Permanent framework protocols and reusable implementations remain in their current post-PR #34
homes:

```text
packages/framework/graph-engine/graph_engine/
├── attempts/
│   ├── kernel.py
│   ├── contracts.py
│   ├── context.py
│   ├── activity.py
│   ├── workspace.py
│   ├── production_host.py
│   ├── production_worker.py
│   ├── host_protocol.py
│   ├── host_receipts.py
│   └── secret_sources.py
├── effects/
└── persistence/
    ├── attempt_journal.py
    └── resource_authorization.py
```

Product owns concrete composition and its selected SQLite adapters:

```text
packages/products/assurance-product/assurance_product/
├── runtime_ports.py
├── runtime_bindings.py
├── sqlite_checkpointer.py
├── sqlite_attempt_store.py
└── sqlite_effect_stores.py
```

Capability wheels retain models, prepare/finalize handlers, Effect factories, Effect schemas, and
store protocols. Adapter wheels retain transport-specific OpenCode behavior. No generic framework
module imports Product, Capability, or OpenCode packages.

Phase I may add private files such as `attempts/journal_state.py`,
`attempts/authorization.py`, `attempts/activity_protocol.py`, and `attempts/commit.py`. Their names
are guidance, not public compatibility promises.

## 14. Verification matrix

| Surface | Minimum proof |
| --- | --- |
| SQLite Attempt journal | canonical round trip, unknown version/corruption rejection, CAS conflict, exact replay, restart, stale fence |
| authorization | cross-process conflict, same-key replay, newer-fence adoption, stale release rejection, durable terminal release |
| terminal cleanup | crash after terminal/before store release, crash after release/before marker, newer-fence cleanup adoption, marker/store mismatch rejection |
| workspace | stable replay binding, actual mutation equality, link/traversal/mode/size rejection, prepare/promote crash recovery |
| host/activity | phase/Attempt/auth/fence identity, synchronous RPC durability, one session/prompt, admission ambiguity, pending observation, cancel/reconcile |
| Agent executor | all three phase calls, 33 schemas/finalizers, raw host-receipt replay, invalid result before finalize, phase-specific write claims/deltas, direct-project and cross-phase write rejection |
| Task executor | all eight installed Task handlers, typed output, durable Effect intent capture |
| commit | output validation, ordered Validator accept/reject, zero promotion on reject, identical promotion receipt |
| Effects | all six kinds, authenticated factory-to-ProductLock closure, two durable family stores, settlement/business-key replay and conflict, apply/reconcile/pending/indeterminate/permanent/crash cases |
| LangGraph bridge | system interrupt issuance/completion anchoring and resumed-node replay |
| lifecycle | `aa start`, `aa run`, `aa resume`, `aa status`, `aa lock show`, `aa export`, and `aa archive` after restart with pinned identities |
| packaging | installed wheels contain permanent modules and no placeholder or legacy execution dependency |
| Checkpoint R | protected exact-candidate 33-row matrix through real Product ports, with no skipped row |

## 15. Acceptance criteria

### Phase P

- all 41 semantic contracts resolve to executable runtime-owned production executors for
  `assurance-opencode`;
- all 33 Agent contracts traverse installed prepare, exact locked OpenCode binding, local schema
  validation, installed finalizer, Kernel commit, and receipt without a test hook;
- prepare/runtime/finalize deltas match their disjoint installed staging claims, and no Capability
  handler writes the canonical project tree during an Attempt;
- eight deterministic Task contracts preserve their `TaskOutcome.effects` durably rather than in
  process memory;
- restart at every section 8 cut duplicates no prompt, promotion, Effect, or terminal result;
- no terminal resolution reaches LangGraph while its Attempt retains an active resource grant;
- production contains no deferred/unused/scripted/pickle/memory-only path listed in section 6.2;
- Attempt, authorization, activity, and Effect state use one fenced durable authority each;
- ProductLock authenticates each installed Effect factory/store contract and all six handlers
  preserve both Kernel settlement identity and Capability business identity without duplicate
  application;
- every mutating checkpointer, BootArtifact, and runtime context is created after runner-lease
  acquisition, carries that exact fence, and rejects a missing or unequal
  lease/checkpointer/context/config/stored token before Kernel entry;
- executable Product boot rejects incomplete required ports before graph execution, while dry
  compile remains offline and non-Agent roots do not require OpenCode;
- the Kernel receives real workspace, Validator, runtime Effect, Schema, and authorization ports;
- normal credential-free CI passes under its existing platform skip policy;
- the protected Checkpoint R selection reports exactly zero skipped, xfailed, waived, cancelled,
  timed-out, or partially selected rows;
- the protected Checkpoint R passes all 33 live contract rows for the exact final candidate;
- `assurance-cursor` remains offline-compilable but executable admission fails before Invocation
  mutation and never consumes the OpenCode qualification result;
- active plans/specs no longer claim that the old skipped one-row probe closed the gate;
- Product source contains no released-SHA constant, synthesized DrainEvidence, fixture-model
  release assertion, test-source gate scanner, or stale 34-occurrence release check;
- no legacy runtime or Structured Artifact architecture is reintroduced.

Only then may the merged `assurance-opencode` 14-root source state be described as
production-ready. This statement does not apply to `assurance-cursor`.

### Phase I

- the public Kernel signature and `AttemptNodeFactory` mapping are unchanged;
- canonical schemas, event order, resolutions, fencing, crash semantics, and same-revision replay
  are unchanged;
- the four private collaborators have single, non-overlapping responsibilities and no caller uses
  them directly;
- full CI and a fresh candidate-bound Checkpoint R pass.

Phase I failure does not invalidate a completed Phase P; it only blocks release of the refactor
candidate.
