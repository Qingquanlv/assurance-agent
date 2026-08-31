# Python-native LangGraph Assurance Runtime

- **Status:** Accepted
- **Date:** 2026-08-31
- **Supersedes:** [LangGraph-first Assurance Boot Runtime](./2026-08-31-langgraph-assurance-boot-runtime-design.md)
- **Implementation plan:** [Python-native LangGraph Migration Program](../plans/2026-08-31-python-native-langgraph-migration.md)
- **Replaces plan:** [Assurance Baseline and Agent Leaf Tracer Implementation Plan](../plans/2026-08-31-assurance-baseline-and-agent-leaf-tracer.md)
- **Scope:** Replace YAML-authored Workflow topology and the custom Graph Runtime with installed-wheel Python `StateGraph` factories, while retaining authenticated capability loading and reliable Attempt commit/recovery.

## 1. Executive Summary

Assurance Workflow topology will be authored directly in Python with LangGraph `StateGraph`. The six Capability wheels own their subgraphs; `assurance-product` explicitly composes those subgraphs into the public Product entrypoints. LangGraph becomes the only authority that decides the next node, loop, branch, fanout, interrupt and Workflow terminal state.

The following are removed from the target architecture:

- all packaged `resources/workflow/module.yaml` files;
- the Product `resources/workflow/main.yaml` / `workflow-schema.yaml` topology file;
- `.aa/workflow-schema.yaml` project replacement;
- `.aa/execution-contracts.yaml` task-contract replacement;
- Workflow module import/export/slot assembly;
- `WorkflowModuleDef`, `GraphDef`, `NodeDef`, edge/projection DSL compilation;
- the custom activation planner, token scheduler, subgraph loop and Workflow checkpoint authority;
- the proposed `AgentLeafSpec` compatibility layer.

Installed Python wheels remain the only executable extension mechanism. A project can change nodes and edges by changing Assurance Python code, running the repository gates, rebuilding the affected wheels and deploying a new authenticated Product revision. The SUT is never scanned for graph factories or executable Python.

The `graph-engine` wheel may keep its package name, but its target role is a Spring-Boot-like application framework: source authentication, registry/contract resolution, graph revision construction, the Attempt Kernel, persistence adapters and CLI application services. It no longer contains a second Workflow Runtime.

LangGraph does not replace reliable file/effect commit. Every effectful graph node delegates one semantic Attempt to `AssuranceAttemptKernel`, which owns workspace isolation, OpenCode activity recovery, sealed write sets, validators, durable prepare, atomic promotion, effects and idempotent recovery. LangGraph checkpoints retain control state; the Attempt journal retains authenticated durable facts.

## 2. Goals

1. Define every Workflow node, edge, subgraph and route as explicit Python `StateGraph` code.
2. Make LangGraph the sole Workflow progression and checkpoint authority.
3. Preserve Feature ownership: a wheel owns its graph, task contracts, handlers, schemas, validators, resources and effects.
4. Preserve the 14 public Product entrypoint names and current CLI lifecycle.
5. Delete the 99 Product phase-alias IDs and collapse 102 phase-slot node occurrences into 34 semantic Agent node occurrences backed by 33 distinct Agent contracts.
6. Keep project/SUT configuration closed data only.
7. Preserve reliable, recoverable, exactly-once-observable workspace promotion and effect settlement.
8. Pin each Invocation to an authenticated Python graph revision.
9. Rewrite current complex loops and joins into specific typed LangGraph state rather than recreating a generic token scheduler.
10. Delete the old Workflow Runtime after parity, drain and rollback gates pass.

## 3. Non-goals

- Loading Python graph code, handlers, validators or effects from the SUT.
- Supporting arbitrary topology replacement through `.aa/`.
- Retaining a generic YAML Workflow language beside Python graphs.
- Building a generic `GraphProvider` plugin ecosystem.
- Serializing `CompiledStateGraph` as a canonical graph definition.
- Treating LangGraph checkpoints as assurance evidence or a replacement for receipts.
- Preserving legacy internal token IDs, activation IDs or checkpoint documents as public contracts.
- Introducing LangGraph solely to orchestrate the pure offline Eval comparator.
- Creating a Nightly Workflow until a real registered Product entrypoint and scheduler contract are specified.

## 4. Authority Model

| Concern | Sole authority |
|---|---|
| Nodes, edges, routes, loops and subgraph composition | Installed Python `StateGraph` factory code |
| Invocation next step, interrupt and Workflow terminal | LangGraph checkpoint + compiled graph revision |
| Checkpoint identity, integrity and lineage | Checkpoint store + matching journal anchor |
| Invocation runner ownership | Invocation lease + monotonic fencing token |
| Task identity, resource claims, handlers and validators | Feature-owned Python Attempt contract resolved at Boot |
| Deployment runtime/model/provider selection | Product-owned `AgentRuntimeBinding` |
| Canonical workspace mutation | `AssuranceAttemptKernel` |
| External effect application/reconciliation | Attempt Kernel effect protocol |
| Authenticated durable facts | Append-only Attempt/evidence journal |
| Organization policy, thresholds and model configuration | Closed `.aa/` data files |
| Graph revision identity | Authenticated Product lock and wheel source digests |

No second planner, token queue, route interpreter or Workflow projection may advance the same Invocation.

## 5. Target Architecture

```text
aa CLI / deployment host
        │
        ▼
GraphEngineBoot
  ├── authenticate Product + six installed Capability wheels
  ├── build handler / validator / effect registries
  ├── resolve immutable Attempt contracts
  ├── import the Product-declared graph factory symbols
  ├── dry-build Python StateGraphs + GraphBuildManifest
  └── bind host checkpointer/runtime ports → BootArtifact
        │
        ▼
AssuranceApplication
  ├── select one public entrypoint graph
  ├── bind invocation_id to LangGraph thread_id
  ├── enforce pinned GraphRevision
  ├── acquire InvocationRunnerLease + fencing token
  ├── invoke / resume the compiled graph
  └── normalize InvocationStatus
        │
        ▼
LangGraph: only Workflow progression authority
  ├── Feature subgraphs
  ├── typed partial-state reducers
  ├── conditional edges and Send fanout
  ├── pure human interrupt nodes
  └── Attempt adapter nodes
        │
        ▼
AssuranceAttemptKernel.execute_or_recover(...)
  ├── resource authorization
  ├── isolated workspace
  ├── prepare hook
  ├── deterministic handler or OpenCode + JSON Schema
  ├── finalize hook
  ├── seal write set
  ├── commit validators
  ├── durable prepare
  ├── atomic promote / recover
  ├── effect apply / reconcile
  └── AttemptResolution + receipt
```

## 6. Target Directory Structure

