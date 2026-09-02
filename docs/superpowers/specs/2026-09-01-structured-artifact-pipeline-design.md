# Structured Artifact Pipeline

**Status:** Proposed amendment to the Python-native LangGraph Assurance design

**Date:** 2026-09-01

**Scope:** OpenCode adapter, semantic Agent contracts, `AssuranceAttemptKernel`, deterministic artifact materialization, Capability finalizers, recovery, and receipts

## 1. Executive Summary

The existing Transaction design remains the authority for isolated execution, validation, sealing, durable prepare, promotion/recovery, effects, receipts, and system interrupts. This design does not replace that transaction and does not add any LangGraph node.

It adds one medium-sized **Structured Artifact Pipeline** inside each semantic Agent Attempt:

1. the OpenCode adapter requests a JSON-Schema-constrained final result through OpenCode's `format.type == "json_schema"` and `format.schema` protocol;
2. the adapter observes the successful value from assistant `info.structured`;
3. the Kernel validates that untrusted value into the Feature-owned `AgentResultT`;
4. the Kernel resolves typed artifact slots, runs any authenticated deterministic artifact projectors, and serializes typed documents into canonical JSON or YAML bytes;
5. the Kernel writes those bytes into the isolated staging workspace and produces a `MaterializationReceipt`;
6. the Capability finalizer consumes the validated typed values and receipt instead of rereading and reparsing typed files;
7. the existing seal, ordered validators, durable prepare, promotion/recovery, effect settlement, and terminal receipt phases continue unchanged.

OpenCode remains an untrusted producer. OpenCode's internal `StructuredOutput` tool improves generation and transport shape, but only Kernel validation and materialization authorize committed bytes.

This design supersedes the earlier wording that described the capability as provider-native structured output or `provider_schema`. The negotiated OpenCode capability is named `opencode_structured_output`; it is not a provider `response_format` guarantee.

## 2. Problem Statement

The existing Capability flow asks OpenCode to write JSON/YAML files as raw text and then return a result that often contains little more than an `output_files` manifest. Capability finalizers subsequently reread those files, parse JSON or YAML, validate them again with Pydantic, reconstruct receipts, and sometimes derive additional fields before the Transaction can seal and promote the output.

This creates four avoidable costs:

1. prompts require the model to maintain JSON/YAML punctuation and formatting even when the desired result already has a typed domain model;
2. the adapter extracts JSON from prose or tool traces instead of consuming a stable structured-result field;
3. many finalizers repeat file reads, parsing, and shape validation already expressible by the result and document models;
4. the Transaction cannot durably resume between a successful Agent result and deterministic file generation without either relying on mutable staging state or redispatching OpenCode.

OpenCode structured output alone does not solve the problem. It constrains the final assistant object, but does not bind that object to files written by `write`, `edit`, `apply_patch`, shell, MCP, or another tool. Trusting an OpenCode plugin to validate a file would merely move the same read-and-validate operation outside the Kernel and weaken the current authority boundary.

The desired result is therefore not “Schema-valid files written by OpenCode.” It is:

> OpenCode produces an untrusted typed candidate; the Kernel validates it, deterministically materializes protected structured artifacts, and then commits the resulting real bytes through the existing Transaction.

## 3. Relationship to the Existing Design

This document is a normative delta to the existing Python-native LangGraph design and Semantic Attempt Kernel plan. All existing decisions remain in force unless explicitly amended here.

The following existing behavior is unchanged:

- `prepare → Agent runtime → finalize` remains one semantic Attempt;
- stable `AttemptKey`, resource authorization, and the isolated workspace remain mandatory;
- `AgentResultT` and final `OutputT` remain separate models;
- local Kernel validation remains mandatory;
- full staged-write sealing and ordered validators remain mandatory;
- durable prepare and promote/recover remain mandatory;
- all six registered effects retain their apply/reconcile semantics;
- Attempt receipts, system interrupts, fencing, and the existing crash-recovery model remain mandatory;
- LangGraph remains the only Workflow control authority and receives a result only after the Attempt reaches a valid resolution.

This document refines only the first half of an Agent Attempt: structured-result transport, typed artifact ownership, deterministic materialization, finalizer inputs, and recovery before sealing.

## 4. Goals

1. Use OpenCode's actual `format.type == "json_schema"` plus `format.schema` request and `info.structured` response path.
2. Keep OpenCode and its internal `StructuredOutput` tool outside the durable assurance trust boundary.
3. Let Feature contracts declare typed-artifact-only, raw-artifact-only, or mixed-artifact ownership.
4. Make managed JSON/YAML bytes deterministic functions of authenticated models, schemas, projectors, media codecs, and versioned serializers.
5. Prevent OpenCode raw tools from mutating paths owned by the Kernel materializer.
6. Let recovery resume from a previously bound OpenCode activity or a durable structured result without issuing a second prompt.
7. Remove duplicated file parsing and shape validation where the Kernel has already validated and materialized the same typed document.
8. Preserve business-semantic validators, cross-reference checks, tests, Eval, sealing, promotion, and effect recovery.
9. Classify all 33 Agent contracts and all 34 semantic Agent occurrences before Feature freeze.
10. Keep `AssuranceAttemptKernel.execute_or_recover(...)` as the single external transaction interface.

## 5. Current Baseline

The accepted target design contains 33 distinct Agent contracts at 34 semantic Agent occurrences. The current Capability catalog divides them as follows:

| Feature | Agent contracts |
| --- | ---: |
| Execution | 2 |
| Generation | 14 |
| Healing | 2 |
| Improvement | 6 |
| Intake | 4 |
| Quality | 5 |
| **Total** | **33** |

The checked current implementation of `AgentExecutionContract` still contains only contract identity, skill, profile, and resources. `InputT`, `PreparedT`, `AgentResultT`, `OutputT`, structured-output requirement, artifact slots, validators, and their digests are target-contract additions, not guarantees already present in the running legacy Runtime.

The current output-route inventory establishes these facts:

- 32 of 33 contracts declare at least one JSON, YAML, or YML candidate output;
- the sole suffix-level exception is the Quality report contract, which declares only Markdown;
- fixed route templates contain 33 JSON/YAML paths and 28 raw paths;
- Intake case design and Generation code generation also have dynamic output roots that are not fully represented by fixed path templates;
- a suffix-only first pass produces 15 typed-artifact-only, 17 mixed-artifact, and one raw-artifact-only candidate, but those numbers are provisional and not a release classification.

