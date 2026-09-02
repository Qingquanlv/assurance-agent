# Structured Artifact Pipeline Resume Tranche Implementation Plan

> ## CANCELLED / SUPERSEDED — Historical Record Only
>
> **Effective 2026-09-02:** this plan is permanently cancelled and superseded by
> [Permanent Raw Agent Runtime Cutover](../specs/2026-09-02-raw-agent-runtime-cutover-design.md)
> and its replacement implementation plan,
> [Raw Agent Runtime Closure](./2026-09-02-raw-agent-runtime-closure.md).
>
> The text below is retained only as decision history. **Every checkbox in this file is
> non-authoritative and non-executable**: do not use it to start work, infer current program
> status, define a gate, or make a release claim.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the already-implemented semantic Attempt transaction so every Agent returns one authenticated structured result, the Kernel deterministically materializes typed JSON/YAML artifacts, raw artifacts remain explicitly Agent-owned, and all 33 Agent contracts close before Agent-dependent Product cutover resumes.

**Architecture:** This is a three-child-plan retrofit inserted after Product T5a at `feat/python-native-langgraph-migration@4a9cd197` and before T5b, but it first passes a standalone Checkpoint S0 investment gate. The OpenCode child owns that minimal eligibility probe plus later transport and certification, the Artifact/Kernel child upgrades the existing provider-neutral contracts, deterministic bytes, durable recovery, and receipts, and the Capability child migrates the exact 33-contract/34-occurrence set already referenced by Python `StateGraph` factories. LangGraph topology and the transaction tail remain unchanged; `AttemptNodeFactory` remains the only graph-facing seam and `AssuranceAttemptKernel.execute_or_recover(...)` remains the only transaction-facing entrypoint.

**Tech Stack:** Python 3.11, Pydantic v2, LangGraph `1.2.11`, OpenCode HTTP structured output, SHA-256 content addressing, canonical JSON, a deterministic YAML 1.2-safe subset, the existing fenced Attempt journal/workspace transaction, pytest, Ruff, Pyright, import-linter, and the `uv` workspace.

**Spec:** `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`

## Global Constraints

- Execute from a clean isolated continuation worktree created from `4a9cd197` or an explicitly reviewed successor containing the complete T5a series. The planning worktree contains user-owned OpenCode adapter, workspace-boundary, test, `.superpowers`, `.vscode`, and benchmark changes; never stash, copy, overwrite, stage, or reconstruct them as migration work.
- `2026-08-31-python-native-langgraph-migration.md` is the only authority for program state, dependency order, cutover, and stop conditions. This document owns only the Structured resume tranche's interfaces and task interleave; it does not overlay, reopen, or replay completed Foundation, Semantic Attempt, Feature, or Product Tasks 1–4. Capability Task 10 synchronizes stale terminology and the approved 25-stage trace, pure `prepare(InputT)`, and blob-backed raw-finalizer seam before source work continues.
- OpenCode Task 0 and Checkpoint S0 are an investment gate ahead of this retrofit. Until S0 is green, do not execute OpenCode Tasks 1–7, Artifact Tasks 1–10, Capability Tasks 1–12, or modify any production Adapter, Kernel, artifact, Agent-contract, Feature, or Product implementation for Structured Output. S0 output is non-promotable and never advertises `opencode_structured_output`.
- `StructuredAgentActivityPort` is defined exactly once by OpenCode child Task 1 in `graph_engine.attempts.structured_activity`; Artifact/Kernel Task 5 consumes it and must not redeclare it.
- The Feature requirement is exactly `requires_structured_output`; the concrete adapter capability is exactly `opencode_structured_output`. Core and Feature code do not name OpenCode.
- The closed executor variants are exactly `ResolvedDirectExecutor` and `ResolvedStructuredAgentExecutor`. Do not retain `CompositeAttemptExecutor` as a parallel authority.
- The normative artifact types are exactly `ArtifactContract`, `ArtifactSlot`, `MaterializationEntryReceipt`, and `MaterializationReceipt`.
- No structured observation, materialization, or finalization LangGraph node or edge is added. Artifact/OpenCode children introduce no Workflow YAML, GraphDef, scheduler, or generic phase-bundle abstraction. Capability Intake/Generation may amend only compatibility input projections in `module.yaml` while the ten Agent-dependent entrypoints still run on `legacy-v2`; those files are not LangGraph authority, may not add/remove a node, edge, route, join, interrupt, or topology, and remain deletion targets in Product Task 8.
- OpenCode is an untrusted producer. In production R1–R5, a request sends the authenticated Agent-result schema as `format.type == "json_schema"` and `format.schema`; success is read only from terminal assistant `info.structured`; Kernel model validation remains authoritative. Task 0 is the sole pre-production exception: it sends one fixed local canary schema, validates only that canary locally, and authorizes no artifact or runtime binding.
- OpenCode `1.18.4` remains a historical negative compatibility fixture. Official `v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661` is the current `message-roundtrip-red` fixture: asynchronous schema admission returns `204`, while V1 list and single-message reads return `400`. A later exact release becomes **S0-eligible** only after the no-model `204 -> 200 -> 200` fail-fast probe, one real provider/model/minimal-schema terminal `info.structured` result, and an operator restart followed by repeated reads of the same result with no second prompt. That non-promotable eligibility does not imply compatibility. Production compatibility still requires the later automated controller restart, repeated-read, terminal-error, exact-schema, size-bound, boundary, recovery, provider/model, and complete 33-contract certification suites. No future version is manually declared eligible or compatible.
- All 33 Agent contracts declare `requires_structured_output=True`, including the raw-artifact-only contract. The exact occurrence count is 34 because `assurance.intake.agent.case-design.v1` occurs twice.
- Slot classification is fail-closed. The source inventory begins with all 69 slots as explicit raw rows; the 69th is Healing coverage-repair's repeatable exact-file raw repair set resolved from `CoverageRepairBrief.allowed_test_files`, which the legacy behavior already edits. An individual row changes to typed only in the same atomic commit as its result/document models, schema, skill, projector, finalizer, permissions, recovery, and parity evidence.
- Every effective promotable path has exactly one authority: Kernel-managed typed bytes, Agent-managed raw bytes, or neither. Typed paths are deny holes in every OpenCode filesystem mutation channel, including beneath a broad raw root.
- Every path-affecting value closes from authenticated `InputT` before resource authorization and `AttemptKey` derivation. A Feature that needs a read-only path preflight must place its authenticated snapshot inside validated `InputT`; all 33 current contracts resolve paths from `InputT` alone. `PreparedT`, `AgentResultT`, the model, project configuration, and finalizers cannot expand authority.
- Agent-readable SUT source/test/config scope is a required sorted public `sut_read_paths` tuple propagated into validated `InputT`; Kernel captures its authorized claims into one immutable input snapshot and rejects sensitive-path policy matches. Feature contracts declare only provider-neutral structured-toolchain/network requirement IDs plus full canonical command-secret requirements (aliases, purpose, delivery/encoding, target classes and byte bound), never handles or values. Exactly the two Execution contracts declare the four semantic requirements `sut-api-test-runner-v1`, `sut-e2e-test-runner-v1`, `sut-fuzz-test-runner-v1`, and `sut-performance-benchmark-runner-v1`; the other 31 rows—including all Generation rows—declare no command/toolchain authority. Each Execution `InputT` also carries a core-owned callable-free invocation selection: exact selected test node IDs or one benchmark file plus bounded integer parameters. `bind_attempt_dispatch(...)` freezes it into `BoundStructuredToolchainInvocationScope` before the key. Product resolves the requirements to one qualified profile, while the broker accepts only an exact bound logical-recipe/typed-parameter row, derives argv/environment internally, and permits exactly one command per selected recipe; model-supplied argv is forbidden. Scope/profile/executable/dependency/image/environment/limit/policy/target/alias/qualification digests bind ProductLock, dispatch/key, workspace/activity/broker, command store/view/receipts, and terminal receipt. No live project, arbitrary venv/cache, ambient endpoint/credential, Feature/project-provided command/network/secret backend, generic shell grant, or inferred formatter/LSP/test permission is mounted.
- The exact contract classifications are `10 input_conditioned / 23 none` for network and `2 input_conditioned / 31 none` for command secrets. The eight Generation network-capable contracts use only installed `sut-openapi-read-v1`; the two Execution contracts select installed backend/frontend test requirements and may select `sut-admin-credential-v1`. OpenAPI access is a Product-issued synthetic-origin L7 read proxy limited to GET/HEAD/OPTIONS and frozen OpenAPI paths, with no CONNECT, upgrade, redirect escape, write method, raw host/IP, package-registry egress, or unverified upstream TLS. Command-secret delivery is one-shot, write-only, exact-target-scoped, metadata-receipted, and never adapter/model readable.
- The public Product input's network/auth authority surface contains only `sut_network_mode`, opaque `sut_network_target_ids`, `sut_auth_mode`, and opaque `sut_command_secret_requirement_ids`, beside the required bounded `sut_read_paths` and existing business fields. The Product parser extra-forbids private selection rows. Before graph dispatch it creates an internal `ResolvedProductInputV1` envelope containing the validated public value plus private canonical per-requirement selection rows/digest; `_plan_start` anchors that envelope as root input and recovery reuses it without catalog re-resolution. Feature projections can narrow to declared requirements but cannot enumerate or expand catalog authority.
- `AttemptNodeFactory` is the only pre-key binder: validate `InputT` -> resolve total and explicit non-artifact `ResourceClaims` -> bind artifact paths/policies -> construct `BoundAttemptDispatch` -> derive `AttemptKey`. Typed slots, raw slots/roots, and declared non-artifact input/scratch claims must exactly account for write/exclusive authority. The key binds the input, resource-claim, and bound-artifact digests; the Kernel authenticates and consumes the bound value and never resolves paths again. Baseline existence/content/mode is not path authority and does not perturb the stable key: the Kernel's `begin_workspace` captures or reopens one durable authenticated baseline before prepare/activity, and recovery never substitutes a new one.
- Serializer identifiers are exactly `canonical-json-v1` and `canonical-yaml-v1`. A byte-behavior change requires a new identifier and normative corpus; there is no second mutable serializer-version field.
- Structured prepare and finalization never receive the full `AttemptExecutionContext`. Every prepare dependency, including prior file-derived business evidence, is hydrated into validated canonical `InputT` before binding/key derivation, so prepare has the exact signature `prepare(InputT)`; finalization receives only `StructuredFinalizeInput`. Neither surface exposes activity, effect, secret, snapshot-admission, journal, fence, workspace read/write, host-path, or clock authority. The Kernel owns fence checks around both pure calls.
- Every Attempt-private prepared/closure/result/raw/typed document/byte/manifest/receipt blob passes one provider-neutral secret/canary admission port before persistence. Large raw captures use its bounded streaming form. Resource authorization first anchors an opaque, versioned secret-generation-set digest supplied by the host; every admission receipt binds that digest and policy version. Production obtains the HMAC generation key only from an owner-only, no-follow absolute key file plus explicit key ID—not the worktree, database, environment, prompt, or Attempt data. Restart must prove the exact generation before any new blob/event, and plain/unsalted secret-value digests are forbidden. The initial store has no delete/GC API and retains all journal/receipt-reachable blobs indefinitely.
- Kernel, not the adapter, registers the lifetime's snapshot-admission and command-injection capabilities directly with the Product command broker under a stable pre-dispatch activity reference. The adapter sees only an opaque broker-binding digest in a minimal activity context. Running/pending may leave Kernel only with authenticated durable tool-production quiescence; a dispatch pending without it is legal only when remote non-acceptance and absence of any tool namespace are proven. Acceptance uncertainty, lost quiescence acknowledgement, or failed reattachment is indeterminate, never a reason to close capabilities and reprompt.
- Existing seal/validator/durable-prepare/atomic-promote/recover outcomes, system interrupts, closed `AttemptResolution` values, and terminal transaction semantics do not change; structured v2 internally freezes immutable promotion sources before durable prepare so mutable Agent staging is never a promotion source. The effect set is exact and frozen: `assurance.healing.effect.allocation.v2`, `assurance.healing.effect.heal-apply.v2`, `assurance.healing.effect.proposal-approved.v1`, `assurance.improvement.effect.archive.v1`, `assurance.improvement.effect.delivery.v1`, and `assurance.improvement.effect.promotion.v1`.
- Keep 25 validators registered and zero production validators bound. Do not delete semantic/cross-reference validation merely because shape parsing moved into the Kernel.
- Do not introduce a custom Schema-writer plugin, private OpenCode fork, project-loadable artifact registry, cryptographic attestation, in-toto/DSSE, or a global ACID claim.
- Every child task follows RED -> focused GREEN -> exact-path staging -> one reviewable commit. Never use `git add .`, `git add -A`, broad globs, xfail, skip, or semantic waivers to close a row.