```text
packages/framework/graph-engine/graph_engine/
├── boot/
│   ├── source_authentication.py
│   ├── graph_revision.py
│   └── boot.py
├── attempts/
│   ├── contracts.py
│   ├── keys.py
│   ├── kernel.py
│   ├── node_factory.py
│   └── resolutions.py
├── effects/
│   ├── contracts.py
│   ├── apply.py
│   └── recovery.py
├── application/
│   ├── application.py
│   ├── status.py
│   └── revision_guard.py
├── persistence/
│   ├── anchored_checkpointer.py
│   ├── runner_lease.py
│   └── journal.py
└── composition/
    ├── registries.py
    ├── lock.py
    └── sources.py

packages/adapters/agent-runtime-contracts/agent_runtime_contracts/
├── execution_contract.py
├── attempt_executor.py
└── runtime_binding.py

packages/capabilities/assurance-generation/assurance_generation/
├── graphs/
│   ├── state.py
│   ├── nodes.py
│   ├── routes.py
│   ├── api.py
│   ├── e2e.py
│   ├── fuzz.py
│   ├── performance.py
│   └── factory.py
├── contracts/
├── handlers/
└── validators/

packages/products/assurance-product/assurance_product/
├── graphs/
│   ├── state.py
│   ├── routes.py
│   ├── entrypoints.py
│   ├── revisions.py
│   └── factory.py
├── application.py
└── cli.py
```

The other five Capability wheels use the same `graphs/` ownership pattern. Feature graph modules may import their own state, pure route functions, domain models and immutable Attempt contracts. They do not import concrete runtime adapters, effect handlers, commit validators or effectful task-handler implementations; those are reached only through the owner-scoped build context. A Feature also must not import another Feature's `graphs`, handlers, validators, effects or implementation. Product is the only cross-Feature graph composer.

## 7. Installed Graph Factory Loading

This design does not introduce dynamic graph discovery. The Product code contains the exact allowlist of six factory symbols:

```python
FEATURE_GRAPH_FACTORIES = (
    FeatureFactoryRef("assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"),
    FeatureFactoryRef(
        "assurance.generation",
        "assurance_generation.graphs.factory:build_generation_graphs",
    ),
    FeatureFactoryRef(
        "assurance.execution",
        "assurance_execution.graphs.factory:build_execution_graphs",
    ),
    FeatureFactoryRef("assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"),
    FeatureFactoryRef("assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"),
    FeatureFactoryRef(
        "assurance.improvement",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
)
```

Boot resolves and authenticates the installed Product and Capability wheel source snapshots before importing these symbols. A symbol is accepted only when its module origin belongs to the authenticated owner/source. No source path, entrypoint or import string is read from `.aa/` or the SUT.

The existing authenticated Capability contribution is extended with immutable `attempt_contracts` data. Its descriptor and realized contribution must have the same contract IDs/digests, just as handlers and validators do today; configuration-tree contributions are forbidden from declaring contracts. This is not graph-provider discovery: the Product still owns the fixed six-symbol allowlist, while each symbol receives only the contracts already authenticated for its owner.

Graph factories are deterministic construction functions. They may use the authenticated owner-scoped contract catalog and framework node factories, but may not inspect the SUT, call the network, read ambient environment variables, use wall-clock/random values or vary topology from organization configuration. Configuration remains typed Invocation state consumed by nodes/routes. Every input capable of changing topology is authenticated wheel code and therefore changes the graph revision.

Python is not a semantic sandbox. The installed, authenticated Product/Capability wheel is a trusted code unit, so provenance alone cannot prove that an arbitrary node is pure. Import-linter plus an architecture test enforce the graph-package dependency rules above, and Feature factory tests build with a spy context to inventory every Attempt binding. Code review remains part of the trust boundary. A graph node that performs hidden filesystem/network/subprocess work is a release-policy violation even if LangGraph can execute it.

Each Feature exposes a small, Feature-specific graph bundle. For example:

```python
@dataclass(frozen=True)
class GenerationGraphs:
    generate: CompiledStateGraph


def build_generation_graphs(context: GraphBuildContext) -> GenerationGraphs:
    return GenerationGraphs(generate=_build_generation(context).compile())
```

Improvement may expose multiple typed fields such as `retro`, `review`, `evaluate`, `export`, `apply`, `rollback` and `archive`. Callers do not receive internal node tables or mutable builders.

## 8. Product Graph Composition

`assurance-product` explicitly imports the authenticated Feature bundles and constructs one root graph factory for each public entrypoint. A public entrypoint is not a dispatch value inside one giant graph; it selects a compiled root graph from `BootArtifact.entrypoints`.

```python
@dataclass(frozen=True)
class ProductGraphs:
    entrypoints: Mapping[ProductEntrypoint, CompiledStateGraph]
    contracts: Mapping[ProductEntrypoint, EntrypointGraphContract]


@dataclass(frozen=True)
class ProductFeatureBundles:
    intake: IntakeGraphs
    generation: GenerationGraphs
    execution: ExecutionGraphs
    quality: QualityGraphs
    healing: HealingGraphs
    improvement: ImprovementGraphs


def build_product_graphs(
    *,
    context: GraphBuildContext,
    features: Mapping[str, object],
) -> ProductGraphs:
    bundles = ProductFeatureBundles.from_owner_mapping(features)
    return ProductGraphs(
        entrypoints={
            "full": build_full_graph(
                context,
                bundles.intake,
                bundles.generation,
                bundles.execution,
                bundles.quality,
                bundles.healing,
            ),
            "execute": build_execute_graph(
                context,
                bundles.generation,
                bundles.execution,
                bundles.quality,
                bundles.healing,
            ),
            "retro": build_retro_graph(context, bundles.improvement),
            "archive": build_archive_graph(context, bundles.improvement),
            # Remaining public entrypoints are equally explicit.
        },
        contracts=ENTRYPOINT_CONTRACTS,
    )
```

The 14 existing public entrypoint names remain stable unless a separate public-interface change is approved.

## 9. State Model and Subgraphs

LangGraph state is control-plane state, not an artifact store. It may contain:

- immutable Invocation identity and pinned revision metadata;
- typed business input and current decisions;
- round/budget counters;
- receipt and evidence references;
- result summaries needed by downstream routes;
- interrupt and terminal envelopes.

It must not contain:

- complete project files or workspace trees;
- secrets;
- unbounded event history;
- raw OpenCode sessions;
- a duplicate token/readiness table;
- external effect payloads that already have a durable receipt.

Each Feature owns a typed state schema. Subgraphs sharing parent keys may be embedded directly and compile with `checkpointer=None`, inheriting the parent persistence configuration. When child and parent schemas differ, Product uses one explicit adapter node that constructs child input, invokes the subgraph and publishes a typed parent update. If that child interrupts, both the parent adapter node and the interrupted child node restart from their beginnings; adapter work before child invocation must therefore be pure or idempotent. Internal Feature projections never move to Product.

Every Feature and Product state schema extends the framework `CheckpointBridgeState`, which reserves one bounded JSON-only last-write channel, `assurance_checkpoint_markers`. The channel carries only successful-resume completion markers emitted by `AttemptNodeFactory`; interrupt-issuance markers are read directly from LangGraph's persisted interrupt payloads. A completion batch contains one marker for every active generation/ordinal replayed by that node invocation, so pending → resume → pending → resume → commit retires the whole checked set atomically after anchoring. A fresh Attempt success writes an empty batch, and a later Attempt overwrites the prior batch; repeated observation of a retained batch is idempotent. The channel is excluded from public input/output adapters and semantic task-input digests, but included in checkpoint integrity. A fixed maximum active-generation count prevents unbounded state.