A `.json` or `.yaml` suffix is not sufficient to make an artifact typed. Some current Agent result schemas contain only `output_files`; some Generation results contain summaries without the complete document bodies; repair contracts may perform bounded changes to existing documents; and some current finalizers add authoritative digest, round, status, or public-projection fields. Every artifact slot therefore requires an explicit content authority and source phase before it can move to Kernel materialization.

The current checked OpenCode 1.18.4 server is not a qualifying runtime for this design: it accepts the JSON-Schema `format` object on asynchronous admission but cannot reliably round-trip the persisted message through the message APIs used for recovery. Cutover requires a pinned version that passes the integration gate in this document. A private OpenCode fork is not an accepted workaround.

## 6. Authority Model

| Concern | Authority |
| --- | --- |
| Workflow routing, loops, subgraphs, interrupts | LangGraph Feature/Product graph code |
| Agent input, typed result, artifact definitions, document models, projectors, semantic validators | Installed Feature wheel |
| Provider/model/runtime selection | Product runtime binding |
| OpenCode session and structured-result transport | OpenCode adapter |
| Authoritative validation, artifact materialization, write-set sealing, commit and recovery | `AssuranceAttemptKernel` |
| Canonical JSON/YAML encoding | Installed Kernel serializer registry |
| Project organization configuration | `.aa/`, within its existing non-code authority |

OpenCode, the model, skills, SUT files, project configuration, and OpenCode plugins may not register artifact models, schemas, projectors, codecs, serializers, validators, handlers, or path authorities. Those objects are installed-code capabilities and participate in Product/Graph revision authentication.

## 7. User Stories

1. As a Capability author, I want a typed Agent result to carry complete structured documents, so that the model does not have to write JSON/YAML punctuation through raw file tools.
2. As a Capability author, I want artifact slots to declare stable models, paths, media codecs, and versioned serializers, so that file generation is an explicit contract rather than prompt convention.
3. As a Capability author, I want to retain raw outputs for source code, Markdown, patches, and formatter-driven files, so that structured output is not forced onto unsuitable artifacts.
4. As a Capability author, I want mixed-artifact contracts to combine typed documents and raw files without path overlap, so that each artifact uses the correct production mechanism.
5. As an OpenCode adapter maintainer, I want to send the locked result schema using `format.type == "json_schema"` and `format.schema`, so that the final result uses OpenCode's supported structured-output mechanism.
6. As an OpenCode adapter maintainer, I want to consume only terminal assistant `info.structured`, so that prose scanning and accidental tool-trace extraction are not the primary result path.
7. As an OpenCode adapter maintainer, I want structured-output errors, content filtering, aborts, truncation, context overflow, and API errors to remain failures, so that partial text is never treated as a valid result.
8. As a release operator, I want capability negotiation to fail closed on unsupported OpenCode versions, so that an unverified server is never described as structured-output capable.
9. As a Kernel maintainer, I want to observe and durably store the intermediate `AgentResultT`, so that materialization and finalization can recover without redispatching the model.
10. As a Kernel maintainer, I want one deterministic materializer for JSON and YAML, so that identical typed values always produce identical bytes.
11. As a security reviewer, I want typed paths excluded from all OpenCode raw-write permissions, so that the model cannot bypass the Kernel materializer.
12. As a security reviewer, I want every promotable output path owned by exactly one typed or raw slot and internal scratch kept non-promotable, so that ambiguous and undeclared mutations fail closed.
13. As an audit consumer, I want receipts to identify the document schema, media codec, versioned serializer, object digest, and final byte digest, so that committed files can be traced to their production contract.
14. As a recovery operator, I want a crash after OpenCode completion to adopt the same bound session, so that recovery never issues a duplicate prompt for the same Attempt.
15. As a recovery operator, I want a durable structured result to be sufficient for replaying materialization and finalization, so that OpenCode availability is no longer required after that boundary.
16. As a recovery operator, I want interrupted multi-file materialization to be idempotently completed, so that no partial set can be promoted.
17. As a finalizer maintainer, I want validated typed documents and a materialization receipt as inputs, so that I can delete redundant file reads and structural parsing.
18. As a finalizer maintainer, I want byte-determining projection logic to run before materialization, so that post-materialization finalization cannot invalidate the receipt.
19. As a validator author, I want business semantics, cross-file references, tests, and Eval to keep running after seal, so that shape validation is not mistaken for correctness.
20. As a graph author, I want the pipeline hidden inside the existing Attempt node, so that no new LangGraph nodes, edges, joins, or subgraphs are introduced.
21. As a migration maintainer, I want all 33 contracts classified by artifact slot before Feature freeze, so that suffix-based assumptions do not silently change output authority.
22. As a migration maintainer, I want dynamic case and code-generation roots explicitly resolved and bounded, so that they cannot escape the authenticated resource envelope.
23. As a Product maintainer, I want artifact models, projectors, media codecs, and serializer identities included in contract and build digests, so that replay under drift fails closed.
24. As a test maintainer, I want one representative end-to-end contract for each of typed-artifact-only, raw-artifact-only, and mixed-artifact behavior, so that all three paths remain supported.
25. As a project user, I want output bytes and business behavior preserved where the legacy representation was already canonical, so that this migration reduces boilerplate without silently rewriting domain semantics.

## 8. Implementation Decisions

### Decision 1: The pipeline is internal to one semantic Attempt

No new LangGraph node represents structured-result observation, materialization, or finalization. The graph-facing seam remains `AttemptNodeFactory`, and the transaction-facing seam remains `AssuranceAttemptKernel.execute_or_recover(...)`.

The pipeline may add internal Attempt journal phases and receipts, but it cannot choose a Workflow edge, publish graph state early, or create an independently schedulable phase alias.

### Decision 2: Capability terminology is OpenCode-specific only at the adapter boundary

The exact runtime capability identifier is `opencode_structured_output`. The terms `provider_schema`, `provider-native structured output`, and claims that OpenCode forwards provider `response_format` are removed from this migration.

