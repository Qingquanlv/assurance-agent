# Pure Graph Engine Phase 4: Assurance Capability Extraction Design

**Date:** 2026-08-22

**Status:** Approved design; implementation not started

**Depends on:** Phase 1 pure graph engine, Phase 2 registry platform and frozen composition, Phase 3 agent runtime adapters

**Feeds:** Phase 5 Assurance product assembly and behavioral comparison

## 1. Decision summary

Phase 4 extracts Assurance business behavior into six independently buildable Python wheels:

- `assurance-intake`;
- `assurance-generation`;
- `assurance-execution`;
- `assurance-healing`;
- `assurance-quality`; and
- `assurance-improvement`.

Each wheel is a vertical, deep module. It owns its business models, operations, skills, prompts, personas, schemas, validators, durable effects, resources, and public contract surface. A caller learns the existing Phase 2 `PluginProvider` interface, not a new Assurance-wide hook interface.

Phase 4 does **not** assemble `assurance-product`, define the final product graph or public entrypoints, switch `aa` to the new runtime, or delete the old runtime. Those actions remain in Phases 5 and 6.

The six wheels use only the five Phase 2 registries and the Phase 3 agent task-handler contract. Phase 4 adds no registry kind, agent/session concept, default graph, Assurance model, prompt field, or routing rule to `graph-engine`.

The current `ProductHooks` mechanism is not moved, wrapped, generalized, or renamed. Each behavior behind it becomes an ordinary task handler, commit validator, durable effect, resource, or private implementation detail of exactly one capability wheel. The old `ProductHooks` path remains frozen for the legacy runtime until Phase 6 and is never imported by the new wheels.

## 2. Context

The current Assurance product is structurally monolithic:

- `assurance_agent/workflow/driver/operations_catalog.py` constructs one product-wide operation table;
- `assurance_agent/artifacts` owns a product-wide artifact registry;
- `assurance_agent/_resources` contains all skills, personas, schemas, and workflow resources;
- domain implementations are spread across discovery, execution, healing, issues, metrics, report, retro, improvement, evaluation, evidence, and verification packages; and
- `assurance_kernel.workflow.core.product_hooks.ProductHooks` exposes a process-global collection of unrelated business callbacks.

This shape prevents independent installation, source authentication, dependency validation, and isolated testing of a capability. It also makes the old kernel know business extension points indirectly through a product-wide catch-all.

Phases 1 and 2 already provide the generic execution and composition seams:

- task handlers;
- commit validators;
- schemas;
- resources;
- durable effects; and
- capability bindings.

Phase 3 provides provider-neutral `AgentRunRequest` and `AgentRunResult` contracts plus OpenCode and Cursor adapters. Phase 4 must use those seams as they exist. It must not recreate the old product protocol beside them.

## 3. Goals

Phase 4 must:

1. create six independently buildable Assurance capability distributions;
2. assign every extracted operation, skill, business model, artifact schema, validator, effect, and resource to exactly one wheel;
3. expose each wheel only through a small `PluginProvider` interface and explicitly owned public contracts;
4. enforce an acyclic wheel dependency graph in packaging metadata and import contracts;
5. make prompt assembly, persona selection, business result interpretation, evidence policy, and capability-level model selection provider-neutral;
6. remove every need for `ProductHooks` from the new runtime path;
7. preserve Phase 2 source authentication, complete contribution authority, lock projection, and fail-closed behavior;
8. preserve Phase 3 recovery truthfulness and keep full provider session history outside the graph ledger;
9. produce per-wheel conformance and behavioral evidence without requiring a Phase 5 product graph; and
10. leave a precise, mechanically checked handoff for Phase 5 product assembly.

## 4. Non-goals

Phase 4 does not:

- create `assurance-product` or a `ProductManifest`;
- define the final full workflow, graph entrypoints, or product policy;
- choose which adapter distribution is enabled in production;
- choose organization endpoints, credential handles, or default model names;
- switch the `aa` CLI or current benchmark scripts to `graph-engine`;
- compare a complete old and new full workflow;
- resume old-runtime invocations in the new engine;
- delete `assurance-kernel`, `assurance-agent`, or their legacy business implementations;
- add forwarding imports, compatibility aliases, dual registration, or an old-to-new runtime bridge;
- add a seventh shared `assurance-contracts`, `assurance-models`, `assurance-skills`, or `assurance-common` wheel;
- expose executable Python or shell through YAML or SUT-owned configuration;
- copy OpenCode `SessionEvent` history into the graph ledger;
- add Cursor session persistence that Cursor does not provide; or
- change the Phase 2 or Phase 3 public interfaces without a separately approved specification amendment.