Concurrent writes are permitted only on state keys with an explicit deterministic, associative reducer; reducers for unordered parallel results must also be commutative. Receipt reducers de-duplicate by receipt ID. Ordinary scalar decisions do not receive a reducer. Concurrent scalar updates raise `INVALID_CONCURRENT_GRAPH_UPDATE` at graph execution time, so fanout tests exercise every permitted concurrent write set before release.

## 10. Semantic Attempt Contracts

The existing 99 Product prepare/execute/finalize alias IDs are deleted. They are deployment plumbing created by YAML capability slots, not 99 independent business tasks. The current Feature YAML contains 102 phase-slot node occurrences. They represent 34 logical Agent node occurrences because Intake invokes one of its contracts in both primary and repair paths; the catalog still contains 33 distinct Agent contracts.

The shared `AgentExecutionContract` type lives in `agent-runtime-contracts`; each of the 33 immutable values remains defined in its owning Feature's contracts package:

```python
@dataclass(frozen=True)
class AgentExecutionContract(Generic[InputT, AgentResultT, OutputT]):
    contract_id: str
    owner_id: str
    prepare_handler_id: str
    finalize_handler_id: str
    skill_id: str
    agent_profile: str
    input_model: type[InputT]
    agent_result_model: type[AgentResultT]
    output_model: type[OutputT]
    requires_provider_schema: bool
    resources: ResourceClaims | ResourceClaimTemplate
    retry: AttemptRetryPolicy
    timeout: AttemptTimeoutPolicy
    validators: tuple[str, ...]
```

The remaining 25 direct task-node occurrences initially present 12 distinct Feature-owned candidates. Characterization classifies four IDs—Generation complete, Generation review-round advance, Intake review-round advance and Healing repair-round advance—as deterministic pure functions with no filesystem, activity, effect or durable-evidence obligation. They account for 16 occurrences and become ordinary typed graph nodes. The other eight IDs are effectful Improvement contracts at nine occurrences and use the following core contract. In particular, `assurance.improvement.evaluate-memory-improvement` appears in both the standalone evaluate graph and the apply graph; it returns `MemoryEvalReceipt` and emits registered effect kind `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`.

```python
@dataclass(frozen=True)
class TaskAttemptContract(Generic[InputT, OutputT]):
    contract_id: str
    owner_id: str
    handler_id: str
    input_model: type[InputT]
    output_model: type[OutputT]
    resources: ResourceClaims | ResourceClaimTemplate
    retry: AttemptRetryPolicy
    timeout: AttemptTimeoutPolicy
    validators: tuple[str, ...]
```

The topology therefore has 45 candidates before purity classification and targets 41 immutable effectful contracts at 43 Attempt-node occurrences: 33 Agent contracts at 34 occurrences plus eight direct Improvement contracts at nine occurrences. Four proven-pure functions cover the remaining 16 direct occurrences.

`validators` is required and has no default. An empty tuple is valid only when explicitly declared. Validator registration never implies binding.

Product owns deployment-only runtime bindings:

```python
@dataclass(frozen=True)
class AgentRuntimeBinding:
    contract_id: str
    runtime_handler_id: str
    provider: str
    model: str
    policy: AgentRuntimePolicy
    secret_handles: tuple[str, ...]
```

Boot requires exactly one Product runtime binding for each of the 33 Agent contract IDs and rejects missing/extra IDs. It combines one Feature Agent contract and one Product runtime binding into a core `ResolvedAttemptContract`, after resolving all handlers and validators to authenticated registry entries and checking owner/dependency closure. Direct task contracts resolve without a Product runtime binding.

The dependency direction remains strict: `agent-runtime-contracts` depends on `graph-engine`; `graph-engine` never imports `agent-runtime-contracts`. The adapter layer constructs a `CompositeAttemptExecutor` from authenticated prepare, selected Agent runtime and finalize entries. Core sees only the generic resolved executor and contract. This preserves the existing boundary test that forbids a reverse dependency.

There are currently 25 registered validators across the six Feature wheels: Execution 2, Generation 8, Healing 3, Improvement 4, Intake 2 and Quality 6. They are not one-to-one with the 41 effectful contracts, and registry membership proves availability rather than use. Current Workflow nodes bind zero validators. Any new non-empty production binding is therefore an explicit behavior change, not preservation of an effective legacy attachment. Before such a binding is approved, its complete staged-set context and validator inputs must be demonstrated. Every shipped effectful contract still declares an ordered `validators` tuple, including an explicit empty tuple when appropriate. Contract closure does not require every registered validator to be used.

The migration nevertheless proves the binding machinery across both runtimes with one test-only parity fixture. Boot/test code starts from the authenticated resolved core contract for `assurance.execution.agent.execute.v1`, clones its data-only `TaskAttemptContract` with a test ID, `validators=("assurance.execution.validator.evidence.v1",)` and `ResourceClaims(writes=("tests", "src"))`, then constructs a test-only `ResolvedAttemptContract` with the existing authenticated Execution executor. The legacy fixture node uses the same test-only resource envelope and validator. Thus both `tests/test_validator_parity.py` and `src/validator_parity.py` pass resource/seal admission and reach the validator: the first must call it exactly once and promote on both sides; the second must call it exactly once, reject, perform zero durable commit-prepare and never promote. No 34th Agent runtime binding is created. This fixture is excluded from all Feature contributions, ProductLock/GraphBuildManifest digests, packaged declarations and production binding counts. The production invariant remains 25 registered and zero bound until a separate behavior-change decision explicitly approves otherwise.

Prepare, runtime execution and finalize are one semantic Attempt and one commit. The composite executor retains the validated logical input, prepared value and validated Agent result and passes the typed bundle to finalize; phase-to-phase data does not leak back into graph projections. Phase receipts may remain internal Attempt evidence. If a prepare/finalize operation has independent business routing, approval, compensation or durable scheduling semantics, it receives its own explicit contract and LangGraph node; three generic phase aliases are not restored.

## 11. Attempt Node Factory

The graph-facing seam is deliberately small:

```python
class AttemptNodeFactory:
    def attempt(
        self,
        contract: ResolvedAttemptContract[InputT, OutputT],
        *,
        semantic_node_id: str,
        activation: Callable[[StateT], BusinessActivation],
        select: Callable[[StateT], InputT],
        publish: Callable[[StateT, OutputT, ReceiptRef], StateUpdate],
    ) -> StateNode[StateT]: ...
```

`select` and `publish` belong to the Feature graph containing the node. This replaces the declarative projection DSL with local typed Python functions. The 102 phase input projections are absorbed by the composite Agent executor; direct Feature task projections remain Feature-local typed functions. Product handles only public Feature-subgraph input/output seams rather than taking ownership of internal projections.

Feature factories receive an owner-scoped `CapabilityBuildContext`. Graph code names one of its own immutable contract IDs; the context resolves that exact authenticated contract and delegates to the internal factory above. Graph code cannot supply arbitrary handler IDs, validator lists or Product runtime settings at a node site.

The node factory hides:

- semantic Attempt key creation;
- resource authorization and conflict waiting;
- Kernel execution/recovery;
- technical retry and timeout handling;
- mapping `AttemptResolution` to state update, failure edge or interrupt;
- receipt reference publication.