---

## Structured Child Plans and Authority

| Plan | Sole authority | Tasks |
| --- | --- | ---: |
| [OpenCode Structured Output Gate](2026-09-01-opencode-structured-output-gate.md) | Standalone exact-release eligibility probe, provider-neutral activity/quiescence values and port seam, real OpenCode request/observation protocol, Product sandbox/network/broker boundary, recovery/error reduction, pinned transport certification, exact provider/model/schema matrix, typed-path write denial | 8 |
| [Artifact Foundation and Structured Attempt Kernel](2026-09-01-artifact-kernel-foundation.md) | Artifact and Product requirement registries, closed resolved executor/dispatch, secret lifetime and broker-registration seams, durable prepared/closure/result/raw-post-image/manifest/receipt snapshots, Kernel trace and recovery cuts | 10 |
| [33-Agent Artifact Contract Migration](2026-09-01-agent-artifact-contract-migration.md) | Exact 33-contract/34-occurrence/69-slot inventory, public-to-private Product input adapter, seven Feature waves, result/document models, skills, projectors, finalizers, parity evidence, pre-cert production runtime/key foundation, post-cert Checkpoint S CI gate | 12 |

The suite contains 30 technical tasks total (`8 + 10 + 12`). Only OpenCode Task 0 may execute before Checkpoint S0; a red S0 defers the other 29 tasks. The accepted Structured Artifact spec is normative for production behavior, and each child plan is normative only for the files and interfaces assigned above. The Python-native migration master owns the continuation state, dependency order, Checkpoints S0/S placement, Product cutover, and stop conditions. If a child task appears to create an object already implemented by the pre-S baseline, interpret it as an in-place extension/migration and correct its file action from `Create` to `Modify`; never introduce a parallel authority. If ownership remains ambiguous, stop and correct the plan before writing code.

## Target File Ownership

```text
packages/framework/graph-engine/graph_engine/
├── attempts/
│   ├── structured_activity.py   # OpenCode child Task 1: provider-neutral value/port seam
│   ├── command_broker.py        # Artifact child Task 6: registration and injection seam
│   ├── contracts.py             # Artifact child Task 5: resolved direct/structured executor union
│   ├── kernel.py                # Artifact child Tasks 9-10: transaction orchestration
│   └── events.py                # Artifact child Tasks 6, 9-10: durable structured phases
├── artifacts/
│   ├── contracts.py             # Artifact child Task 1
│   ├── baseline.py              # Artifact child Tasks 1, 8: durable workspace baseline/mutation facts
│   ├── registry.py              # Artifact child Task 4
│   ├── codecs.py                # Artifact child Task 2
│   ├── materializer.py          # Artifact child Tasks 7-8
│   └── receipts.py              # Artifact child Task 3
└── persistence/
    └── attempt_artifacts.py     # Artifact child Task 6

packages/adapters/
├── agent-runtime-contracts/     # Artifact child Task 5: four-model contract and requirement mapping
└── agent-runtime-opencode/      # OpenCode child Tasks 2-7 only

scripts/
└── opencode_structured_output_eligibility_probe.py  # OpenCode Task 0 only

tests/agent_runtime/
└── test_opencode_structured_output_eligibility_probe.py  # OpenCode Task 0 only

packages/products/assurance-product/assurance_product/
├── runtime_ports.py             # Capability Task 11: production lifetime/close ordering
├── secret_generation_key.py     # host-owned key-file producer + key ID
├── toolchain_requirements.py    # Capability Task 2: four semantic runner rows
├── toolchains.py                # OpenCode Task 6: qualified Product profile
├── network_requirements.py      # installed provider-neutral requirement rows
├── network_targets.py           # installed closed target catalog
├── command_network.py           # qualified L7/default-deny gateway
├── command_secrets.py           # requirement binding and one-shot delivery
└── command_sandbox_broker.py    # Product-only broker registration/session

packages/capabilities/assurance-*/
├── contracts/artifacts.py       # Capability child Feature waves
├── artifacts/projectors.py      # deterministic Feature-owned projection
├── resources/schemas/           # installed Agent-result/document schemas
└── resources/skills/            # structured-result/raw-write authority instructions

tests/architecture/               # exact inventory, wording, and dependency closure
tests/product/                    # Product composition, certification, parity, and Checkpoint S
```