## 5. Considered extraction shapes

### 5.1 Chosen: six vertical capability wheels

Each wheel owns one coherent business slice from public contract through implementation and resources. Cross-wheel use goes through an upstream owner's narrow public contract surface.

This gives callers leverage: one plugin declaration provides the complete capability. It gives maintainers locality: a schema, its validator, its prompt, its handler, and its effects change together.

### 5.2 Rejected: horizontal technical-layer wheels

Packages such as `assurance-models`, `assurance-validators`, `assurance-skills`, and `assurance-effects` would make every business change span multiple distributions. They would recreate the monolith as a dependency hub and make ownership ambiguous.

### 5.3 Rejected: one temporary `assurance-capabilities` wheel

A single catch-all wheel would reproduce `ProductHooks` and the current global catalogs behind a new entry point. Splitting it later would require a second migration and would not prove that the plugin seam is real.

## 6. Terms and ownership rules

### 6.1 Capability wheel

A capability wheel is an installed Python distribution that publishes exactly one `graph_engine.plugins` entry point and one strict static plugin declaration. Its `PluginProvider` returns one `PluginDescriptor` and one complete `PluginContribution`.

### 6.2 Business contract

A business contract is a versioned, canonical, JSON-compatible shape owned by one wheel. Its public interface includes:

- the stable qualified schema ID;
- its canonical serialized form;
- validation rules;
- compatibility policy; and
- the owning wheel.

Python models may implement that contract, but cross-task and cross-wheel data is authoritative only in its canonical serialized form. No task relies on Python object identity across a wheel seam.

### 6.3 Contract owner

Every business contract has exactly one owner. Consumers may import only the owner's declared public `contracts` package or consume the registered schema/resource bytes. They may not import another wheel's implementation modules.

If two capabilities need the same concept, the earliest semantic producer owns it. A new shared wheel is not introduced merely to avoid a small dependency.

### 6.4 Capability operation

A capability operation is a Phase 2 task handler. It accepts a generic `TaskRequest`, validates canonical input, performs its domain behavior, and returns a typed `TaskOutcome`. It does not access a global catalog or resolve another plugin at runtime.

### 6.5 Agent-driven skill

An agent-driven skill is not an engine concept. In a capability wheel it is represented by:

- versioned instruction/persona/result-contract resources;
- a deterministic request-preparation handler;
- one product-selected adapter binding supplied in Phase 5; and
- a deterministic result-finalization handler plus any commit validators.

## 7. Repository and wheel shape

Phase 4 adds:

```text
packages/
  assurance-intake/
    pyproject.toml
    assurance_intake/
      __init__.py
      plugin.py
      contracts/
      operations/
      validators/
      effects/
      resources/
        plugin-declaration.json
        schemas/
        skills/
        personas/
        prompts/
  assurance-generation/
  assurance-execution/
  assurance-healing/
  assurance-quality/
  assurance-improvement/
```

Every wheel follows the same external shape. Internal folders may differ where the capability has no effect or no agent skill. Empty placeholder modules are forbidden.

Each distribution:

- is buildable as a wheel on its own with only its declared dependencies;
- owns a unique import package;
- exposes no product manifest;
- has no dependency on `assurance_agent` or `assurance_kernel`;
- has no import-time registration or process-global mutable catalog;
- loads no executable content from the SUT;
- publishes exact static and live declarations required by Phase 2; and
- includes all contributed resource and schema bytes in its authenticated wheel source.

## 8. Dependency graph

The only allowed Assurance wheel dependencies are:

```text
assurance-intake
    ↑
assurance-generation
    ↑
assurance-execution
    ↑
assurance-healing
    ↑
assurance-quality
    ↑
assurance-improvement
```

The diagram expresses ordering, not a requirement that every wheel import its immediate predecessor. The exact allowed direct edges are:

| Consumer | Allowed Assurance dependencies |
|---|---|
| `assurance-intake` | none |
| `assurance-generation` | `assurance-intake` |
| `assurance-execution` | `assurance-intake`, `assurance-generation` |
| `assurance-healing` | `assurance-intake`, `assurance-generation`, `assurance-execution` |
| `assurance-quality` | `assurance-intake`, `assurance-generation`, `assurance-execution`, `assurance-healing` |
| `assurance-improvement` | all five upstream wheels |

