# Permanent Raw Agent Runtime Cutover

**Status:** Accepted design amendment; amended 2026-09-04

**Date:** 2026-09-02

**Amends:** [Python-native LangGraph Assurance Runtime](./2026-08-31-python-native-langgraph-assurance-design.md)

**Supersedes:** [Structured Artifact Pipeline](./2026-09-01-structured-artifact-pipeline-design.md)

**Scope:** OpenCode Agent-result transport, semantic Agent execution, `AssuranceAttemptKernel`, Product cutover, legacy Workflow deletion, recovery, and ordinary repository CI

**2026-09-04 amendment:** [Checkpoint R Removal Design](./2026-09-04-checkpoint-r-removal-design.md)
removes the former protected live-provider certification and every dependency on it. This document
remains authoritative for Raw Agent runtime semantics, not external-provider release proof.

## 1. Executive Summary

The Product permanently adopts **Raw Agent execution with strict local validation**. The Structured Artifact Pipeline is cancelled rather than deferred.

Every semantic Agent Attempt remains one reliable transaction:

```text
validated semantic input
→ prepare
→ one recoverable OpenCode root session
→ OpenCode writes authorized raw workspace files
→ exact assistant JSON result
→ adapter JSON-Schema validation
→ Feature-owned Pydantic AgentResult validation
→ deterministic, read-only finalization over prepared data and raw files
→ Output model validation
→ seal
→ ordered validators
→ durable prepare
→ promote/recover
→ six-effect apply/reconcile
→ terminal Attempt receipt
```

The Agent-result JSON Schema is generated from installed contract code and appended to the runtime prompt. It is not stored as the authoritative copy in a skill, supplied by the project, or sent through OpenCode's `format.type == "json_schema"` protocol. OpenCode does not advertise or need `opencode_structured_output`.

This design does not add a LangGraph node or edge. LangGraph remains the sole Workflow-control authority. `AssuranceAttemptKernel.execute_or_recover(...)` remains the sole transaction entrypoint, and `AttemptNodeFactory.attempt(...)` remains the sole effectful graph-node seam.

The existing four Product T5a roots remain accepted. The ten Agent-dependent roots may cut over
after production runtime closure and their focused deterministic and ordinary repository tests pass.

## 2. Normative Decision and Supersession

The following proposed features are permanently outside the target architecture:

- OpenCode `format.type == "json_schema"` admission;
- terminal assistant `info.structured` as the Product result channel;
- `opencode_structured_output` capability negotiation or certification;
- provider-side or provider-native response-format enforcement;
- `ArtifactContract`, `ArtifactSlot`, typed-path ownership, and artifact projectors;
- Kernel canonical JSON/YAML materialization;
- materialization manifests and materialization receipts;
- a registry of typed document codecs or serializers;
- typed-only, raw-only, and hybrid artifact classifications;
- migration of Capability finalizers to typed materialization receipts;
- Structured Artifact Tasks A1–A10, Capability Tasks C1–C12, and OpenCode Structured Tasks O1–O7 as active work.

The cancelled Structured design and implementation plans remain in the repository as historical decision records. Each receives an explicit `CANCELLED / SUPERSEDED` banner and is removed from every active dependency graph, completion gate, CI claim, and release requirement.

The completed OpenCode Structured eligibility probe is historical research evidence only. It cannot
block Raw Agent implementation, advertise a runtime capability, or remain a required CI check.
Product code dedicated only to the cancelled capability is removed after its research record is
preserved.

## 3. Relationship to the Existing Python-native Design

The following accepted behavior is unchanged:

- Python `StateGraph` factories own nodes, edges, routes, loops, subgraphs, and interrupts;
- Product explicitly composes the six installed Feature bundles into 14 public roots;
- an Invocation is permanently pinned to its runtime kind, ProductLock, GraphRevision, checkpoint, journal, and deployment artifact;
- stable `AttemptKey`, isolated workspace, resource authorization, runner lease, and monotonic fencing remain mandatory;
- full staged-write sealing and ordered validators remain mandatory;
- durable prepare and atomic promote/recover remain mandatory;
- all six registered technical effects retain their apply/reconcile semantics inside the Attempt transaction;
- system interrupts, publication-indeterminate outcomes, receipts, and crash recovery remain mandatory;
- legacy Workflow deletion still requires 14/14 new-start cutover and authenticated zero-active legacy proof;
- project `.aa/` data and the SUT cannot load graph factories, handlers, validators,
  Agent-result schemas, runtime bindings, or executable Python. The existing legacy-only whole-file
  Workflow/execution-contract replacements remain available only to legacy Invocations until Product
  T8 removes that replacement surface with the Workflow compiler.