## Frozen Cross-Plan Interfaces

```python
class StructuredAgentActivityPort(Protocol):
    capabilities: frozenset[str]

    async def dispatch_or_adopt_activity(
        self,
        request: StructuredAgentActivityRequest,
        context: StructuredActivityCallContext,
    ) -> StructuredActivityDispatch: ...

    async def observe_activity(
        self,
        binding: StructuredActivityBinding,
        context: StructuredActivityCallContext,
    ) -> StructuredActivityObservation: ...

    async def close_raw_writes(
        self,
        binding: StructuredActivityBinding,
        context: StructuredActivityCallContext,
    ) -> StructuredRawWriteClosure: ...
```

`SecretMaterialGenerationPort` returns a provider-neutral `SecretGenerationSetIdentity(algorithm_id, key_id, generation_set_digest)` plus ephemeral materials. Product owns the concrete HMAC generation algorithm; Artifact Task 6 owns persistence of that exact identity in `ResourceAuthorizationRecord`; Kernel never recomputes a second digest from a narrower material projection. `StructuredActivityCallContext` is an ephemeral closed dataclass, never a wrapper/proxy over `AttemptExecutionContext`. Its only fields are `attempt_key_digest`, stable `activity_reference_digest`, `active_fence`, the adapter-only generation-scoped `SecretPort`, the anchored identity's `secret_generation_set_digest`, and opaque `structured_command_broker_binding_digest`. It has no snapshot-admission, command-injection, broker-registration, journal, workspace, effect, resource, promotion, or raw-write capability. Kernel registers the lifetime's admission/injection ports directly with the Product-owned broker before dispatch; the unforgeable sink/session token flows directly to the trusted boundary plugin outside adapter/model data. The adapter cannot reopen or independently resolve material, probe admission, construct a sink, or register another broker.

`StructuredWorkspaceAccess`/workspace binding v2 carry exact `input_snapshot_digest`, `input_snapshot_policy_digest`, nullable-only-for-no-requirements toolchain profile/invocation-scope digests, nullable command-secret/network access and backend/policy/qualification digests, `structured_command_broker_binding_digest`, `broker_registration_receipt_digest`, sandbox policy/qualification digests, and the closed raw/typed path tuples. Artifact Kernel Task 9 copies these values from the authenticated workspace identity, bound dispatch, ProductLock, Product runtime binding, and Kernel-owned broker-registration result; Kernel separately registers the full bound invocation scope with Product while adapter/model see only its digest. The adapter/plugin/broker may compare identities but never choose alternatives or receive a host path, raw endpoint, secret handle/value, or target-catalog row. Terminal receipt carries and verifies scope plus both broker digests (rather than assuming one recursively commits to another).

`StructuredActivityToolProductionQuiesced` is a durable provider-neutral receipt over Attempt/activity/broker/workspace/generation, closed admission generation, allocated/terminal/queued ordinal watermarks, boundary acknowledgement, and receipt digest. Running/pending observations carry it. A dispatch-level pending either proves “not accepted/no namespace” or recovers the stable reference and carries the same receipt; uncertain acceptance is indeterminate. Recovery re-registers/adopts the exact generation and broker, reattaches the quiesced plugin, and processes any durably queued ordinal at most once without a second prompt.

`close_raw_writes(...)` is the provider-neutral terminal adapter-admission barrier, not an OS syscall interposer for an already detached child. It returns one immutable, idempotently adoptable closure over Attempt/activity/workspace, toolchain invocation-scope digest, **broker-binding digest and broker-registration-receipt digest independently**, closed generation, historical closing fence, boundary-marker digest, canonical command-transcript digest, mandatory command-secret-injection aggregate digest (canonical empty when no injection), nullable network-transcript digest, and receipt digest only after successful recipe-use cardinality and every allocated command ordinal are reconciled/terminal/imported. A later active fence may adopt the same immutable marker/receipt and append the Kernel event; a stale fence may neither create/change the marker nor append state. The method reports unresolved command or publication only through the provider-neutral `StructuredRawWriteClosePending` / `StructuredRawWriteCloseIndeterminate` exceptions consumed by the existing Attempt resolution mapping—there is no adapter-specific fourth result union. Kernel's descriptor-safe immutable raw post-image, final seal equality, and blob-backed promotion are the authoritative integrity barrier against delayed/background writers.

```python
@dataclass(frozen=True, slots=True)
class ResolvedDirectExecutor(Generic[InputT, OutputT]):
    execute: AttemptExecutor[InputT, OutputT] = field(compare=False, repr=False)
    executor_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedStructuredAgentExecutor(
    Generic[InputT, PreparedT, AgentResultT, OutputT]
):
    contract_id: str
    prepare_handler_id: str
    finalize_handler_id: str
    instruction: FrozenJSONValue
    prepare: Callable[[InputT], Awaitable[object]] = field(
        compare=False, repr=False
    )
    prepared_model: type[PreparedT] = field(compare=False, repr=False)
    prepared_model_symbol: str
    prepared_schema: FrozenJSONValue
    prepared_schema_digest: str
    activity: StructuredAgentActivityPort = field(compare=False, repr=False)
    agent_result_model: type[AgentResultT] = field(compare=False, repr=False)
    agent_result_model_symbol: str
    agent_result_schema_id: str
    agent_result_schema: FrozenJSONValue
    agent_result_schema_digest: str
    max_schema_bytes: int
    max_result_bytes: int
    requires_structured_output: Literal[True]
    provider_model: str
    certification_row_digest: str
    runtime_binding_digest: str
    structured_toolchain_requirement_ids: tuple[str, ...]
    structured_toolchain_profile: ResolvedStructuredToolchainProfile | None
    structured_toolchain_profile_digest: str | None
    structured_network_requirement_ids: tuple[str, ...]
    structured_network_requirements: tuple[StructuredNetworkRequirement, ...]
    structured_network_requirements_digest: str | None
    structured_network_mode: Literal["none", "required", "input_conditioned"]
    structured_network_backend: ResolvedStructuredNetworkBackend | None
    structured_network_backend_digest: str | None
    structured_network_target_catalog: ResolvedStructuredNetworkTargetCatalog | None
    structured_network_target_catalog_digest: str | None
    structured_command_secret_requirements: tuple[StructuredCommandSecretRequirement, ...]
    structured_command_secret_mode: Literal["none", "required", "input_conditioned"]
    activity_secret_handles: tuple[str, ...]
    activity_secret_handles_digest: str
    structured_command_secret_binding_catalog: tuple[ResolvedStructuredCommandSecretBinding, ...]
    structured_command_secret_binding_catalog_digest: str | None
    artifact_contract: ResolvedArtifactContract
    finalize: Callable[
        [StructuredFinalizeInput[InputT, PreparedT, AgentResultT]], Awaitable[object]
    ] = field(compare=False, repr=False)
    executor_digest: str
```

The detailed immutable metadata and digest fields are defined in Artifact/Kernel Task 5. The two snippets above freeze ownership and method names; child plans must not introduce a second dispatch, observation, executor, or finalizer abstraction. `AgentExecutionContract` is the sole author of `agent_result_schema_id`, `max_schema_bytes`, and `max_result_bytes`; resolution freezes the installed result model's exact schema document once and computes its digest. Product bindings and OpenCode consume those values unchanged and cannot provide alternatives.

```python
@dataclass(frozen=True, slots=True)
class BoundAttemptDispatch(Generic[InputT]):
    semantic_occurrence_id: str
    business_activation: BusinessActivation
    business_activation_digest: str
    artifact_mutation_phase: ArtifactMutationPhase | None
    task_input: InputT = field(compare=False, repr=False)
    task_input_snapshot: FrozenJSONValue
    task_input_digest: str
    resources: ResourceClaims
    resource_claims_digest: str
    structured_toolchain_profile: ResolvedStructuredToolchainProfile | None
    structured_toolchain_profile_digest: str | None
    structured_toolchain_invocation_scope: BoundStructuredToolchainInvocationScope | None
    structured_toolchain_invocation_scope_digest: str | None
    structured_network_access: BoundStructuredNetworkAccess | None
    structured_network_access_digest: str | None
    structured_command_secrets: tuple[BoundStructuredCommandSecret, ...]
    structured_command_secret_set_digest: str | None
    authorized_secret_handles: tuple[str, ...]
    authorized_secret_handles_digest: str
    artifacts: BoundArtifactContract | None
    bound_artifact_contract_digest: str | None
    binding_digest: str
```