All 33 Agent contracts require a structured `AgentResultT` at cutover. Their provider-neutral Feature requirement is `requires_structured_output`; Product runtime binding resolves it against the selected adapter's authenticated capability set. This preserves provider-neutral Feature contracts while naming the concrete OpenCode capability truthfully. Raw-artifact-only describes file ownership, not an exemption from structured Agent results.

An adapter may advertise `opencode_structured_output` only for a pinned OpenCode version that passes admission, terminal observation, process-restart recovery, message-list, single-message, error, and repeated-read tests. The selected provider/model combinations must also accept the exact schemas used by all 33 Agent contracts within declared schema/result size limits. Unsupported versions or bindings advertise the capability as absent, and required contracts fail before external dispatch. Prompt-only JSON is not a silent fallback for a contract requiring structured output.

### Decision 3: The adapter uses the real OpenCode structured-result protocol

For every Agent contract, the adapter sends a prompt body with `format.type == "json_schema"` and `format.schema` equal to the authenticated `AgentResultT` schema document selected for the contract. The structured schema is no longer embedded as a “return only JSON” prose convention on the primary path. This exact object shape is normative.

An asynchronous admission response proves only that the prompt was accepted. Completion requires observing a terminal assistant message, rejecting any assistant error, and reading `info.structured`. Text parts, JSON substrings in prose, and arbitrary completed tool inputs are not accepted as the primary structured result.

The adapter owns protocol-shape checks, terminal error reduction, credential/canary rejection, and defense-in-depth validation against the same locked Agent-result schema. Kernel model validation into the installed `AgentResultT` remains authoritative even if OpenCode's internal tool and adapter have already validated the JSON value. Artifact document-model validation occurs in the Kernel, not in the adapter.

OpenCode's documented retry count is not treated as a durable retry guarantee until the pinned runtime proves it. Technical retry remains governed by the existing Attempt/activity policy.

### Decision 4: Agent contracts gain an explicit artifact contract

Every Agent contract declares an `ArtifactContract` even when all artifacts remain raw. An artifact contract is an immutable ordered collection of `ArtifactSlot` declarations plus an aggregate digest.

Each artifact slot declares at least:

- a stable slot identifier;
- content authority: Agent structured value, deterministic derived value, or Agent raw bytes;
- fixed or repeatable cardinality;
- an authenticated path or closed path resolver/template;
- create, replace, or bounded-repair mutation semantics;
- a file-mode authority: fixed mode or preserve an authenticated baseline mode;
- the document model and model identifier for typed slots;
- the artifact document schema identifier and `artifact_document_schema_digest` for typed slots;
- the media codec and versioned serializer identifier for typed slots;
- the selector or authenticated deterministic projector that obtains the document from validated Attempt data;
- size and collection bounds;
- a parity mode: exact bytes or an authenticated semantic migration record;
- the raw or typed namespace and applicable cross-reference validators.

Every path that affects read/write authorization is closed before `authorize_resources`. Dynamic variables may be derived only from validated `InputT` or from a pure, read-only artifact preflight snapshot that becomes part of `InputT` before the Attempt key and resource claims are finalized. `PreparedT` may not expand path authority. The model may choose only values from a contract-declared, validated finite domain; it may not supply an arbitrary logical path. Dynamic case paths and code-generation targets therefore enter the validated preflight/input closure rather than appearing for the first time in prepare or the final result.

Contract classification is derived from its slots: all typed slots means typed-artifact-only, all raw slots means raw-artifact-only, and a mixture means mixed-artifact. These terms classify artifact paths only; every Agent Attempt still returns a structured `AgentResultT`. A separate manually entered classification may be exposed for diagnostics but must equal the derived value.

### Decision 5: Models, schemas, projectors, and serializers use installed registries

The Kernel owns the internal `graph_engine.artifacts` package. Its focused modules are:

| Module | Responsibility |
| --- | --- |
| `contracts` | Immutable `ArtifactContract` and `ArtifactSlot` values |
| `registry` | Installed model, schema, projector, and serializer resolution |
| `codecs` | Versioned canonical JSON/YAML serializers |
| `materializer` | Typed document projection, validation, byte preparation, and staging installation |
| `receipts` | Per-artifact `MaterializationEntryReceipt` and aggregate `MaterializationReceipt` values |

Feature wheels contribute document models and authenticated deterministic projectors through the existing installed-wheel composition and owner-closure machinery. The Kernel contributes canonical serializer implementations; this is not a second schema-authority system parallel to the existing authenticated registry.

Boot verifies that every slot resolves to exactly one owner-authorized model/projector/serializer, that the model's canonical schema digest equals `artifact_document_schema_digest`, and that all artifact dependencies participate in the resolved contract and graph-build manifest. Feature-owned models/projectors are explicitly allowed to depend on framework-owned serializers through the installed Product dependency closure; an undeclared cross-owner contribution is rejected. The OpenCode envelope separately binds `agent_result_schema_digest`; the two digests are never interchangeable. Missing, extra, unauthorized, project-loaded, or digest-drifting entries fail before graph execution.

Both schema digests use one algorithm: lowercase hexadecimal SHA-256 over the existing framework canonical-JSON bytes of the exact immutable schema document stored by the registry. The registry constructs each schema document once; that same value is sent through the adapter, used for local validation, and hashed. Raw resource-file bytes, reparsed JSON with a different normalization, and independently regenerated schemas are not alternative digest authorities.

### Decision 6: `AgentResultT` carries values, not trusted file claims

For typed slots, `AgentResultT` carries the complete typed document data or the complete input required by the slot's deterministic projector. A plain `output_files` list is insufficient.

Raw output hints may remain in a result for user feedback or Capability logic, but they are not write-set authority. Kernel path resolution, staging scan, and seal remain authoritative. Model-supplied byte digests are not trusted and are unnecessary for Kernel-materialized files.

### Decision 7: Artifact projection occurs inside `materialize_artifacts`

Some current finalizers add fields that determine the authoritative document bytes. To preserve the required top-level order `materialize_artifacts → deterministic_finalize`, byte-determining logic moves into an authenticated Feature-owned artifact projector called by the materializer.

The materializer therefore performs one closed operation:

1. resolve the declared slots and paths;
2. select or deterministically project each document from validated `InputT`, prepared data, and `AgentResultT`;
3. validate the exact document model;
4. produce canonical bytes with the declared media codec and versioned serializer;
5. prepare an immutable aggregate manifest of expected paths and digests;
6. install the bytes idempotently into the typed portion of staging;
7. verify installed bytes and return one aggregate `MaterializationReceipt`.

Projectors are pure, deterministic, side-effect free, and may not read ambient project files, time, randomness, process identity, provider state, or network state. If a transformation needs authenticated workspace inputs, those inputs must already be represented in the validated Attempt input or prepared value.

The materializer reuses the existing workspace dirfd/containment, no-symlink, atomic-write, immutable-byte, and fencing primitives. It may not use an unauthenticated direct path write. Recovery of typed slots modifies only that Attempt's managed typed targets; it never clears or reconstructs unrelated raw staging output.

### Decision 8: Canonical serialization is versioned and byte deterministic

The media codec identifies the logical representation, initially JSON or YAML. The serializer identifier is the versioned byte contract, initially `canonical-json-v1` or `canonical-yaml-v1`. A separate independently mutable numeric version is forbidden because it would create two version authorities for the same bytes.

`canonical-json-v1` is the existing framework canonical-JSON byte algorithm over a normalized JSON value: UTF-8 without BOM, deterministic mapping-key order, deterministic compact separators and escaping, finite JSON numbers only, and no trailing newline. `canonical-yaml-v1` accepts only the same normalized JSON value domain and emits a YAML 1.2-safe subset: UTF-8 without BOM, deterministic mapping-key order, two-space block indentation, JSON-style quoted strings and escapes, lowercase null/boolean scalars, no directives/tags/anchors/aliases, LF line endings, and exactly one final newline. Values not covered by those profiles fail instead of falling back to library defaults.

A checked normative corpus covers every supported scalar/container class, Unicode/control characters, ordering, empty values, nesting, and rejected numeric/tag cases. The corpus digest is part of the serializer registry record. Serializer library or behavior changes require a new serializer identifier, corpus digest, and contract/build revision. They may not silently alter existing identifier output. Replaying the same validated document under the same slot contract must reproduce byte-identical output.

Every slot declares `parity_mode`. `exact_bytes` requires legacy and materialized bytes to match for the checked corpus/fixtures. `semantic_migration` requires a Feature-owned authenticated migration record containing the old/new representation identities, rationale, approved golden digests, and downstream compatibility tests; that record participates in the contract/build digest. An implementer cannot self-approve semantic drift at test time.

### Decision 9: Effective output paths have one authority

Before OpenCode dispatch, the resolved contract computes three path classes:

1. managed typed artifact outputs, written only by the Kernel;
2. Agent raw artifact outputs, written through the OpenCode staging boundary;
3. Attempt-internal input/scratch paths, which are explicit resource claims but can never be promoted as artifacts.

Pattern-level claims may have parent/child containment, as current generated-source roots do. The effective raw set is the closed raw claim set minus every resolved typed target and internal reserved path. After this subtraction, every promotable concrete path belongs to exactly one typed or raw authority. Any intersection or dynamic set that cannot be finitely resolved or proven closed before dispatch fails closed.

- typed targets are never present in effective OpenCode `allowed_outputs` or raw mutation permissions;
- typed-artifact-only contracts expose an empty effective raw output set;
- raw-artifact-only contracts retain the current OpenCode staging boundary and sealing behavior;
- mixed-artifact contracts expose only the effective raw set to OpenCode;
- duplicate slots, same-authority collisions, traversal, symlinks, and unbounded resolvers fail closed;
- a broad raw root cannot override a typed deny hole;
- raw attempts against typed paths must leave staging unchanged and remain observable in activity diagnostics.

Resource authorization covers all three classes before external dispatch. Every promoted output must be typed or raw; an internal scratch/input claim is never a third promotion authority. Kernel materialization does not create an undeclared write authority.

### Decision 10: The Kernel must observe the intermediate Agent result

The composite Agent executor can no longer hide `PreparedT` and `AgentResultT` by returning only `OutputT`. Resolved execution is a closed variant: a direct executor retains the existing one-step behavior, while a structured Agent executor exposes authenticated prepare, `PreparedT` model/schema, dispatch/adopt/observe activity, `AgentResultT` model/schema, artifact-contract, and deterministic-finalize ports to the Kernel. Product composition constructs that variant; Product runtime binding may select runtime/provider/model but may not alter Feature-owned prepared/result models, artifact slots, projectors, schemas, or serializers.

`graph-engine` consumes only its provider-neutral resolved executor protocol and does not import `agent-runtime-contracts` or name OpenCode in the Kernel trace. The OpenCode adapter implements the generic activity/structured-result port and contains all OpenCode `format` and `info.structured` knowledge.

The provider-neutral structured-executor phase bundle is singular: `prepare` returns an untrusted candidate for `PreparedT`; `dispatch_or_adopt_activity` returns the stable bound activity reference; `observe_activity` returns the untrusted structured JSON candidate or an existing activity resolution; and `finalize` receives validated `InputT`, `PreparedT`, `AgentResultT`, and aggregate `MaterializationReceipt` to produce an `OutputT` candidate. Artifact projection/materialization is Kernel-owned and is not another executor callback. No second composite abstraction with overlapping phase ownership is permitted.

The required happy-path trace is:

```text
adopt_or_create
→ authorize_resources
→ begin_workspace
→ prepare_or_recover
→ validate_prepared
→ persist_prepared
→ dispatch_or_adopt_agent_activity
→ observe_structured_agent_result
→ validate_agent_result
→ persist_structured_result
→ prepare_materialization
→ persist_materialization_manifest
→ install_typed_artifacts
→ persist_materialization_receipt
→ deterministic_finalize
→ validate_output
→ seal_candidate
→ run_validators
→ durable_prepare
→ promote_or_recover
→ settle_effects
→ publish_receipt
```

Direct non-Agent Attempt contracts continue to use their existing executor path and do not pay for unused structured-artifact phases.

The Feature graph's validated input selection and pure artifact preflight close all path-affecting values before this trace and before the `AttemptKey` is computed. `prepare_or_recover` may read only authorized inputs and cannot expand resource claims.

### Decision 11: Structured results and materialization are durable recovery boundaries