This amendment replaces only the Agent production path. It does not reopen completed Foundation
Tasks 1–10, Semantic Attempt Tasks 1–10, Feature Tasks 1–9, Product Tasks 1–4, or Product T5a.

## 4. Current Baseline and Required Closure

The accepted baseline contains:

- 33 distinct Agent contracts at 34 semantic StateGraph occurrences;
- six Feature factory bundles and 19 Feature graph exports;
- 14 Product roots;
- four T5a non-Agent roots already selected as `langgraph-v1` for new starts;
- ten Agent-dependent roots still selected as `legacy-v2`;
- a provider-neutral `CompositeAttemptExecutor` shape and a real OpenCode raw-result implementation;
- finalizers that read and validate model-written raw files;
- the reliable Kernel tail from seal through receipt.

The current OpenCode adapter already supports the intended result style in principle:

1. append an installed result contract to the prompt;
2. observe assistant text or tool records;
3. extract a JSON object;
4. validate it locally against the locked result schema;
5. return an `AgentRunResult`.

That code is not yet a production LangGraph Agent path. The current baseline also contains blocking placeholders and incompatible policy:

- all 33 Agent contracts require `provider_schema=True` while OpenCode truthfully advertises `False`;
- `CompositeAttemptExecutor` and Product Application reject the binding before execution;
- Product semantic runtime assembly binds `_DeferredPhase` objects that fail when called;
- Product runtime ports include `_UnusedWorkspace`, in-memory authorization, and non-final journal wiring;
- the Composite executor does not close the required in-flight activity reconcile protocol;
- runtime binding still derives important facts through 99 legacy phase aliases and Workflow composition;
- the permissive JSON parser may search mixed prose or completed tool inputs for a candidate object.

Production code and deterministic integration tests must close these gaps. Ordinary CI is not
represented as proof of a live external provider/model.

## 5. Goals

1. Make Raw Agent execution the permanent production contract for all 33 Agent contracts.
2. Keep the result and file formats locally validated by installed code.
3. Drive every Agent Attempt through a real, recoverable OpenCode activity without the legacy Workflow scheduler.
4. Guarantee that recovery never knowingly emits a second prompt for the same Attempt.
5. Bind prepare, runtime, finalize, resources, result models, and deployment choices by semantic contract ID.
6. Remove dependency on the 99 legacy phase aliases before those aliases and Workflow YAML are deleted.
7. Replace every production placeholder port with durable, fenced implementations.
8. Preserve the existing Kernel commit, effect, and receipt guarantees.
9. Verify the exact raw binding inventory and all 33 local result contracts before Agent-root cutover.
10. Continue through T5b, T5c, T5d, drain, and physical deletion of the custom Workflow Runtime.
11. Keep Raw Agent parser, binding, runtime, recovery, and lifecycle tests in ordinary CI.

## 6. Non-goals

- Provider-enforced structured output.
- OpenCode `StructuredOutput` tool integration.
- Kernel-generated JSON or YAML files.
- Removing Capability file parsing or file-level Pydantic validation.
- Treating valid JSON shape as proof of business correctness.
- Adding a generic artifact plugin platform.
- Preserving arbitrary prose around the final Agent result.
- Running a child OpenCode session hierarchy; each Attempt uses one independent root session.
- Changing Feature graph topology, `join:any` rewrites, `min_matches` dispositions, or Product public schemas.
- Weakening cutover, drain, revision-retention, fencing, effect, validator, interrupt, or security gates.

## 7. Authority Model

