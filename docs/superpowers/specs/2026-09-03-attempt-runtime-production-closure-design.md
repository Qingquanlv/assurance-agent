# Attempt Runtime Production Closure Design

> **Status:** implemented architecture; amended 2026-09-04.
>
> **Date:** 2026-09-03.
>
> **Companion:** [Attempt Kernel Internal Refactor Design Note](./2026-09-02-attempt-kernel-internal-refactor-design.md)
> is a later, behavior-preserving cleanup.
>
> **Amendment:** [Checkpoint R Removal Design](./2026-09-04-checkpoint-r-removal-design.md)
> removes the former protected live-provider gate. The ordinary repository gate and focused
> deterministic production-port/recovery suites are the only source-level acceptance requirements.

## 1. Decision

Ship one current Product runtime:

- Python LangGraph is the only Workflow authority;
- `AssuranceAttemptKernel` is the only Attempt transaction authority;
- OpenCode is the only Agent runtime integration;
- Raw Agent output plus local validation is the permanent Agent result model;
- only current control-plane and Capability artifact schemas are accepted.

Phase P closes the incomplete production ports around the existing Attempt transaction. It also
physically removes Cursor integration and every old-version reader, selector, fallback, migration,
and data-shape alias. There is no dual-provider mode, dual-runtime mode, compatibility deployment,
drain protocol, backfill, quarantine path, or historical-data export path.

This work introduces no LangGraph node or edge. One semantic Attempt node still invokes one
`execute_or_recover(...)` transaction. The later internal refactor may reorganize private code only
after this production closure is implemented on a clean, repository-gate-green base.

## 2. Audited baseline

The source baseline is revision `d2a866ea0848460454bb40139de9eb51e0c01301`. It already contains:

- 14 public Python/LangGraph roots;
- six Python Feature bundles;
- 41 semantic Attempt contracts, comprising 33 Raw Agent contracts and eight deterministic Task
  contracts;
- 35 Agent occurrences and 44 total Attempt occurrences;
- the permanent Attempt Kernel, node factory, isolated workspace, production host/worker protocol,
  terminal receipt store, and Effect settler;
- strict extraction of one complete assistant JSON object followed by local `AgentResultT`
  validation; no provider structured-output dependency.

The release path is incomplete:

| Surface | Defect to close |
| --- | --- |
| Product runtime assembly | deferred Agent/Task executors and placeholder workspace, secret, Validator, Effect, and authorization ports remain reachable |
| Attempt persistence | the Product journal pickles and rewrites an in-memory graph instead of using a canonical fenced transactional store |
| execution lifetime | invocation-bound graph/checkpointer state can be constructed before the authoritative runner fence exists |
| activity | the production host can reopen a second activity authority instead of using the Attempt journal |
| raw phase writes | prepare/runtime/finalize do not yet enforce disjoint installed staging claims end to end |
| deterministic Tasks | Effect intents can remain in process-local executor memory after output observation |
| Effects | static handlers capture in-memory stores; settlement and business idempotency identities are conflated |
| obsolete surfaces | Cursor packaging and old runtime/data readers remain in source despite having no place in the target Product |

## 3. Scope

### 3.1 Production closure

Phase P must:

- construct every mutable invocation dependency after runner-lease acquisition and bind it to that
  exact fence;
- install canonical SQLite Attempt and authorization persistence;
- resolve all 41 semantic contracts to real runtime-owned executors;
- run Agent prepare, one OpenCode root session, local result validation, and deterministic finalize
  inside one Attempt;
- enforce authenticated workspace, resource, secret, network, Validator, Schema, and Effect ports;
- persist observed output and Effect intents before commit processing can continue;
- preserve seal, ordered Validators, durable prepare, atomic promote/recover, Effect settlement,
  terminal receipt, and durable resource release.

### 3.2 Direct deletion

The same change removes these product surfaces rather than disabling them:

1. the `agent-runtime-cursor` wheel, Product provider, entry point, binding models, declarations,
   secrets, extras, fixtures, examples, benchmarks, smoke scenarios, tests, and documentation;
2. legacy Workflow evidence readers, runtime discriminators, entrypoint cutover selectors,
   backfill, drain authorization, shadow/parity harnesses, historical status/export/archive, and
   coexistence naming;