Prepare is deterministic and free of external effects. Its validated `PreparedT` is stored as a canonical, content-addressed snapshot with `prepared_schema_digest` and `prepared_digest`, and a durable journal reference is anchored before Agent dispatch. A crash before that anchor may rerun prepare from the same validated `InputT` and authenticated workspace inputs; after the anchor, recovery uses the stored snapshot. Projector and finalizer input closure binds the prepared digest.

After authoritative `AgentResultT` validation, the Kernel similarly stores a canonical, content-addressed structured-result snapshot with `agent_result_schema_digest` and appends a durable reference to the Attempt journal before materialization. Large prepared/result content is not duplicated unboundedly in journal events.

Before installing the first typed file, the Kernel deterministically prepares the complete immutable materialization manifest containing all resolved targets, prepared/result/projector/serializer identities, expected document digests, byte digests, sizes, and mode policies. It stores the manifest and materialized byte blobs in the Attempt-private content-addressed store, then anchors `MaterializationPrepared` with the manifest digest under the active fence. Only after that durable boundary may installation modify typed staging paths. Installation is replayable and idempotent. `MaterializationCompleted` anchors the aggregate `MaterializationReceipt` before post-materialization finalization may be treated as complete.

Once an OpenCode activity has been bound, recovery never issues another prompt for the same Attempt merely because a later Kernel phase crashed. If OpenCode completed but the structured result was not durably stored, recovery adopts and re-observes the same bound session. If that result cannot be reconciled, the Attempt becomes pending or indeterminate according to the existing activity protocol; it is not silently redispatched.

After the prepared and structured-result references are durable, materialization and finalization require no OpenCode call. Attempt-private snapshots are permission-restricted, subject to contract size/cardinality limits, never contain accepted secrets/canaries, and remain retained while any nonterminal Attempt, receipt, or export reference exists. Garbage collection is allowed only after terminal retention policy proves the blobs unreachable.

### Decision 12: Post-materialization finalization is deterministic and cannot change bytes

The post-materialization finalizer receives the prepared value, validated `AgentResultT`, resolved typed documents or their typed references, and `MaterializationReceipt`. It produces `OutputT` and business receipts without reparsing Kernel-materialized JSON/YAML files.

It cannot write staging files, modify a materialized document, change a materialized path, or recompute the file shape through a second parser. Existing finalizer logic that determines document bytes moves to the slot projector. Logic that authenticates raw artifacts, computes business results, derives receipt references, or checks cross-artifact semantics may remain, provided it is deterministic and side-effect free.

After the Agent activity becomes terminal, the workspace is closed to further Agent writes. Mixed/raw finalizers may read only their declared raw artifacts through the existing workspace boundary. Seal remains the final authority over all typed and raw bytes.

### Decision 13: Receipts bind object semantics to committed bytes

The receipt model has exactly two levels:

- one `MaterializationEntryReceipt` for each resolved concrete artifact path, including every item of a repeatable slot;
- one aggregate `MaterializationReceipt`, containing all entries in stable `(slot_id, logical_path)` order plus the immutable manifest digest and aggregate receipt digest.

Each entry binds at least:

- Attempt key and artifact-contract digest;
- structured-result digest and `agent_result_schema_digest`;
- slot identifier and resolved logical path;
- artifact document model/schema identifier and `artifact_document_schema_digest`;
- media codec and versioned serializer identifier;
- canonical document-object digest;
- final byte digest, size, resolved file mode, and the slot's fixed/preserve-baseline mode policy.

The aggregate binds the prepared digest, structured-result digest, sorted entry digests, manifest digest, and its own receipt digest. The journal and post-materialization finalizer reference the aggregate receipt. The terminal Attempt receipt references the aggregate digest alongside the existing sealed-write, validation, promotion, and effect receipts. Seal verifies that each typed path's actual staging bytes match its entry. A finalizer or later phase cannot substitute different bytes under the same aggregate receipt.

### Decision 14: Existing Attempt resolutions remain closed

This design introduces no new graph-visible resolution type.

- unsupported capability or registry/schema drift fails before dispatch;
- invalid/missing OpenCode structured output follows the existing Agent execution failure and retry policy;
- local `AgentResultT` or document-model rejection produces no typed file;
- configuration errors such as overlapping slots or unbounded resolvers are permanent failures;
- recoverable I/O interruption during materialization is resumed under the same Attempt key;
- unreconciled bound activity or storage publication uses the existing pending/indeterminate system-interrupt path;
- no incomplete materialization may reach durable prepare or promotion.

### Decision 15: Classification is slot-first and mandatory for all contracts

The 33-contract migration begins with a machine-readable artifact inventory. Every one of the 34 Agent occurrences must resolve through one of the 33 classified contracts. Every declared resource write, fixed output template, dynamic case path, and generated-source root must belong to exactly one slot or to an explicitly retained non-artifact workspace claim.

The provisional 15 typed-artifact-only / 17 mixed-artifact / one raw-artifact-only suffix profile is only a review queue. Final classification depends on content authority, path cardinality, mutation semantics, available document model, size, formatter/test needs, and whether a deterministic projector can replace current finalizer-derived bytes.

The inventory must explicitly handle:

- Intake contracts whose current result is only an artifact list;
- dynamic case-design and repair paths, including bounded repair semantics;
- Generation plans that do not yet contain every output document body;
- generated source roots and files that require formatter/LSP/test interaction;
- current finalizers that add status, mapping, candidate, receipt, round, coverage, or public-outcome fields;
- the Quality Markdown report raw-artifact-only candidate.

Not every JSON/YAML candidate must become typed. A justified raw slot is valid. No contract may remain unclassified at Feature freeze.

Current skills that require the model to raw-write a JSON/YAML file, read it back, and then return only an `output_files` receipt change semantics for migrated typed slots. For those slots, the complete typed document in `AgentResultT` becomes the pre-materialization authority, and the Kernel writes the file only after the OpenCode session is terminal. The same session therefore cannot format, test, reread, or consume that Kernel-generated file. A contract with a genuine same-session dependency on the file must retain a raw slot or receive a separate approved design; it cannot be marked typed by suffix alone.

### Decision 16: Only duplicated format handling is removed