| Concern | Sole authority |
| --- | --- |
| Workflow progression, routing, loops, joins, interrupts | Installed Python LangGraph factories |
| Agent business input, result model, final output, resources, validators | Installed Feature contract |
| Provider, model, adapter, policy, and secret handles | Product runtime binding |
| OpenCode session create/adopt/observe/reconcile | OpenCode adapter behind the raw executor |
| Result-schema document and digest | Installed Agent contract resolved at Boot |
| Raw workspace mutation | OpenCode tools within the Kernel-authorized workspace boundary |
| Raw file shape and business validation | Feature finalizer and ordered validators |
| Canonical promotable bytes | Kernel sealed write set |
| Durable prepare, promotion, effects, receipts | `AssuranceAttemptKernel` |
| Invocation next step and terminal status | LangGraph checkpoint bound to GraphRevision |

Skills describe business procedure, tool usage, and expected files. They are not the authoritative store for the Agent-result JSON Schema. Project files, skills, the model, and OpenCode plugins cannot register or replace result models, handlers, validators, resource claims, or runtime bindings.

## 8. Permanent Agent Contract

The semantic Agent path retains four type roles across the contract and its resolved executor:

```text
AgentExecutionContract[InputT, AgentResultT, OutputT]
ResolvedRawAgentExecutor[InputT, PreparedT, AgentResultT, OutputT]
```

- `InputT` is the validated semantic node input.
- `PreparedT` is the executor-private deterministic OpenCode request preparation. It is not a
  fourth public type parameter on `AgentExecutionContract`.
- `AgentResultT` is the strict locally validated JSON result returned by the Agent runtime.
- `OutputT` is the Feature-owned final output after raw-file validation and projection.

`requires_provider_schema`, `provider_schema`, `requires_structured_output`, and equivalent capability flags are removed rather than permanently set to misleading values. Local result validation is unconditional and therefore is not an optional runtime capability.

Permanent result-envelope names also describe the actual raw protocol:

- `ResultContract.delivery_mode` is the closed value `assistant_json_local_v1`;
- `AgentRunResult.result_payload` contains the locally validated JSON object;
- active code does not use `extraction_mode == "structured"` or a field named
  `structured_result` to describe this payload.

Revision-pinned historical deployment artifacts remain responsible for decoding their own old
record names. New source does not retain misleading aliases as a second production contract.

`AgentExecutionContractProjectionV1` is a closed canonical object with exactly these keys:

```text
schema_version = "raw-agent-contract-v1"
contract_id
owner_id
input_model_symbol
input_schema_digest
agent_result_model_symbol
agent_result_schema_digest
output_model_symbol
output_schema_digest
prepare_handler_id
finalize_handler_id
skill_id
agent_profile_id
resource_claims
raw_path_policy_digest
validator_ids
timeout_seconds
retry_policy
```

`RawAgentRuntimeBindingProjectionV1` is a separate closed Product-owned canonical object with
exactly these keys:

```text
schema_version = "raw-agent-runtime-binding-v1"
contract_id
contract_projection_digest
runtime_handler_id
adapter_id
adapter_version
adapter_source_digest
provider_id
model_id
policy_profile_id
secret_handle_ids
target_policy_digest
activity_recovery_profile_id
```

Both objects use the framework canonical-JSON projection; undeclared keys are rejected. Ordered
sequences preserve declared order, while the two 33-row ProductLock tables are sorted by canonical
UTF-8 contract ID before hashing.

The runtime binding references and authenticates the Feature contract. It cannot repeat with a
different value or override the contract's prepare/finalize handlers, result models/schemas,
resources, path policy, validators, timeout, or retry policy. ProductLock v3 contains the ordered
33 contract projection digests and ordered 33 matching runtime-binding projection digests. These
two versioned projections, rather than an open-ended object or callable representation, determine
the relevant ProductLock and GraphRevision identity.

Callables are resolved from authenticated installed contributions and do not enter canonical data projections directly.

## 9. Prompt and Result Protocol

### 9.1 Prompt composition

The OpenCode request contains, in this order:

1. Feature skill/business instructions;
2. the prepared task context and authenticated input projections;
3. a runtime-generated result-contract section appended last.

The result-contract section contains:

- `schema_id`;
- `schema_digest`;
- the complete canonical JSON Schema document derived from `AgentResultT`;
- an instruction that the final assistant response must be exactly one JSON object, with no Markdown fence, commentary, summary, or trailing text.

The same immutable schema document is used for prompt rendering and local validation. The adapter does not independently regenerate a second schema after dispatch. Project configuration and the workspace cannot replace the schema.