3. historical ProductLock/InvocationLock parsing, omitted-field serialization compatibility,
   pickle Attempt state, legacy workspace temporary-name recovery, and legacy Effect settlement;
4. Capability readers that accept old artifact versions, missing version fields, renamed fields,
   historical enum aliases, or permissive read models that differ from current authoring models;
5. dead structured-output negotiation APIs left after Raw Agent became permanent.

The `cursor` field used for OpenCode pagination and similarly named graph iteration variables are
not Cursor IDE integration. Same-version crash temporaries, archive recovery, current
GraphRevision reopening, binding aliases, and current OpenCode poll/list fallbacks are not
old-version compatibility and remain.

### 3.3 Out of scope

- Structured Artifact Pipeline, typed artifact slots, Kernel materialization, OpenCode
  `format.json_schema`, provider response formats, or child-session hierarchy;
- Workflow YAML, GraphDef lowering, a custom scheduler, or project-loadable Python/plugins;
- migration, conversion, export, quarantine, or recovery of data written by an older release;
- a second Agent provider or a generic provider plugin surface;
- changing business Workflow topology;
- the private Kernel decomposition described by the companion note;
- adding production Validator bindings merely to exercise plumbing.

## 4. Authority and invariants

| Concern | Sole authority |
| --- | --- |
| branches, subgraphs, business retry, human interrupt | Python LangGraph Feature/Product graphs |
| Attempt identity, transaction order, recovery, final resolution | `AssuranceAttemptKernel` |
| resolution-to-state/interrupt mapping | `AttemptNodeFactory` |
| input/result/output models and prepare/finalize semantics | installed Capability wheel |
| runtime dependency lifetime and concrete adapters | `assurance-product` |
| OpenCode session/admission/observation protocol | `agent-runtime-opencode` |
| Raw Agent composition and local result validation | `agent-runtime-contracts` |
| journal and resource protocols | `graph_engine.persistence` |
| staging, seal, prepare, promote/recover | `graph_engine.attempts.workspace` |
| Effect state machine | `AttemptEffectSettler`, installed handler, Product Effect state port |
| source acceptance | ordinary repository gate plus focused deterministic runtime/recovery suites |

The following are non-negotiable:

1. one business activation produces one stable `AttemptKey`;
2. one Attempt authorizes at most one OpenCode root session and one admitted prompt;
3. stale fences cannot dispatch, bind, prepare, promote, settle an Effect, publish terminal state,
   or release a newer grant;
4. external activity success is not Attempt success;
5. prepare/runtime/finalize mutate only disjoint installed staging claims and never the canonical
   project tree;
6. output and Effect intents are durable before sealing and independent of executor process memory;
7. Validators run before durable prepare and promotion;
8. Effects run only after promotion and reconcile by stable settlement identity;
9. a terminal result reaches LangGraph only after durable terminal state and resource-release proof;
10. no project input, environment override, or runtime callback can replace installed executable
    code or expand an authenticated binding.

## 5. Current-version-only data contract

The Product recognizes one exact version of each control-plane object and each installed business
artifact. Required version and identity fields are always serialized and always required on read.
There are no unions of current and historical records and no defaulting of a missing version.
Here, “current” means the explicit schema ID/version locked into the candidate ProductLock; it never
means dynamically selecting a `latest` or highest installed version. The implementation plan must
freeze the exact accepted inventory before changing readers.

### 5.1 Control plane

- `ProductLock` is the only composition-lock model. Historical manifests and `InvocationLock` v2
  are deleted.
- one `InvocationIdentityRecord` replaces `RuntimeKind`, `LegacyRuntimeRecord`,
  `LangGraphRuntimeRecord`, and the selection union. It has no runtime discriminator.
- revision retention is keyed by current `revision_id`, not `(runtime, revision_id)`.
- the current SQLite Attempt journal is the only Attempt record. `attempts.pkl` is neither
  inspected nor named in an error contract.
- strict workspace allowlisting rejects unknown control entries before mutation. The error reports
  invalid current layout/schema, not which historical release may have produced the bytes.
- status, lock, export, and archive operate only on the current LangGraph projection and current
  identity records.

Old data is not routed to another binary and no source constant claims that an old deployment can
handle it. If an input does not validate as the current record, the operation fails before mutation.

### 5.2 Capability artifacts