After a typed slot is migrated and its parity tests pass, Capability code may remove:

- repeated `read_bytes()` calls used only to recover the typed document;
- repeated `json.loads()` or `yaml.safe_load()` used only for shape reconstruction;
- duplicate Pydantic required/type/enum/additional-properties validation of the same materialized model;
- prompt instructions whose only purpose is maintaining JSON/YAML syntax;
- model-reported `output_files` fields that no longer carry business meaning or raw-output hints.

The following remain:

- Kernel validation of untrusted `AgentResultT` and typed document models;
- path authorization, traversal/symlink/TOCTOU defenses, and resource arbitration;
- final staging scan and seal;
- semantic, cross-file, mapping-closure, and business-rule validators;
- tests and Eval;
- raw artifact authentication;
- durable prepare, promotion/recovery, effects, and terminal receipts.

### Decision 17: LangGraph topology and the Transaction tail do not change

The migration adds zero LangGraph nodes and zero graph edges. It does not change join, fanout, subgraph, interrupt, activation, retry-loop, or route semantics.

The six effect kinds and their ordering, idempotency, apply/reconcile, pending, and committed-effect-failure semantics are unchanged. Transaction-tail ordering and graph-visible resolution semantics remain unchanged; earlier journal phases, seal inputs, typed-byte receipt reconciliation, and validator context are extended by the structured pipeline.

Graph and Product revisions may change because authenticated contract, model, projector, adapter, or serializer source digests change. Topology cardinality remaining constant does not imply revision identity remains constant.

### Decision 18: The migration is a new Attempt-freeze subphase

The master migration adds a dedicated Structured Artifact Pipeline subphase inside Checkpoint B, after the base Attempt journal/workspace primitives exist and before Feature freeze is allowed.

Checkpoint B cannot close until:

- the qualifying OpenCode version and adapter capability are pinned;
- artifact registries, serializers, materializer, receipts, and recovery are green;
- all 33 contracts and 34 occurrences are classified;
- typed/raw path closure is proven;
- representative typed-artifact-only, raw-artifact-only, and mixed-artifact framework paths pass end to end;
- every slot finally classified as typed has completed its schema/result, skill, projector, finalizer, permission, recovery, and parity migration; any slot not completing that evidence remains finally classified as raw;
- all structured-result/materialization crash cuts pass without duplicate dispatch;
- Feature finalizers that determine document bytes have been split into pre-materialization projectors and post-materialization finalizers.

Only then may Checkpoint C Feature freeze proceed.

### Decision 19: Two earlier conclusions are explicitly superseded

The original plan's provider-native structured-output wording is replaced by OpenCode internal `StructuredOutput` tool semantics and the capability name `opencode_structured_output`. The implementation documentation revision must update these exact anchors:

- Semantic Attempt Kernel Task 3's opaque composite/provider-schema capability and Task 8's happy-path trace/crash windows;
- Python-native LangGraph Assurance sections “Attempt Kernel Transaction” and “Structured Agent Output”;
- LangGraph Assurance Boot/Runtime section “OpenCode structured output narrows only the adapter”;
- the master migration's Checkpoint B gate.

The research spike's recommendation for a product-owned custom Schema writer plugin is superseded by this design. No planned production plugin is being deleted because none was part of the accepted implementation plan. The research document must retain the spike as historical evidence but mark its recommendation as superseded by `Structured Output → Kernel ArtifactMaterializer`.

### Decision 20: This design does not add cryptographic attestation

The recently evaluated in-toto, DSSE, signing-key, threshold-functionary, and independent cryptographic-verifier ideas are outside this scope. Existing local digests and receipts retain their current trust meaning.

## 9. Attempt Data Flow

```text
LangGraph semantic Agent node
        │ stable AttemptKey + typed InputT
        ▼
AssuranceAttemptKernel
        │ prepare
        ▼
OpenCode adapter
        │ format.type=json_schema + format.schema
        │ terminal assistant info.structured
        ▼
untrusted structured candidate
        │ Kernel validates AgentResultT
        │ durable content-addressed snapshot
        ▼
ArtifactMaterializer
        │ resolve slots and paths
        │ deterministic Feature projector where required
        │ validate DocumentT
        │ canonical-json-v1 / canonical-yaml-v1
        ▼
typed staging bytes + MaterializationReceipt
        │
        ▼
deterministic Capability finalizer
        │ OutputT
        ▼
validate output → seal all typed/raw bytes → validators
        │
        ▼
durable prepare → promote/recover → effects → Attempt receipt
        │
        ▼
LangGraph receives committed/rejected/pending resolution
```

## 10. Recovery Model

| Crash cut | Durable/recoverable source | Required recovery behavior |
| --- | --- | --- |
| Prepare complete, prepared snapshot not yet durable | Validated `InputT`, authenticated workspace inputs, no bound Agent activity | Deterministically rerun prepare, require the same prepared digest if previously observed, then anchor the snapshot before dispatch |
| OpenCode terminal, structured result not yet durable | Bound activity/session identity and OpenCode terminal message | Adopt and re-observe the same session; never issue a second prompt; pending/indeterminate if the same session cannot be reconciled |
| Structured result durable, materialization manifest not yet durable | Canonical prepared/result snapshots and journal references | Recompute the same manifest, anchor it and its prepared bytes, then install; do not contact OpenCode |
| Manifest durable, before first typed install | Canonical prepared/result snapshots plus anchored immutable manifest | Install the manifest exactly; no new projection or schema/serializer selection is allowed |
| During multi-file materialization | Anchored immutable manifest and materialized byte blobs | Idempotently install or verify every typed target without clearing raw staging; reject conflicting drift; do not expose a completed receipt until the full set matches |
| Materialization complete, before finalizer | Durable aggregate `MaterializationReceipt` plus prepared/result snapshots | Verify receipt/staging agreement and run deterministic finalizer |
| Finalizer complete, before seal | Durable prepared/result/materialization phases | Re-run deterministic finalizer and require identical `OutputT`, then seal; finalizer output has no separate optional anchor in this design |
| Seal and later phases | Existing sealed candidate, durable prepare, promotion/effect receipts | Use the unchanged Attempt recovery matrix |

For every row, external dispatch count for the Attempt remains one after the OpenCode activity has been bound. Recovery may observe, reconcile, or adopt that activity; it may not create a new activity to hide uncertainty.