Pure deterministic functions with no filesystem, external activity, effect or durable evidence requirement are ordinary LangGraph nodes and do not call the Kernel.

## 12. Stable Attempt Identity

An Attempt key must not use LangGraph private task IDs. It is derived from stable semantic data:

```text
AttemptKey = digest(
  invocation_id,
  graph_revision,
  public_entrypoint,
  semantic_node_id,
  business_activation,
  contract_id,
  task_input_digest,
)
```

`business_activation` is an explicit stable identity for the relevant business occurrence. `select()` output is validated into `InputT` and canonically serialized before `task_input_digest` is computed. Technical retries and replay after interrupt reuse the same key. A new business repair/review round or a distinct late trigger receives a new key.

Every effectful node supplies a required `activation(state) -> BusinessActivation` selector. `BusinessActivation` is a frozen validated value object with `kind: Literal["root", "round", "trigger"]` and a nonempty bounded canonical `value`. `one_shot()` returns the fixed root occurrence, `for_round(index)` represents an ordinary business loop, and `for_trigger(arrival_id)` represents the stable current-trigger arrival. Neither field may use LangGraph task/checkpoint IDs, time, randomness or process identity. This makes a same-epoch late arrival a distinct Attempt while replay of the same arrival reuses its key.

## 13. Attempt Kernel Transaction

`AssuranceAttemptKernel.execute_or_recover(attempt_key, contract, task_input)` performs:

1. adopt or create the durable Attempt record;
2. obtain `ResourceArbiter` authorization;
3. create/adopt the isolated workspace;
4. execute the resolved executor: either one direct authenticated handler or the composite prepare → Agent runtime → `agent_result_model` validation → finalize sequence;
5. validate the final executor result into the contract's `output_model`;
6. seal the complete staged write set and staged bytes;
7. run the contract's ordered commit validators;
8. write durable prepare data;
9. atomically promote or recover the workspace mutation;
10. apply/reconcile declared effects;
11. return one terminal or suspended `AttemptResolution` with receipt references.

The Kernel never chooses the next Workflow node and never writes an Invocation terminal directly.

## 14. Attempt Resolution Mapping

| Kernel resolution | LangGraph adapter behavior | Workflow meaning |
|---|---|---|
| `CommittedTaskResult` | `publish()` partial state update | normal downstream routing |
| `RejectedTaskResult` | typed failure update | explicit failure/rework edge |
| `PermanentTaskFailure` | typed failure update | graph-selected failure terminal or recovery branch |
| `PendingTaskResult` | system interrupt envelope | resume same node/Attempt key later |
| `IndeterminateTaskResult` | system interrupt envelope | reconcile same Attempt before progress |
| `CommittedEffectFailure` | nonretryable update with `writes_promoted=true` | explicit failure edge; never repeat file mutation |

Human decisions use separate pure interrupt nodes. System interrupts carry no human action and instead contain a typed reconciliation/wakeup reference.

A stable Attempt key alone is not sufficient for a system interrupt. Once an adapter emits `interrupt()`, LangGraph requires that call to recur at the same ordinal whenever the node restarts. The Attempt journal therefore records a `pending_generation` and interrupt ordinal before emission. On resume/replay the adapter first replays every previously issued interrupt at the same ordinal, then re-enters `execute_or_recover`; it never conditionally skips or reorders an issued interrupt merely because the Kernel/effect state has since changed.

Checkpoint observation has two distinct phases. Anchoring the interrupt-bearing checkpoint records `SystemInterruptIssuanceAnchored` and permits the interrupt to be exposed, but does **not** retire the pending generation. After the resumed node has replayed the ordinal and obtained a non-pending Kernel resolution, its state update carries a completion marker for every active generation replayed during that invocation. `aput_writes` may persist that partial update, but it must never publish a completion observer notice. Only `aput` anchoring the merged successful resumed-node checkpoint records the checked `SystemInterruptCompletionCheckpointed` batch and retires that exact active set. Both marker deliveries are idempotent and digest/generation checked. Thus a crash after the pending write but before the successful checkpoint still replays every active ordinal, while a crash after that checkpoint only redelivers observer work and never re-enters the node.

## 15. Structured Agent Output

`AgentExecutionContract.agent_result_model` is the provider-facing structured response contract. The selected Agent runtime adapter receives `agent_result_model.model_json_schema()` when the runtime supports schema-constrained output and always validates the returned value into `AgentResultT` before finalize. `output_model` is separate: it validates the finalize result that the Kernel commits and `publish()` exposes to downstream graph state.

Capability negotiation is fail-closed: a Contract that requires provider-side schema enforcement cannot run on an adapter that does not advertise it. Local Pydantic validation remains required because provider enforcement is not a durable trust boundary.

This migration does not require carrying a private OpenCode source fork. Product pins an OpenCode/adapter version that advertises the required structured-output capability; until that is available, a contract marked `requires_provider_schema=True` cannot cut over. Contracts that permit adapter-side validation may run with strict local parsing, but they do not claim provider-side enforcement.

JSON Schema removes response-envelope parsing and field-shape boilerplate. It does not remove:

- workspace path authorization;
- full staged-write sealing;
- artifact cross-reference validation;
- semantic commit validators;
- promotion and effect recovery.

## 16. Direct Control-flow Rewrites

The migration rewrites each current behavior directly; it does not lower or interpret legacy `GraphDef` semantics.

| Legacy feature | Python-native LangGraph implementation |
|---|---|
| feature/product subgraph | compiled child `StateGraph` embedded by Product |
| `join: all` | explicit multi-input barrier and reducer |
| `join: any` | flow-specific typed activation inbox/current-trigger state and conditional edge |
| fanout | `Send` with typed child input |
| `min_matches` | graph-visible fanout asserts the selected count before returning `Send`; phase-internal fanout is proven inside the composite Attempt |
| exclusive route | route function computes all named matches; more than one is fatal, exactly one wins, and zero selects the declared `otherwise` target |
| gate expression DSL | pure typed Python predicate |
| input/output projection DSL | Feature-local `select` / `publish` functions |
| `max_activations` | explicit business loop/budget counters |
| retry declaration | Kernel technical retry or explicit graph business loop |
| interrupt action list | Pydantic decision model at a pure interrupt node |

The 9 current `join:any` sites are explicit migration items, not a fallback. Every replacement preserves the meaning of downstream `/tokens/0`: “the trigger that caused this activation,” not a predecessor-keyed aggregate with ambiguous ordering.

All 7 looped sites carry a business `epoch`/round plus a typed trigger containing predecessor, stable arrival sequence and value, and all 7 use a flow-specific pending-trigger inbox plus dispatch cursor so late arrivals can re-activate downstream work exactly once. This is local business state for that named flow, not a reusable token scheduler. Only the two nonloop Product assessment exits—`assess-satisfied` and `assess-unsatisfied`—may use one typed `AssessmentTrigger`, and only after tests prove outcome/predecessor mutual exclusion plus no late reactivation; either failed proof requires the same equivalent inbox/cursor implementation.