Installed Capability contracts become strict current authoring/read contracts:

| Capability | Compatibility surface to remove |
| --- | --- |
| Intake | legacy review actions/instructions, permissive historical case/explore read models, and old prompt field aliases |
| Generation | the read-only `approved` review form and corresponding persona compatibility text |
| Quality | historical baseline/coverage/MRC mappings, Trace V1/V2 and missing-version aliases, Issue reconciliation V1/V2, nullable old fingerprint inputs, and old report/setup-link forms |
| Healing | old event-kind and field-name fallbacks and `form=legacy` projections |

The Python graph and ProductLock identify the one installed schema used for new artifacts. Any
artifact with a different or missing version, alias-only field, or former shape fails local schema
validation. Tests that previously proved compatibility are deleted or rewritten to prove strict
rejection. Domain values such as a currently valid `deprecated` lifecycle state are retained.

## 6. Target runtime architecture

### 6.1 Compile and execution lifetimes

`aa compile` authenticates installed metadata, builds Python graphs, and emits ProductLock and
GraphRevision data. It does not open an invocation database, resolve a secret, spawn a worker, or
contact OpenCode.

Executable start/run/resume load and authenticate the current GraphRevision, ProductLock,
Invocation identity, and runtime authorization. The Application then acquires the authoritative
runner lease. Only inside that lease does an `InvocationBoundExecutionFactory` create:

- `CheckpointAnchorState` and `AnchoredCheckpointer` carrying the exact live fencing token;
- the invocation-bound 14-root `BootArtifact`;
- the matching `AssuranceRuntimeContext` and `AttemptNodeFactory`;
- every port capable of dispatching or mutating invocation state.

No caller supplies a default fence. Missing or unequal lease, checkpointer, context, configuration,
or stored tokens fail before Kernel entry. Read-only status/lock/export open only read ports and do
not resolve secrets, dispatch work, or repair a checkpointer outbox.

### 6.2 Product composition

`ProductRuntimePorts.open` assembles one invocation-scoped lifetime:

```text
authenticate current ProductLock / GraphRevision / Invocation identity / authorization
  → open descriptor-pinned ChangeWorkspace
  → open shared Product SQLite backend with synchronous=FULL
  → construct SQLite Attempt journal and resource authorization
  → install and seal the Attempt checkpoint observer
  → construct TaskWorkspaceStore and TaskWorkspaceProvider
  → construct dedicated host terminal-receipt store
  → construct current secret and network policy
  → construct Product SQLite EffectStatePort
  → construct production TaskExecutionHost and journal-backed activity bridge
  → resolve 33 Agent and eight Task contracts
  → preflight the selected root's reachable external dependencies
  → construct AssuranceAttemptKernel with the single EffectRegistry and all durable ports
  → expose the lease-bound execution factory
```

Static composition contains authenticated specifications and static handlers only. It never
captures an invocation-owned executor, connection, workspace, secret, mutable store, or deferred
placeholder. Product boot rejects every reachable unresolved handler, binding, model, schema,
secret authorization, Validator ID, Effect port, or workspace port before graph execution.

Forbidden on the production path are `_DeferredPhase`, `_DeferredTaskExecutor`, `_UnusedWorkspace`,
pickle or memory-only production stores, `fixture-model`, scripted Kernel resolutions, tolerant
factories, synthetic inputs, and untyped `object()` placeholders.

### 6.3 Persistence and execution scope

Product adds `SqliteAttemptJournal` and `SqliteResourceAuthorizationStore` over the same SQLite
connection and lock already owned by `AssuranceSqliteBackend`. They remain separate tables and
typed state machines; sharing a transaction substrate does not merge them with LangGraph
checkpoints.

The journal stores versioned canonical JSON and provides:

- `AttemptKey.digest` identity and monotonic record revisions;
- explicit current event discriminators and schema versions;
- digest, Attempt identity, expected revision, and fence validation before fold;
- `BEGIN IMMEDIATE` CAS append committed before acknowledgement;
- exact idempotent replay and rejection of different bytes at the same revision;
- rejection of gaps, unknown fields/versions, omitted required fields, corruption, and stale fences;
- a verified durable-revision barrier and typed terminal replay after restart.