`AssuranceAttemptKernel.execute_or_recover(...)` receives this value as `dispatch`; it does not accept an independently supplied `task_input`, resource/path closure, toolchain selection, target selector, or secret selector. A direct Attempt has `artifacts=None` and empty structured runtime authority; every structured Agent Attempt, including raw-only, has a bound artifact contract. A nonempty toolchain requirement set always has a profile plus an invocation scope, even when the validated selection contains zero recipe rows; only an empty requirement set has both fields `None`. The invocation-scope digest enters `AttemptKey`, workspace/broker registration, command store/view/receipt, and terminal receipt. The authorized handle tuple is exactly adapter activity handles union the **selected** command handles—not the executor's full requirement-to-handle binding catalog—and its digest enters `AttemptKey` before raw values/generations are resolved after resource authorization.

The exact structured Attempt trace is:

```text
adopt_or_create
-> authorize_resources
-> begin_workspace
-> prepare_or_recover
-> validate_prepared
-> persist_prepared
-> dispatch_or_adopt_agent_activity
-> observe_structured_agent_result
-> close_agent_raw_writes
-> validate_agent_result
-> persist_structured_result
-> capture_raw_postimage
-> persist_raw_postimage
-> prepare_materialization
-> persist_materialization_manifest
-> install_typed_artifacts
-> persist_materialization_receipt
-> deterministic_finalize
-> validate_output
-> seal_candidate
-> run_validators
-> durable_prepare
-> promote_or_recover
-> settle_effects
-> publish_receipt
```

## Required Execution Order

Do not execute the three children independently from top to bottom. The Python-native migration master has already completed Foundation 1–10, Semantic Attempt 1–10, Feature 1–9, Product 1–4, and Product T5a. Use only the following remaining interleave.

**Current state: PARKED AT CHECKPOINT S0.** Authorized now: plan synchronization and OpenCode Task 0 only. Not authorized while S0 is red: OpenCode Tasks 1–7, Artifact Tasks 1–10, Capability Tasks 1–12, Structured R1–R6, and Product T5b–T10.

### R0: Synchronize the plan suite and freeze the recovery baseline

- [ ] Accept `feat/python-native-langgraph-migration@4a9cd197` or an explicitly reviewed successor as the continuation base. Record its full SHA; do not reset to `a622d116`, replay old tasks, or reconstruct user-owned adapter changes.
- [ ] Force-add only the four Structured plan files, both accepted specs, the corrected Python-native/Product plans, and `docs/research/2026-09-02-opencode-current-structured-output-source-audit.md` onto the continuation branch. The migration master's twelve-path `git ls-files --error-unmatch` gate must pass before source implementation.
- [ ] Freeze the current selection map: four non-Agent roots on `langgraph-v1`, ten Agent-dependent roots on `legacy-v2`. Relabel progress as `Product T5a complete / Task 5 partial`.
- [ ] Freeze active counts and artifacts by `(runtime_kind, revision_id)`. Existing pre-S Invocations keep their original runtime marker, GraphRevision/ProductLock, Attempt journal, checkpoint, receipt, and deployment artifact/container.
- [ ] Execute OpenCode Task 0 only. Retain OpenCode `1.18.4` as the historical negative fact and install `v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661` as Task 0's fixed negative fixture; v1.18.26 must fail at the no-model message round-trip before any provider prompt spends quota.

### Checkpoint S0: OpenCode Release Eligibility

- [ ] A trusted operator records one exact official candidate tag/commit/asset, platform/architecture, local binary digest, direct loopback endpoint, local OpenCode data-root identity, and workspace `directory` query scope, then runs Task 0's no-model `204 -> list 200 -> single 200` round-trip. Both successful `WithParts.info.format` reads retain canonical-equal submitted `type/schema` and exact server-added `retryCount == 2`; `v1.18.26` remains `204/400/400` and red.
- [ ] Only after that fail-fast gate passes, run exactly one real provider/model/minimal-canary schema and accept only a terminal, error-free assistant `info.structured` object that passes the local canary schema and byte bound.
- [ ] The trusted operator restarts the declared local candidate with the same OpenCode data root and workspace `directory` query scope, then a fresh probe process re-reads the same session/user/assistant IDs and structured-candidate digest through both V1 endpoints with no POST-capable path.
- [ ] The resulting canonical eligibility report contains no password or Authorization value, labels restart evidence `operator_executed_unverified`, and cannot create/promote installed certification, satisfy Product Boot, or advertise `opencode_structured_output`. It proves only that R1 implementation investment may begin; authenticated serving identity, full controller-backed recovery, boundary isolation, all 33 schemas, and production certification remain mandatory in OpenCode Tasks 4–7 and Checkpoint S.
- [ ] If any S0 row is red, stop this plan before R1. Leave all other 29 technical tasks unchecked, keep the four T5a roots on LangGraph and ten Agent-dependent roots on legacy, and wait for a later exact official release.

### R1: Retrofit structured contracts and the Artifact foundation — blocked until S0 is green

- [ ] Execute Capability Task 10 through its single documentation/test commit first. Its wording RED must fail, then all active plans/specs must use the 25-stage trace, strict `prepare(InputT)`, blob-backed raw finalization, `requires_structured_output`, `opencode_structured_output`, `ResolvedStructuredAgentExecutor`, and Checkpoints S0/S. Re-run—not reimplement or recommit—this wording gate after final certification.
- [ ] Execute Capability Task 1, then OpenCode Task 1, Artifact Tasks 1–4, Capability Task 2, and Artifact Task 5 in that exact order.
- [ ] Treat every existing Attempt/registry/Product file as `Modify`, not `Create`. Migrate the current `CompositeAttemptExecutor` implementation to `ResolvedStructuredAgentExecutor`; do not recreate Semantic Attempt Tasks 1–6 or preserve a second executor authority.
- [ ] Keep Product Boot able to compile/authenticate all roots. An unsatisfied structured requirement creates a closed, non-dispatchable binding for its reachable Agent site and blocks that entrypoint's future cutover/dispatch; it does not globally fail four direct/non-Agent roots.
- [ ] Verify exactly `33 contracts, 34 occurrences, 69 slots, 69 raw, 0 typed` at the initial fail-closed baseline and prove `bind_attempt_dispatch(...)` completes before `AttemptKey` derivation.

The exact R1 child dependency is:

```text
Capability C10 documentation/test sync
  -> Capability C1
  -> OpenCode O1
  -> Artifact A1-A4
  -> Capability C2
  -> Artifact A5
```

### R2: Extend the existing AttemptKernel durable prefix — depends on S0 green

- [ ] Execute Artifact Tasks 6–8, OpenCode Tasks 2–4 and 6, then Artifact Tasks 9–10.
- [ ] Extend the existing `AssuranceAttemptKernel`; preserve and reuse its stable key, workspace, seal, ordered validators, durable prepare, promote/recover, six-effect, system-interrupt, and terminal-receipt implementation. Do not re-execute Semantic Attempt Tasks 7–10.
- [ ] Replace the old shorter trace with the frozen 25-stage trace and prove the seven structured crash cuts plus activity binding, message/toolpart observation, raw-write closure, immutable raw post-image, typed install, finalization, and frozen promotion-source cuts.
- [ ] Run direct-Attempt, all-six-effect, system-interrupt, validator 25/0, stale-fence, and pre-S revision reopen regressions after the extension.

The exact R2 child dependency is:

```text
Artifact A6-A8
  -> OpenCode O2-O4
  -> OpenCode O6
  -> Artifact A9-A10
```

### R3: Retrofit all 33 Agent contracts into existing StateGraphs — depends on S0 green

- [ ] Execute Capability Tasks 3–9 in order, followed by Capability Task 11. These tasks modify contracts, input hydration, mutation-phase selection, result/document models, skills, projectors, finalizers, permissions, and Product runtime ports already consumed by existing Python factories; they do not recreate Feature Tasks 1–9.
- [ ] After every Feature wave, regenerate exact row-level evidence. If typed evidence is incomplete, retain the reviewed raw row in that same wave; no partial typed or waiver state is allowed.
- [ ] Emit one internal canonical `AttemptSiteCatalog` from the existing dry/runtime factories and require `live StateGraph sites == ArtifactContract occurrence domain == frozen 34-occurrence fixture`. Fold its digest into the existing entrypoint contract/Product closure without adding a graph node or changing `GraphBuildManifest` fields.
- [ ] Re-run the exact nine `join:any` rows, seven SCC anchors, three `min_matches` dispositions, 50 exclusive routes, test-only validator parity, six effects, and all six Feature bundle suites. Topology drift is a migration-stop defect.
- [ ] Extend the already-existing `ProductRuntimePorts`; Capability Task 11 does not create a second Product lifetime or replace Product Task 3 history.