Direct imports are allowed only from an upstream wheel's public `contracts` surface. Importing an upstream `operations`, `validators`, `effects`, `plugin`, or private module is forbidden.

The dependency graph must be identical in:

- wheel metadata;
- `PluginDescriptor.dependencies`;
- static declaration JSON;
- import-linter contracts; and
- the Phase 4 ownership ledger.

Any disagreement fails packaging or composition. Dependency cycles are rejected before provider import by the Phase 2 registry platform.

## 9. Capability ownership

### 9.1 `assurance-intake`

Owns change intake, exploration, case authoring, case review, and their canonical artifacts.

Initial skills include:

- `aa-intake`;
- `aa-explore`;
- `aa-case-design`; and
- `aa-case-reviewer`.

It owns change/exploration/case/review schemas, case identity rules, minimum relevant context, case candidate validation, and any deterministic operation required to finalize intake artifacts.

### 9.2 `assurance-generation`

Owns test planning, plan review, initial test code generation, and generated-test correction for API, E2E, fuzz, and performance families.

Initial skills include:

- `aa-api-plan`, `aa-api-plan-reviewer`, `aa-api-codegen`, `aa-api-codegen-fixer`;
- `aa-e2e-plan`, `aa-e2e-plan-reviewer`, `aa-e2e-codegen`, `aa-e2e-codegen-fixer`;
- `aa-fuzz-plan`, `aa-fuzz-plan-reviewer`, `aa-fuzz-codegen`; and
- `aa-performance-plan`, `aa-performance-plan-reviewer`, `aa-performance-codegen`.

It owns plan, review, generated-file manifest, capability mapping, codegen result, and generated-test candidate schemas and validators. A codegen fixer belongs here because it corrects generation output. Product repair diagnosis and authorization belong to healing.

### 9.3 `assurance-execution`

Owns test selection, closed mapping, execution requests, test command execution, raw execution evidence, and execution result normalization.

Initial skills include:

- `aa-execute`; and
- `aa-run` when it is used as an execution skill rather than as the product CLI.

It owns execution-plan, selected-test, closed-mapping, raw-result, process-receipt, and normalized execution-evidence contracts. It does not own coverage interpretation, issue analysis, or provider session history.

### 9.4 `assurance-healing`

Owns diagnosis, fix proposal, repair authorization, safety policy, healing durable effects, repair application, reconciliation, and repair status.

Initial skills include:

- `aa-fix-proposal`; and
- `aa-coverage-repair` for the repair action only.

Coverage-gap detection remains in quality. Healing consumes the quality-owned gap contract and decides or applies a repair under explicit policy.

Healing owns allocation, approval, override-token, proposal, applied-repair, reconciliation-receipt, and healing-status contracts. Every external mutation is a registered Phase 2 durable effect with an exact intent schema, receipt schema, idempotency rule, and reconciliation implementation.

### 9.5 `assurance-quality`

Owns fact baseline, traceability, coverage analysis, issue analysis, metrics, inspection, reporting, and quality projections.

Initial skills include:

- `aa-fact-baseline`;
- `aa-inspect`;
- `aa-issue-analyzer`;
- `aa-issue-triage-advisor`;
- `aa-report-generator`; and
- `aa-dashboard` where it produces a report resource rather than a UI implementation.

It owns trace, coverage, issue, metric, inspection, report, gate-evidence, and quality-summary contracts. It may observe healing artifacts but must not invoke healing implementation modules.

### 9.6 `assurance-improvement`

Owns retro analysis, improvement review, improvement evaluation, approved delivery, archival projections, and their effects.

Initial skills include:

- `aa-retro`;
- `aa-retro-eval-analysis`;
- `aa-retro-issue-analysis`;
- `aa-retro-workflow-analysis`;
- `aa-improvement-reviewer`; and
- `aa-archive`.

It owns retro signal/candidate, improvement proposal/review/evaluation/delivery, promotion/archive, and improvement-effect contracts.

### 9.7 Product and development resources left for Phase 5

`aa-workflow` describes the whole product and is not owned by a Phase 4 wheel. The final full workflow schema, public graph entrypoints, product-wide execution contract, and CLI help belong to `assurance-product` in Phase 5.