## 11. Capability Migration Requirements

The migration produces a reviewed artifact catalog with one row per slot and at least these columns:

| Field | Purpose |
| --- | --- |
| Contract and occurrence IDs | Close the 33-contract / 34-occurrence inventory |
| Slot ID and cardinality | Identify fixed and repeated outputs |
| Content authority | Distinguish Agent structured, deterministic derived, and Agent raw content |
| Path resolver/template | Prove resource and namespace closure |
| Document model and schema digest | Bind typed semantics |
| Media codec and versioned serializer | Bind deterministic bytes without duplicate version authorities |
| Mutation mode | Distinguish create, replace, and bounded repair |
| Projector/source | Identify whether bytes come directly from `AgentResultT` or deterministic Feature projection |
| Raw/typed authority | Drive OpenCode permissions and Kernel materialization |
| Size/collection bounds | Prevent unbounded structured results and checkpoint growth |
| Semantic validators | Preserve business and cross-reference checks |
| Migration disposition and test IDs | Prove that every slot, skill, projector, finalizer, permission rule, and parity result is closed |
| Parity result | Record exact byte digests or the authenticated semantic-migration record and golden digests |

Migration proceeds slot by slot, not extension by extension. A contract becomes typed-artifact-only or mixed-artifact only after all affected skills, result models, projectors, finalizers, permissions, receipts, and parity tests change together. The three representative mode tests prove framework behavior but cannot close an untested catalog row.

For bounded repair of an existing typed document, the contract must choose one explicit semantic strategy: return a complete authoritative post-image typed document, or return a closed typed patch that an authenticated deterministic projector applies to an authenticated baseline. Raw whole-file rewrite is not implicitly converted into typed replacement.

## 12. Testing Decisions

Tests assert observable contract behavior at the highest available seam. The primary seam is a resolved Agent contract executed through `AssuranceAttemptKernel.execute_or_recover(...)`. Lower-level adapter, serializer, registry, and materializer tests exist only where a failure cannot be diagnosed through that seam.

### 12.1 Adapter contract tests

- Verify the exact authenticated schema is sent through `format.type == "json_schema"` and `format.schema`.
- Verify success is read from terminal assistant `info.structured`.
- Verify asynchronous 204 admission is not treated as completion.
- Verify message-list and single-message reads survive server and adapter restart.
- Verify repeated observation returns the same structured value.
- Verify missing tool output, malformed tool arguments, structured-output error, content filtering, length, abort, context, and API failures cannot become success.
- Verify unsupported versions advertise no `opencode_structured_output` capability and required contracts fail before dispatch.
- Verify every selected provider/model binding accepts the authenticated schemas used by its contracts and enforces declared schema/result size bounds.
- Verify no test relies on unproven OpenCode retry-count behavior.

### 12.2 Contract and Boot tests

- Assert exactly 33 classified contracts and 34 resolved Agent occurrences.
- Assert every declared write template/root maps to exactly one typed/raw slot or an explicit non-artifact claim.
- Assert dynamic case/codegen resolvers are closed and bounded.
- Assert effective concrete typed/raw output sets are disjoint after typed deny holes and internal reserved paths are applied; unresolved pattern intersections fail.
- Assert `prepared_schema_digest` and the canonical `PreparedT` snapshot participate in the resolved executor and recovery closure.
- Assert `agent_result_schema_digest` equals the authenticated `AgentResultT` schema used for OpenCode and local validation.
- Assert each `artifact_document_schema_digest` equals its installed document model's canonical schema.
- Assert missing, extra, cross-owner, and drifted models/projectors/serializers fail Boot.
- Assert artifact contracts and registry digests participate in the resolved contract and build manifest.

### 12.3 Codec and materializer tests

- Golden-test `canonical-json-v1` and `canonical-yaml-v1` bytes, including Unicode, ordering, null/boolean/numeric values, line endings, and rejected unsupported values.
- Assert the checked normative corpus digest equals the serializer registry record and changes only with a new serializer identifier.
- Prove repeated and cross-process serialization is byte identical.
- Prove invalid documents create zero typed files.
- Prove duplicate paths, traversal, symlinks, same-authority collisions, excessive size/cardinality, and unresolved effective typed/raw intersections fail closed.
- Prove multi-file preparation yields one immutable aggregate manifest and one completed receipt only after every byte matches.
- Prove receipt object digests and byte digests agree with the later seal.

### 12.4 Kernel and recovery tests

- Assert the exact expanded happy-path trace in Decision 10.
- Inject all seven structured-pipeline crash cuts from the recovery matrix, including the five user-mandated cuts plus prepared-snapshot and manifest-before-first-install boundaries.
- Assert recovery never dispatches OpenCode twice after activity binding.
- Assert recovery from a durable structured result performs no OpenCode call.
- Assert materialization replay is idempotent and no partial typed set reaches promotion.
- Assert contract, result schema, document schema, media codec, serializer, path, result, or graph-revision drift fails replay.
- Assert stale fencing tokens cannot persist a structured result, materialize, finalize, seal, prepare, promote, settle effects, or publish a receipt.

### 12.5 Capability parity tests

- Exercise at least one typed-artifact-only, one raw-artifact-only, and one mixed-artifact contract through the real Adapter/Kernel seam.
- For every one of the 33 contracts and every typed slot, execute the catalog-linked skill/result/projector/finalizer, permission, recovery, and exact-byte or semantic-migration parity tests; no representative test substitutes for this closure.
- Add dedicated tests for dynamic Intake case paths and Generation codegen roots rather than relying only on representative contracts.
- Prove migrated finalizers consume typed values/receipts and do not reread/reparse typed files.
- Prove migrated skills no longer instruct OpenCode to write/read back managed typed paths, and prove no migrated contract depends on consuming a Kernel-generated typed file during the same OpenCode session.
- Compare canonical legacy and new output bytes where legacy output was deterministic; otherwise compare typed business semantics plus the approved serializer migration.
- Preserve current business validators, cross-reference failures, tests, Eval outcomes, and downstream `OutputT` projections.
- Prove raw source, Markdown, patch, formatter, LSP, and test workflows retain their existing staging/seal behavior.

### 12.6 Security tests