A Feature-owned `join:any` rewrite that fails any current-trigger, same-epoch late-arrival, replay, de-duplication or dispatch-cursor parity assertion is a migration-stop defect with zero waivers. This stops Feature acceptance, Product cutover, legacy drain and YAML/Runtime deletion; it does not terminate production Invocations, which remain on their immutable full `legacy-v2` runtime. A point exception, embedded legacy join, predecessor-map approximation, dropped late token or last-write-wins substitute is forbidden. Changing this behavior requires reopening this spec and the Runtime-deletion objective for explicit review.

Generation plan-review loops additionally store `round_budget`, current plan and current review result. Intake case-design/rework stores its current case result. Product execution failure, coverage and assessment paths use typed branch outcomes. Tests cover first arrival, late arrival, repeated loop epochs, stable trigger selection and downstream input identity for all 9 sites before legacy deletion.

Exactly one legacy `min_matches` site becomes a target LangGraph fanout: `assurance.generation.workflow.graph.generation/fanout` validates four matches and raises `InsufficientRouteMatches` before returning its four `Send` values. Exactly two sites—`assurance.intake.workflow.graph.case-design/prepare` and `/repair-prepare`, both with `min_matches: 2`—are eliminated as graph nodes and preserved only as fixed two-consumer dataflow inside `CompositeAttemptExecutor`; primary and repair tests prove both the prepared value and validated Agent result reach finalize. No second target `Send`, generic `min_matches` helper or phase-fanout compatibility shim is permitted. Exclusive routes retain their required declared fallback: zero named matches selects `otherwise`, exactly one selects that target, and more than one fails with ambiguity. They do not become Python `if/elif` chains that silently choose the first match.

Business round and activation counters remain the authoritative domain limits. `AssuranceApplication` also supplies a pinned, entrypoint-specific `recursion_limit` as a top-level LangGraph config key, sized above the longest valid execution. `GraphRecursionError` is normalized as a runtime failure and tested independently from business-budget exhaustion.

## 17. Resource-aware Parallelism

LangGraph owns readiness and parallel scheduling. `ResourceArbiter` owns whether an effectful Attempt may begin external work.

Each resolved Contract declares read/write/exclusive claims. Before OpenCode, filesystem mutation or effect execution starts, the Kernel obtains a durable authorization. Non-conflicting parallel Attempts run concurrently. Conflicting Attempts wait or return a typed pending resolution; the system does not encode resource scheduling as hidden graph edges.

The initial behavior preserves the Invocation-wide pending barrier. When one branch interrupts, already completed sibling tasks may finish and their task-level writes are durably persisted as pending writes. Those tasks are not rerun on resume, but their updates are not committed into the next graph-state snapshot until the interrupted superstep completes. No subsequent superstep begins while that interruption remains unresolved.

## 18. Effect Semantics

The six registered effect kinds are:

- `assurance.healing.effect.allocation.v2`;
- `assurance.healing.effect.heal-apply.v2`;
- `assurance.healing.effect.proposal-approved.v1`;
- `assurance.improvement.effect.archive.v1`;
- `assurance.improvement.effect.delivery.v1`;
- `assurance.improvement.effect.promotion.v1`.

`improvement-evaluate`, `improvement-export`, `improvement-apply` and `improvement-rollback` are graph names, not effect kinds. The first is still effectful: its `evaluate-memory-improvement` Attempt emits `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval` and settles that intent before publishing its committed receipt. This is distinct from the out-of-scope offline benchmark comparator called Eval.

Technical apply/reconcile states remain inside the Attempt Kernel transaction. An effect becomes a dedicated LangGraph node/subgraph only when it has independent business-visible routing, human approval, compensation or scheduling semantics. Discovery of an installed effect never makes it graph-reachable.

## 19. Interrupts and Status

Human interrupt nodes call `raw = interrupt(request.model_dump(mode="json"))`. `AssuranceApplication` later resumes the same thread with `Command(resume=resume_payload)`. After restart, the node validates `raw` into the typed action model and returns either a partial state update or `Command(update=..., goto=...)`. A node never returns `Command(resume=...)`. The current action sets—approve, reject, request rework and the allowed supersede variants—remain explicit in Python.

LangGraph restarts an interrupted node from its beginning. Therefore:

- human interrupt nodes are pure;
- effectful Attempt nodes are idempotent through stable Attempt keys;
- no non-idempotent side effect may execute before an interrupt outside the Kernel protocol.

Application status remains:

```text
running | blocked | interrupted | stopped | failed | completed
```

Only the compiled graph adapter and terminal nodes map state to this status. The journal cannot independently infer and advance a second Workflow state.

## 20. Persistence and Graph Revision Pinning

LangGraph checkpointers use `thread_id`; one Assurance Invocation maps to one `thread_id == invocation_id`. The initial checkpoint stores immutable revision, Product lock and input digests.

```python
@dataclass(frozen=True)
class GraphRevision:
    revision_id: str
    product_lock_digest: str
    wheel_source_digests: Mapping[str, str]
    factory_symbols: tuple[str, ...]
    state_schema_versions: Mapping[str, str]
    langgraph_version: str
    checkpoint_contract_version: str


@dataclass(frozen=True)
class GraphBuildManifest:
    revision: GraphRevision
    entrypoint_contract_digests: Mapping[str, str]
    attempt_contract_digests: Mapping[str, str]


@dataclass(frozen=True)
class BootArtifact:
    manifest: GraphBuildManifest
    entrypoints: Mapping[str, CompiledStateGraph]
    attempt_contracts: Mapping[str, ResolvedAttemptContract]
    checkpointer_backend_id: str
```

`revision_id` is a canonical digest of the authenticated Product lock, Product/Feature wheel source digests, complete fixed factory-symbol set, state-schema versions, checkpoint contract and pinned LangGraph version. It is not derived by serializing `CompiledStateGraph`.

Contract digests use a data-only canonical projection: owner/contract IDs, handler IDs, fully qualified model symbols plus JSON-Schema digests, resource claims/templates, retry/timeout policy and the ordered validator IDs. They never hash Python `repr`, callable identity or process memory addresses. Entrypoint contract digests cover the public name plus input/output/state schema identities; authenticated source digests pin the topology code itself.

`aa compile` authenticates, resolves and dry-compiles every root without a persistence backend, then emits only the serializable `GraphBuildManifest`. A running host reconstructs the same factories, verifies that manifest, and creates `BootArtifact` by compiling each Product root with the host checkpointer. Feature subgraphs use `checkpointer=None` and inherit that root saver. Thus a compiled graph is never serialized and a saver is never guessed during offline compile.

Attempt nodes close over immutable resolved contracts only. Per-invocation services—Kernel port, secret resolver, workspace provider, pinned revision and runner fencing token—arrive through a typed LangGraph runtime `context_schema`; they are not graph state and are never checkpointed. Boot rejects invocation if the runtime context revision differs from the compiled manifest.

LangGraph normally resumes a checkpoint using the currently deployed graph; the checkpoint does not contain prior topology. Assurance therefore requires a revision-pinned deployment artifact:

- `aa resume` verifies the installed `BootArtifact.manifest.revision.revision_id` before invoking the thread;
- a mismatch fails closed and reports the required revision;
- old deployment artifacts/containers remain available until their Invocations drain;
- initial implementation does not import two versions of one wheel into a process;
- a future state migration is explicit for one source/target revision pair.