`writing-skills` is a development aid rather than a runtime Assurance capability. It is not registered by a Phase 4 wheel. Phase 5 may package it as documentation, but it must not become executable product configuration.

### 9.8 Evaluation harness

Business evidence projection needed by quality or improvement moves to its semantic owner. Benchmark datasets, scorers, comparison harnesses, and release-evaluation orchestration remain outside the six runtime wheels. Phase 5 consumes them as an external behavioral-comparison harness.

## 10. Machine-checked ownership ledger

Phase 4 maintains a non-runtime ownership ledger under its implementation evidence directory. It maps every migrated item from the old product to exactly one new owner:

- Python module or callable;
- operation ID;
- skill ID;
- persona ID;
- schema ID;
- artifact type;
- commit-validator ID;
- durable-effect kind;
- resource ID; and
- former `ProductHooks` behavior.

The ledger records the legacy source path, new wheel, new qualified ID, migration status, and verification test. CI rejects:

- an old item with no declared owner;
- one item assigned to multiple owners;
- duplicate qualified IDs;
- a contributed ID absent from the owning descriptor;
- a descriptor ID absent from the live contribution; or
- a new wheel importing the legacy implementation.

The ledger is evidence for extraction and deletion planning. It is not loaded at runtime.

## 11. Public interfaces and qualified IDs

### 11.1 Plugin interface

The only wheel-level runtime interface is the Phase 2 interface:

```python
class PluginProvider(Protocol):
    def descriptor(self) -> PluginDescriptor: ...
    def contribute(self, ports: RegistryPorts) -> PluginContribution: ...
```

No Assurance base plugin class is added. No product-wide registry builder is exposed.

### 11.2 ID namespaces

Every public ID is versioned or has a versioned referenced schema/resource. Initial namespaces are:

```text
assurance.intake.*
assurance.generation.*
assurance.execution.*
assurance.healing.*
assurance.quality.*
assurance.improvement.*
```

An ID has one owner for the lifetime of a major contract version. Re-exporting or aliasing another wheel's ID is forbidden.

Compatibility aliases from the old operation catalog are not contributed. Phase 5 graphs must use canonical new IDs.

### 11.3 Public Python contract surface

Cross-wheel Python imports are limited to:

```text
assurance_<domain>.contracts
assurance_<domain>.contracts.<focused_module>
```

The public contract surface contains frozen data models, canonical encoders/decoders, typed validation errors, and ID constants. It contains no handler, filesystem access, policy lookup, adapter call, or registration side effect.

## 12. Registry contribution rules

Each wheel contributes only through the existing Phase 2 registries.

| Business concern | Phase 2 contribution |
|---|---|
| Deterministic operation | task handler |
| Agent request preparation | task handler |
| Agent result finalization | task handler |
| Candidate-tree enforcement | commit validator |
| Artifact shape | schema |
| Prompt/persona/policy template | resource |
| External idempotent mutation | durable effect |
| Logical capability to selected runtime | capability binding supplied by Phase 5 composition |

No business behavior is hidden in import side effects, a global dictionary, or a generic `object` callback field.

All six categories in a `PluginContribution` must exactly match the static and live descriptor. Phase 2 complete-contribution authority and frozen composition validation remain authoritative.

## 13. Agent-driven capability flow

### 13.1 Three-stage pattern

Every agent-driven capability follows this logical pattern:

```text
canonical business input
  -> capability prepare handler
  -> frozen AgentRunRequest
  -> product-selected Phase 3 adapter binding
  -> frozen AgentRunResult
  -> capability finalize handler / commit validators
  -> canonical business artifact and evidence
```

Phase 4 publishes the prepare/finalize handlers and all referenced resources and schemas. Phase 5 supplies the graph edges and the logical-to-adapter bindings.

This pattern is logical, not an engine macro. Phase 4 adds no composite-node feature to the engine.

### 13.2 Request preparation

The prepare handler:

- validates its input against the owner schema;
- reads only registered, digest-authenticated resources named in its request;
- assembles ordered `InstructionPart` values deterministically;
- selects one persona from the wheel's closed persona resources;
- chooses one exact model from Phase 5 locked binding/configuration data;
- constructs one exact `ResultContract`;
- computes request policy and configuration digests; and
- returns a complete `AgentRunRequest` as canonical JSON.

