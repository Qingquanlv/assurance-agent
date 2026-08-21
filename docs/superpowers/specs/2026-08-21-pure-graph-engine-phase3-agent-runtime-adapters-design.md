# Pure Graph Engine Phase 3: Agent Runtime Adapters

**Status:** proposed for implementation planning on 2026-08-21
## 1. Context

Phase 3 implements the agent-runtime adapter slice described by the [umbrella architecture](./2026-08-20-pure-graph-engine-plugin-architecture-design.md). It starts only after the [Phase 2 registry platform](./2026-08-20-pure-graph-engine-phase2-registry-platform-design.md) acceptance gates have passed.
Phase 1 established a business-neutral graph runtime. Phase 2 established explicit plugin composition, immutable registries, frozen invocation locks, generic task handlers, and durable effects. Phase 3 adds two installed wheel plugins that can execute one already-frozen agent run:

- `agent-runtime-opencode`, using OpenCode HTTP and server-sent events; and
- `agent-runtime-cursor`, using a confined Cursor headless child process and `stream-json` output.
Neither adapter moves Assurance capabilities. Phase 3 does not decide what an Assurance skill means, assemble its instructions, select a persona, or route a model. Those decisions remain in Phase 4 capability plugins and Phase 5 product configuration. Phase 3 accepts a complete frozen request, validates it, passes it to one selected provider, and returns a typed result.
This design also closes a Phase 2 recovery gap that real remote or process-backed tasks expose. That increment is deliberately business-neutral and remains in `graph-engine`.
### 1.1 Current Phase 2 facts

The current `packages/graph-engine/graph_engine/plugin_api.py` has:

``` python
class TaskHandler(Protocol):
    async def execute(
        self,
        request: TaskRequest,
        context: TaskContext,
    ) -> TaskOutcome: ...
```

`TaskHandler` has only `execute()`. `TaskContext` contains only `workspace_root`, `heartbeat`, and the pure `effect()` value constructor. `TaskRequest` has fields for resolved binding data and resource IDs, but the current scheduler does not populate all of those resolved fields when it constructs a request.
The current `Engine.open()` authenticates the invocation and workspace, then calls `scheduler.reclaim_expired()` immediately. Reclamation converts every expired running attempt into a transient task failure without first asking the handler whether external work is still running or has already completed.
The current `resume_running()` calls `SnapshotStore.reset_attempt()`. That deletes and recreates the deterministic attempt directory from current HEAD. For an external worker, this can erase the exact attempt workspace that the worker edited before the engine process failed.
Those behaviors are valid only for disposable, replay-safe local handlers. They are unsafe for an activity whose external state can outlive an engine process, whose creation response can be ambiguous, or whose workspace contains uncommitted remote-worker edits.
### 1.2 Provider facts used by this design

The OpenCode adapter targets an explicitly pinned server profile. Current OpenCode documentation exposes session creation and lookup, asynchronous prompt dispatch, message and diff lookup, abort, status polling, and an SSE event stream. Current OpenCode source also supports opaque session metadata. The server generates session IDs; Phase 3 does not assume that a caller can choose one.
The Cursor adapter targets the documented non-interactive CLI mode using `--print --output-format stream-json`. A successful stream has an init record and a terminal result record; failure can exit non-zero without a terminal record. Although current CLI output can contain a session identifier, Phase 3 does not treat the Cursor CLI as a durable-session API.
The provider-specific facts are pinned by adapter source version and by a validated protocol profile. They are not graph-engine concepts.

### 1.3 Normative provider references