The local, single-host CLI pins the separate `langgraph-checkpoint-sqlite` package and uses the Assurance anchoring adapter over `SqliteSaver` or `AsyncSqliteSaver` to match its invocation model. SQLite is not the multi-worker deployment backend. Tests use an equivalent anchored adapter over the official in-memory saver. A deployment host may use a durable LangGraph-supported checkpointer behind the same anchoring contract; changing the backend does not change the graph/Kernel interfaces. Checkpoint deserialization uses strict msgpack/module allowlisting, and graph state is limited to approved data-only types.

### Checkpoint integrity and journal anchoring

The checkpoint store lives in a runtime-owned control directory outside every Attempt workspace. It rejects symlinks/unsafe ownership and records canonical checkpoint bytes and pending task writes plus `thread_id`, checkpoint ID/parent, task identity, revision, Product lock and input digests. Its saver `put`/`put_writes` protocols durably write data and an anchor-outbox record in one store transaction, append the matching canonical digest/lineage through the Invocation journal port, mark that record anchored, and only then return control to LangGraph. A checkpoint or pending write is visible/resumable only when both sides match.

Recovery treats an unanchored store row as an outbox item to anchor, and a journal anchor whose store row is not yet marked as anchored as a mark-complete retry. Missing bytes, digest drift, lineage forks or metadata disagreement fail closed with `CheckpointIntegrityError`; the journal verifies a checkpoint but never chooses the next node. Invocation creation uses the same recoverable handshake, so the initial checkpoint and `InvocationStarted` identity cannot silently diverge.

### Single-runner lease and fencing

Before `start`, `run` or `resume`, `AssuranceApplication` obtains one Invocation-scoped runner lease and monotonically increasing fencing token. Local CLI uses a nonblocking OS file lock plus durable owner metadata; a multi-worker backend uses transactional compare-and-swap lease/heartbeat. The application places the token in the top-level LangGraph configurable metadata, the checkpointer requires it on every write, and the typed runtime context supplies it to Kernel Attempt adoption. The Kernel rechecks the Invocation fence before external dispatch, durable prepare, promotion, effect application and terminal receipt publication. A stale/expired owner may observe an in-flight activity finish, but only the current owner can adopt/reconcile and commit it; the stale owner cannot publish a checkpoint or start new work after a newer token exists. Concurrent `aa start`/`aa run`/`aa resume` therefore returns a typed runner-conflict result instead of advancing one thread twice.

## 21. Crash Recovery Matrix

| Crash point | Required recovery |
|---|---|
| before Kernel durable prepare | rerun same Attempt key |
| after durable prepare, before promotion | Kernel completes or rolls back promotion |
| after promotion, before LangGraph checkpoint | node restarts; Kernel returns existing committed receipt |
| during external Agent activity | reconcile/adopt activity using same Attempt key |
| during effect apply with unknown outcome | reconcile effect; never repeat workspace promotion |
| after system-interrupt issuance, before resume | replay the same generation and ordinal |
| after resume, before node checkpoint | replay the issued interrupt ordinal, then recover the same Attempt |
| after interrupt-bearing checkpoint, before issuance-observer delivery | redeliver the issuance anchor; keep the generation pending |
| after successful resumed-node checkpoint, before completion-observer delivery | redeliver the completion anchor; retire exactly the matching generation without rerunning the node |
| runner loses lease during a superstep | fence checkpoint/new dispatch; successor reconciles Attempts before resume |
| after LangGraph checkpoint | continue from checkpointed state |
| installed revision differs from pinned revision | refuse resume; require original deployment artifact |

Checkpoint writes and canonical workspace promotion need not share one database transaction. Idempotent Kernel recovery closes the crash window between them.

## 22. Existing Workflow Coverage

The accepted working-tree baseline is 64 graphs, 325 nodes, 367 edges, 124 conditional edges, 73 subgraph nodes, 12 joins (9 `any`, 3 `all`), 13 interrupts, 7 loop SCCs and a maximum nesting depth of 5. The preflight derives loop components from assembled `CompiledGraph.sccs`, requires exactly one `join:any` anchor per loop SCC, records full SCC membership for diagnostics, and compares this canonical sorted anchor tuple by exact equality rather than checking only the count:

```python
EXPECTED_LOOP_SCC_ANCHORS = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ("assurance.intake.workflow.graph.entry", "advance-join"),
    ("assurance.product.workflow.graph.product-execute", "coverage-needed"),
    ("assurance.product.workflow.graph.product-execute", "failed-join"),
)
```

These numbers include the uncommitted Generation completion and Intake composite-dataflow changes present when this design was accepted. They are characterization inventory, not a target Python node count; composite Attempts and direct rewrites deliberately reduce the implementation surface. Implementation starts only from an explicit integration-base commit containing that baseline, or after the inventory/spec/plan are deliberately regenerated for a different base.

All current topologies are representable directly:

- **Intake:** case design, review, repair and human decision loops become typed routes and explicit round state.
- **Generation:** fixed four-family subgraphs use `Send`, typed reducers and family-specific review/fix loops.
- **Execution:** one semantic Agent Attempt per execute/rerun job.
- **Quality:** assess, coverage state, report and repair routing use typed outcomes.
- **Healing:** proposal, approval, repair, rerun and budget loops remain business graph state.
- **Improvement:** review/apply/rollback/archive are Python graphs; technical effects remain Kernel details.
- **Product full/execute:** Product explicitly composes the above subgraphs and cross-Feature recovery routes.
- **Retro:** topology is representable, but current production input/output contracts must be repaired before cutover.
- **Offline benchmark Eval:** its export comparator remains a deterministic function; a multi-sample campaign may later receive a graph. It is not the effectful `improvement-evaluate` Product entrypoint.
- **Nightly:** current handlers are not a working Product graph/entrypoint and are not silently promoted during migration.

## 23. CLI Lifecycle

The Product continues to own `aa`:

- `aa compile` authenticates wheels, resolves contracts, dry-compiles all Python roots and emits the Product lock plus `GraphBuildManifest`; it does not serialize a compiled graph, choose an Invocation checkpointer or start OpenCode.
- `aa start` boots the manifest with the selected checkpointer/runtime ports, acquires the initial runner lease and creates the anchored initial LangGraph checkpoint plus Kernel Invocation journal identity.
- `aa run` boots the manifest with the local checkpointer/runtime context, acquires the Invocation runner lease and invokes the pinned graph until terminal or suspended.
- `aa resume` acquires the same lease, validates the typed human/system resume envelope and continues the same thread/revision. When multiple interrupts are pending it invokes LangGraph with `Command(resume={interrupt_id: validated_value, ...})`; a scalar resume value is accepted only when the checkpoint proves exactly one interrupt is pending.
- `aa status` reads normalized graph state plus receipt references.
- `aa export` and `aa archive` require the same authenticated terminal/evidence contracts as today.

The CLI has no option for graph module paths, Python import strings or Workflow YAML files.

## 24. Security Model