### R4: Re-certify the existing LangGraph/Product implementation — depends on S0 green

- [ ] Re-run Foundation/Attempt public-contract suites, all Feature gates, Product Tasks 1–4 composition/CLI/shadow/crash suites, and the T5a direct/effect paths against the post-Structured implementation.
- [ ] Build a post-S ProductLock and GraphRevision from the changed authenticated sources/bindings. Prove the pre-S and post-S revision IDs differ and the post-S `GraphBuildManifest` authenticates the 33 structured Attempt digests; do not claim Attempt digests are direct fields of `GraphRevision`.
- [ ] Prove one pre-S T5a Invocation reopens only through its recorded pre-S deployment artifact before checkpoint/Kernel access, while a new T5a start uses the post-S revision and performs zero structured activity calls.
- [ ] Keep ten Agent-dependent roots on `legacy-v2` throughout R0–R5. Legacy alias/artifact paths and the four T5a roots must pass coexistence tests in the same candidate.

### R5: Certify OpenCode and close Checkpoint S — depends on S0 green

- [ ] Execute OpenCode Task 5 against the frozen R3 catalog, rerun Task 6 against the exact final typed/raw paths, then execute OpenCode Task 7.
- [ ] Re-run—not reuse—the no-model gate before provider calls and require the exact release to pass `prompt_async + noReply + format.json_schema == 204`, V1 message list `== 200`, and V1 single-message `== 200`. A source-level feature, generated SDK type, or passing S0 report is not certification.
- [ ] Execute Capability Task 12 and rerun Capability Task 10's wording test on the final integrated candidate. Publish the release workflow and protected aggregate gate for that exact `candidate_sha`; the producer/downstream artifact, binding wheel, build record, ProductLock, certification, catalog, and wheel digest must all identify that same candidate.
- [ ] If an S0-eligible release later fails the complete gate, stop R5 and downstream Product T5b–T10 work. Keep the four T5a roots on LangGraph and the other ten roots on legacy; do not roll back completed Structured implementation, Feature factories, or globally fail Product Boot.
- [ ] A later change to the Adapter, OpenCode release/provider/model/schema/limits, structured contract/serializer, Kernel, binding closure, relevant factory, or protected workflow invalidates Checkpoint S and requires the affected qualification plus aggregate gate to rerun.

### R6: Resume the remaining Product tasks

- [ ] After Checkpoint S closes, execute Product T5b for `intake`, `case`, `archive`, `retro`, `issue-review`, `issue-analyze`, `issue-reconcile`, and `improvement-review`.
- [ ] Execute Product T5c for `execute`, then T5d for `full`. Each cutover changes future starts only and creates a new candidate SHA; before release, rerun the protected Checkpoint S aggregate gate for that exact candidate and record its GraphRevision, ProductLock, structured certification, binding wheel, Attempt-site catalog, parity/crash/join evidence, and entrypoint. Reuse the external OpenCode qualification only when its authenticated adapter/server/provider/model/schema/limits and contract catalog closure are unchanged.
- [ ] Only after T5d proves all 14 future-start selectors use `langgraph-v1` may Product Tasks 6–10 run. Legacy drain does not authorize retirement of a pre-S LangGraph deployment artifact while a resumable Invocation remains pinned to it.

## Superseded Greenfield Order — historical rationale only

The stages below preserve the original dependency reasoning for review. **Every checkbox in this historical section is non-authoritative and must not be executed.** R0–R6 above and the Python-native migration master replace this order.

### Stage 0: Accept a clean integration base

- [ ] Run `git status --short` in the source worktree and record every user-owned path.
- [ ] Have the owner land or otherwise explicitly select the commit containing the current OpenCode/workspace-boundary work. Do not make a migration commit from the dirty planning worktree.
- [ ] Create a new isolated worktree from that exact commit with `superpowers:using-git-worktrees`.
- [ ] In the isolated worktree run `git status --short`; expected output is empty.
- [ ] Run the integration-base preflight from `2026-08-31-python-native-langgraph-migration.md`; all inventory and baseline suites must pass before Stage 1.
- [ ] Historical ordering note only: the greenfield draft placed 33-Agent Artifact Contract Migration Task 10 first. That order is superseded; current R0 executes OpenCode Task 0, requires S0 green, and only then makes Task 10 the first retrofit commit as specified in R1.

### Stage 1: Preserve the original Foundation/Attempt type interleave

- [ ] Execute Python-native LangGraph Foundation Task 1 only.
- [ ] Execute Semantic Attempt Kernel Task 1.
- [ ] Execute Python-native LangGraph Foundation Tasks 2–7.
- [ ] Execute Semantic Attempt Kernel Task 2.
- [ ] Run the focused type/revision/registry tests named by those tasks. Do not run Foundation Tasks 8–10 yet.

This preserves the original migration's mandatory `Foundation 1 -> Attempt 1 -> Foundation 2–7 -> Attempt 2` dependency. Finishing Foundation 1–7 before Attempt 1 is forbidden.

### Stage 2: Freeze Artifact types, install the raw baseline, and finish Attempt contracts

- [ ] Execute 33-Agent Artifact Contract Migration Task 1 and verify exactly `33 contracts, 34 occurrences, 69 slots, 69 raw, 0 typed`; typed annotations remain non-authoritative targets. The added Healing coverage-repair raw set must be derived solely from validated `brief.allowed_test_files`, require existing baseline files, and preserve the current model-edits-tests behavior.
- [ ] Execute OpenCode Structured Output Gate Task 1 only. This creates the sole `graph_engine.attempts.structured_activity` value/port seam and has no dependency on the later 33 bindings.
- [ ] Execute Artifact Foundation and Structured Attempt Kernel Tasks 1–4.
- [ ] Execute 33-Agent Artifact Contract Migration Task 2 to install the six Feature-owned raw `ArtifactContract`/path-resolver catalogs **and** Task 4's complete 33-row provider-neutral `AgentExecutionAuthority` table: toolchain requirement IDs, exact network mode/IDs, and secret mode plus full canonical `StructuredCommandSecretRequirement` values (alias tuple, purpose, one-shot delivery, UTF-8/no-NUL encoding, target classes, byte bound, digest). The same atomic commit installs the exact four Product-owned production semantic runner rows in `toolchain_requirements.py`, the public Product selector schema, and internal `ResolvedProductInputV1` root envelope/private selection adapter. It contains no concrete Product profile/backend, target, address, handle, value, or generation.
- [ ] Execute Artifact Foundation and Structured Attempt Kernel Task 5. Import the OpenCode Task 1 activity types, consume the exact Capability Task 2 production authority table and four production semantic runner rows plus authenticated generic `satisfied_requirements`, and never redeclare the port or name a concrete adapter capability. Exercise toolchain binding with those production rows and an explicitly test-only concrete profile; only the production concrete profile arrives in OpenCode Task 6, so production resolution remains fail-closed until then. Define the sole invocation-selection/scope values, bind exact logical recipe parameters from validated Execution `InputT`, and extend `AttemptKey` with input/resource/toolchain profile/invocation-scope/network/command-secret/authorized-handle/bound-artifact digests. Prove `bind_attempt_dispatch(...)` completes before key derivation; a provisional pre-binding key is forbidden.
- [ ] Execute Semantic Attempt Kernel Task 4 with this overlay: attach `RAW_AGENT_ARTIFACT_CONTRACTS[contract_id]`, explicit `non_artifact_resources`, Agent-result schema ID/limits, `requires_structured_output=True`, and the exact Task 2 provider-neutral toolchain/network/command-secret authority row to all 33 Agent contracts; no inferred/default fallback is permitted.
- [ ] Execute Semantic Attempt Kernel Task 5 only for the exact 33-entry Product runtime-binding catalog and legacy-alias coexistence. Replace its `CompositeAttemptExecutor` resolution with Artifact Task 5's `ResolvedStructuredAgentExecutor`; before OpenCode Task 4, the production bindings have no satisfied structured-output requirement and production resolution is expected to fail closed. Data-set equality and a separately installed test-only satisfied binding cover development tests; neither may advertise a production capability.
- [ ] Execute Semantic Attempt Kernel Task 6 unchanged for sealed bytes/validator context, except for Artifact Task 9's later `materialization_receipt` extension.
- [ ] Run the Artifact contract/codec/registry/executor and 33-contract raw-baseline suites before continuing.