The first OpenCode profile is derived from the official [server API](https://opencode.ai/docs/server/), the current upstream OpenCode [`Session.CreateInput`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.ts#L260-L270), [`SessionEvent` schema](https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-event.ts), and [durable prompt-admission contract](https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md). The first Cursor profile is derived from the official [Cursor headless CLI documentation](https://cursor.com/docs/cli/headless). These moving upstream pages are non-normative discovery inputs: implementation must record the exact upstream commit/tag used to build its closed protocol fixture, and the installed adapter source plus that versioned fixture are the invocation authority. A later upstream change cannot change an existing invocation.

## 2. Architectural decisions

The engine sees only generic `TaskHandler` values and an optional generic recoverable-task interface; it contains no OpenCode, Cursor, agent, model, prompt, persona, or provider-session behavior. Durable task activity is an engine mechanism, while provider interpretation of its opaque reference remains adapter implementation. Preparation shares one authoritative batch with attempt start and initial lease acquisition; reconciliation precedes reclaim and may adopt the same attempt while preserving its workspace.
Cancellation is durable intent, not an inference from timeout or lost connection, and acknowledgement is not terminal completion. A typed provider result becomes durable as `terminal_observed` before normal task finalization. Adapters persist only stable references, canonical digests, bounded diagnostics, and typed terminal results. OpenCode remains authoritative for full session storage, messages, reasoning, tool calls, model history, token/cost accounting, and provider events; this project creates no `SessionEvent` copy.
OpenCode creation is rediscovered by exact metadata and an ambiguous create is never blindly repeated. Cursor has no assumed durable-session behavior: unknown process outcome is indeterminate and cannot consume normal retry budget. Both adapters obey one conformance state machine while declaring different recovery capability. Phase 3 is a hard replacement in the new runtime, with no old-runtime adapter, alias, event translator, or resume shim.
## 3. Goals

Add thin, independently buildable OpenCode and Cursor wheels while keeping all provider dependencies and vocabulary outside `graph-engine`; define frozen `AgentRunRequest`/`AgentRunResult` contracts outside the engine; and pass and validate a request upstream has fully resolved.
Add the minimum generic durable-activity seam: atomic preparation, durable reference binding and terminal observation, preserved attempt workspace identity, idempotent cancellation, reconcile-before-reclaim, and same-attempt adoption. OpenCode uses SSE with bounded poll fallback and crash recovery without duplicate create or prompt; Cursor confines children and refuses blind retry after crash/unknown.
Return structured result, diff, and evidence digests through `TaskOutcome`; prove a neutral declarative run fixture can bind to either adapter without engine change; and provide reusable conformance and fault-injection suites.
## 4. Non-goals

No Assurance capability, business graph/model/validator/effect, prompt, persona, skill body, role instruction, or model-routing policy moves in Phase 3. An adapter implements no model default, fallback, cost choice, or provider preference. No engine type contains `AgentRun`, OpenCode, Cursor, prompt, persona, model, message, tool-call, or session fields, and no provider transcript, token/tool stream, or model history enters its ledger.
No plugin receives a ledger/event/checkpoint writer or store path. No hostile-wheel containment is claimed. OpenCode has no caller-selected session ID or immediate-consistency assumption. Cursor has no assumed durable adoption, detached child, daemonization, orphan adoption, or best-effort PID reuse check.
There is no `aa` cutover, complete Assurance flow, behavioral comparison, old-runtime deletion, new registry kind, executable declarative configuration, or compatibility with pre-Phase-3 invocations.
## 5. Terminology

**Task attempt** is the engine's numbered execution of an activation. **Task activity** is its business-neutral durable link to work that can outlive an engine process. A **prepared activity** durably fixes activity identity, request digest, and exact workspace before dispatch. A **bound reference** is size-bounded canonical JSON supplied by a handler, stored opaquely, and interpretable only by that same implementation.
**Dispatch started** opens the single initial external-dispatch window but does not prove provider receipt. A **host terminal receipt** is an immutable engine-internal prepare record proving the confined call's exact response and quiescence; it is not graph authority. **Terminal observed** means the ledger has promoted that response and, for success, bound it to one immutable candidate. **Adoption** transfers the lease for that same attempt after reconciliation. **Indeterminate** means neither absence nor a unique running/terminal activity is provable; it is not a transient failure and permits no new attempt.
## 6. Two plugin layers

Phase 3 preserves two distinct plugin layers.
### 6.1 Capability layer

A capability plugin owns business meaning: the skill and final instruction assembly; persona/role selection; result schema and semantic validation; evidence requirements; exact model-selection/routing policy; and conversion between business values and `AgentRunRequest`/`AgentRunResult`.
Those are Phase 4 concerns. Product configuration chooses among the available bindings and supplies organization policy in Phase 5.
For Phase 3 tests, a separate installed fixture-capability wheel owns one declarative, non-business instruction resource, its strict result schema, and the code that assembles a completely frozen request. Declarative files are authenticated package resources and graph/binding data only; a SUT cannot register executable skills or capabilities by dropping in configuration. The fixture proves rebinding only and is neither a default product nor a production skill.
### 6.2 Runtime adapter layer

An agent-runtime plugin owns provider transport and lifecycle behavior. It contributes one direct `TaskHandler`, parses `TaskRequest.input` as frozen `AgentRunRequest`, verifies locked configuration/digests, creates or launches work, binds an opaque reference, observes progress/cancellation/terminal state, reduces the response to `AgentRunResult`, and returns normal `TaskOutcome`.
It does not understand the business reason for the run. It does not alter the instructions, infer a persona, choose a model, or silently retry with another model.
The graph registry binds a capability alias to one runtime adapter capability. Changing that explicit binding changes the composition and invocation-lock digest. It never changes a running invocation.
## 7. Package ownership and import rules

Phase 3 adds these independently buildable distributions:

``` text
packages/
  graph-engine/
  agent-runtime-contracts/
  agent-runtime-opencode/
  agent-runtime-cursor/
examples/
  agent-runtime-fixture/
```

`examples/agent-runtime-fixture` is itself an isolated buildable wheel used only by conformance and provider-live benchmarks. It is never discovered ambiently.

### 7.1 `graph-engine`

The engine owns only generic task-activity models/interfaces/events/fold, preparation/binding/terminal/cancel transitions, reconcile-before-reclaim, same-attempt adoption, workspace identity authentication, and generic errors/conformance helpers.
It does not import any `agent_runtime_*` package.
### 7.2 `agent-runtime-contracts`

Import package: `agent_runtime_contracts`.
It owns only frozen provider-neutral value models used between future capability plugins and runtime adapters. It may import public value types from `graph_engine.plugin_api`; `graph-engine` may not import it.
It contains no transport client, provider endpoint, product configuration, business instructions, default model, or default persona.
### 7.3 `agent-runtime-opencode`

Import package: `agent_runtime_opencode`.
It owns the OpenCode descriptor/handler, validated endpoint/profile configuration, secret-sourced HTTP authentication, metadata/create rediscovery/lookup/validation/abort, SSE reconnect plus poll fallback, terminal reduction/digests, and provider fakes.
It imports `graph_engine`, `agent_runtime_contracts`, and its declared HTTP/SSE client dependencies. It imports neither adapter peer nor any Assurance package.
### 7.4 `agent-runtime-cursor`

Import package: `agent_runtime_cursor`.
It owns the Cursor descriptor/handler, strict argv construction, confined launch and process receipt, NDJSON parsing, terminal/stderr reduction, process-group cancellation, and provider fakes.
It imports `graph_engine`, `agent_runtime_contracts`, and process-support dependencies only. It imports neither adapter peer nor any Assurance package.
### 7.5 Mechanical import contracts

Import-linter rules must enforce:

``` text
graph_engine
  X agent_runtime_contracts
  X agent_runtime_opencode
  X agent_runtime_cursor
  X assurance_*
agent_runtime_contracts
  -> graph_engine.plugin_api
  X agent_runtime_opencode
  X agent_runtime_cursor
  X assurance_*
agent_runtime_opencode
  -> agent_runtime_contracts
  -> graph_engine public interfaces
  X agent_runtime_cursor
  X assurance_*
agent_runtime_cursor
  -> agent_runtime_contracts
  -> graph_engine public interfaces
  X agent_runtime_opencode
  X assurance_*
```

Wheel-isolation tests install each adapter with the engine and contract wheel, without the other adapter and without Assurance packages.
## 8. Minimal generic `TaskRequest` and `TaskContext` increment

The Phase 2 `TaskHandler.execute()` shape remains the common task interface. Phase 3 does not introduce an `AgentHandler` into the engine.
The scheduler must finish the Phase 2 request projection. Every dispatch populates `target_capability_id`, frozen alias `binding_data` (or `None` for a direct entry), canonical `resource_ids` and keyed `resource_digests`, exact compiled resource claims, and immutable invocation metadata containing at least lock and composition digests.
The added fields remain generic. The engine treats all binding and input JSON as opaque data after schema and canonical-JSON validation.
`TaskContext` becomes:

``` python
@dataclass(frozen=True, slots=True)
class TaskContext:
    workspace_root: Path
    heartbeat: Callable[[], None]
    cancel_requested: Callable[[], bool]
    invocation: InvocationMetadata
    activity: TaskActivityPort | None = None
    secrets: SecretPort | None = None
    def effect(self, kind: str, payload: JSONValue) -> EffectIntent: ...
```

`InvocationMetadata` is read-only and contains no mutable registry, business repository, credential, provider object, or ledger access.
Only a handler satisfying `RecoverableTaskHandler` receives a non-null `TaskActivityPort`. A normal handler retains the simpler disposable execution path.

`SecretPort` is a host-owned, non-enumerable resolver for the exact non-secret handles authorized by frozen binding/configuration data. It returns credential bytes only to the confined handler call, never writes them to task input/output or host diagnostics, rejects every undeclared handle, and clears its per-call material when the host operation ends. The composition root injects the resolver into `TaskExecutionHost`; adapters never read ambient process environment or global credential stores directly.

### 8.1 Task-host confinement remains mandatory

The engine invokes `execute()`, `reconcile()`, and `cancel()` only through `TaskExecutionHost`; it never calls plugin wheel code directly in the engine process. The host contract gains corresponding generic recover/cancel operations and must preserve the Phase 2 exact-attempt confinement for every operation. Every descendant spawned by a handler must inherit the host's authenticated containment and cleanup boundary; a host that cannot prove descendant inheritance must reject the call before plugin code runs.

Heartbeat, cancel observation, and activity transitions cross that boundary as capability-limited host RPCs bound to one invocation/task/attempt/activity identity. The plugin receives no live ledger, store, lock, checkpoint, registry, or unrestricted path object. The engine authenticates identity, state, and CAS position again for every activity-port call, so a compromised or stale host cannot address a different attempt or manufacture an arbitrary event.

This is a fixed, engine-internal, same-language execution transport, not a public or user-extensible out-of-process plugin protocol. Products and plugins cannot register a transport, worker command, codec, or cross-language handler. The invocation lock pins the execution-host implementation/build digest and closed wire-schema version. The host loads only the exact already-resolved Python wheel entry point and capability ID from the frozen composition; request, context, activity RPC, response, and error projections use versioned engine-owned schemas. Thus the umbrella/Phase 2 non-goal remains: plugin discovery, composition, and contracts stay in the installed-wheel Python SPI resolved by `RegistryPlatform`, with no alternative RPC plugin surface. Installed wheel code remains trusted; this reliability/namespace boundary makes no hostile-code sandbox claim.

Before a host reports a terminal handler response, it first ends the handler call and proves every workspace writer/descendant in that call's containment is quiescent. Only after that proof does it durably install an immutable final `TaskHostTerminalReceipt` in an engine-owned namespace that plugin code cannot address, and only after receipt durability may it acknowledge completion to the engine. The receipt binds host implementation/wire digest, invocation/task/activation/attempt/activity IDs, operation (`execute`, `reconcile`, or `cancel`), request/workspace/fingerprint/reference digests, exact `TaskOutcome`, outcome/proof digests, quiescence evidence digest, and a monotonic host-call ID. There is no promotable prepare receipt: a partial or pre-quiescence record is invalid.

The receipt is recovery evidence, not graph authority and not a checkpoint: only the engine can authenticate it against the ledger/activity state, seal the exact success candidate, and promote it to `TaskActivityTerminalObserved`. It is retained until that event is durable, then removed through authenticated cleanup. A host crash before dispatch is a typed no-dispatch failure; after `dispatch_started`, absence of both a terminal receipt and provider reconciliation proof is indeterminate. A valid receipt after host/engine crash is replayed without provider access, while a missing, foreign, changed, multiply installed, or workspace-drifted receipt fails closed.

## 9. Generic durable task-activity interface

### 9.1 Value models

The engine exports these business-neutral frozen values:

``` python
class TaskActivitySnapshot(FrozenModel):
    activity_id: str
    request_digest: str
    workspace_identity: AttemptWorkspaceIdentity
    state: Literal[
        "prepared",
        "dispatch_started",
        "bound",
        "terminal_observed",
    ]
    reference: JSONValue | None = None
    reference_digest: str | None = None
    dispatch_fingerprint: JSONValue | None = None
    dispatch_fingerprint_digest: str | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    terminal: TaskOutcome | None = None
    outcome_digest: str | None = None
    terminal_proof_digest: str | None = None
    candidate_tree_id: str | None = None
    write_set_digest: str | None = None
class TaskActivityReconcileResult(FrozenModel):
    status: Literal[
        "not_dispatched",
        "running",
        "terminal",
        "absent",
        "indeterminate",
    ]
    reference: JSONValue | None = None
    outcome: TaskOutcome | None = None
    proof: JSONValue | None = None
    reason: str | None = None
class TaskActivityCancelResult(FrozenModel):
    status: Literal["acknowledged", "terminal", "indeterminate"]
    outcome: TaskOutcome | None = None
    reason: str | None = None
```

Closed-model validators require an outcome exactly for `terminal`, a canonical proof exactly for `absent`, forbid references on states that do not bind one, and require a non-empty reason for `indeterminate`. A terminal activity requires the canonical outcome digest. Candidate tree and write-set digests are required exactly for a succeeded terminal outcome and forbidden for failed/stopped outcomes. An unbound terminal transition after `dispatch_started` additionally requires `terminal_proof_digest`; it authenticates the bounded proof projection supplied by the recoverable handler but remains opaque to the engine.
`absent` is a strong assertion that the provider can prove no external work was created or can still appear. Temporary list emptiness, a 404 after a previously bound reference, process disappearance, transport timeout, and malformed provider output are not `absent`.
### 9.2 Recoverable handler

``` python
@runtime_checkable
class RecoverableTaskHandler(TaskHandler, Protocol):
    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult: ...
    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult: ...
```

`execute()` remains inherited from `TaskHandler`. The registry still stores a generic task handler. Runtime protocol conformance only changes recovery behavior; it does not create a sixth registry kind or provider-specific capability kind.
`reconcile()` and `cancel()` must be idempotent for the same request digest and activity snapshot. Exceptions never imply absence or successful cancellation.
### 9.3 Narrow activity port

The activity port is a constrained transition interface:

``` python
class TaskActivityPort(Protocol):
    @property
    def snapshot(self) -> TaskActivitySnapshot: ...
    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot: ...
    def bind(self, reference: JSONValue) -> TaskActivitySnapshot: ...
```

Every method performs one exact state transition using engine-owned CAS and canonical validation. The port cannot append an arbitrary event, choose a sequence number, mutate another task, write an invocation lock, or access a ledger object. It is not a ledger writer.
`mark_dispatch_started()` is called immediately before the first external create/spawn attempt. Its canonical, size-bounded, non-secret fingerprint records runtime-observed endpoint/executable/profile identity and its digest. A repeated call with the exact fingerprint authenticates the existing state but does not open a second dispatch epoch; a different fingerprint is drift.
`bind()` stores one canonical, size-bounded reference. Repeating the exact reference is idempotent. A different reference is a conflict unless reconciliation proves a unique provider match and the prior state was not yet bound. The engine hashes the canonical reference but never interprets its fields.

Terminal publication is deliberately not a plugin port operation. The adapter returns a `TaskOutcome` from `execute()`, `reconcile()`, or `cancel()` through the confined host. The host ends the call, proves all workspace writers/descendants quiescent, and only then installs the exact durable final terminal receipt. The engine authenticates that receipt and canonicalizes the outcome. For a succeeded outcome it first seals and authenticates an immutable candidate from the exact attempt workspace, then appends `TaskActivityTerminalObserved` with the full outcome, outcome digest, candidate tree ID, and write-set digest. Failed/stopped outcomes append the same event without a candidate; if dispatch started but no reference was ever bound, the event also carries the exact terminal/absence proof digest. The event becomes authoritative only after every required immutable object exists.

Recovery after successful `terminal_observed` opens only the exact recorded candidate and never reseals the mutable attempt directory. Recovery after failed/stopped terminal observation consumes only the stored outcome and never opens a candidate. A later validator, durable effect, or commit failure may still make a successful external outcome's task attempt fail through its normal causal events, but it cannot change the recorded external outcome or substitute a different candidate. A succeeded `TaskAttemptSucceeded` must consume that exact outcome and candidate.
### 9.4 Events

Phase 3 adds six generic runtime events: `TaskActivityPrepared` carries activity/task/activation/attempt identity, request digest, and exact workspace identity; `TaskActivityDispatchStarted` carries activity ID, ordinal `1`, opaque dispatch fingerprint, and digest; `TaskActivityBound` carries opaque canonical reference and digest; `TaskActivityCancelRequested` carries stable reason and request time; `TaskActivityTerminalObserved` carries the typed outcome/outcome digest, any required unbound-terminal proof digest, and, exactly for success, immutable candidate tree/write-set digests; and `TaskLeaseAdopted` carries existing attempt identity, new owner/times, and reconciliation evidence digest.
There is no event named for an agent, provider, prompt, model, message, tool, or session. There is no generic event carrying provider progress records.
The canonical initial batch for a recoverable attempt is:

1.  `TaskAttemptStarted`;
2.  `TaskLeaseAcquired`; and
3.  `TaskActivityPrepared`.
All three append under one expected sequence and one durable ledger batch. No handler code runs and no external dispatch is allowed before that batch is authoritative.
### 9.5 Pure fold invariants

The fold enforces exactly one derived activity ID per recoverable attempt; exact request/workspace identity; normal `prepared -> dispatch_started -> bound -> terminal_observed` progress with cancellation as overlay; and two closed failure bypasses: `prepared -> terminal_observed` only for deterministic pre-dispatch failed/stopped outcomes, and `dispatch_started -> terminal_observed` only for failed/stopped outcomes with exact proof that no bindable external work exists. Success always requires `bound`. It also enforces one dispatch ordinal/fingerprint, immutable bound reference and terminal observation, exact idempotent calls, no post-dispatch attempt outcome before terminal observation, exact success candidate consumption, no new attempt while an earlier activity is live/indeterminate, and no adoption without reconciliation evidence.
Cancellation may be requested from prepared, dispatched, or bound state. Terminal observation closes the activity whether cancellation was requested or not. Fold property tests reject success without a bound reference, unbound post-dispatch terminal events without proof, a candidate on failed/stopped, and any candidate mismatch.

Idempotency is resolved before append: repeating an exact port operation authenticates and returns the already-persisted state without adding a second transition event. The fold rejects a duplicate transition event, even when its payload is identical.
### 9.6 Workspace identity

Before the initial activity batch, the engine creates the attempt workspace from authoritative HEAD. `AttemptWorkspaceIdentity` contains deterministic attempt-directory ID, baseline tree ID, invocation/task/activation/attempt digest, and workspace-layout schema version.
The identity contains no unrestricted host path. The handler receives only the already-confined physical root.
On recovery, the engine opens the existing attempt directory and verifies the saved identity and baseline. It does not call `reset_attempt()`. Missing, replaced, symlinked, baseline-drifted, or otherwise unauthenticated workspace state raises `AttemptWorkspaceLost` and leaves the activity indeterminate.
An orphan workspace created before the initial ledger batch can be removed by the existing authenticated orphan cleanup because no activity was authorized. An attempt workspace named by `TaskActivityPrepared` cannot be discarded until the activity and attempt are terminal and normal finalization has consumed it.

### 9.7 Authority exclusions

External create/spawn, dispatch, reference binding, cancellation, and terminal observation are authorized only by the task-activity event chain. An adapter must not encode any of those transitions as an `EffectIntent`: durable effects are admitted only after nominal task success and therefore cannot close a create-to-bind or running-task crash window.

A checkpoint is only a ledger-derived acceleration cache. It cannot introduce, repair, or override activity state, reference, workspace identity, dispatch fingerprint, terminal outcome, or candidate digest. Deleting all checkpoints and replaying the ledger must produce the same recovery decision and provider call count.

## 10. Recovery and cancellation order

`Engine.open()` remains synchronous authentication and fold. It no longer reclaims expired recoverable attempts. Async recovery occurs before the next planning wave through the invocation handle.
For each live recoverable attempt, recovery performs this order:

1.  authenticate the invocation lock, ledger, HEAD, activity fold, and original attempt workspace identity;
2.  if `terminal_observed` is already durable, finalize its stored failed/stopped outcome or, for success, authenticate and finalize only its recorded immutable candidate, without contacting the provider;
3.  before any provider call, look up the exact host-call identity: one valid final `TaskHostTerminalReceipt` is promoted directly (sealing its success candidate when applicable); no receipt continues recovery; any partial, foreign, changed, or multiple receipt fails closed;
4.  if `cancel_requested` is durable, call `cancel()` before ordinary reconciliation;
5.  call `reconcile()` even when the persisted lease is expired;
6.  validate and persist any unique bound reference returned by reconciliation;
7.  if running, append `TaskLeaseAdopted` and continue observing the same attempt;
8.  if terminal, authenticate the durable host receipt after the confined call has quiesced workspace writes, seal/authenticate the success candidate when applicable, append `TaskActivityTerminalObserved`, adopt if needed, and finalize the same attempt;
9.  if not dispatched, adopt the same attempt and permit its one initial `execute()` call;
10. if definitively absent, authenticate its proof, synthesize the typed failed/stopped outcome required by frozen task policy, append the unbound `TaskActivityTerminalObserved` failure bypass, finalize that attempt, and only then allow normal planner retry rules; and
11. if indeterminate, raise `TaskActivityIndeterminate`, preserve the workspace and ledger state, and schedule neither reclaim nor a new attempt.
Only after this sequence may `reclaim_expired()` process a recoverable attempt, and only a reconcile result that proves `absent` authorizes it. Disposable non-recoverable handlers may retain the Phase 2 expired-lease behavior.
An adopted lease uses the current runner owner and a new lease interval but keeps the original task ID, activation ID, attempt number, activity ID, request digest, and workspace identity.
### 10.1 Timeout and stop

A timeout, operator stop, invocation stop, or cancellation signal first appends `TaskActivityCancelRequested`. The engine then calls `cancel()` under its own bounded cancellation timeout.

- `acknowledged` means the provider accepted the request; recovery continues to reconcile until terminal.
- `terminal` is returned through the confined host, sealed when successful, persisted as `TaskActivityTerminalObserved`, and finalized.
- `indeterminate`, timeout, transport error, or exception preserves the cancel request and blocks blind retry.
An adapter may return `TaskOutcome.stopped()` only after it observes a provider terminal cancellation or proves no dispatch occurred. Sending a signal or receiving HTTP success from an abort endpoint is not itself terminal proof.
## 11. Frozen `AgentRun` data ownership

`AgentRunRequest` and `AgentRunResult` live in `agent-runtime-contracts`, not in the engine.
### 11.1 Request

The request is a strict, frozen, extra-forbidden model with at least:

``` python
class AgentRunRequest(FrozenModel):
    schema_version: Literal["1"]
    instructions: tuple[InstructionPart, ...]
    result_contract: ResultContract
    execution: FrozenExecutionSelection
    request_policy_digest: str
    request_config_digest: str
```

`InstructionPart` contains a closed media type, exact text or JSON-compatible content, and its canonical digest. The content is already final. The adapter does not prepend, rewrite, summarize, or derive instructions.
`ResultContract` declares the expected strict result schema ID/digest and the closed extraction mode. Phase 3 adapters validate structure; Assurance semantic validation remains in Phase 4.
`FrozenExecutionSelection` contains only already-decided values that a provider needs to execute, such as an exact provider/model coordinate, exact worker profile identifier, permission profile digest, and bounded transport-neutral limits. Every optional field has defined absent semantics. There is no list of candidates, routing expression, fallback chain, price policy, or defaulting rule.
`request_config_digest` covers the exact upstream business configuration used to assemble this request, not the selected runtime-adapter binding or transport configuration; those remain separately authenticated by `TaskRequest` binding data and the invocation lock.
An adapter must reject an unsupported frozen selection. It must not repair it by choosing a provider default unless the request explicitly encodes `provider_default` as the selected policy and that policy is locked.
The canonical request digest covers the complete model plus the selected task binding and resource digests. The engine stores that digest in `TaskActivityPrepared`; it does not parse the model.
### 11.2 Result

`AgentRunResult` contains schema version/status, schema-valid structured result, stable-reference/result/evidence digests, optional provider diff digest, adapter ID/source version, and bounded redacted diagnostics.
It does not contain the full provider transcript, provider event stream, tool arguments/results, chain-of-thought, credential, or model history.
The successful `AgentRunResult` is serialized into `TaskOutcome.output`. The adapter does not know or claim the engine candidate tree/write-set digest; those are computed by the engine after the confined handler returns and live only in `TaskActivityTerminalObserved` and normal commit state. Once the task's success and workspace commit are durable in the graph ledger, downstream graph execution depends only on that typed output and the committed workspace. OpenCode or Cursor state may later be unavailable without changing the completed invocation.
## 12. OpenCode adapter

### 12.1 Configuration and protocol profile

The strict configuration contains normalized endpoint identity (scheme/host/port/base path and project/directory scoping), TLS policy and optional CA digest, secret-source names but not values, connection/SSE/poll/cancel/overall limits, pinned protocol profile, result extraction/schema digest, and request-policy/config digests.
Adapter source/version, implementation digest, normalized endpoint identity, expected protocol profile, request-policy digest, and config digest are static and already authenticated by the Phase 2 composition lock before activity preparation. After read-only preflight, `mark_dispatch_started()` records the live server/profile observation in its dispatch fingerprint; the later bound reference repeats the authenticated identity needed for recovery. Runtime observations are never retroactively attributed to `TaskActivityPrepared`. Credentials are excluded from every projection.
The adapter preflights the pinned profile. Missing required routes, missing metadata support, incompatible response shapes, or an unsupported SSE profile is a non-retryable configuration failure before dispatch.
### 12.2 Discovery metadata

The create request writes versioned metadata derived from invocation/task/activation/attempt, selected and target capabilities, invocation-lock digest, canonical request digest, request-policy/config digests, and workspace-identity digest.
The values are namespaced and versioned. The matching key is the canonical digest of the complete metadata projection. A human-readable title is diagnostic only and is never used for identity.
The adapter never supplies a session ID to `POST /session`. OpenCode generates the ID. The metadata key allows deterministic rediscovery after an ambiguous create response.

Recovery always adopts the exact existing session. It never creates a child session with the previous session as `parentID`, because that is a new execution lineage rather than reconnection.
### 12.3 Create and bind flow

The initial flow is ordered: (1) validate frozen request/digests and workspace; (2) call `mark_dispatch_started()`; (3) list within exact endpoint and directory/project scope and compare metadata canonically; (4) validate/bind one match, fail closed on multiple, or on fresh execution send one metadata-bearing `POST /session` without caller ID; (5) validate and bind its returned reference before prompt dispatch; (6) on transport ambiguity, rediscover without another create.
The opaque reference contains only stable, non-secret fields needed to find and validate the session: endpoint/profile identity digest, generated session ID when known, metadata-match digest, request digest, expected prompt message ID, and adapter/source version.
HTTP rejection known to occur before creation may become a typed terminal failure. A timeout, disconnect, invalid success body, proxy reset, or ambiguous response publication does not prove absence.
After an ambiguous create, a temporarily empty session list yields `indeterminate/pending observation`; it never triggers a second POST. Exactly one later metadata match is adopted. Multiple later matches fail closed. If the configured observation horizon expires without proof, the activity remains indeterminate for operator resolution rather than duplicating work.
### 12.4 Prompt dispatch

The adapter constructs the provider request only from the frozen request. It uses a deterministic message identity when the pinned OpenCode profile supports one. The expected identity and request-body digest are part of the bound reference before dispatch.
If prompt dispatch is ambiguous, the adapter inspects session messages/admission state for the exact message identity and content digest. One exact match continues observation; any conflicting or duplicate match fails closed. An absent read never authorizes a new message identity or changed body; the only permitted repeat is the exact idempotent admission defined below.
The adapter never adds an unrequested model, agent name, system instruction, tool override, or fallback. Exact frozen selections are forwarded and verified against the resulting provider records when the profile exposes them.

The first accepted OpenCode profile must prove a durable idempotent prompt-admission contract for a caller-supplied message identity: exact reuse for the same session, body digest, and delivery mode returns the same admission/does not execute twice, while any conflicting reuse fails closed. Merely accepting a `messageID` field is insufficient. The adapter validates this profile before session creation and binds the deterministic message identity plus canonical body/delivery digest before the first prompt request.

After a crash with a bound session but no durable terminal observation, reconciliation first reads the exact message/admission identity. One exact admission resumes observation; a conflict or duplicate fails closed. If it is absent, the adapter may repeat only the exact idempotent admission request under the pinned contract. Therefore a crash before the POST and a lost response after successful POST converge without duplicate execution. If a deployed OpenCode version cannot prove these semantics, the adapter rejects the profile before dispatch and does not claim cross-process prompt recovery.
### 12.5 SSE-primary observation

The adapter subscribes to the pinned SSE endpoint before or immediately around prompt dispatch so a fast terminal transition cannot be missed. It filters events by exact session identity and treats all other events as unrelated.
SSE behavior is:

- parse only the pinned envelope fields and ignore documented additive fields;
- reject malformed identity-bearing events;
- heartbeat the engine lease while valid provider progress or keepalives are observed;
- reconnect using the provider cursor when the profile supports one;
- after any gap or reconnect, authenticate state using GET endpoints rather than assuming complete event delivery; and
- switch to bounded polling when SSE is unavailable, silent beyond the locked threshold, or explicitly unsupported by a validated fallback profile.
Polling uses `GET /session/{id}`, status, and messages with locked intervals and backoff. It never treats the first idle observation alone as completion. Terminal success requires an idle/terminal provider state plus a complete final message/result with no open tool work. Provider error or cancellation requires the corresponding terminal record.
### 12.6 GET validation and drift

Every session GET validates exact bound ID, endpoint/scope, metadata-match digest, server/profile identity, expected prompt identity/content digest, and the frozen execution selection when exposed.

Absence from the aggregate status map is not interpreted as idle, terminal, or missing. The adapter first performs an exact session GET and then authenticates messages/tool completion under the pinned profile; only those authoritative reads may classify the activity.
A 404 for a previously bound reference is `ExternalActivityMissing`, not proof that a new session may be created. Foreign metadata is `ExternalActivityForeign`. Changed metadata, prompt, profile, model selection, or endpoint identity is `ExternalActivityDrift`. All fail closed and prohibit a new attempt unless an operator terminates the invocation under an explicit audit decision.
### 12.7 Terminal reduction

At terminal state the adapter fetches only the structured result, provider terminal/error state, supported session diff, message/tool-history canonical digests, and bounded redacted diagnostics.
OpenCode remains authoritative for full session storage, messages, reasoning, tool calls, model history, token/cost accounting, and raw event history. The adapter does not serialize them into a graph event, resource, artifact, or `SessionEvent` equivalent.
The adapter validates the result contract, computes result/diff/evidence digests, constructs `AgentRunResult`, and returns the corresponding `TaskOutcome` through its confined host call. The host ends the call, quiesces and authenticates every workspace writer/descendant, then durably records that exact response plus quiescence proof; the engine authenticates the final receipt, seals the exact success candidate, and publishes `TaskActivityTerminalObserved`. Normal validators, candidate commit, effects, and task-success publication then apply.
After graph-ledger success, OpenCode is no longer required to replay or consume the completed task.
### 12.8 OpenCode cancellation

`cancel()` validates the same bound reference and calls the pinned abort route at most according to the locked idempotent cancel policy. HTTP success means acknowledged only. The adapter continues SSE/GET reconciliation until it sees a terminal canceled, failed, or completed state.
If completion races cancellation, the observed provider terminal result wins and is recorded once. A missing, foreign, or drifted session during cancellation is indeterminate, not canceled.
## 13. Cursor adapter

### 13.1 Execution contract

The Cursor adapter invokes one pinned executable in the exact attempt workspace using non-interactive print mode and `stream-json`. The argv is constructed from a closed configuration schema and a frozen request; no shell parsing or shell execution is used.
The instruction payload is sent through the configured safe stdin/request path rather than interpolated into a shell command. Exact model or profile flags are present only when already selected in the frozen request. Unsupported selection fields fail validation.
Executable expectation, argv/environment policies, adapter source, and configuration are static lock inputs. Before launch, the adapter verifies the physical executable, binary digest, and reported version, then records those runtime observations with request/workspace identity in the dispatch fingerprint and process receipt. Credential values are absent.
### 13.2 Child lifecycle confinement

The production `TaskExecutionHost` must guarantee that a Cursor child cannot outlive its authorized host lifecycle. The platform implementation uses an authenticated process group plus a real containment primitive appropriate to the host, such as a job object, cgroup/container supervisor, or parent-death supervisor with verified cleanup.
The adapter must refuse execution when the host cannot provide the required confinement. Merely recording a PID and promising to kill it in `finally` is not sufficient because the engine process itself can crash.
The child receives only:

- the exact attempt workspace as current workspace;
- a minimal allowlisted environment;
- explicitly selected credential injection from the host secret source;
- bounded stdin/stdout/stderr pipes; and
- no engine store, sibling attempt, invocation lock, or host repository path.
### 13.3 Process receipt

Immediately after a successful spawn, the adapter binds an opaque process receipt containing:

- host instance/boot identity digest;
- confinement identity;
- process-group identity and non-reusable start token;
- executable/version digest;
- request and argv-policy digests;
- workspace identity digest;
- start time; and
- observed Cursor stream session identifier when later available.
The session identifier is evidence only in Phase 3. The adapter does not invoke `--resume`, adopt an unknown process, or claim that the identifier is a durable provider activity reference.
### 13.4 Stream parsing and terminal state

The parser reads newline-delimited JSON with byte, line, nesting, and total output limits. It requires:

- one compatible system init record;
- the exact attempt workspace in the init record;
- a consistent stream session identifier within one process;
- structurally valid known event types;
- ignored additive unknown fields under the pinned compatibility rule;
- one terminal result record; and
- an exit status consistent with that terminal record.
Progress records may drive lease heartbeat and bounded diagnostics but are not copied to the graph ledger. Tool records and assistant deltas remain transient.
A successful terminal record is reduced to the strict `AgentRunResult`, result digest, process receipt digest, and evidence digest. The adapter returns `TaskOutcome.succeeded()`; the host ends the call, quiesces and authenticates all confined writers/descendants, and only then persists the exact terminal response plus quiescence proof. The engine authenticates that final receipt, seals the workspace, and publishes the terminal observation bound to that exact candidate.
A definite validation or authentication failure before spawn can be a normal typed task failure. A definite non-zero exit with a complete provider error can be a typed terminal failure. A process disappearance, truncated stream after work started, missing terminal record, host crash, unknown exit, or receipt mismatch is indeterminate.
### 13.5 Recovery behavior

Cursor implements the same `RecoverableTaskHandler` methods. Its locked adapter protocol profile names `confined_process` for audit and conformance, while OpenCode's names `durable_reference`; the engine never switches on either label and derives all behavior from the generic activity state and typed reconcile/cancel result.
On recovery:

- `not_dispatched` may execute the original same attempt once;
- a terminal observation already in the graph ledger finalizes without Cursor;
- an authenticated durable `TaskHostTerminalReceipt` promotes the completed result without adopting a process or contacting Cursor;
- a child still owned by the same live host object may continue to be observed;
- a process receipt from a dead or different host cannot be adopted;
- an unknown or missing child after dispatch is indeterminate; and
- the engine cannot reclaim, reset the workspace, or schedule a new attempt.
The lifecycle-confinement guarantee limits damage from an orphan, but it does not prove whether the provider acted or whether workspace edits are complete. Therefore crash/unknown is never translated into `absent` and never consumes a normal numeric retry.
If Cursor later publishes a stable, documented, testable durable-session API, a future adapter version may use it through the same task-activity seam. That change requires a new source/config/profile digest and does not weaken Phase 3 rules for existing invocations.
### 13.6 Cursor cancellation

`cancel()` sends the configured graceful signal to the authenticated confined process group, waits a bounded interval, then uses the host containment mechanism for forced termination when permitted by locked policy.
Signal delivery is acknowledgement only. A complete canceled/error result plus authenticated exit can become terminal. A host crash or unknown exit remains indeterminate even when lifecycle confinement should have killed the child.
## 14. Prompt, persona, skill, and model ownership

Phase 3 enforces the following ownership table:

| Concern | Owner | Phase 3 adapter behavior |
|----|----|----|
| Business skill meaning | Phase 4 capability plugin | Receives only final frozen data |
| Prompt/instruction assembly | Phase 4 capability plugin | Forwards exact content |
| Persona/role policy | Phase 4 capability plugin | Forwards an exact supported selection |
| Model-routing policy | Phase 4 capability plus Phase 5 product config | Never routes or falls back |
| Provider endpoint choice | Phase 5 product config | Validates locked endpoint identity |
| Provider transport | Phase 3 runtime adapter | Implements HTTP/SSE or child process |
| Retry/recovery mechanism | Graph engine | Applies frozen generic policy |
| Provider-specific reconciliation | Phase 3 runtime adapter | Interprets opaque reference |

An adapter may normalize syntax required by its wire protocol, but canonical request digests are computed before transport transformation and the transformation algorithm is pinned by adapter source/config digest.
No adapter package ships an Assurance prompt or named Assurance persona. No engine package ships any prompt or model field.
## 15. Security, workspace, and credentials

### 15.1 Workspace

Both adapters operate only on the exact task attempt workspace. OpenCode sends that root through its validated directory scoping mechanism on every relevant request. Cursor uses it as cwd and verifies it in the init stream.
The engine authenticates workspace identity before prepare, execute, reconcile, cancel, terminal observation, sealing, and commit. Adapters never receive the engine workspace-store parent or path to an invocation ledger.
Reconciliation never recreates the attempt directory. Candidate sealing and commit continue to enforce resource claims and validator ordering from Phase 2.
### 15.2 Credentials

Credentials come only from the composition-root-injected `TaskExecutionHost` secret resolver and an exact locked non-secret handle. The per-call `SecretPort` may expose them to the adapter as bounded HTTP headers or an allowlisted child environment variable; it cannot enumerate handles and is revoked when the host call ends. Ambient environment/global credential lookup is forbidden.
Credential values, authorization headers, API keys, cookies, OAuth tokens, secret environment values, and credential fingerprints are not written to:

- `InvocationLock`;
- graph events or checkpoints;
- task input/output;
- opaque activity references;
- result/evidence digests;
- process receipts;
- host terminal receipts; or
- normal logs and diagnostics.
Redaction occurs before diagnostic size limiting and persistence. Tests use canary secrets and scan every durable file and captured log.
### 15.3 Locked and recorded identities

The invocation lock or task activity record includes:

- adapter plugin ID, version, source identity, and source digest;
- contract schema version and digest;
- normalized endpoint or executable identity;
- pinned provider protocol profile and observed source/version identity;
- request-policy digest;
- adapter/product configuration digest;
- exact frozen request digest; and
- workspace identity digest.
Runtime-resolved network addresses are diagnostic only unless the locked endpoint policy explicitly pins them. Redirects to a different origin are rejected unless the locked profile names the exact allowed origin.
## 16. Errors and retry classification

Phase 3 adds generic engine errors:

- `TaskActivityConflict`: illegal or competing activity transition;
- `TaskActivityIndeterminate`: external outcome cannot be proven;
- `TaskActivityReferenceInvalid`: non-canonical, oversized, or changed reference;
- `TaskActivityRecoveryUnsupported`: a running activity lacks the required recoverable protocol;
- `AttemptWorkspaceLost`: the prepared attempt workspace cannot be authenticated; and
- `TaskActivityProtocolViolation`: handler result violates the closed state contract.
These errors do not become retryable task failures automatically.
Adapters map definite terminal failures to existing typed `TaskFailure` values and set `retryable` explicitly. Authentication, frozen-request invalidity, foreign reference, drift, duplicate metadata matches, result-schema failure, and unsupported protocol profile are non-retryable.
Rate limit or transport failure is retryable only when the adapter proves no external dispatch occurred. After `dispatch_started`, ambiguity is represented by task-activity state, not a transient failure. A timeout after external work may have started requests cancellation and reconciliation; it does not directly schedule a new attempt.
Provider error messages are bounded and redacted. Full remote bodies and child stderr remain transient unless a small redacted projection is required for the typed result evidence digest.
## 17. Compatibility and hard cut

Phase 3 directly replaces the Phase 2 runtime event schema and plugin API for new-engine invocations. It does not preserve resume compatibility for Phase 2 prototype invocations.
The change includes:

- an engine API version increment;
- a runtime-event/checkpoint schema version increment;
- updated plugin conformance requirements;
- updated source and composition lock digests; and
- regenerated golden folds and isolated wheel fixtures.
There are no aliases for an earlier activity model because none existed. There is no translation from current `assurance_kernel` agent request/result, OpenCode adapter, headless adapter, driver state, or provider events.
Phase 3 may use old code as behavioral reference, but new adapter wheels import no old runtime module. `aa` continues using the old runtime until later phases; that temporary side-by-side state is not compatibility between invocation formats.
## 18. Shared conformance semantics

Both adapters run a common black-box suite against the generic interface. The suite proves:

- strict frozen request parsing and invalid-extra rejection;
- no business instruction or routing mutation;
- exact request/config/policy digest validation;
- prepare/start/lease atomicity;
- no dispatch before prepared state is authoritative;
- bind-before-provider-follow-up where a stable reference exists;
- idempotent exact reference binding;
- cancel request durability before provider cancellation;
- host quiescence and immutable candidate sealing before successful terminal observation;
- terminal observation durability before attempt outcome, with exact candidate consumption;
- durable host terminal receipts promoted exactly once and never treated as graph authority on their own;
- reconcile before reclaim;
- same-attempt adoption without attempt-number increment;
- original workspace identity preservation;
- no arbitrary ledger access from the handler;
- all lifecycle calls confined by the task host and every child inheriting that boundary;
- only locked secret handles resolving through the ephemeral host port;
- effects/checkpoints rejected as activity authority;
- no credential persistence;
- no new attempt after an indeterminate result; and
- successful task replay without contacting a provider after graph-ledger success.
Capability assertions are adapter-specific:

| Capability | OpenCode | Cursor Phase 3 |
|----|---:|---:|
| Stable external reference | Yes | Process receipt only |
| Cross-engine-process reconciliation | Yes | No |
| Same-attempt remote adoption | Yes | No unknown-process adoption |
| Durable cancellation reconciliation | Yes | Only while host ownership is proven |
| SSE/stream observation | SSE with poll fallback | Child NDJSON stream |
| Crash-safe blind retry | Never | Never |

Passing the same suite means both honor the same generic state transitions and safety rules. It does not claim that a process receipt is equivalent to an OpenCode durable reference.
## 19. Test and fault matrix

### 19.1 Generic engine matrix

Tests inject a crash or ambiguous ledger publication:

| Cut | Required recovery |
|----|----|
| Attempt workspace created before initial batch | Remove unauthorised orphan |
| During initial prepared/start/lease batch | Authenticate exact batch or no activity |
| After prepared, before dispatch marker | Adopt same attempt; allow first execute |
| Deterministic handler failure before dispatch marker | Record `prepared -> terminal_observed` failed/stopped with no candidate |
| During dispatch-marker publication | Authenticate exact event; never open two epochs |
| After dispatch marker, before provider call | Reconcile first; never blind reclaim |
| Definite create/spawn rejection with no bound work | Require exact absence proof, then record unbound failed/stopped terminal |
| After provider creation, before bind | Rediscover or remain indeterminate |
| During bind publication | Authenticate exact reference digest |
| After bind, before progress observation | Reconcile same activity |
| During heartbeat | Reconcile before expired-lease reclaim |
| After handler response, before writer/descendant quiescence | No promotable receipt; provider reconcile, or Cursor indeterminate if the host is lost |
| After authenticated quiescence, before final receipt | No promotable receipt; provider reconcile, or Cursor indeterminate if the host is lost |
| During host terminal-receipt publication | Authenticate the exact receipt or no receipt; never accept partial bytes |
| After receipt durability, before host acknowledgement | Promote the exact receipt without provider access |
| After host acknowledgement, before success candidate seal | Promote the exact receipt; never trust a mutable workspace as committed evidence |
| During candidate seal/terminal-event publication | Authenticate the exact candidate/event pair or no terminal observation |
| After terminal event, before validators/commit | Finalize only the recorded candidate without provider access |
| After candidate preparation, before commit publication | Use existing Phase 2 commit recovery |
| After task success | Replay without adapter calls |
| During cancel-request publication | Authenticate request before cancel call |
| After cancel call, before terminal | Reconcile; do not infer cancellation |
| Workspace missing or replaced | Raise `AttemptWorkspaceLost`; no reset/retry |

Fold property tests generate illegal event permutations and require rejection. Two runners racing reconcile/adopt use CAS so at most one lease is authoritative.
### 19.2 OpenCode matrix

The fake OpenCode server covers:

- preflight profile success and every missing required endpoint/field;
- exact metadata creation without caller-selected ID;
- zero, one, and multiple pre-create metadata matches;
- create success response, definite rejection, timeout, disconnect, truncated JSON, and ambiguous proxy response;
- temporarily empty list after ambiguous create, followed by one match;
- permanently empty list after ambiguous create, remaining indeterminate;
- multiple delayed matches, failing closed;
- bind publication crash and exact rediscovery;
- crash after session bind but before prompt POST;
- prompt dispatch success, response loss, and ambiguous response;
- exact, temporarily absent, duplicate, and conflicting deterministic prompt identities;
- exact same-ID/body/delivery retry before and after durable admission, proving one execution;
- same-ID conflicting body/session/delivery rejection;
- SSE fast completion, normal progress, malformed event, wrong-session event, disconnect, reconnect, cursor gap, silence, and duplicate delivery;
- polling fallback with busy, retry, transient idle, sustained terminal, error, and cancellation;
- GET 404 after bind, foreign metadata, endpoint drift, request drift, profile drift, and frozen-selection drift;
- terminal structured-result success and schema failure;
- session diff availability and absence under the pinned profile;
- abort acknowledgement, completion/cancel race, abort transport ambiguity, and missing session during cancellation;
- graph-ledger success followed by provider deletion; and
- credential canaries absent from every durable output.
Assertions include exactly one session create POST per dispatch epoch, no second create while an ambiguous result is unresolved, exactly one prompt identity, and no copied provider event history.
### 19.3 Cursor matrix

The fake confined process host covers:

- validation failure before spawn;
- spawn failure proven to create no child;
- successful init/progress/tool/result stream and zero exit;
- init cwd mismatch, version mismatch, and session-identity mismatch;
- malformed JSON, oversized line, excessive output, truncated final line, and unknown additive fields;
- non-zero exit with complete typed error;
- zero exit without terminal result;
- terminal success followed by contradictory non-zero exit;
- process death after workspace edit;
- engine crash before receipt bind, after receipt bind, and before terminal observation;
- engine/host crash before terminal-response receipt, during its publication, after it is durable but before acknowledgement, and after acknowledgement before ledger terminal observation;
- host boot identity change and PID reuse simulation;
- confinement unavailable, confinement cleanup success, and cleanup ambiguity;
- graceful cancellation, forced cancellation, completion/cancel race, and unknown exit;
- credential canaries absent from argv, receipts, events, output, and logs; and
- a printed session identifier that is never used for recovery adoption.
Assertions include no shell invocation, exact attempt cwd, allowlisted environment only, no unknown-process adoption, no workspace reset, and no blind retry after dispatch ambiguity.
### 19.4 Rebinding and isolation

The installed neutral fixture-capability wheel has one frozen instruction resource, strict result schema, and deterministic request assembler. It emits one byte-identical provider-neutral `AgentRunRequest` for both runs; the fixture explicitly selects the locked `provider_default` policy so no provider/model routing is hidden in either adapter. Two compositions differ only in the explicit alias target and adapter configuration:

- fixture alias -\> OpenCode task handler; and
- fixture alias -\> Cursor task handler.
Both compile without an engine change, deliver the exact same canonical request bytes to the selected handler, and return the same contract-level typed result. Their invocation locks and provider evidence digests differ as expected.
Offline wheel smoke tests install:

1.  graph-engine plus contract wheel only;
2.  graph-engine, contract wheel, OpenCode adapter, and fixture;
3.  graph-engine, contract wheel, Cursor adapter, and fixture; and
4.  both adapters plus fixture with one explicit selected binding.
No installation gains a default product or ambient enabled capability.

### 19.5 Provider-live Phase 3 adapter benchmarks

These are explicitly Phase 3 adapter benchmarks, not the existing Assurance benchmark suite. A committed manifest pins the fixture wheel/source digests, one-task graph/entrypoint, canonical `AgentRunRequest`, initial workspace tree, declared write-set, expected result schema, expected output file and content digest, adapter protocol/profile, external tool version, and terminal success criteria.

Before Phase 3 is accepted, that exact item runs once through a real pinned OpenCode server and once through a real pinned Cursor headless installation. Each run must reach graph-ledger terminal success, commit the expected declared workspace output, produce a schema-valid `AgentRunResult`, preserve adapter-specific evidence digests, and replay locally after provider state is removed. The OpenCode run also proves metadata/session and message-admission rediscovery against the live API; the Cursor run proves the documented `stream-json` contract and confinement preflight.

These live runs are release evidence, not the crash-safety oracle and not an `aa` cutover. Deterministic fakes and fault injection remain authoritative for every ambiguous network/process cut. The full existing Assurance single-item benchmark moves with the Assurance capability/product cutover and is a Phase 5 gate. If either Phase 3 external prerequisite is unavailable, Phase 3 remains unaccepted rather than silently waiving its adapter benchmark.

## 20. Phase 3 acceptance

Phase 3 is complete only when all of the following are true:

- Phase 2 acceptance remains green except for explicitly versioned hard-cut goldens replaced by Phase 3 equivalents.
- `graph-engine` source and wheel contain no OpenCode, Cursor, agent-run, provider, model, prompt, persona, message, tool, or session implementation.
- `TaskHandler.execute()` remains the only normal dispatch interface and the recoverable extension is business-neutral.
- Recoverable attempt preparation, attempt start, and initial lease are one authoritative batch.
- Opaque reference binding, durable cancel request, terminal observation bound to the exact immutable success candidate, and same-attempt lease adoption pass fold and crash tests.
- Async recovery reconciles every live recoverable activity before expired-lease reclaim.
- The original attempt workspace is authenticated and never reset during recovery/adoption.
- A lost or drifted attempt workspace blocks recovery and never creates a fresh attempt behind a live activity.
- The engine exposes no arbitrary ledger writer to a plugin.
- All execute/reconcile/cancel calls remain inside `TaskExecutionHost`, descendant processes inherit its containment, and only the exact authorized secret handles resolve through its ephemeral `SecretPort`.
- The fixed engine-internal host transport is versioned and lock-pinned but is not a public/cross-language plugin protocol; exact terminal receipts survive engine/host acknowledgement cuts and are removed only after ledger promotion.
- Durable effects and checkpoints cannot authorize or repair task-activity state; deleting checkpoints leaves provider call counts and recovery decisions unchanged.
- OpenCode creates sessions without caller-selected IDs and rediscovers by exact versioned metadata.
- OpenCode ambiguous create plus temporarily empty list never issues a duplicate create POST.
- OpenCode prompt admission proves exact client-message idempotency/conflict semantics; crashes before the prompt POST and after a lost successful response converge to one execution.
- Multiple OpenCode metadata matches, foreign session, missing bound session, and drift all fail closed.
- OpenCode uses SSE primarily, falls back to bounded authenticated polling, and does not treat one transient idle observation as terminal.
- OpenCode remains authoritative for full session storage, messages, reasoning, tools, model history, and token/cost data; no `SessionEvent` copy exists in this repository.
- OpenCode terminal structured result, diff digest, and evidence digest enter a typed `TaskOutcome`, and post-success replay needs no OpenCode access.
- Cursor uses a confined child, strict `stream-json`, and an authenticated process receipt.
- Cursor never adopts an unknown process or treats a printed session identifier as a durable recovery handle.
- Cursor crash/unknown outcome is indeterminate and never causes blind retry.
- Both adapters pass shared conformance while advertising their different recovery capabilities honestly.
- An independently installed neutral fixture-capability wheel emits byte-identical canonical request input and rebinds to either adapter without engine code change or SUT-registered executable configuration.
- One complete provider-live single-item benchmark passes through OpenCode and one through Cursor, with post-success replay independent of provider retention.
- Endpoint/executable identity, adapter/source version, request-policy digest, and config digest are locked or recorded per task.
- No credential value appears in lock, ledger, checkpoint, activity reference, receipt, task result, evidence digest input, or log.
- Import-linter, Ruff, format, Pyright, graph-engine tests, adapter tests, conformance, fault injection, offline wheel isolation, packaging smoke, and the full repository gate pass.
Completing these criteria does not move an Assurance capability or cut over `aa`. It proves that later capability plugins have a safe provider-runtime seam.
## 21. Phase 4 and Phase 5 handoff

Phase 4 capability extraction may depend on:

- the frozen `AgentRunRequest`/`AgentRunResult` contract;
- explicit runtime adapter capability IDs;
- exact request and result schema validation;
- opaque activity recovery with typed indeterminate state;
- committed task outputs independent of provider retention; and
- normal graph workspace, validator, and durable-effect semantics.
Phase 4 must supply business instruction assembly, skill semantics, persona selection, result interpretation, evidence policy, and model-routing decisions. It must not extend the engine with agent-specific fields.
Phase 5 product assembly must explicitly choose adapter bindings, endpoint or executable configuration, secret-source names, exact provider/model policy, permission policy, and organization defaults. Those choices become frozen composition and invocation-lock inputs.
Neither later phase may reach around the activity port to write provider state into the graph ledger. If a future provider offers richer durable recovery, its adapter implements the same generic reconcile/cancel seam and pins a new source and protocol profile.
## 22. Implementation-plan boundaries

The implementation plan derived from this design is divided into independently reviewable tracer tasks in this order:

1.  generic activity models, errors, events, and golden canonical encoding;
2.  pure activity fold and illegal-transition property tests;
3.  request projection, immutable invocation metadata, task-host lifecycle operations, and ephemeral secret port;
4.  narrow task-activity dispatch-fingerprint/reference port with CAS/idempotency tests;
5.  attempt workspace create-before-prepare and identity authentication;
6.  reconcile-before-reclaim recovery and same-attempt lease adoption;
7.  durable cancel request, host terminal receipts, host quiescence, success-candidate sealing, terminal observation, and timeout integration;
8.  `agent-runtime-contracts` frozen models and schema conformance;
9.  OpenCode descriptor/configuration/preflight and HTTP fake;
10. OpenCode metadata discovery, create ambiguity, and reference binding;
11. OpenCode exact-idempotent prompt admission, SSE-primary observation, polling fallback, and cancellation;
12. OpenCode terminal reduction, digests, credential redaction, and full fault matrix;
13. Cursor descriptor/configuration and confined process-host interface;
14. Cursor process receipt, strict stream parser, terminal reduction, and cancellation;
15. Cursor crash/unknown indeterminate behavior and full fault matrix;
16. shared adapter conformance and honest capability assertions;
17. neutral declarative rebinding fixture and offline wheel isolation;
18. import contracts, packaging smoke, complete CI/fault evidence, and Phase 3 acceptance audit.
Engine tracer tasks modify only `packages/graph-engine`, its tests, and required workspace metadata. Contract and adapter tracer tasks modify only their new packages, shared conformance fixtures, adapter examples, packaging metadata, and Phase 3 evidence files.
The plan must not modify current `assurance_kernel` or `assurance_agent` runtime behavior, move Assurance skills, change `aa` composition, add an old-runtime bridge, or delete old code. Those actions belong to later phases.
Every tracer task starts with a failing test at the public interface or event fold, implements only its named slice, runs its isolated wheel/import checks, and leaves the repository gate green. Deterministic fakes and fault injection are the acceptance authority for crash semantics; the two provider-live single-item benchmarks are additional mandatory release gates.
## 23. Design review closure

This specification intentionally resolves the Phase 2 ambiguity rather than deferring it:

- external work is represented by generic task activity, not an agent/session abstraction in the engine;
- prepared/start/lease atomicity defines when dispatch becomes legal;
- the dispatch marker separates safe first execution from ambiguous recovery;
- opaque binding gives adapters durable provider identity without provider fields in the engine;
- successful terminal observation closes the external ambiguity only after host quiescence and exact immutable candidate sealing;
- reconciliation precedes reclamation and can adopt the same attempt;
- the original attempt workspace remains authoritative and authenticated;
- OpenCode metadata handles generated session IDs and exact idempotent message admission closes ambiguous prompt dispatch;
- Cursor's weaker durability remains explicit and blocks blind retry; and
- Phase 4/5 own every business instruction and routing decision.
All Phase 3 decisions required for implementation planning are closed. Future stable provider features enter through versioned adapter implementations and the same generic activity seam, not through engine vocabulary expansion.