It must not call OpenCode, Cursor, a model provider, or a secret resolver.

### 13.3 Adapter execution

The adapter receives only the complete frozen request. It may transform transport syntax as specified in Phase 3, but it does not infer skill meaning, add Assurance instructions, choose a model, choose a persona, or fall back to another provider.

### 13.4 Result finalization

The finalize handler:

- authenticates the `AgentRunResult` structure and digests;
- validates the business result against the capability-owned schema;
- validates referenced workspace artifacts and closed mappings;
- produces canonical business output and evidence references;
- emits only owner-declared durable-effect intents; and
- rejects extra, missing, or semantically inconsistent output.

Provider diagnostics may support bounded error reporting but are not business truth.

### 13.5 Model-routing ownership

Phase 4 owns the business decision that a capability needs a role, quality tier, tool profile, or reasoning level. Phase 5 supplies the locked organization mapping from that intent to one exact provider/model selection and adapter binding.

The prepare handler performs a closed lookup and writes one exact `provider_model` into `AgentRunRequest`. Absence, ambiguity, a fallback list, or a route to an unselected adapter fails before dispatch.

No common model router wheel or engine hook is introduced.

### 13.6 Session ownership

OpenCode remains authoritative for its full session messages, reasoning, tool records, token data, and UI projection. Cursor remains limited to the durable facts guaranteed by its process protocol.

Capability wheels persist only:

- canonical business artifacts;
- `AgentRunResult`-derived result and evidence digests;
- provider-independent task output; and
- effect intents/receipts required by the business workflow.

They do not define `SessionEvent`, copy provider conversation history, or use provider history for post-success replay.

## 14. Prompt, persona, and skill resources

Prompts, personas, result schemas, and skill instructions are immutable registered resources in the owning wheel. Their IDs and bytes are part of the wheel's authenticated source and the frozen composition.

Requirements:

- instruction ordering is explicit and canonical;
- templates have a strict placeholder schema;
- every placeholder value comes from canonical task input or locked configuration;
- unknown placeholders fail closed;
- no template executes Python, shell, Jinja extensions, or SUT code;
- no ambient filesystem search chooses a prompt or persona;
- no resource lookup falls back to a same-named file in the SUT;
- secrets are never interpolated into durable instruction resources; and
- the final request digest changes when any instruction, persona, result contract, routing input, or locked policy changes.

Phase 5 may select an explicitly declared configuration source. Such a source is authenticated and frozen by Phase 2. It is not an ambient project plugin and contains no executable behavior.

## 15. Business models and artifact schemas

### 15.1 Canonical form

All task inputs, outputs, effect payloads, and cross-wheel artifacts are JSON-compatible and have canonical serialization. YAML may be an authoring representation, but canonical JSON bytes and schema IDs define runtime identity.

### 15.2 Single ownership

The producing capability owns the artifact contract. Consumers validate the exact owner schema and do not create structurally similar private copies.

Examples:

- intake owns a reviewed case;
- generation owns a reviewed execution plan and generated-test manifest;
- execution owns normalized test evidence;
- healing owns a repair proposal and healing receipt;
- quality owns a coverage gap, issue candidate, metric, and report;
- improvement owns a retro candidate and improvement decision.

### 15.3 Version changes

A breaking artifact change receives a new schema ID. Silent widening, permissive extra fields, and prefix-only capability-key validation are forbidden. References to capability keys, schema IDs, artifact IDs, and mapping IDs must resolve against the exact frozen registry or catalog owned by the relevant wheel.

### 15.4 Filesystem artifacts

Filesystem locations are data in the artifact contract, not Python import paths. Validators require canonical relative paths, reject traversal and symlink escapes, and authenticate bytes before accepting a candidate tree.

## 16. Commit validators

Each commit validator is owned by the wheel that owns the invariant it enforces.

A validator:

- consumes `CandidateWriteSet` and `ValidationContext` only;
- uses declared resource claims;
- reads only the exact candidate workspace through engine-provided paths;
- returns a typed `ValidationResult`;
- has no global product access;
- does not dispatch an agent or durable effect;
- rejects unknown schema IDs and unresolved business references; and
- is deterministic for the same candidate bytes, context, and frozen resources.

Product-wide validator name tables disappear from the new path. The complete set is derived from selected plugin contributions and frozen in the invocation lock.

## 17. Durable effects