Semantic Attempt Kernel Task 3 is replaced, not executed literally:

- Its distinct `InputT`, `PreparedT`, `AgentResultT`, and `OutputT` requirement and resolved executor are implemented by Artifact Task 5.
- Its provider-neutral requirement closure consumes the generic satisfied-requirement set; OpenCode-specific mapping arrives later in OpenCode Task 4.
- Its `provider_schema` and `CompositeAttemptExecutor` wording is not executed.
- Its two Intake `min_matches` characterizations remain mandatory; their two-consumer dataflow is Feature-owned `prepare` code behind `ResolvedStructuredAgentExecutor`, never a generic composite executor.

### Stage 3: Finish Foundation, extract the Kernel, and extend its durable prefix

- [ ] Execute Python-native LangGraph Foundation Tasks 8–10. Foundation Task 8 now sees Semantic Attempt Tasks 2–6 as required by the original interleave.
- [ ] Foundation/Boot fixtures that need a resolved structured executor use the explicitly test-only satisfied requirement binding. Production Product Boot remains fail-closed until a real OpenCode transport + 33-row matrix record is promoted in Stage 6.
- [ ] Execute Semantic Attempt Kernel Task 7.
- [ ] Execute Semantic Attempt Kernel Task 8 only through the base journal/workspace/transaction extraction.
- [ ] Execute Artifact Foundation and Structured Attempt Kernel Tasks 6–8. Task 6 establishes the sole `AttemptSnapshotAdmissionPort`; do not implement Kernel Task 9 yet.
- [ ] Execute OpenCode Structured Output Gate Tasks 2–4, then Task 6. Artifact Task 6 first defines the sole Kernel-owned broker-registration seam. OpenCode Task 6 installs the Product broker/sandbox/network boundary and keeps `StructuredActivityCallContext` minimal: adapter activity secrets plus generation, activity-reference, fence, and opaque broker-binding digests only. Kernel registers the full `BoundStructuredToolchainInvocationScope` plus snapshot-admission and command-injection capabilities directly with Product; adapter/model receive only inert digests. `assurance_exec` sends logical recipe ID plus typed parameters, never argv, and Product derives the concrete command under the registered scope.
- [ ] Execute Artifact Foundation and Structured Attempt Kernel Task 9 against that final context shape.
- [ ] Execute Semantic Attempt Kernel Task 9 so the six-effect apply/reconcile state machine exists, then execute Artifact Task 10's extended recovery/fence matrix, then execute Semantic Attempt Task 10's graph bridge. Artifact Task 10 may not fake or locally reimplement the effect tail.
- [ ] Do not accept Semantic Attempt Task 8's old shorter trace; the 25-stage trace, durable raw/typed snapshots, seven required structured cuts, workspace-begin creation cuts, raw-write-closure cuts, and immutable-raw-capture cuts are its extended exit condition.
- [ ] While executing Semantic Attempt Task 10, replace its old `select -> derive key -> Kernel` shorthand with the frozen sequence `select BusinessActivation + graph-owned mutation phase once from the same anchored state -> select/validate InputT -> resolve claims and bind artifact paths/policy -> bind_attempt_dispatch -> derive key -> execute_or_recover(dispatch=...)`. Freeze activation/phase together before binding; exact replay reuses both, a distinct late trigger selects `repeat`, and `semantic_occurrence_id == semantic_node_id`. Add node-factory tests that activation/phase/input/path selectors run once before the key, phase is absent for direct Attempts, resource authorization consumes the same claims, and Kernel/recovery run zero selectors/resolvers. Baseline capture remains inside Artifact Task 8's durable `begin_workspace`, after authorization and before prepare/activity; it is not a node-factory plugin or key input.
- [ ] Run the complete Foundation, Semantic Attempt, and Artifact Foundation + Kernel exit gates.

### Stage 4: Historical pre-Feature transport ordering (superseded by R2–R5)

- [ ] Re-run OpenCode Structured Output Gate Tasks 2–4 and Task 6 focused gates; their implementation was deliberately interleaved in Stage 3 so Artifact Kernel Task 9 consumes the final activity/admission context.
- [ ] In OpenCode Task 4 make Product map `requires_structured_output` to `opencode_structured_output` only for a positively certified selected OpenCode adapter, then persist generic satisfied requirements plus the concrete capability-set digest.
- [ ] OpenCode Task 6 uses **33-Agent Artifact Contract Migration Task 1**'s exact intended typed-path inventory as non-authoritative security fixtures and implements generic deny-hole behavior; the final catalog is rechecked after contract retrofit.
- [ ] Keep the installed positive transport-certification set empty if no real OpenCode release passes every recovery check, and retain OpenCode `1.18.4` plus v1.18.26 as negative fixtures.
- [ ] Historical Feature fixtures may use only the installed test/replay `StructuredAgentActivityPort`; production Checkpoint S still requires a real positive record.

### Stage 5: Migrate all Feature artifact contracts

- [ ] Execute 33-Agent Artifact Contract Migration Tasks 3–9 in order.
- [ ] After every Feature wave regenerate the inventory and compare exact row-level evidence; counts alone are insufficient.
- [ ] If any typed row lacks required evidence or its semantic-migration record, keep/revert that row to reviewed raw in the same wave; never add an exception.
- [ ] After Task 9, freeze all 33 Agent-result schemas, effective typed/raw paths, size limits, semantic-migration records, and selected Product provider/model bindings.
- [ ] Execute 33-Agent Artifact Contract Migration **Task 11 (Production Structured Runtime Foundation)** now. It commits the sole owner-only generation-key file producer, selected-source authorization/lifetime bridge, ProductRuntimePorts broker/store/sandbox/network composition, CLI selection-time secret authorization, and immutable dispatch-to-broker invocation-scope handoff. It deliberately has no dependency on a positive OpenCode record and must be green before conformance Task 5/7 uses real Product ports.

### Stage 6: Certify the complete OpenCode matrix, build the production binding wheel, and align documentation

- [ ] Execute OpenCode Structured Output Gate Task 5 against the frozen Stage 5 catalog, rerun Task 6's boundary tests against its exact final paths, then execute Task 7.
- [ ] A complete report contains exactly 33 contract rows even when contracts share schema bytes.
- [ ] Task 7's real conformance input includes a frozen **synthetic fixture** target catalog, requirement-to-handle bindings, authorized synthetic secret source, and owner-supplied generation-key configuration. The raw report and promoted record bind those fixture identities plus broker registration, one-shot injection, no-leak, generation rotation/restart, L7 path/method/redirect, and upstream-TLS verification evidence; they do not authorize an organization's deployment catalog.
- [ ] After promotion, run the exact release build/install test below with the promoted certification and separate deployment-owned target catalog/secret bindings. Predeclare `AA_OPENCODE_BINDINGS_BUILD_RECORD` as a new absolute owner-only/no-follow path outside the SUT/worktree; the builder must emit one deterministic external wheel plus that `binding-wheel-build-v1.json`. Install only into the isolated target directory and require Boot to resolve the exact capability/toolchain/network/secret closure. External raw evidence, target catalogs containing organization endpoints, binding manifests/wheels/build records, and secret sources/keys remain outside Git.

  ```bash
  uv run aa bindings build --json \
    --manifest "$AA_OPENCODE_BINDINGS_MANIFEST" \
    --output-dir "$AA_OPENCODE_BINDINGS_OUTPUT_DIR" \
    --structured-output-certification packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json \
    --structured-network-target-catalog "$AA_STRUCTURED_NETWORK_TARGET_CATALOG" \
    --structured-command-secret-bindings "$AA_STRUCTURED_COMMAND_SECRET_BINDINGS" \
    --build-record "$AA_OPENCODE_BINDINGS_BUILD_RECORD"
  test -f "$AA_OPENCODE_BINDINGS_WHEEL"
  test -f "$AA_OPENCODE_BINDINGS_BUILD_RECORD"
  uv pip install --no-deps --target "$AA_OPENCODE_BINDINGS_INSTALL_DIR" "$AA_OPENCODE_BINDINGS_WHEEL"
  AA_OPENCODE_BINDINGS_INSTALL_DIR="$AA_OPENCODE_BINDINGS_INSTALL_DIR" \
    AA_OPENCODE_BINDINGS_WHEEL="$AA_OPENCODE_BINDINGS_WHEEL" \
    AA_OPENCODE_BINDINGS_BUILD_RECORD="$AA_OPENCODE_BINDINGS_BUILD_RECORD" \
    uv run pytest -q tests/product/test_opencode_structured_binding_release.py
  ```