The request omits OpenCode's structured format field. In particular, it never sends:

```json
{"format": {"type": "json_schema", "schema": {}}}
```

### 9.2 Strict final-result grammar

The adapter first closes the message choice. For one caller message ID, it requires exactly one
successful result-bearing assistant message whose session, parent/caller identity, provider/model,
completion marker, and error-free terminal state match the bound activity. Intermediate assistant
messages may contain completed tool parts but no non-empty text part. The result-bearing message
must contain exactly one text part, and that entire text value must be one complete JSON object.
Any second non-empty assistant text part or second result-bearing assistant message is ambiguous
and fails.

The permanent result parser rejects:

- empty content;
- JSON arrays or primitives;
- Markdown fences;
- leading or trailing commentary;
- multiple JSON objects;
- a JSON substring embedded in prose;
- a completed tool input presented as the final Agent result;
- a non-terminal, errored, aborted, truncated, or ambiguous assistant message;
- content beyond the contract's byte and structural bounds.

Tool calls may still create or modify authorized raw files. Their input is not an alternative result channel.

### 9.3 Local validation

After strict parsing, the adapter validates the object against the locked JSON Schema and rejects secret/canary reflection. The raw executor then validates into the installed `AgentResultT`. Capability finalization validates `OutputT` after inspecting the actual raw files.

Adapter validation is defense in depth. The Kernel-bound model validation remains authoritative for the Attempt contract.

## 10. Semantic Runtime Binding

Product publishes exactly one authenticated raw runtime binding for each of the 33 semantic Agent
contract IDs. The Feature contract remains the sole authority for prepare/finalize handlers,
models/schemas, resources, validators, and semantic retry policy. A Product binding selects only:

- OpenCode runtime handler and adapter version;
- provider/model and policy profile;
- secret handles and allowed target classes;
- activity recovery policy.

The binding carries the exact Feature-contract digest as a consistency proof. Boot rejects any
binding whose contract ID, contract digest, owner closure, runtime source, or selected deployment
values do not match; it never resolves a Product copy of Feature-owned fields.

New LangGraph execution must not resolve these facts through `prepare`, `execute`, or `finalize` phase aliases. The 99 aliases may remain temporarily as legacy-runtime compatibility data until drain, but they are not an authority for Raw Agent execution and are deleted in Product T8.

Missing, duplicate, extra, wrong-owner, wrong-source, or digest-drifting semantic bindings fail Boot. The SUT, `.aa/`, environment variables, and CLI flags cannot select a handler, Python symbol, provider-schema mode, or alternate runtime path.

## 11. `ResolvedRawAgentExecutor`

`ResolvedRawAgentExecutor` is the one production executor for semantic Agent contracts. It replaces `_DeferredPhase` and the non-recoverable Composite binding while preserving the four-role execution flow.

Its logical flow is:

```text
prepare(validated_input)
→ bind authenticated AgentRunRequest
→ create or adopt one OpenCode root session
→ admit one prompt or prove that the same prompt was admitted
→ observe or reconcile terminal activity
→ recover the complete AgentRunResult
→ validate AgentResultT
→ build one closed RawFinalizeBundle
→ finalize(RawFinalizeBundle)
→ validate OutputT
```

The public executor protocol supports both initial execution and in-flight reconcile. The executor may use adapter-specific objects internally, but `graph_engine` core and Feature graph code remain provider-neutral.

### 11.1 Session identity

Each Attempt uses one independent OpenCode root session. Child-session orchestration is neither required nor permitted by this contract.

The activity journal binds the session ID, caller message ID, prompt-body digest, workspace identity, provider/model, adapter identity, AttemptKey, and active fence. Server-generated session identity is bound durably before prompt admission whenever the protocol permits. Discovery/adoption after an uncertain create must prove the full binding; otherwise the Attempt becomes indeterminate rather than creating another session.

### 11.2 Phase requirements

The finalizer has one closed input:

```text
RawFinalizeBundle[InputT, PreparedT, AgentResultT]
  validated_input: InputT
  prepared: PreparedT
  agent_result: AgentResultT
  agent_run_result: AgentRunResult
  raw_workspace: ReadOnlyRawWorkspace
```