The authorization store durably records acquire, adopt, and release. Same-key replay under the same
live fence reuses a grant. Adoption requires the same Attempt, a strictly newer live fence, and
proof that the earlier runner is no longer current. A stale runner cannot release a newer grant.

After authorization and workspace open, the Kernel creates one private scope containing the
authenticated execution context and staging binding. Executors cannot reopen the workspace or
derive a write root from model output.

Successful execution returns an internal value containing validated output, Effect intents, and an
optional source terminal-receipt reference. It cannot return a committed resolution: only the
Kernel commits. The Kernel validates and atomically journals final output, source receipt link, and
Effect intents before seal. Recovery reads them from the journal and never relies on an executor
object retaining process memory.

### 6.4 Agent and Task executors

Every Agent contract resolves to one real `ResolvedRawAgentExecutor`:

```text
installed prepare(validated input, authorized staging scope)
  → validated AgentRunRequest using the locked OpenCode binding
  → execute/adopt/reconcile one OpenCode root session
  → durable authenticated TaskHostTerminalReceipt(raw TaskOutcome)
  → extract exactly one complete assistant JSON object
  → local installed AgentResultT/schema validation
  → installed deterministic finalize over RawFinalizeBundle
  → validated OutputT + Effect intents + source receipt reference
```

The outbound prompt contains the installed JSON Schema as instructions. The adapter does not send a
structured-output `format` field and does not negotiate provider schema capability. The unused
`StructuredOutputCapabilityError` and `negotiate_provider_schema` APIs and tests are deleted.

Prepare, runtime, and finalize share one authenticated staging identity but receive disjoint,
installed write claims:

- prepare: deterministic request/context artifacts;
- runtime: model-produced raw paths;
- finalize: deterministic evidence/result artifacts, excluding runtime-owned paths.

The host measures the tree before and after every phase and durably records the exact delta. Direct
canonical-project writes, undeclared paths, cross-phase overwrites, traversal, link/mode/ownership
drift, and claim expansion are permanent pre-commit failures. The later Kernel seal validates the
aggregate actual bytes; it does not trust a self-reported manifest.

The eight deterministic Task contracts use a Product-owned adapter over installed Task handlers.
They do not contact OpenCode, but use the same authorization, workspace, validation, Effect,
terminal, and recovery protocol. Their `TaskOutcome.effects` is copied into the durable observed
batch rather than retained in process-local state.

All binding fields come from authenticated installed contributions and the matching ProductLock:
provider, fully qualified model, limits, policy, resource IDs, secret handles, adapter
configuration, handler identities, schemas, and complete contract digest. Environment defaults or
phase aliases cannot rebuild or override a production binding.

### 6.5 One activity authority

`JournalBackedTaskActivityPort` is the sole owner of durable activity prepare, dispatch admission,
session binding, observation, reconciliation, and cancellation state. The production host does not
open a second invocation ledger.

The OpenCode handler records the actual provider admission fingerprint. There is no generic
pre-dispatch fingerprint derived independently by the Kernel. The synchronous worker-facing port
submits journal operations to the owner event loop and acknowledges only after the durable append
commits.

Recovery remains closed:

| Durable state | Action |
| --- | --- |
| no activity | prepare and execute this Attempt once |
| fingerprint durable, no proven session binding | discover and authenticate the same session or return indeterminate |
| bound, admission uncertain | reconcile admission; never blindly send again |
| admitted or running | observe the same session or return pending |
| terminal host receipt durable | reuse its raw outcome without contacting OpenCode |
| final output/Effect batch durable | skip the executor and continue commit recovery |

Activity RPC and host receipt identities bind Attempt, authorization, fence, phase, workspace,
request, GraphRevision, handler, and host implementation. Only the exact current wire version is
accepted. Host receipts live in a dedicated authenticated invocation activity directory and are
retained with the current lifecycle archive.

### 6.6 Workspace, secrets, and network

The Product constructs the existing descriptor-pinned `TaskWorkspaceStore` from the concrete
`ChangeWorkspace.runtime_binding()` fields. Replay opens the same Attempt root. Actual mutation is
restricted to resolved resource claims; path traversal, symlink/hardlink substitution, undeclared
or missing files, mode drift, control-tree mutation, and size violations fail before prepare or
promotion.