1. Only Product-declared installed wheel factory symbols are importable.
2. Source snapshots and module origins are verified before graph factory import.
3. No SUT directory is placed on an executable import path by graph loading.
4. `.aa/` continues to reject Attempt contracts, handlers, validators, effects, Python, callable, command, module and import authority.
5. Graph code changes require a new authenticated wheel/Product revision.
6. Attempt Contracts list validators explicitly; registry membership cannot infer binding.
7. Agent runtime bindings cannot change Feature-owned resources or validators.
8. LangGraph state and checkpoint documents contain no secrets.
9. OpenCode output schemas constrain response data, not filesystem authority.
10. Journal receipts authenticate canonical mutations independently of checkpoint internals.
11. Checkpoint serialization/deserialization accepts only approved data models and allowlisted modules.

## 25. Package Dependency Rules

- `graph-engine` depends on pinned LangGraph packages and remains independent of Product/Feature implementation.
- Each Feature wheel directly depends on LangGraph for its graph factory code and on `graph-engine` for Attempt/Boot contracts.
- Product depends on all six Feature wheels and is the only cross-Feature graph composer.
- A Feature may import another Feature's public domain contracts only where the existing import-linter contract allows it.
- A Feature may not import another Feature's `graphs`, handlers, validators, effects, plugins or resources.
- A Feature `graphs` package may not import runtime adapters or effectful handler implementations; effectful work enters through its owner-scoped Attempt context.
- No Feature imports a concrete OpenCode/Cursor adapter or Product module.

Import-linter rules are updated from forbidding cross-Feature `.workflow` imports to forbidding cross-Feature `.graphs` imports.

## 26. Explicit Deletion Target

The following production surfaces are deleted after cutover:

### Workflow declarations and resources

- six Capability `resources/workflow/module.yaml` files;
- Product `resources/workflow/main.yaml`;
- Workflow module resource IDs and media types in Capability plugins;
- Product `graph-inventory.yaml` as an execution allowlist;
- promised `.aa/workflow-schema.yaml` and `.aa/execution-contracts.yaml` loading.

### Workflow schema/compiler

- `graph_engine/graph/module_schema.py`;
- `graph_engine/composition/workflow_assembler.py`;
- legacy Workflow portions of `graph_engine/graph/schema.py`;
- `graph_engine/graph/compiler.py`;
- expression and input/output projection DSL modules once all callers are migrated;
- Product manifest fields for inline/resource/module Workflow forms and slot bindings.

### Agent phase/binding plumbing

- `expand_agent_job_slots` and all `AGENT_SLOT_PHASES` constants;
- Product prepare/execute/finalize alias expansion and alias-keyed runtime bindings;
- `bind_agent_execution_contracts`, slot ownership audits and phase-slot projection helpers;
- six Feature `contracts/workflow.py` module names after their surviving semantic catalogs move to `contracts/attempts.py`.

### Workflow Runtime

- activation planner;
- generic token and offered-token state;
- scheduler wave as Workflow progression;
- legacy subgraph execution loop;
- legacy Workflow checkpoint/projection authority;
- engine-level settle loop superseded by the Attempt effect protocol.

The following remain as refactored modules:

- source authentication and wheel registries;
- Product/Capability dependency closure;
- Product lock and source digests;
- Attempt workspace, activity and promotion recovery;
- validators and typed task contracts;
- effect adapters and receipts;
- append-only journal and evidence projection;
- CLI/application lifecycle.

PyYAML may remain for closed Product/configuration declarations; deleting Workflow YAML does not imply deleting all YAML support.

## 27. Migration Strategy

Migration is staged for review and rollback, but no production Invocation is advanced by two runtimes.

### Phase 0: Characterize external behavior

- Freeze public entrypoint inputs, outputs, terminal statuses, interrupt actions and durable receipt facts.
- Replace YAML-structure assertions with behavioral characterization where possible.
- Do not implement the obsolete whole-file Workflow replacement or `AgentLeafSpec` plan.

### Phase 1: LangGraph foundation

- Pin LangGraph/checkpointer dependencies.
- Add `GraphRevision`, `GraphBuildManifest`, runtime `BootArtifact`, `AssuranceApplication` and `AttemptResolution` contracts.
- Add checkpoint anchoring, the Invocation runner lease/fencing protocol, the in-memory graph harness and revision mismatch tests.

### Phase 2: Attempt Kernel

- Resolve 33 distinct Agent contracts and eight distinct direct Improvement task contracts alongside the legacy aliases; freeze four characterized pure direct functions; do not delete the 99 alias IDs while shadow/rollback still needs the old Runtime.
- Implement stable Attempt keys, resource arbitration, workspace commit/recovery, validators and effect settlement.
- Extend validator context with immutable sealed candidate bytes plus authenticated task input/output/evidence before approving any non-empty target binding.
- Implement the single `AttemptNodeFactory.attempt` interface.

### Phase 3: First real LangGraph tracer

- Implement the Execution Feature Python graph directly.
- Run it with scripted Kernel resolutions and then the real Kernel.
- Clone one test-only Execution contract that binds authenticated `assurance.execution.validator.evidence.v1`; prove accept/promote and reject/no-promote through the real LangGraph/Kernel path while all shipped contracts remain `validators=()`.
- Do not route it through `GraphDef`, the old planner or an `AgentLeafSpec`.

### Phase 4: Feature graph migration

- Implement Intake, Generation, Quality, Healing and Improvement Python graph bundles.
- Rewrite the five Feature-owned any-join cases—including epoch/current-trigger and late-arrival behavior—with zero semantic waivers; any parity failure hard-stops later cutover/drain/deletion phases while production stays on `legacy-v2`.
- Implement exactly one `min_matches` target `Send` at Generation `generation/fanout`; absorb exactly Intake `case-design/prepare` and `repair-prepare` into Composite internal two-consumer dataflow.
- Repair Retro and the effectful `improvement-evaluate` contract before their graphs are accepted; the latter settles `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval` inside the Attempt, while only offline benchmark Eval remains pure.

### Phase 5: Product entrypoints

- Implement the 14 Product root graph factories.
- Rewrite the four Product-owned any-join cases; use inboxes for the two looped sites and prove predecessor exclusion/no late reactivation for the two assessment sites or use the inbox fallback.
- Preserve public names and black-box behavior.
- Add resource-aware parallel and effect-pending tests.

### Phase 6: Shadow and cutover

- Drive old and new runtimes only with scripted/snapshotted inputs or separate shadow Invocations.
- Compare semantic decisions, Attempt calls, interrupts, receipts and terminals rather than internal IDs.
- Run the same authenticated test-only evidence Validator against identical accepted/rejected candidate sets on both runtimes; require exactly-once calls and identical promotion blocking without changing the production 25/0 binding inventory.
- Cut over a public entrypoint only when its new graph passes parity and crash tests.
- One Invocation records one runtime-specific authenticated build identity at start and never switches in place: legacy retains its `InvocationLock` v2 digest, while LangGraph records GraphRevision/ProductLock v3.

### Phase 7: Drain and delete

- Stop new legacy Invocations.
- Drain or explicitly terminate old Invocations.
- Extract and test the read-only legacy-v2 evidence reader, migrate generic examples/CLI/smoke/benchmark and every retained consumer, and relocate reusable activity/workspace/host primitives while compatibility wrappers still exist.
- After the authenticated zero-active gate and preparation commit are green, atomically switch `aa compile` from the coexistence bundle to ProductLock v3/`GraphBuildManifest` only, then delete Workflow YAML/module packaging, obsolete topology fixtures, the 99 Product phase aliases and their 102 phase-slot projections in that same commit.
- Delete the mutually dependent Workflow compiler/assembler/projection DSL and planner/token scheduler/custom Runtime in one atomic commit; no intermediate commit removes graph types still imported by Runtime.
- Update `AGENTS.md`, README, architecture docs and wheel smoke tests to the Python-native rule.