`agent_run_result.result_payload` must be canonically equal to the payload used to construct
`agent_result`; the remaining envelope fields are metadata/evidence, not a second business-result
authority. Every finalizer implements the same bundle signature. There is no optional “original
input where required” or alternate per-Capability signature.

- `prepare` is deterministic for the same validated input and authenticated configuration.
- `prepare` may construct request data but cannot publish an external effect or expand authority after resource authorization.
- OpenCode is the only phase allowed to mutate Agent-authorized raw workspace paths.
- `finalize` receives exactly one `RawFinalizeBundle`.
- `finalize` may parse and validate files and construct `OutputT`; it cannot mutate promotable bytes or apply an external effect.
- business and delivery effects remain declared outputs settled only by the Kernel effect stage.

## 12. Attempt Transaction and Recovery

The permanent Agent transaction is:

```text
derive stable AttemptKey
→ acquire durable resource authorization
→ create/adopt isolated workspace
→ execute_or_reconcile ResolvedRawAgentExecutor
→ validate OutputT
→ seal candidate bytes
→ run ordered validators
→ durable workspace prepare
→ atomic promote/recover
→ apply/reconcile six registered effect kinds
→ durable terminal receipt
```

The Attempt/activity journal durably records authenticated request identity, the bound OpenCode activity reference, prompt-admission identity, terminal `TaskOutcome`, promotion state, effect state, and terminal resolution. Raw Agent results do not introduce an artifact content store or materialization receipt family.

Recovery obeys these rules:

- before prompt admission, a retry may admit only the identical caller message to the proven bound session;
- after prompt admission, recovery adopts and observes the same session;
- a terminal activity outcome is reused and never causes another model dispatch;
- after Agent completion but before finalize, deterministic finalize may replay over the same persistent workspace and terminal result;
- after finalize but before seal, finalize may replay but OpenCode may not;
- after seal, promotion consumes the sealed durable source and never rereads mutable staging bytes;
- after promotion or effect application, existing reconcile receipts determine completion;
- an unprovable external state returns an indeterminate resolution and never silently retries the Agent;
- every irreversible boundary rechecks the active fence.

### 12.1 Deadline, cancellation, and retry identity

The Feature contract's authenticated timeout is the outer activity deadline. Before that deadline,
a proven-running OpenCode activity returns `PendingTaskResult` with a durable wakeup. Repeated
wakeups continue to adopt the same activity and do not reset the deadline.

At the deadline, the adapter issues at most one idempotent cancel request for the bound activity.
A terminally confirmed cancellation produces `PermanentTaskFailure(kind="timeout")`. If completion
or cancellation cannot be proved, the result is `IndeterminateTaskResult`. An operator cancellation
uses the same bound cancel/reconcile protocol and cannot be represented as successful completion.

A provider terminal `transient` or `timeout` outcome ends the current AttemptKey; it does not
authorize a second session or prompt under that key. Policy may route a new business retry only by
creating a new `BusinessActivation` and therefore a new AttemptKey. Technical replay of one
AttemptKey is always adoption/reconciliation, never a new model dispatch.

## 13. Production Runtime Ports

Before T5b, Product must replace all test-only and placeholder Agent ports with durable implementations:

- real isolated `TaskWorkspaceProvider` backed by the Product change workspace;
- strict Attempt journal serialization with compare-and-swap append;
- durable resource authorization and adoption;
- OpenCode activity host and reconcile port;
- metadata-only secret resolution and redaction boundary;
- authenticated network target policy;
- workspace prepare/promote/recover;
- the existing six-effect apply/reconcile registry;
- checkpoint observer integration and fencing.

Permanent activity and workspace primitives live in the closed modules
`graph_engine.attempts.activity`, `graph_engine.attempts.workspace`,
`graph_engine.attempts.production_host`, `graph_engine.attempts.production_worker`,
`graph_engine.attempts.host_protocol`, `graph_engine.attempts.host_receipts`, and
`graph_engine.attempts.secret_sources`. Attempt/resource journals remain under
`graph_engine.persistence`, and effect apply/reconcile remains under `graph_engine.effects`.
Product T9 cannot delete `graph_engine.runtime` while any Raw Agent production import still depends
on it; no additional catch-all permanent module is permitted without amending this design.