- Attempt typed-path mutation through OpenCode `write`, `edit`, `apply_patch`, shell, MCP, and custom tools; assert no typed staging byte changes.
- Assert typed-artifact-only contracts expose zero raw output paths.
- Assert mixed-artifact raw writes succeed only inside raw slots and unexpected files are rejected by seal.
- Assert model-supplied paths cannot escape closed slot resolvers.
- Assert project `.aa` and SUT files cannot register or replace artifact models, schemas, projectors, media codecs, or serializers.

### 12.7 Regression gate

The full existing CI gate remains required: Ruff lint and format, Pyright, import-linter, complete pytest, and the product wheel smoke test. Existing Attempt, legacy runtime, staged-promotion, activity-recovery, effect, system-interrupt, Feature contract, and Product integration suites remain green throughout the migration.

## 13. Acceptance Criteria

- [ ] No LangGraph node or edge is added for structured observation, materialization, or finalization.
- [ ] `AssuranceAttemptKernel.execute_or_recover(...)` remains the only transaction entrypoint.
- [ ] The adapter capability is named `opencode_structured_output`; no provider-native guarantee is claimed.
- [ ] A pinned OpenCode version passes the full asynchronous/recovery structured-output integration gate.
- [ ] All selected provider/model bindings pass the exact 33-contract schema compatibility and size-limit matrix.
- [ ] The exact 33-Agent-contract set declares `requires_structured_output`; raw-artifact-only does not create an exception.
- [ ] All 33 Agent requests send `format.type == "json_schema"` plus the authenticated `format.schema`, and successful results are consumed from `info.structured`.
- [ ] Kernel validation remains authoritative after OpenCode returns a candidate.
- [ ] The artifacts package provides immutable contracts, authenticated registry resolution, canonical JSON/YAML serializers, deterministic materialization, and receipts.
- [ ] Exactly 33 contracts and 34 occurrences close in the classification inventory.
- [ ] All 32 structured-candidate contracts receive an explicit slot decision; none is classified solely from its suffix.
- [ ] Every final typed slot has catalog-linked schema/result, skill, projector, finalizer, permission, recovery, and parity evidence; every non-migrated slot is explicitly final raw.
- [ ] Every promotable output path/root has exactly one typed/raw authority, including dynamic case and codegen roots; internal scratch/input claims are explicit and non-promotable.
- [ ] Typed paths are inaccessible to OpenCode raw mutation channels.
- [ ] Migrated skills treat `AgentResultT` as the pre-materialization authority and contain no typed-path raw write/read-back protocol.
- [ ] Materialization of the same validated document is byte identical under the same media codec and serializer identifier.
- [ ] Finalizers cannot alter materialized bytes and do not duplicate typed-file parsing/shape validation.
- [ ] `MaterializationReceipt` and terminal Attempt receipt bind result/document schemas, media codec, serializer, object, and byte digests.
- [ ] All seven structured-pipeline crash cuts recover without duplicate OpenCode dispatch or partial promotion.
- [ ] Existing seal, validators, durable prepare, promotion/recovery, six effects, system interrupts, and receipt semantics remain green.
- [ ] The custom Schema writer plugin recommendation is marked superseded in research documentation; no production plugin is introduced.
- [ ] Checkpoint B includes and passes the Structured Artifact Pipeline subphase before Feature freeze.

## 14. Out of Scope

- Adding or changing LangGraph topology, joins, subgraphs, fanout, routing, or interrupt semantics.
- Replacing the Attempt transaction, workspace seal, validator pipeline, promotion protocol, effect settlement, or crash recovery.
- Claiming or requiring provider-native `response_format` behavior underneath OpenCode.
- Carrying a private OpenCode source fork.
- Adding a custom OpenCode Schema-writer plugin as a trusted artifact authority.
- Forcing source code, Markdown, patches, formatter-dependent content, or large generated trees through typed structured output when their slot remains raw.
- Loading project-defined Python, schemas, document models, media codecs, serializers, projectors, handlers, or validators from the SUT.
- Introducing new production validator bindings; the existing registered-versus-bound baseline remains a separate behavior decision.
- Introducing in-toto, DSSE, signature keys, threshold functionaries, transparency logs, or an independent cryptographic verifier.
- Providing global ACID semantics across external effect providers.

## 15. Consequences

### Benefits

- Typed JSON/YAML production becomes deterministic Kernel behavior rather than prompt discipline.
- The adapter stops relying on prose JSON extraction for required structured results.
- Recovery gains a durable boundary before file generation and can avoid repeated model execution.
- Typed-file parsing and shape-validation boilerplate can be removed from Capability finalizers.
- Receipts connect typed object semantics to actual committed bytes.
- Raw files remain supported without weakening the typed path.

### Costs

- All 33 Agent contracts require slot-level inventory and coordinated migration.
- Result models must grow from file manifests into complete typed document payloads or deterministic projector inputs.
- Existing finalizers that determine artifact bytes must be split into pre-materialization projectors and post-materialization result finalizers.
- Canonical YAML behavior must be specified and maintained as a versioned compatibility surface.
- Structured results may be larger, requiring explicit size/cardinality limits and content-addressed durable storage.
- OpenCode cutover remains blocked until a pinned release passes the asynchronous recovery contract.

## 16. Further Notes

This design amends the following accepted artifacts:

- [Python-native LangGraph Assurance design](./2026-08-31-python-native-langgraph-assurance-design.md), especially Semantic Attempt Contracts, Attempt Kernel Transaction, and Structured Agent Output.
- [Semantic Attempt Kernel implementation plan](../plans/2026-08-31-semantic-attempt-kernel.md), especially the composite executor and Kernel transaction tasks.
- [Python-native LangGraph migration plan](../plans/2026-08-31-python-native-langgraph-migration.md), by adding the Structured Artifact Pipeline subphase to Checkpoint B.
- [OpenCode structured-output research](../../research/2026-08-31-opencode-structured-output-json-schema.md), whose custom-writer recommendation is retained as research history but superseded for production architecture.

This is a design specification, not the implementation plan. The implementation plan must decompose the work into adapter capability/version gating, artifact foundation, Kernel recovery phases, three-mode tracer migration, all-Feature migration, documentation correction, and Checkpoint B closure.