- [ ] If no external OpenCode release produces a passing report, stop here. Do not close Checkpoint S or begin Agent-dependent Product T5b, drain, or legacy deletion; preserve T5a and the ten-root legacy fallback.
- [ ] Rerun 33-Agent Artifact Contract Migration Task 10's exact wording test against the final catalog and promoted names; do not create a second documentation implementation commit merely because certification ran later.
- [ ] Confirm the custom Schema-writer recommendation remains marked superseded while dated negative evidence remains.

### Stage 7: Close historical Checkpoint B (superseded by R5/Checkpoint S)

- [ ] Execute 33-Agent Artifact Contract Migration **Task 12 (Checkpoint S Aggregate Gate)** through its RED, implementation, and local deterministic non-closing gate. It consumes only Stage 6's exact committed promoted transport/matrix records; Stage 6's local wheel is a Task 7 verification artifact and is never handed to Task 12. Task 12 does not redefine the Task 11 runtime/key/source authority.
- [ ] Commit the Task 12 workflow/script/tests as one candidate revision **before** requesting the protected run. Publish that exact commit through the protected branch/tag process, record its full `candidate_sha`, and verify the remote protected ref resolves to that SHA and contains the workflow. An uncommitted/unpublished workflow, a moving `latest` ref, or a prior revision cannot produce Checkpoint S evidence.
- [ ] The binding wheel is not repository state or assumed process state. Dispatch the workflow definition from the protected ref with the recorded `candidate_sha` and one immutable deployment-input revision; producer and downstream both check out/authenticate exactly that commit and independently resolve the same protected manifest/target-catalog/command-secret-binding inputs. It must not rerun qualification or promotion; it reruns only the exact binding build against those checked-in promoted bytes and uploads one immutable artifact containing the exact wheel, value-free `binding-wheel-build-v1.json`, and independent SHA file—never the raw deployment inputs. Its downstream Checkpoint job separately authenticates Actions producer/candidate-revision/run/artifact provenance, proves both checkouts equal `candidate_sha`, recomputes deployment-input digests and matches producer metadata outputs, validates the record schema and manifest/input/ProductLock/certification/catalog/binding identities, then proves recorded SHA = independent SHA = actual wheel bytes before install and passes explicit wheel/install/SHA/build-record paths to the gate. Ordinary PR CI runs the deterministic fail-closed precursor but cannot close Checkpoint S; the protected downstream release job for that exact SHA is the non-waivable closure signal.
- [ ] Recheck that startup authenticates only supplied source-catalog/adapter-always handles; the selected command handles become mandatory only from `BoundAttemptDispatch` after input selection and before resource authorization. Offline/unauthenticated runs may omit the admin source, while a selected authenticated Execution attempt must provide it. Capability Task 11 extends the `ProductRuntimePorts` already implemented by Product Task 3; it does not create a second lifetime module.
- [ ] In the protected downstream, run `bash scripts/structured_artifact_pipeline_gate.sh` and repeat the complete repository gate from Capability Task 12 against the same `candidate_sha`.
- [ ] Require the Product-composed broker registration/admission/injection E2E, exact opaque secret-generation replay/rotation rejection, owner-key missing/permission/rotation/restart cases, encoded/chunk-split secret rejection, all four Execution network/auth combinations, immutable raw post-image, delayed-background-writer rejection, and frozen promotion-source recovery to pass; direct Kernel test assembly is not sufficient.
- [ ] Request review focused on authority closure, durable replay, typed-path denial, schema certification, semantic-migration records, result/document digest separation, and preservation of the transaction tail.
- [ ] Require the exact candidate downstream to be green before Product T5b. There is no post-green Task 12 commit; a change to certification-relevant source creates a new candidate and invalidates the prior closure result.

### Stage 8: Historical Feature/Product continuation (superseded by R4–R6)

- [ ] Feature Tasks 1–9 and Product Tasks 1–4 are already implemented; R4 re-certifies them instead of executing them after this gate.
- [ ] Product continuation resumes only at T5b after R5/Checkpoint S is green.
- [ ] Each dry/runtime graph factory emits an internal canonical `AttemptSiteCatalog` for the same 34 existing semantic Agent sites. Boot requires that live catalog = Artifact contract domain = frozen fixture, then folds its digest into the existing per-entrypoint contract digest. `GraphBuildManifest` retains its exact existing field set; it does not gain a site-mapping field. Add zero nodes, contracts, or semantic occurrences.
- [ ] Preserve the existing zero-waiver `join:any`, exact SCC anchor, `min_matches`, exclusive-route, validator-shadow, 14-entrypoint, drain, and deletion gates.

## Checkpoint S Structured Artifact Acceptance

Checkpoint S is closed only when all rows below are true in the same final integrated revision:

- [ ] The continuation history is exact: Foundation 1–10, Semantic Attempt 1–10, Feature 1–9, Product 1–4, and Product T5a remain completed; no task or Invocation identity was reset or replayed to fit this tranche.
- [ ] For the initial Checkpoint S closure, the cutover map remains four direct/non-Agent LangGraph roots and ten Agent-dependent legacy roots. A missing structured capability blocks only reachable Agent dispatch/cutover, not global Product Boot. Subsequent T5b/T5c/T5d candidate re-runs validate the previously approved map plus that tranche's proposed roots; they do not require the historical 4/10 split.
- [ ] Pre-S and post-S GraphRevision/ProductLock/deployment artifacts coexist. Existing Invocations reopen only with their recorded artifact; new starts use the current revision; retirement uses an authenticated resumable scan rather than treating a raw binding-file count as terminal-state proof.