Production gates forbid `_DeferredPhase`, `_DeferredTaskExecutor`, `_UnusedWorkspace`, pickle state, in-memory-only authorization, and `test_kernel_resolutions` on a release path.

## 14. Raw File Authority

OpenCode continues to write source, Markdown, JSON, YAML, patches, manifests, and other declared outputs as raw workspace bytes.

The permanent assurance boundary is:

1. validated input and the semantic contract establish allowed roots or exact paths before dispatch;
2. the workspace sandbox prevents writes outside those bounds;
3. the finalizer verifies required files, file formats, domain models, path locks, and cross-file semantics;
4. the Kernel scans the actual mutation set and rejects undeclared, missing, extra, symlink, hardlink, traversal, mode-drift, or oversized outputs;
5. sealing authenticates the exact promotable bytes;
6. validators test business semantics and generated code;
7. promotion consumes only the sealed durable source.

File suffixes do not create typed ownership. JSON/YAML parsing and file-level Pydantic validation remain Capability responsibilities. Model-supplied `output_files` and byte digests are hints only; they do not authorize the write set.

## 15. Failure Semantics

| Condition | Required resolution |
| --- | --- |
| Final assistant content is not exactly one JSON object | Permanent invalid-output failure |
| JSON fails locked schema or `AgentResultT` validation | Permanent invalid-output failure; no finalize or promotion |
| Required file is absent or invalid | Finalization failure; no seal or promotion |
| File is extra, outside authority, linked, oversized, or drifted | Workspace/seal failure; no durable prepare or promotion |
| Resource is held by another Attempt | Pending system interrupt with durable wakeup |
| OpenCode activity is still running | Pending system interrupt; resume the same node and activity |
| Prompt admission or external completion is unprovable | Indeterminate; no automatic redispatch |
| Runner fence is stale | Conflict before the next irreversible operation |
| Validator rejects | Rejected Attempt; zero durable promotion |
| Promotion is uncertain | Existing promote/recover protocol decides outcome |
| Effect publication is pending or uncertain | Existing effect pending/indeterminate protocol decides outcome |

Human interrupts remain pure graph nodes. System interrupts may originate from the Attempt node and rely on stable AttemptKey replay and Kernel idempotency.

## 16. Repository Qualification

Raw Agent source changes use the ordinary repository gate plus focused deterministic tests for the
affected parser, binding, Product port, Attempt transaction, crash/replay, security, and lifecycle
paths. The exact 33 semantic contracts, 33 runtime bindings, and 35 graph occurrences remain local
inventory invariants.

The repository does not maintain a protected live-provider matrix, candidate evidence manifest,
OpenCode binary certification, or external model release claim. Manual live benchmarks may inform
operators but do not block merge, cutover, or release.

## 17. Product Cutover and Deletion Sequence

The authoritative continuation order becomes:

```text
completed Foundation / Attempt / Feature / Product T1–T4
→ completed T5a: four non-Agent roots on langgraph-v1
→ Raw Agent Runtime Closure
→ verify real production ports with focused deterministic tests and ordinary CI
→ implement and verify T5b: eight Agent-dependent thin roots
→ implement and verify T5c: execute
→ implement and verify T5d: full
→ T6: disable legacy starts and prove zero-active drain
→ T7: migrate every retained production primitive and consumer
→ T8: switch compile to ProductLock v3-only and delete Workflow YAML/module/99 aliases
→ T9: delete Workflow compiler and custom Runtime after focused deletion tests and ordinary CI
→ T10: remove migration selectors; retain the permanent revision registry
```

Checkpoint S0 and Checkpoint S are removed rather than moved. A failed cancelled Structured probe has no effect on Product Boot or Raw Agent cutover.

The four T5a roots remain on their recorded revisions while Raw Agent closure is developed. T5b/T5c/T5d switches affect only future starts. Existing Invocations never change runtime or GraphRevision in place.

## 18. Deletion-safe Permanent Assets

Product T8 and T9 delete legacy topology and runtime authority but retain:

- 33 semantic Agent contracts and local result schemas;
- 33 Product raw runtime bindings;
- prepare and finalize handlers/projectors;
- skills and raw output policies;
- `ResolvedRawAgentExecutor`;
- durable Attempt/activity/workspace/effect ports in permanent modules;
- the six Feature factories, Product composer, and 14 root contracts;
- ProductLock v3, GraphRevision, GraphBuildManifest, and deployment-artifact retention;
- focused deterministic Raw Agent and Product lifecycle tests in ordinary CI;
- read-only historical v2 evidence support required by retention policy.