`InvocationRuntimeAuthorization` reaches Product runtime construction and is authenticated against
the selected binding. Secret bytes resolve only inside an authorized host call and are revoked
afterward. Logs, checkpoints, journals, and receipts contain handles and digests only. OpenCode
network access is restricted to the locked endpoint and target policy; redirect or resolved-target
drift fails closed.

### 6.7 Effect runtime seam

Keep the existing single static `EffectRegistration(handler)` contribution and `EffectRegistry`.
The installed handler remains the object whose `apply` and `reconcile` provenance is authenticated
and locked. It becomes stateless: invocation state arrives through a narrow call context.

This follows the useful assembly property of Claude Code Skills—one installed definition, loaded
with invocation-time arguments/tools/environment—without copying Skills' weaker execution
semantics. See [Claude Code Skill loading and Effect seam research](../../research/2026-09-03-claude-code-skill-loading-and-effect-seam.md).

The framework contracts are conceptually:

```python
class EffectStatePort(Protocol):
    """Product-private durable persistence for the common Effect envelope."""

    async def observe(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        fencing_token: int,
    ) -> EffectStateObservation: ...

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ) -> None: ...


class EffectCallContext(Protocol):
    @property
    def settlement_key(self) -> str: ...

    async def observe(self, *, business_key: str, intent_digest: str) -> EffectStateObservation: ...

    async def commit(
        self,
        *,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
    ) -> None: ...


class DurableEffectHandler(Protocol):
    async def apply(
        self, intent: EffectIntent, context: EffectCallContext
    ) -> EffectApplyResult: ...

    async def reconcile(
        self, intent: EffectIntent, context: EffectCallContext
    ) -> EffectReconcileResult: ...
```

`AttemptEffectSettler` receives the Product-owned `EffectStatePort` and creates a context already
bound to the authenticated Effect kind, Kernel settlement key, and live fence. The context hides
the kind and fence from Capability mutation and exposes the settlement key read-only for receipt
construction. Capability code supplies only its derived business key, intent digest, payload, and
receipt; it cannot substitute another settlement key. `EffectStateObservation` is the closed
missing-or-committed result; key or digest disagreement raises a typed conflict. Product provides
`SQLiteEffectState`; tests may provide `MemoryEffectState`.

There is no `EffectStoreContract`, `EffectRuntimeFactory`, `EFFECT_FACTORY` executable kind,
factory registry, runtime registry, runtime profile resolver, or second handler authentication.
ProductLock continues to authenticate the static registration, schemas, policy, and handler
`apply/reconcile` provenance. The Product runtime revision and one exact current Effect-state schema
version cover the concrete SQLite adapter.

The six installed kinds remain:

| Capability | Effect kinds |
| --- | --- |
| Healing | `allocation.v2`, `heal-apply.v2`, `proposal-approved.v1` |
| Improvement | `archive.v1`, `delivery.v1`, `promotion.v1` |

Handlers continue to own payload validation, business-key derivation, external operation, and
receipt construction. The Kernel owns
`settlement_key = effect_idempotency_key(AttemptKey, effect_ordinal)`. SQLite records
`(effect_kind, settlement_key, business_key, intent_digest, payload, receipt)` with uniqueness on
both `(effect_kind, settlement_key)` and `(effect_kind, business_key)`.

Same settlement identity with changed intent is corruption. The same business identity from
another Attempt may reuse an identical committed receipt but cannot reapply the operation; changed
intent is a permanent conflict. The delivery-only `_kernel_settlement_key` branch is deleted.
Durable intent, ordered apply/reconcile, pending/indeterminate handling, fencing, receipt
validation, and crash recovery remain Kernel/Settler guarantees; the Claude Skills analogy does
not weaken them.

## 7. Normative transaction

```text
derive stable AttemptKey                         [AttemptNodeFactory]
  → authenticate current identity and load/open snapshot
                                                   [Attempt journal]
  → finish/verify terminal resource release, if terminal exists
                                                   [Kernel + authorization]
  → return terminal replay only after release proof
  → acquire/reuse/adopt resources                [authorization]
  → open authenticated isolated staging workspace
                                                   [workspace provider]
  → execute/adopt/reconcile installed executor  [Agent or Task executor]
      Agent only:
        prepare → one OpenCode root session → raw host receipt
        → exact JSON extraction → local AgentResultT validation
        → deterministic finalize
  → validate OutputT and Effect intents          [Kernel + schemas]
  → atomically record output, source receipt link, and Effect intents
                                                   [Attempt journal]
  → seal actual candidate bytes                  [workspace provider]
  → run ordered declared Validators              [Kernel]
  → durable workspace prepare                    [workspace provider]
  → atomic promote or recover                    [workspace provider]
  → apply/reconcile persisted Effects in order   [AttemptEffectSettler]
  → durably record terminal result and receipt   [Attempt journal]
  → release the resource grant under the live fence
                                                   [authorization]
  → durably record ResourcesReleased proof       [Attempt journal]
  → publish state update or system interrupt     [AttemptNodeFactory]
```