- [ ] Exactly 33 Agent contract IDs and 34 semantic occurrences resolve; only case-design has multiplicity two.
- [ ] Exactly 32 contracts have at least one structured artifact candidate; `assurance.quality.agent.report.v1` is the sole raw-only exception, and every candidate has an explicit slot decision.
- [ ] Exactly 69 artifact slots have final typed/raw authority—34 typed and 35 raw, deriving 14 typed-only, 18 mixed, and one raw-only contract—with no pending, unknown, suffix-derived, or waiver state.
- [ ] Every one of the 33 contracts sets `requires_structured_output=True` and sends its exact authenticated result schema.
- [ ] One pinned OpenCode server/adapter/provider/model/schema/size matrix passes asynchronous admission and full recovery certification; the selected capability is `opencode_structured_output`.
- [ ] A production OpenCode activity adapter (not the test activity port) runs typed-only, raw-only, and mixed Product contracts end to end through `AttemptNodeFactory` and `AssuranceAttemptKernel` against the scripted protocol server; the separate real-version certification remains mandatory.
- [ ] Every typed slot has installed result/document models and schemas, projector, serializer, path resolver, skill, finalizer, permission, recovery, and parity evidence.
- [ ] Every retained raw slot has a nonempty reviewed reason, explicit exact-file/tree-root target kind, and is the only raw authority for its effective path/root; there are exactly four primary tree roots, two Generation repeatable exact-file fix sets, and one Healing coverage-repair repeatable exact-file set. Coverage-repair resolves it only from validated `brief.allowed_test_files`, requires each baseline member to exist, uses whole-file bounded repair with preserve-baseline mode, and exposes original bytes through the immutable input snapshot/command view. Kernel computes the complete actual changed set from the full bound allowed set's baseline versus immutable raw post-image/sealed partition and requires `actual_changed_set == CoverageRepairAgentResultV1.files_modified ⊆ prekey_allowed_test_files`; false-positive reports, unreported allowed changes, out-of-set/new/delete/mode drift all fail before promotion.
- [ ] Dynamic Intake case paths, four Generation codegen roots, two codegen-fix exact allowed-path sets, and complete public `sut_read_paths` close before authorization and Attempt-key derivation. The exact toolchain partition is `2 Execution / 31 none`; each Execution contract resolves all four semantic runner rows to one qualified profile and binds its validated exact logical-recipe/typed-parameter selection to an invocation scope before the key, while every other contract has profile/scope `None`. A legal empty Execution selection still has a non-`None` zero-row scope. The broker rejects unknown/duplicate/cross-family/broadened/mismatched requests and derives argv itself; recovery authenticates the same scope and durable command.
- [ ] The Product parser exposes only public opaque network/auth selectors, extra-forbids private rows, anchors one `ResolvedProductInputV1(public + private selections/digest)` root envelope, and replays it without catalog re-resolution. Contract classification is exactly network `10 input_conditioned/23 none` and command-secret `2 input_conditioned/31 none`; all eight Generation network rows are OpenAPI-read-only and only Execution may select test targets/admin credentials.
- [ ] Product L7 network enforcement admits only frozen synthetic target aliases and requirement-specific methods/path scopes, blocks CONNECT/upgrade/write/redirect escape/registry egress, and verifies upstream TLS CA-or-pin plus hostname. One-shot command-secret injection is exact-target scoped, write-only, UTF-8/no-NUL bounded, value-free in every receipt/transcript, and inaccessible to adapter/model/Feature code.
- [ ] OpenCode cannot mutate a typed target through native write/edit/patch, shell, MCP, custom tool, or broad-parent raw permission.
- [ ] Canonical JSON/YAML corpus tests prove deterministic bytes; schema, object, serializer, and byte digests appear in materialization and terminal Attempt receipts.
- [ ] Kernel independently bounds both the untrusted activity candidate and the validated canonical `AgentResultT` bytes by the Feature-owned `max_result_bytes`; adapter checks cannot satisfy this invariant.
- [ ] All 33 prepare handlers accept only their exact validated `InputT`, all 33 finalizers accept only `StructuredFinalizeInput`, and purity tests prove neither can reach full Attempt context, secret/admission/activity/effect/write/clock authority, ambient mutable workspace, or host paths. The 17 legacy prepare handlers that read files first receive those values through graph-owned, pre-key input hydration.
- [ ] The exact 34-occurrence mutation-phase matrix is enforced: 16 occurrences support `initial + repeat`, Intake case-design repair and two codegen-fix occurrences are repair-only, and the remaining 15 are initial-only. Phase is graph-owned, bound before the Attempt key, and cannot come from business input or prepare.
- [ ] Every typed create/replace/complete-post-image bounded-repair rule and every raw create/replace/whole-file-bounded-repair rule enforces its fixed/preserve-baseline mode from the durable workspace baseline; baseline existence/content/mode, selected phase, mutation, and strategy appear in manifest/receipt closure.
- [ ] Baseline/post-Agent scanning enforces authenticated hard file/entry/byte/file-size/depth budgets before buffering, persists file sizes and the applied budget digest, meters internal scratch, keeps v1/v2 identities distinct, and creates v2 workspaces publish-atomically with crash reconciliation. `begin_workspace` captures exactly the authorized read claims into one immutable input snapshot, applying the versioned sensitive-path/type deny policy before any byte is exposed; OpenCode native reads and qualified command execution can see only that snapshot or an authenticated composed command view, never the live canonical project.
- [ ] New raw-write admissions are durably and idempotently closed at the OpenCode boundary before result validation/materialization; the closure and `AgentWritesClosed` independently authenticate `structured_command_broker_binding_digest`, `broker_registration_receipt_digest`, canonical injection aggregate (including empty), command/network transcripts, generation, and marker. In-flight managed writes, detached background writes, takeover fences, immutable capture, and late staging drift are covered without claiming the marker interposes on OS syscalls.
- [ ] Sealing partitions typed/raw promotable writes from authenticated internal scratch; only the promotable partition reaches validators/durable prepare, durable prepare freezes immutable source refs under runtime-owned control that is outside every structured-command sandbox mount, promotion reads only the independently authenticated frozen set, and canonical project never receives scratch or late staging bytes.
- [ ] Every `semantic_migration` slot resolves a Feature-owned authenticated migration record containing old/new representation identities, rationale, approved golden digests, downstream compatibility tests, and a digest included in Attempt-contract/ProductLock/build closure; `exact_bytes` slots carry no such record.
- [ ] All seven required structured crash cuts plus broker registration, request-accepted/dispatch-response-lost, observation quiescence, workspace-begin creation, raw-write-closure, immutable-raw-capture, and frozen-promotion-source cuts recover without duplicate prompt/namespace/command/injection or OpenCode dispatch after activity binding, permanent orphan workspaces, untrusted staging bytes reaching finalization/promotion, or partial promotion. Pending escapes only with proven non-acceptance/no namespace or authenticated durable quiescence and exact reattachment.
- [ ] Every durable Attempt-private blob kind rejects direct/encoded secret canaries before write; authorization anchors an opaque secret-generation-set digest computed with the owner-supplied HMAC key file/key ID, every admission receipt binds it, key/source/value rotation or unprovable generation blocks replay before any blob/event, and no referenced snapshot can be garbage-collected in this phase.
- [ ] Workspace access, broker registration, command store/view/receipt, closure/event, and terminal receipt bind the toolchain invocation-scope digest plus the opaque broker binding and broker-registration receipt; sandbox qualification has a required one-shot-injection section/digest. The promoted release evidence feeds an actual bindings build whose closed build record is handed off with the installed wheel and independently verified before the Boot exact-closure test.
- [ ] The 25-stage structured trace includes durable raw-write closure and immutable raw post-image capture; finalizers read only that blob-backed view, promotion installs only immutable typed/raw refs, and the flow rejoins the unchanged seal -> validators -> durable prepare -> promote/recover -> effects -> receipt tail.
- [ ] Twenty-five validators remain registered, zero production validators are bound, and the exact six effect kinds retain their ordering/apply/reconcile tests.
- [ ] No LangGraph topology count changes because of this pipeline.
- [ ] Research and active migration instructions contain no provider-native Schema claim, custom Schema-writer recommendation, or parallel legacy composite-executor authority; historical text is explicitly non-executable.
- [ ] Ruff, format, Pyright, import-linter, pytest, and all wheel smoke gates pass without an artifact-pipeline skip or waiver.

## Spec Coverage Matrix

| Spec area | Implemented by |
| --- | --- |
| Pre-implementation release eligibility | OpenCode Task 0 / Checkpoint S0; non-production and non-promotable |
| Decisions 1, 10, 14, 17 | Artifact Tasks 5, 9-10; Semantic Attempt Tasks 8-10 |
| Decisions 2-3 | OpenCode Tasks 1-4 |
| Decisions 4-7 | Artifact Tasks 1, 4-5; Capability Tasks 1-9 |
| Decision 8 | Artifact Tasks 1-4, 7; Capability Tasks 3-9 |
| Decision 9 | Artifact Tasks 1, 5, 7-9; OpenCode Task 6; Capability Tasks 2-9 |
| Decisions 11-13 | Artifact Tasks 3, 6-10 |
| Decisions 15-16 | Capability Tasks 1-9 |
| Decisions 18-19 | Capability Tasks 10-12 and this master |
| Decision 20 | Global constraints and architecture tests |
| Adapter tests 12.1/12.6 | OpenCode Tasks 2-7 |
| Contract/Boot tests 12.2 | Artifact Tasks 1, 4-5; Capability Tasks 1-2, 11 |
| Codec/materializer tests 12.3 | Artifact Tasks 2-3, 7-8 |
| Kernel/recovery tests 12.4 | Artifact Tasks 6, 9-10 |
| Capability parity tests 12.5 | Capability Tasks 3-9 |
| Regression gate 12.7 | Capability Task 12 |

## Program Stop Conditions

Stop without a waiver when any of these occurs:

- The accepted integration base is dirty or does not contain the owner-approved OpenCode/workspace-boundary changes.
- Checkpoint S0 is red or cannot be reproduced for one exact official release. In that state, stop before R1 and defer all other 29 technical tasks rather than speculatively implementing the production pipeline.
- A child plan attempts to define an interface owned by another child.
- A contract or slot cannot be mapped to the exact inventory.
- Path closure depends on `PreparedT`, model output, mutable project configuration, or an ambient filesystem read.
- A typed migration cannot preserve declared exact-byte parity or record its explicit semantic-migration golden.
- A stale fence can persist, materialize, finalize, seal, promote, settle, or publish.
- OpenCode cannot prove restart-safe `info.structured` recovery for the exact selected release and all 33 schema rows.
- Any Checkpoint S row is skipped, xfailed, count-only, manually signed off without machine evidence, or marked with a semantic waiver.

There is no per-contract production exception. While S0 is red, no Structured production implementation begins. After S0 is green but until Checkpoint S passes, the four non-Agent T5a roots remain on their accepted LangGraph revisions and the ten Agent-dependent roots remain on `legacy-v2`; Product T5b, legacy drain, YAML/compiler deletion, and custom Runtime deletion do not begin.