Temporary entrypoint cutover switches must be deleted in the final Phase 7 cleanup commit; they are migration controls, not a permanent runtime abstraction.

## 28. Test Strategy

### Feature factory tests

- Build each Feature graph from authenticated resolved contracts.
- Assert public graph exports, typed inputs/outputs and route outcomes.
- Test loops, budgets, fanout counts, ambiguity and interrupt action validation.
- Test the five Feature-owned any-join rewrites for current-trigger identity, late arrival and repeated activation epochs.
- Require all five rows green with zero waivers before Product graph work may begin.
- Test only through the Feature graph bundle interface, not private node tables.
- Use a spy owner-scoped build context to assert contract IDs and ensure graph code cannot inject handler/validator/runtime bindings.

### Workflow harness

Use compiled StateGraphs, the anchored in-memory checkpointer adapter and a scripted `AttemptNodeFactory`/Kernel. The harness records semantic node calls, input values, state updates, interrupts and terminals. It never invokes OpenCode or real promotion.

### Product graph tests

- Test the two looped Product any-join rewrites for current-trigger identity, same-epoch late arrival, replay and dispatch cursor behavior.
- Test both Product assessment exits for outcome/predecessor mutual exclusion and no late reactivation; if characterization fails, run the same inbox matrix.
- Together with Feature tests, account for all nine legacy any-join sites before deletion with zero xfails or semantic waivers, and close all pending rows in the exact seven-anchor SCC inventory.

### Kernel tests

- full staged write set and sealed bytes;
- validator order, rejection and exceptions;
- one test-only authenticated `assurance.execution.validator.evidence.v1` binding executes accepted/rejected candidate sets exactly once through both legacy and LangGraph paths without changing production binding counts;
- resource conflicts;
- OpenCode activity adopt/reconcile;
- prepare/promote crash windows;
- effect apply/reconcile crash windows;
- committed receipt replay;
- contract closure requires an explicit validator tuple on every effectful Agent/direct task contract, but does not require every registered validator to be bound.
- system-interrupt generation/ordinal replay, including crash after resume but before node checkpoint.

### Revision tests

- source or graph code change produces a new revision;
- same revision reproduces the same public graph contracts;
- resume rejects a mismatched revision;
- original deployment artifact resumes the thread;
- old revision retirement refuses while active Invocations remain;
- offline `GraphBuildManifest` and runtime-compiled `BootArtifact` must match exactly;
- Kernel/secrets/workspace/fencing runtime context is never present in checkpoint state.

### Persistence and concurrency tests

- initial checkpoint/`InvocationStarted` identity handshake;
- crash at each checkpoint store → anchor outbox → journal → anchored transition;
- checkpoint byte/digest/lineage/revision drift fails closed;
- two concurrent `start`/`run`/`resume` calls yield one fenced owner and one runner conflict;
- lease expiry/reclaim prevents the stale owner from checkpointing or dispatching new work.

### Product tests

- all 14 public entrypoints compile;
- Product has exactly one runtime binding for each of the 33 Agent contracts and no extras;
- black-box behavior matches the frozen characterization;
- no SUT graph/Python source is loadable;
- CLI compile emits one manifest, and start/run/resume/status reconstruct and enforce that same manifest/revision guard;
- SQLite restart resumes the same thread;
- full crash matrix passes;
- multiple simultaneous interrupts resume by interrupt-ID mapping; scalar resume is rejected when ambiguous.

### Repository gate

The final gate remains:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
```

## 29. Acceptance Criteria

1. No production Workflow topology is read from YAML.
2. No `workflow/module.yaml`, Product Workflow YAML or Workflow graph inventory ships in the final wheels.
3. All public entrypoints are built from Python StateGraph factories in authenticated installed wheels.
4. LangGraph is the only Workflow progression/checkpoint authority.
5. The custom planner, token scheduler and subgraph Runtime are deleted.
6. The 99 Product phase-alias IDs and 102 phase-slot occurrences are deleted; 34 Agent node occurrences resolve through 33 semantic Agent contracts.
7. Product supplies exactly 33 runtime bindings and cannot change Feature validators/resources.
8. Every effectful node uses the Attempt Kernel and stable Attempt key.
9. Crash after promotion but before checkpoint never repeats canonical mutation.
10. All 9 legacy any-join flows have specific typed Python rewrites, with no residual token scheduler and zero parity waivers; any Feature current-trigger mismatch blocks cutover, drain and deletion.
11. Exactly one `min_matches` site becomes a four-value Generation `Send`, exactly two Intake sites become Composite internal two-consumer dataflow, and no compatibility shim exists; exclusive ambiguity, loop budgets and interrupt action validation remain fail-closed.
12. A revision mismatch refuses resume instead of running the latest graph.
13. No SUT executable graph code is imported.
14. Retro is not cut over until its production contracts close.
15. Full repository and wheel smoke gates pass after legacy deletion.
16. Only journal-anchored checkpoints with matching identity and lineage are resumable.
17. One Invocation has at most one unfenced `start`/`run`/`resume` owner at a time.
18. Offline compile emits metadata only; runtime Boot binds the real checkpointer and non-checkpointed service context.

## 30. Consequences

### Benefits

- The graph visible in Python is the graph LangGraph executes.
- Nodes, routes and state are typed and locally testable.
- The project stops maintaining a general Workflow language and engine.
- Feature graph ownership remains modular without a dynamic plugin platform.
- Repeated prepare/execute/finalize plumbing disappears behind one deep Attempt interface.
- Project graph changes use the normal code review, test, package and deployment lifecycle.

### Costs

- Organization-level topology changes require a code release.
- Python graph revisions must be retained operationally while old Invocations exist.
- Assurance still owns an anchored checkpointer adapter and Invocation fencing; LangGraph does not supply the project's trust boundary.
- Existing YAML topology tests are rewritten rather than mechanically reused.
- Feature maintainers must understand LangGraph state/reducer/interrupt semantics.
- Direct rewrites require careful behavior characterization for complex loops.
- Enabling any of the 25 currently unbound validators is a separately reviewed behavior change.

These costs are intentional consequences of choosing Python `StateGraph` as the sole graph-definition authority.

## 31. Reference Facts

- LangGraph `StateGraph` nodes communicate through shared state and reducers and become executable after `compile()`.
- Compiled subgraphs can be used as parent nodes and inherit the parent checkpointer by default.
- Durable interrupt/resume requires a checkpointer and `thread_id`; an interrupted node restarts from its beginning.
- `Command` carries update/goto/resume semantics; `Send` provides dynamic dispatch.
- Checkpoints store graph state, not the historical Python topology; deployed graph compatibility and revision pinning remain application responsibilities.

Official references:

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph concurrent state update error](https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE)
- [LangGraph backward compatibility](https://docs.langchain.com/oss/python/langgraph/backward-compatibility)