The immutable commit tail is:

```text
validate output/effects
→ record output/effect intents
→ seal
→ ordered Validators
→ durable prepare
→ promote/recover
→ Effects
→ terminal receipt
→ authorization-store release
→ ResourcesReleased proof
```

Terminal and resource release remain two durable authorities. If terminal state exists but the
same Attempt still owns a grant, a current live runner may adopt that exact grant for terminal
cleanup, release it, and append the proof. A release marker paired with an active grant is an
integrity failure. No transaction stage is removed merely to reduce `kernel.py`.

## 8. Recovery and failure semantics

| Crash cut or condition | Required recovery |
| --- | --- |
| before resource grant commits | reevaluate durable claims; never assume ownership |
| grant committed, before workspace open | reuse the same grant and binding under the live fence |
| earlier runner dead, newer fence acquired | durably adopt the same Attempt; block the old runner |
| workspace open, before prepare completes | rerun deterministic prepare against the same binding |
| before OpenCode creation | execute the same admission protocol once |
| session created, before durable bind | discover/authenticate that session or return indeterminate |
| bound, prompt acknowledgement uncertain | reconcile admission; never resend blindly |
| admitted/running | observe the same session or return pending |
| provider terminal, before host receipt | reread the bound session and install one receipt |
| host receipt durable, before finalize | rerun deterministic validation/finalize without OpenCode |
| finalize returned, before output/Effect batch | rebuild from the same receipt and append identical bytes |
| output/Effect batch durable, before seal | replay the journaled values and skip execution |
| Validator rejects | terminal rejected; no prepare, promotion, or Effect |
| durable prepare, before promotion | recover the authenticated prepared write set |
| promotion publication uncertain | prove committed or return indeterminate; never restage |
| promoted, before/during Effect | reconcile the same settlement and business identities |
| terminal durable, grant active | adopt same-Attempt grant under live fence, release, append proof |
| grant released, before release proof | prove absence and append the idempotent proof |
| release proof present, grant active | integrity failure; never return terminal |
| terminal/release proof durable, before graph checkpoint | replay resolution and finish checkpoint anchoring |
| stale fence at any irreversible cut | stop before the cut; never mutate for a newer runner |

Invalid assistant JSON, local schema failure, old artifact shape, invalid file, undeclared write,
or invalid finalizer output is a permanent pre-promotion failure. Resource contention and a
proven-running activity are pending. Unprovable prompt admission, promotion publication, or Effect
publication is indeterminate. A permanent Effect failure after promotion is
`CommittedEffectFailure`, not a synthetic rollback.

## 10. Implementation sequence

1. Add failing architecture and Product tests for the direct deletion/current-only boundary and
   for every currently reachable production placeholder.
2. Remove Cursor packaging and Product integration, update the workspace lock, imports, fixtures,
   benchmarks, smoke scripts, and public documentation.
3. Remove legacy runtime/control-plane readers, selectors, backfill/drain/shadow code, obsolete
   serialization fallbacks, compatibility tests, and coexistence names. Replace them with one
   current Invocation identity and strict record models.
4. Remove old Capability artifact readers and aliases in Intake, Generation, Quality, and Healing;
   update installed schemas, prompts, fixtures, and rejection tests as one current-contract change.
5. Add canonical SQLite Attempt/authorization persistence and lease-bound Product construction;
   wire real workspace, host/worker, activity, secrets, network, Validator, and Schema ports.
6. Resolve all 33 Agent and eight Task contracts; enforce phase write claims and durable output/
   Effect observation; delete structured-output negotiation and every deferred/test production path.
7. Replace captured Effect stores with the single static registration plus call-context seam; add
   Product SQLite Effect state and migrate all six handlers to settlement/business-key semantics.