Durable effects are used only for business mutations that cannot be represented by publishing the candidate workspace.

Healing owns repair authorization/application effects. Improvement owns approved delivery, promotion, archival, or rollback effects when those actions affect external durable state. Execution owns an effect only if a test execution has an external durable mutation that cannot be represented as a recoverable task activity.

Every effect kind has:

- one owner;
- an exact intent schema;
- an exact receipt schema;
- a deterministic idempotency-key algorithm;
- a declared policy;
- apply and reconcile behavior;
- typed absent/applied/indeterminate outcomes; and
- crash-cut tests around intent, external mutation, receipt, and ledger commit.

No generic `register_healing_effects()` callback remains in the new path.

## 18. Replacing `ProductHooks`

The old hook behaviors map as follows:

| Legacy hook concern | Phase 4 owner and seam |
|---|---|
| Product code roots and test-tree safety | healing policy resource plus healing commit validator |
| Candidate document digest | quality-owned canonical contract function/private implementation |
| Healing allocation commit | healing durable effect |
| Issue analyzer output completion | quality finalize handler |
| Improvement reviewer output completion | improvement finalize handler |
| Retro signal/candidate completion | improvement finalize handlers |
| Healing effect registration | healing `PluginContribution.effects` |
| Healing episode projection | healing deterministic task handler |
| Test-change override policy/token | healing schema, resource, and validator |
| Healing allocation/proposal/record reconciliation | healing durable-effect reconciliation |
| `semantic_pins` | removed; Phase 2 source snapshots and contribution authority pin implementation identity |

Rules:

- no new capability wheel imports `ProductHooks`, `current_product_hooks`, or the legacy product module;
- no Phase 4 package installs process-global hooks;
- no catch-all replacement dataclass or callback map is introduced;
- each migrated behavior has a public registry contribution or remains private to one such contribution; and
- parity tests call the legacy and new behavior separately on fixed fixtures; the implementations do not call each other.

## 19. Legacy coexistence during Phase 4

The current `aa` runtime remains available while Phase 4 is implemented. Coexistence is deliberately asymmetric:

- legacy packages do not import new capability wheels;
- new capability wheels do not import legacy packages;
- no forwarding module preserves an old import path;
- no handler registers into both runtimes;
- no invocation moves between runtimes; and
- no persisted legacy state is read by the new engine.

Phase 4 may temporarily duplicate business implementation while the new wheel becomes authoritative for the new path. This is bounded migration duplication, not a compatibility seam. The ownership ledger identifies the duplicate legacy source scheduled for Phase 6 deletion.

New behavior changes after extraction are made in the new owner only. If legacy parity must be retained before cutover, the same behavioral change is made independently and tested independently; no shared implementation dependency is introduced.

## 20. Failure semantics

### 20.1 Composition failures

Missing dependencies, dependency cycles, duplicate IDs, declaration/contribution mismatch, source drift, unknown schemas/resources, and invalid bindings fail during Phase 2 resolution before an invocation starts.

### 20.2 Deterministic task failures

Malformed business input returns `invalid_input`. A structurally valid but semantically invalid agent or operation output returns `invalid_output`. Internal invariant violations return `internal`. Retryability is explicit in the returned `TaskFailure` and is constrained by the Phase 5 graph policy.

### 20.3 Routing failures

Missing model configuration, multiple matches, an unsupported persona, an unknown adapter capability, or a fallback expression fails before provider dispatch. No default adapter or model is selected.

### 20.4 Agent ambiguity

Phase 3 `indeterminate` activity state remains indeterminate. A capability finalize handler is not run until an authenticated terminal `AgentRunResult` exists. A capability wheel cannot translate unknown provider state into absence, failure, or a new attempt.

### 20.5 Effect ambiguity

An ambiguous durable effect blocks ordinary progression until the owner effect reconciles it or explicit operator action resolves it. It never consumes a normal task retry.

### 20.6 Business STOP

A genuine business stop is an explicit `TaskOutcome.stopped()` with a stable, owner-qualified reason code and canonical supporting output. Exceptions, invalid output, missing files, adapter ambiguity, and validation failure are not converted to STOP.

## 21. Security and trust model

Phase 4 inherits the Phase 2 and Phase 3 trust model and adds these constraints:

- only explicit installed wheel/config sources contribute capability code or resources;
- the SUT never supplies executable plugin code;
- no capability scans `sys.path`, arbitrary entry points, or ambient project directories;
- no prompt, persona, schema, or policy is selected by an unpinned path;
- all callable authority remains tied to the authenticated contribution generation;
- all workspace reads and writes stay inside exact engine-provided attempt roots and resource claims;
- credentials remain available only through the Phase 3 ephemeral secret port;
- credential values never enter business artifacts, prompts persisted as evidence, locks, ledgers, or diagnostics;
- business output that can trigger a durable effect is schema- and policy-validated before intent creation; and
- external provider output never gains executable authority.

## 22. Testing strategy

### 22.1 Ownership and import tests

Tests prove:

- every migrated item has exactly one ledger owner;
- every contributed ID is owner-qualified and unique;
- the wheel dependency graph matches the approved DAG;
- only public upstream contract modules are imported;
- `graph-engine` and agent adapters import no Assurance wheel;
- no new wheel imports `assurance_agent` or `assurance_kernel`; and
- no new wheel imports another wheel's implementation modules.

### 22.2 Per-wheel contract tests

Each wheel has public-interface tests for:

- static declaration and live descriptor equality;
- complete contribution authority;
- canonical schemas and resources;
- task input/output validation;
- commit validator accept/reject behavior;
- effect apply/reconcile/idempotency behavior where present;
- exact error kinds; and
- source/resource/config drift.

Tests use the same public plugin interface as Phase 5. They do not construct private registries or bypass `RegistryPlatform`.

### 22.3 Agent-skill conformance

For every agent-driven skill family, tests prove:

- deterministic request bytes from the same canonical input and locked configuration;
- prompt/persona/result-contract/resource drift changes the request digest;
- exactly one model and one adapter target are selected;
- OpenCode and Cursor bindings receive the same canonical `AgentRunRequest` for the same policy choice;
- provider-specific output is reduced to the same `AgentRunResult` contract;
- business finalization is independent of provider session retention; and
- malformed or semantically false structured output is rejected.

Offline fakes are authoritative for deterministic conformance. Phase 3 provider-live smoke need not be repeated for every skill.

### 22.4 Legacy characterization parity

For each migrated behavior, fixed fixtures compare legacy and new paths on externally meaningful facts:

- terminal business status;
- canonical business artifact bytes;
- validation/gate decisions;
- durable-effect intent and idempotency key;
- evidence digest inputs; and
- stable business reason codes.

Internal class names, call order, event IDs, prompt whitespace that is intentionally versioned, and private diagnostic text need not match.

Parity tests are temporary migration evidence. They do not make legacy types part of the new public interface.

### 22.5 Wheel isolation

Each wheel is built from committed source and installed into a clean environment with only:

- `graph-engine`;
- `agent-runtime-contracts` when required;
- its declared upstream Assurance wheels; and
- test-only conformance fixtures.

The test imports the entry point, authenticates the declaration and source, resolves the plugin, freezes composition, and exercises at least one task/validator/effect path. It must not rely on the monorepo source checkout or legacy packages.

### 22.6 Cross-wheel integration without a product

A test-only neutral product selects the six wheels and a fake agent adapter. It proves registry closure, dependency order, artifact handoff, and logical adapter rebinding. It is not shipped as the Assurance product and exposes no production entrypoint.

### 22.7 Repository gates

Phase 4 completion requires:

```text
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run pytest
bash scripts/packaging_smoke_test.sh
```

It also requires committed-HEAD offline wheel isolation for all six capability wheels and the Phase 3 adapters used by conformance.

## 23. Acceptance criteria

Phase 4 is complete only when all of the following are true:

1. all six distributions build and install independently with their declared dependencies;
2. each exposes one authenticated `graph_engine.plugins` provider and no product provider;
3. their dependency graph is acyclic and exactly matches Section 8;
4. the ownership ledger covers every in-scope old operation, skill, model, artifact, validator, effect, resource, and hook exactly once;
5. each wheel owns complete vertical behavior and imports only upstream public contracts;
6. no new wheel imports `assurance_agent`, `assurance_kernel`, or `ProductHooks`;
7. no new Assurance-wide hook, model, registry, or common wheel exists;
8. every old hook behavior has a typed new owner and parity evidence;
9. agent-driven capabilities produce complete frozen `AgentRunRequest` values and consume typed `AgentRunResult` values without provider-specific imports;
10. prompt, persona, result-schema, routing-input, and policy changes affect locked/canonical identity;
11. adapter/model selection is exact and has no fallback;
12. every artifact reference and capability key resolves against an exact frozen owner catalog, not a prefix check;
13. every durable effect has crash/reconciliation/idempotency evidence;
14. OpenCode session history remains OpenCode-owned and Cursor ambiguity remains explicit;
15. the legacy and new runtimes share no implementation imports or persisted state;
16. the test-only six-wheel composition resolves and executes representative capability paths;
17. committed-HEAD wheel isolation and the complete repository gate pass; and
18. `aa` still uses the legacy product by default, proving that Phase 5/6 scope did not leak into Phase 4.