They delete:

- Workflow YAML topology and module packaging;
- project Workflow replacement promises;
- 99 phase aliases and phase-slot binding expansion;
- Graph/Node definitions and projection/expression compiler;
- activation planner, token scheduler, legacy subgraph loop, and settle loop;
- executable legacy Workflow checkpoints and resume authority;
- migration-only runtime selectors after drain;
- Structured eligibility/runtime code with no remaining consumer.

## 19. Security Requirements

- Skills and prompt text cannot expand resource or path authority.
- OpenCode receives only the bound workspace, agent profile, provider/model, task instructions, result schema, and authorized tool surface.
- Secrets remain handles resolved through a metadata-only boundary and never enter durable prompt/result/receipt data.
- Network access is default-deny and limited to authenticated target classes and exact targets.
- The parser rejects credential/canary reflection before accepting a result.
- Raw-file scanners reject symlinks, hardlinks, traversal, file-type drift, mode drift, undeclared deletes, and write-set mismatch.
- No shell, MCP, tool, skill, SUT, or project file may register or replace a handler, validator,
  Agent-result schema, semantic contract, or runtime binding. Legacy whole-file Workflow and
  execution-contract replacement remains isolated to legacy Invocations until T8 deletes it.
- Checkpoint state contains no workspace service, secret, raw session client, file bytes, or executable runtime object.
- Every irreversible operation is fenced and every durable record is revision-bound.

## 20. Test Matrix

| Layer | Required proof |
| --- | --- |
| Contract | Exact 33 contracts, 34 occurrences, schemas, owners, resources, validators, and runtime bindings |
| Parser | Exact-object success; fence, prose, multiple-object, tool-input, error, truncation, and size rejection |
| Binding | Missing, duplicate, extra, owner/source drift, alias dependency, and digest drift fail Boot |
| Executor | Prepare, dispatch, adoption, reconcile, terminal reuse, local validation, finalization |
| Activity | One root session and one prompt per AttemptKey across restart and runner takeover |
| Workspace | Required/extra files, path escape, symlink, hardlink, delete, mode, size, and digest drift |
| Security | Secret/canary redaction, command/tool bounds, target-aware network denial |
| Kernel | Resource, workspace, output validation, seal, validator, prepare, promote, six effects, receipt |
| Crash | Session create/bind, prompt admission, terminal result, finalize, seal, prepare, promote, effect, checkpoint |
| Graph | Nine `join:any`, seven SCC anchors, three `min_matches`, 50 exclusive routes, interrupts and budgets |
| Product migration | 14 public roots, live legacy/LangGraph shadow parity before drain, lifecycle and cutover evidence |
| Product permanent | Raw Agent E2E, lifecycle, status, export, archive, revision retention; no live legacy import |
| Deletion | No Workflow YAML, phase alias, compiler, or custom Runtime authority; Raw Agent E2E still passes |
| Repository gate | Ruff, format, Pyright, import-lint, full pytest, and all three wheel smokes |

Production validator inventory remains 25 registered and zero bound. A test-only authenticated clone continues to prove accept/promote and reject/no-promote parity without changing shipped contracts.

Live cross-runtime shadow is migration evidence and is removed with the legacy Runtime. Permanent
ordinary tests retain current Raw Agent protocol, binding, recovery, and lifecycle coverage without
importing or executing deleted legacy authority.

## 21. Required Plan and Documentation Changes

The implementation-planning step must synchronize all normative references in one reviewed series.

Active documents to rewrite:

1. `2026-08-31-python-native-langgraph-migration.md`;
2. `2026-08-31-langgraph-product-cutover.md`;
3. `2026-08-31-semantic-attempt-kernel.md`;
4. `2026-08-31-feature-stategraph-migration.md`;
5. `2026-08-31-python-native-langgraph-assurance-design.md`.

The Raw Agent Runtime Closure child plan retains only implementation and deterministic CI tasks.

Documents to retain with `CANCELLED / SUPERSEDED` status and no active checkboxes:

- `2026-09-01-structured-artifact-pipeline-design.md`;
- `2026-09-01-structured-artifact-pipeline.md`;
- `2026-09-01-artifact-kernel-foundation.md`;
- `2026-09-01-agent-artifact-contract-migration.md`;
- `2026-09-01-opencode-structured-output-gate.md`.

Research about OpenCode Structured Output remains historical and must state that the Product deliberately chose the permanent raw protocol. By Product T10 completion, Product documentation and `AGENTS.md` state:

> Python wheels own StateGraph topology and semantic Agent contracts. OpenCode writes authorized raw workspace files and returns one locally validated JSON result. The Kernel seals and commits the actual bytes. There is no provider-structured-output or typed-artifact materialization layer.

## 22. Acceptance Criteria

- [ ] The Structured Artifact Pipeline is absent from every active dependency and completion graph.
- [ ] Cancelled Structured specs/plans are visibly non-executable historical records.
- [ ] `requires_provider_schema`, `provider_schema`, `requires_structured_output`, and `opencode_structured_output` are absent from the permanent production contract.
- [ ] Active result models use `assistant_json_local_v1` and `result_payload`; misleading
  `extraction_mode == "structured"` and `structured_result` names survive only inside retained old
  deployment artifacts, not new production source.
- [ ] The OpenCode request omits `format.type == "json_schema"` and appends the authenticated result schema to prompt text.
- [ ] Only one exact terminal assistant JSON object can become `AgentResultT`.
- [ ] Every finalizer accepts the one closed `RawFinalizeBundle`; there is no alternate
  Capability-specific finalizer signature.
- [ ] Raw Agent closure adds no LangGraph node or edge.
- [ ] Exactly 33 semantic Agent contracts and 33 raw runtime bindings resolve without phase aliases.
- [ ] Every production executor and runtime port is real, durable, authenticated, and fenced.
- [ ] Recovery adopts the same OpenCode root session and never knowingly emits a second prompt for the same Attempt.
- [ ] Finalizers validate actual raw files; seal and promotion authenticate the resulting real bytes.
- [ ] Focused Raw Agent/Product suites and the ordinary repository gate pass before each remaining
  Agent cutover tranche is accepted.
- [ ] All 14 roots route future starts to LangGraph before legacy drain begins.
- [ ] Workflow YAML, phase aliases, compiler, planner, scheduler, and custom Runtime are physically absent at completion.
- [ ] Raw Agent parser, binding, recovery, and lifecycle coverage remains in ordinary CI after
  migration switches disappear.
- [ ] Existing Invocations reopen only through their recorded revision and retained deployment artifact.
- [ ] No target documentation promises Structured Output, typed slots, Kernel materialization, or materialization receipts.

## 23. Risks and Accepted Trade-offs

### Model format adherence remains probabilistic

The prompt requests exact JSON but OpenCode does not enforce it through a structured transport. Invalid responses fail closed and may require a policy-authorized new business Attempt. The system does not recover malformed content by scraping prose.

### File-validation boilerplate remains

Capability finalizers continue to read, parse, and validate JSON/YAML files. This is an accepted cost of removing the typed materialization subsystem.

### Raw workspace mutation has a larger attack surface

The design compensates with closed resource claims, sandboxing, exact mutation scans, link/path checks, finalizer validation, sealing, validators, and promotion from durable sealed bytes.

### External OpenCode compatibility is not repository-certified

Structured-output support is not required. Deterministic tests preserve the project's raw
create/admit/read/observe/reconcile assumptions, but they cannot prove a current OpenCode release or
provider/model honors them. That compatibility check is an operator/deployment responsibility.

### Historical plans increase documentation volume

Cancelled plans remain for auditability but are prominently non-executable and excluded from active indexes and gates.

## 24. Final Target

The completed Product is:

```text
six authenticated Python Feature factories
+ fourteen Product LangGraph roots
+ thirty-three recoverable Raw Agent contracts
+ one reliable AssuranceAttemptKernel transaction
+ revision-pinned restart and deployment retention
+ zero Workflow YAML topology
+ zero phase aliases
+ zero custom Graph Runtime
+ zero Structured Artifact or typed-materialization claims
```

This is the final architecture, not an intermediate fallback awaiting Structured Output.