8. Pass deterministic transaction, crash, security, lifecycle, packaging, and full repository
   tests plus all three wheel smoke tests on the clean implementation candidate.

Phase I is deliberately absent from this sequence. It receives a separate implementation plan only
after Phase P is accepted and after reviewers can inspect the resulting Kernel shape.

## 11. Expected ownership

Framework packages retain reusable protocols and transaction mechanisms:

```text
graph_engine/
├── attempts/       # Kernel, context, workspace, host/worker, activity and receipts
├── effects/        # settler, EffectStatePort and bound call context
├── persistence/    # Attempt journal and resource authorization protocols
└── composition/    # current ProductLock/GraphRevision and single EffectRegistry
```

Product owns concrete invocation composition and current SQLite adapters:

```text
assurance_product/
├── runtime_ports.py
├── runtime_bindings.py
├── sqlite_checkpointer.py
├── sqlite_attempt_store.py
└── sqlite_effect_state.py
```

Capability wheels retain current models, prepare/finalize handlers, static Effect handlers,
business-key formulas, and receipt builders. The OpenCode adapter retains transport-specific
session and message behavior. The SUT cannot install executable code or schemas.

The Cursor adapter package and all old-version runtime/data readers have no target directory: they
are deleted.

## 12. Verification and acceptance

Minimum focused proofs are:

| Surface | Proof |
| --- | --- |
| Attempt journal | canonical current-version round trip, required fields, corruption/unknown rejection, CAS, restart, stale fence, exact replay |
| authorization | cross-process conflict, same-key reuse, newer-fence adoption, stale release, durable terminal cleanup |
| identity/revision | single current record, exact ProductLock/GraphRevision pin, current reopen/retire, no runtime discriminator |
| workspace | stable binding, phase-specific deltas, actual mutation equality, path/link/mode/size rejection, prepare/promote recovery |
| Agent | all 33 contracts, one session/prompt, strict JSON/result validation, host-receipt replay, no direct project write |
| Task | all eight handlers, typed output, durable Effect-intent capture |
| activity | one journal authority, admission ambiguity, pending observation, cancellation/reconcile, current wire identity |
| commit | output validation, ordered Validator accept/reject, zero promotion on reject, stable promotion receipt |
| Effects | one registry, six static handlers, Product SQLite state, two-key replay/conflict, apply/reconcile/pending/indeterminate/permanent/crash cases |
| current-only artifacts | old control and Capability shapes reject before mutation; no compatibility decoder or alias remains |
| lifecycle | start/run/resume/status/lock/export/archive after restart with current pinned identities |
| packaging | no Cursor wheel/provider/declaration/fixture, legacy reader, pickle path, placeholder, or factory registry |
| repository gate | Ruff, format, Pyright, import-lint, full pytest, and all three wheel smokes |

Phase P is accepted only when:

- all 41 contracts resolve to real production executors and all 44 occurrences retain stable
  Attempt identity;
- every Agent follows prepare → one OpenCode session → local validation → deterministic finalize;
- every phase writes only its installed staging claims and the Kernel seals actual bytes;
- restart at every section 8 cut duplicates no prompt, promotion, Effect, terminal result, or
  resource release;
- no terminal resolution reaches LangGraph while an active same-Attempt grant remains;
- output and Effect intents survive process death independently of executor memory;
- one authenticated `EffectRegistration`/`EffectRegistry` path serves all six handlers and no
  factory/store/runtime registry chain exists;
- Product runtime uses only current canonical SQLite records and rejects all former control and
  business artifact shapes without routing, migration, or special historical error protocol;
- Cursor integration is absent from workspace membership, distributions, Product declarations,
  runtime bindings, CLI choices, fixtures, benchmarks, smoke tests, and release documentation;
- executable boot fails before graph mutation when a required current production port is missing,
  while offline compile and non-Agent roots do not require live OpenCode;
- focused production-port, journal, crash/replay, security, and lifecycle suites pass;
- the full repository gate and all three wheel smoke tests pass on a clean candidate.

This acceptance makes no claim that a real OpenCode release, provider, or model currently satisfies
the repository's protocol assumptions. It makes no claim about data or integrations from an older
release and does not imply completion of the separate Kernel internal refactor.