## 24. Phase 5 handoff

Phase 5 receives:

- six authenticated capability wheels;
- their exact plugin descriptors and dependency constraints;
- complete registry contributions;
- canonical public contracts and resource IDs;
- test-only composition and parity evidence;
- the ownership/deletion ledger; and
- logical business capabilities that can be bound to Phase 3 adapters.

Phase 5 then creates `assurance-product` and owns:

- the explicit `ProductManifest`;
- selected plugin versions;
- product-level graphs and public entrypoints;
- product-wide graph input/output wiring;
- logical capability-to-adapter bindings;
- endpoint, secret-handle, permission, and exact model configuration;
- organization policy/config sources;
- the parallel new-runtime CLI entry; and
- complete old/new workflow behavioral comparison.

Phase 5 must not move business implementation back into the product wheel.

## 25. Phase 6 handoff

After Phase 5 proves the new product, Phase 6:

- drains or explicitly terminates old-runtime invocations;
- switches `aa` to the new `assurance-product`;
- deletes the old graph runtime and duplicate legacy business implementations in the same hard cut;
- removes `ProductHooks`, legacy catalogs, old product entry points, and obsolete resources; and
- adds no compatibility imports or old-invocation resume adapter.

Phase 4 therefore optimizes for a clean deletion in Phase 6, not for minimizing temporary line count.

## 26. Implementation-plan boundaries

The Phase 4 implementation plan must use independently reviewable tracer tasks in this order:

1. create the ownership ledger, canonical ID policy, wheel DAG, and import contracts;
2. add shared test-only plugin conformance helpers without a shared production Assurance wheel;
3. extract and verify `assurance-intake`;
4. extract and verify `assurance-generation`;
5. extract and verify `assurance-execution`;
6. extract and verify `assurance-healing`, including all former healing hooks and durable effects;
7. extract and verify `assurance-quality`, including issue, coverage, metrics, inspect, report, and evidence projections;
8. extract and verify `assurance-improvement`, including retro, delivery, archive, and their effects;
9. close cross-wheel artifact, capability-key, request/finalize, and dependency conformance;
10. close every former `ProductHooks` mapping with parity evidence;
11. build/install all six wheels in clean committed-HEAD environments;
12. run the test-only six-wheel composition and both adapter rebindings;
13. complete import/security/fault audits and the full repository gate; and
14. publish the Phase 5 handoff and Phase 6 deletion inventory.

Each tracer starts with a failing public-interface test, changes one wheel or one cross-wheel contract seam, and ends with its wheel-isolation gate green. Later wheels may depend on completed upstream wheels; parallel work must never edit the same contract owner.

The plan must not include a product manifest, final workflow graph, CLI cutover, legacy deletion, or compatibility shim.

## 27. Design closure

This specification fixes the Phase 4 seams:

- six vertical wheels, not horizontal technical layers;
- one owner for every business contract and executable contribution;
- one acyclic dependency direction;
- only Phase 2 registries at the plugin seam;
- only Phase 3 frozen requests/results at the provider seam;
- prepare/adapter/finalize as the agent-driven capability flow;
- prompts, personas, semantic validation, evidence, and capability-level routing in capability wheels;
- organization model/adapter configuration and graph assembly in Phase 5;
- no provider session duplication;
- no `ProductHooks` successor;
- no old/new implementation imports or resume bridge; and
- temporary duplication accepted only to enable a safe Phase 6 hard deletion.

No implementation decision required for Phase 4 is left open. Any proposal to add a registry kind, a shared Assurance runtime wheel, a generic product hook, executable SUT configuration, provider-specific capability logic, a product manifest, or a legacy bridge requires a new design review rather than an implementation-plan deviation.
