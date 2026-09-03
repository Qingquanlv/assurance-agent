# OpenCode Structured Output Gate Implementation Plan

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

**Goal:** Make OpenCode a fail-closed implementation of the provider-neutral structured Agent activity seam: send the authenticated `AgentResultT` schema through `format.type=json_schema`/`format.schema`, observe only terminal assistant `info.structured`, survive activity recovery, advertise `opencode_structured_output` only for a certified server/provider/model/schema matrix, and deny OpenCode raw writes to Kernel-owned typed paths.

**Architecture:** `graph-engine` owns a small provider-neutral `StructuredAgentActivityPort`; Artifact/Kernel Task 5 consumes an authenticated generic `satisfied_requirements` set without naming OpenCode; the Product's OpenCode binding maps `requires_structured_output` to the selected adapter's truthful `opencode_structured_output` capability. `agent-runtime-opencode` owns OpenCode request, session adoption, terminal observation, error reduction, and certification logic. The Product supplies installed, digest-bound certification rows, a v2 workspace boundary, and a brokered OS-isolated command sandbox. The adapter receives only the exact result schema, effective raw paths, typed deny paths, and limits. It does not own artifact document models, serialization, materialization, finalization, LangGraph control, or transaction commit.

**Tech Stack:** Python 3.11, Pydantic v2, `httpx`, the existing fenced Task activity/session protocol, OpenCode HTTP `prompt_async` and message APIs, Node.js for the installed Assurance boundary, pytest, and the existing `uv` workspace.

**Spec:** `docs/superpowers/specs/2026-09-01-structured-artifact-pipeline-design.md`, especially Decisions 2, 3, 9–11 and tests 12.1/12.6. This plan replaces the `provider_schema` wording in Semantic Attempt Kernel Task 3; it does not edit that earlier plan.

## Global Constraints

- Task 0 is a standalone exact-release eligibility probe with no dependency on Artifact, Kernel, Capability, Product binding, or production Adapter code. It executes after plan synchronization and before every other Structured child task. While Checkpoint S0 is red, Tasks 1–7 and both other Structured child plans are unauthorized. Task 1 executes only after S0 is green and after Foundation Task 1 and Semantic Attempt Task 1 have created the core `graph_engine.attempts` package; it does not require Agent runtime bindings. Tasks 2–4 execute only after Artifact/Kernel Task 5 has landed the four-type `AgentExecutionContract[InputT, PreparedT, AgentResultT, OutputT]`, `AgentRuntimeBinding`, and provider-neutral resolved executor. Task 6 additionally requires Artifact/Kernel Tasks 6–8 and Capability Task 2's complete 33-row provider-neutral authority/public-to-private Product input foundation so it imports the sole admission/broker/input-snapshot contracts and can exercise real Product selection without redeclaring them. Task 5 and Task 7 execute only after all 33 bindings, authoritative `AgentResultT` schemas, effective typed/raw paths, declared schema/result size limits, and Capability Task 11's production runtime/key/source foundation are frozen.
- Start in a clean isolated worktree. Run `git status --short`; expected output is empty. The planning worktree currently contains user-owned, uncommitted changes in `handler.py`, `observation.py`, `protocol.py`, `workspace_binding.py`, the OpenCode fake/tests, `assurance-boundary.mjs`, and `test_opencode_staging_boundary.py`. Do not stash, copy, stage, overwrite, or re-create those changes. Wait for their owner to commit them, then create/rebase the implementation worktree from that commit.
- The Feature flag is exactly `requires_structured_output`. The concrete adapter capability is exactly `opencode_structured_output`. Delete code-level `provider_schema`, `requires_provider_schema`, and provider-native response-format claims as this seam lands.
- Keep `StructuredAgentActivityPort.dispatch_or_adopt_activity(...)` and `observe_activity(...)` provider-neutral. `graph-engine` must not import `agent_runtime_contracts` or name OpenCode. Product/runtime code may resolve a requirement to a capability but may not alter Feature-owned models or schemas.
- For production Tasks 1–7, the normative request keys are `format.type == "json_schema"` and `format.schema == authenticated AgentResultT schema`. Omit `retryCount`; OpenCode retry counts are not a durable guarantee. Task 0 is the sole exception: it uses one fixed local canary schema and has no Agent contract.
- A 204 from `prompt_async` is admission only. Success requires one terminal assistant message with no assistant error and a JSON object in `info.structured`. Text, prose JSON, completed tool inputs, `structured_output`, and other guessed fields are not fallback success paths.
- In production Tasks 1–7, Adapter schema validation is defense in depth and Kernel validation into the installed `AgentResultT` remains authoritative. Task 0 has no Kernel and validates only its fixed canary locally; its result cannot authorize an artifact. Artifact document validation and all materialization stay outside this plan.
- For production Tasks 1–7, a server health/version read is allowed before dispatch and no session creation or prompt POST is allowed until requirement/capability, certification row, schema digest/size, result bound, and typed/raw path checks pass. Task 0 is not a production dispatch: it may create only its disposable no-model/canary sessions after its own exact release, direct-endpoint, file, bound, and evidence checks pass.
- OpenCode 1.18.4 is a historical negative fixture. Official `v1.18.26 / 774cc7c1914e4329eefde5a669f938b0cf566661` is the current fixed `message-roundtrip-red` fixture: no-model `prompt_async + noReply + format.json_schema` returns 204, then V1 message list and single-message reads both return 400. Task 0 may emit only a canonical non-promotable eligibility report; that report never enters `capabilities_for(...)`, installed resources, Product Boot, or a binding wheel. Never add a positive eligibility/certification version or provider/model row by editing a `passed` boolean. Only Task 7's gate promotion command may generate positive installed records from a complete passing production report.
- Preserve the legacy prompt-only handler during semantic shadow. It is not a fallback for a request whose Feature contract says `requires_structured_output`.
- Use workspace binding v2 for structured Attempts and retain v1 for legacy shadow. V2 carries `effective_raw_paths`, `effective_raw_roots`, and `typed_output_denials`; it carries no artifact model, document, codec, serializer, or materialization receipt.
- A structured-session executor command never runs as an ordinary child in the Product/OpenCode filesystem namespace. It runs only through an authenticated `StructuredCommandSandboxPort` whose separate UID/container/mount/network namespace exposes a read-only input snapshot plus scratch and declared raw-output mounts, but never canonical project paths, typed staging, workspace parent/control, Attempt blob/journal roots, host paths, or secrets not explicitly brokered for that command. Network is denied by default. A command may reach only pre-key `BoundStructuredNetworkAccess` targets through the qualified Product gateway; it never receives a host network/default route or registry/package-manager egress. If no qualifying sandbox/network backend is installed, the corresponding executor capability is absent; there is no same-UID or ordinary-network fallback. Native boundary checks remain defense in depth, not syscall isolation.
- Every task is red-green-refactor: add the named failing test, run the exact focused command and observe the stated failure, implement only the named behavior, rerun, then commit only the listed paths.

## Cross-plan interfaces

The provider-neutral seam has this closed shape; names and ownership are acceptance criteria:

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

`StructuredActivityCallContext` is a closed minimal value, not a wrapper/proxy over `AttemptExecutionContext`:

```python
@dataclass(frozen=True, slots=True)
class StructuredActivityCallContext:
    attempt_key_digest: str
    activity_reference_digest: str
    active_fence: int
    activity_secrets: SecretPort = field(compare=False, repr=False)
    secret_generation_set_digest: str
    structured_command_broker_binding_digest: str
```

It has no attribute/proxy path to the secret-lifetime factory, snapshot admission, command-secret injection, broker registration, journal, workspace, effect, resource, promotion, or raw-write ports. After Artifact Task 6 defines the sole provider-neutral `StructuredCommandBrokerRegistrationPort`, Kernel registers snapshot admission and command injection directly with the Product broker against the stable activity reference; only the opaque broker-binding digest enters this minimal call context, while `StructuredWorkspaceAccess` carries both the binding and independent registration-receipt digests as inert authenticated values. The Product implementation delivers any unforgeable session token directly to the trusted boundary plugin, outside adapter request/model/tool data. The adapter uses only its scoped activity secret view and cannot probe admission as a membership oracle, read command secrets, construct a sink, or call injection/registration. Architecture and malicious-adapter tests enumerate public attributes and require zero admission/injection/value-resolution calls. There is no duplicate admission/injection/registration type, scanner, resolver, policy, or generation; ephemeral capabilities close in broker-session-before-secret-lifetime order. Prompt/model/prepare/finalize/Feature code never receives an injection capability or secret value.

`StructuredAgentActivityRequest` contains authenticated instruction/prepared payload, selected provider/model, `StructuredResultSchema(schema_id, schema_digest, schema_document, max_schema_bytes, max_result_bytes)`, stable `activity_reference_digest`, and `StructuredWorkspaceAccess(input_snapshot_digest, input_snapshot_policy_digest, structured_toolchain_profile_digest, structured_toolchain_invocation_scope_digest, structured_command_secret_set_digest, structured_command_broker_binding_digest, broker_registration_receipt_digest, sandbox_policy_digest, sandbox_qualification_digest, structured_network_access_digest, structured_network_backend_digest, structured_network_policy_digest, structured_network_qualification_digest, effective_raw_paths, effective_raw_roots, typed_output_denials)`. The two broker digests independently authenticate an already registered Product session and its durable registration receipt; neither is a callable, socket token, handle, or sink, and neither may be inferred from the other. The toolchain profile and invocation-scope digests are nullable together: both are `None` exactly when the contract declares no toolchain requirements. OpenCode receives only the invocation-scope digest; Kernel has already sent the complete callable-free `BoundStructuredToolchainInvocationScope` directly to the registered Product broker, so neither adapter nor model may supply or broaden recipe authority. The four network fields are nullable together and non-`None` exactly when the pre-key dispatch selected a nonempty `BoundStructuredNetworkAccess`; OpenCode sees only those digests and Product-issued synthetic target aliases, never a raw target catalog row. All three path tuples are sorted, unique, canonical project-relative values. `effective_raw_paths` and `typed_output_denials` are exact files; only `effective_raw_roots` grants descendant containment. Typed denials are disjoint from exact raw files and may sit beneath a raw root, where denial always wins. OpenCode reads the immutable Attempt input snapshot identified by the first digest and the Product broker resolves the already bound profile/invocation scope/network access by digest; no field is a host path or model-selected URL. The request also carries the selected certification-row digest. It does not contain `ArtifactContract`, `ArtifactSlot`, artifact document schemas, projector, codec, serializer, materialization callback, DNS resolver, network-policy callback, injection port, or session token.

`StructuredActivityDispatch` is a closed bound/pending/indeterminate union. `StructuredActivityObservation` is a closed running/succeeded/failed/canceled/pending/indeterminate union. `StructuredActivityToolProductionQuiesced` is a frozen receipt over Attempt/activity reference, broker binding and registration receipt, workspace, secret generation, closed-admission generation, highest allocated/terminal/queued ordinal, boundary acknowledgement identity, and receipt digest. A running/pending observation may leave the Kernel call only after the adapter has durably quiesced remote boundary-tool production for that identity: the plugin closes new tool admission, reconciles already allocated ordinals, persists this receipt, and queues any later tool request without completing or executing it. The observation carries the full receipt, not only an unauthenticated digest. `StructuredActivityDispatchPending` may omit it only when its signed state proves the request was not accepted and no tool namespace/ordinal can exist. If acceptance may have occurred—including request accepted/response lost—dispatch must recover the stable activity reference and carry/adopt the same quiescence receipt; otherwise it is indeterminate. Kernel authenticates the receipt before closing broker/session lifetime. On recovery it rebuilds the exact generation, `register_or_adopt(...)`s the same broker binding, and `observe_activity(...)` first reattaches the quiesced plugin before polling; queued calls retain their durable ordinal and execute at most once, with no new prompt. If quiescence/reattachment cannot be proven—including process death or acknowledgement loss before the receipt became durable—the result is indeterminate and the release gate cannot qualify that runtime. E2E covers both running → pending and request-accepted/response-lost → dispatch-pending → late-tool → same-activity recovery, with no duplicate prompt/namespace/command/injection. Success contains the untrusted JSON candidate, candidate digest, exact Agent-result schema digest, terminal assistant message ID, and raw observation digest. It is not an `AgentResultT` and not an Attempt receipt. `StructuredRawWriteClosure` is an immutable receipt over Attempt/activity/workspace, toolchain invocation-scope digest, **broker binding and broker-registration receipt**, closed write generation, historical `closing_fence`, boundary-marker digest, canonical command-transcript digest, mandatory `command_secret_injection_aggregate_digest` (one specified canonical empty aggregate when no injection), nullable `structured_network_transcript_digest`, and receipt digest. Closing is idempotent for the same binding/scope/generation, requires every selected recipe to be invoked exactly once on a successful activity (zero is legal for a zero-row scope), every allocated command ordinal to be terminal/imported, and every known command namespace killed/reaped, waits for mutations already admitted through the boundary gate, and durably rejects every later adapter/tool admission. The network transcript is required exactly for bound network access and contains only canonical target-ID/connection-count/byte/status aggregates and their digest—never URL path/query, headers, bodies, responses, credentials, or secret values. A new active fence may authenticate and adopt the same immutable marker/receipt after a crash; the old fence cannot create, alter, or append it. Unresolved command/network reconciliation or close publication is reported only with provider-neutral `StructuredRawWriteClosePending` or `StructuredRawWriteCloseIndeterminate` exceptions, which the Kernel maps through its existing Attempt-resolution policy; `close_raw_writes(...)` has no separate result union. The Kernel's immutable raw post-image capture and later seal/promotion equality remain the final integrity boundary for any mutation outside the known command namespaces.

The compatibility record key is exactly:

```text
(adapter_version, server_version, contract_id, provider_model,
 agent_result_schema_digest, max_schema_bytes, max_result_bytes)
```

No wildcard contract, provider, model, schema digest, or size bound satisfies a row.

---

### Task 0: Build the exact-release eligibility probe

**Current status:** authorized now. Implement and run this task only. The known `v1.18.26` result completes the negative fixture but leaves Checkpoint S0 red; in that state Tasks 1–7 and every Artifact/Capability child task remain deferred.

**Files:**

- Create: `scripts/opencode_structured_output_eligibility_probe.py`
- Create: `tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py`
- Create: `tests/fixtures/opencode/v1.18.26-message-roundtrip-red.json`

**Interfaces:** a standalone two-phase CLI, `pre-restart` and `post-restart`, that uses `httpx` directly. After all local inputs and the output sink validate, `pre-restart` atomically emits the closed `OpenCodeStructuredOutputEligibilityPreStateV1(status = "message_roundtrip_red" | "provider_canary_red" | "awaiting_restart")`; only `awaiting_restart` may enter `post-restart`. After its inputs/sink validate, the second phase atomically emits the closed `OpenCodeStructuredOutputEligibilityReportV1(status = "post_restart_red" | "eligible")`. An input/sink/fsync failure exits nonzero without claiming a report exists. Both statuses and the final `eligible` value are derived from recorded checks, never caller supplied. The probe targets a direct native OpenCode loopback origin, not a Product gateway, and accepts `auth_mode = "none" | "opencode_basic"`; Basic mode uses an explicit username and an owner-only password file according to OpenCode server authentication. It must not import `graph_engine`, `agent_runtime_contracts`, `agent_runtime_opencode`, a Capability package, or Product code. It must not modify a plugin declaration, installed resource, runtime selector, Adapter capability, Kernel contract, or binding wheel.

- [ ] **Step 1: Write the probe RED.** Cover the following exact cases with a scripted HTTP transport:

  - successful no-model `204 -> list 200 -> single 200`, with both `WithParts.info` values retaining the exact caller message/session/role and canonical-equal submitted `format.type=json_schema`/`format.schema`, while requiring the only server-added format field to be `retryCount == 2`;
  - the fixed v1.18.26 `204 -> 400 -> 400` report, keyed by its exact official release asset, platform, architecture, and binary digest rather than presenting one platform's hash as release-wide, which is ineligible and makes zero provider/model prompt calls;
  - one real-prompt-shaped terminal assistant `info.structured` canary object, followed by a fresh-process post-restart list/single re-read of the same session, user/assistant message IDs, and candidate digest, with exactly one total real prompt POST; intermediate tool-calling assistants may exist but cannot count as the unique terminal candidate;
  - every session create, prompt, list, and single-message request carries the exact canonical `?directory=` workspace query; missing/drifted query values fail, and health is the only request outside that scoped family;
  - bounded eventual visibility after admission/restart: `200` without the target or `404` retries until the fixed deadline, while schema-decode `400`, other terminal HTTP errors, deadline expiry, or oversized bodies fail closed;
  - wrong/missing persisted format/default retry count, version or operator-declared local-binary/data-root drift (including data-root inode replacement), list/single body drift, assistant linkage/provider/model/completion drift, assistant error, text JSON, tool input, `info.structured_output`, missing/wrong canary, schema-invalid/oversized candidate, post-restart digest drift, a post-restart prompt attempt, `401`/Basic challenge or wrong username/password, redirect/proxy use, and any password/Authorization value appearing in state/report all fail closed; ordinary SQLite/WAL content, entry, size, or timestamp mutation under the same data-root inode must not cause drift.

  ```bash
  uv run pytest -q tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py
  ```

  Expected failure: the standalone probe does not exist.

- [ ] **Step 2: Implement the closed pre-state and fail-fast pre-restart phase.** Require operator-declared exact upstream release tag/commit plus release-asset/platform/architecture identity, expected health version, local candidate-binary SHA-256, direct loopback endpoint-origin digest, local OpenCode data-root identity digest, workspace `directory` query-scope digest, auth-mode/username digest, provider/model, fixed canary-schema digest, response/deadline/poll bounds, and request/message/candidate digests. `--server-data-root` is the already resolved OpenCode `Global.Path.data`, not its `XDG_DATA_HOME` parent or the workspace directory; the operator start/restart record must state the launch/XDG setting that makes this exact path effective. Its stable no-follow identity projection is exactly canonical path, `st_dev`, `st_ino`, `st_uid`, and owner permission class. It excludes contents, entry set, mtime, ctime, and size because SQLite/WAL mutation is expected; deleting/recreating the directory changes the inode and fails. The workspace-directory digest binds only its canonical `?directory=` query identity and is never mixed with data-root state. These are explicitly operator-trusted eligibility inputs: the probe does not claim that a local file hash cryptographically identifies the serving process or that a restart occurred. Reject `current`, `latest`, ranges, absent identities, endpoint userinfo/path/query/fragment, any host other than IP literal `127.0.0.1` or `[::1]`, redirects, environment proxies, symlinked/non-regular password or binary inputs, symlinked/non-owner-only data/evidence directories, and reused output files. Use `httpx` with `trust_env=False`, `follow_redirects=False`, fixed connect/read/total deadlines, bounded polling, and a maximum response-body size enforced while streaming. `opencode_basic` sends only HTTP Basic using the explicit username and a bounded UTF-8 password file with no NUL/CR/LF; `none` sends no Authorization; Bearer and gateway profiles are forbidden. Store neither the password, raw Authorization header, raw data/workspace path, nor provider response text. Before the real provider/model request:

  1. read health and match the exact expected version; every later session/message request must carry the exact canonical workspace `?directory=` query bound in pre-state;
  2. create a no-model session;
  3. send `prompt_async` with an official-shaped caller ID (`msg_` plus a valid 26-character OpenCode ID suffix), `parts = [{"type": "text", "text": fixed_no_reply_instruction}]`, `noReply: true`, and `format = {"type": "json_schema", "schema": fixed_schema}`; omit `retryCount`, `model`, and tools and require `204`;
  4. after a `204`, run V1 list and V1 single-user-message probes independently even if the first is terminal-red, so the fixed negative can record `204/400/400`; retry only transient `200`-without-target/`404` under the short fixed deadline;
  5. in both successful `WithParts.info` reads require the exact `id`, `sessionID`, `role == "user"`, `format.type`, and canonical-equal schema, with exact server-added `retryCount == 2` and no other format field;
  6. always atomically write `PreStateV1`; on any mismatch set `message_roundtrip_red`, exit nonzero, and make zero real provider prompts.

  Only then create the one provider/model canary session and issue exactly one asynchronous request with another official-shaped `messageID`, `model = {"providerID": provider, "modelID": model}`, `parts = [{"type": "text", "text": fixed_canary_instruction}]`, and the same `format`, omitting `retryCount`, tools, and `noReply`. Under bounded polling, filter assistant messages by exact session plus `parentID == caller_message_id`; allow intermediate tool assistants, but require exactly one candidate whose `info.role == "assistant"`, provider/model fields match, `info.time.completed` is present, `info.error` is absent, and `info.structured` exists. Accept only `info.structured == {"canary": "AA_OPENCODE_S0_V1"}` under the local schema and byte bound, then single-read that server-generated assistant ID and require canonical equality. Write `provider_canary_red` and exit nonzero on any failure, or `awaiting_restart` and exit `0` with all IDs/digests on success. This owner-only pre-state lives outside the worktree and is not installed certification.

- [ ] **Step 3: Implement the fresh-process post-restart phase.** A trusted operator restarts the direct loopback server from the same declared local candidate with the same OpenCode data root and workspace `directory` query scope between phases. The CLI accepts only an `awaiting_restart` pre-state, re-hashes the binary, recomputes the exact stable data-root identity projection above, rechecks health/version and every other pre-state identity digest, then uses only V1 list and single-message GETs carrying the exact recorded `?directory=` value to re-read the recorded user and terminal assistant messages. Health readiness and transient empty/404 reads may retry only within fixed deadlines. Require identical message fields, `info.format` projection, structured object, and candidate digest and exactly zero POST-capable code paths in this phase. After output-sink validation, write `post_restart_red` with nonzero exit on mismatch or `eligible` with exit `0`; an output publication failure remains an error and never claims either report. Record `restart_evidence_level = "operator_executed_unverified"`; Task 5/7 must later prove old-instance to new-instance recovery. Never promote this report.

- [ ] **Step 4: Expose the two explicit operator commands.** Passwords are file inputs, not command-line values. Before `pre-restart`, the evidence directory is absolute, owner-only, outside every worktree, and empty. Before `post-restart`, that directory may contain only the exact named pre-state; the final output must not exist. The data root is an operator-owned disposable OpenCode data directory, distinct from the workspace `directory` query scope. The operator starts/restarts the official direct server with that data root; this manual fact is trusted only for S0 investment eligibility and is not production evidence.

  ```bash
  uv run python scripts/opencode_structured_output_eligibility_probe.py pre-restart \
    --endpoint "$AA_OPENCODE_S0_ENDPOINT" \
    --auth-mode opencode-basic \
    --username "$AA_OPENCODE_S0_USERNAME" \
    --password-file "$AA_OPENCODE_S0_PASSWORD_FILE" \
    --server-data-root "$AA_OPENCODE_S0_DATA_ROOT" \
    --workspace-directory "$AA_OPENCODE_S0_WORKSPACE_DIRECTORY" \
    --server-binary "$AA_OPENCODE_S0_BINARY" \
    --release-tag "$AA_OPENCODE_S0_RELEASE_TAG" \
    --release-commit "$AA_OPENCODE_S0_RELEASE_COMMIT" \
    --release-asset "$AA_OPENCODE_S0_RELEASE_ASSET" \
    --platform "$AA_OPENCODE_S0_PLATFORM" \
    --architecture "$AA_OPENCODE_S0_ARCHITECTURE" \
    --expected-server-version "$AA_OPENCODE_S0_VERSION" \
    --provider "$AA_OPENCODE_S0_PROVIDER" \
    --model "$AA_OPENCODE_S0_MODEL" \
    --state "$AA_OPENCODE_S0_EVIDENCE_DIR/pre-restart.json"

  # Trusted operator restarts the same binary with the same data root here.

  uv run python scripts/opencode_structured_output_eligibility_probe.py post-restart \
    --endpoint "$AA_OPENCODE_S0_ENDPOINT" \
    --auth-mode opencode-basic \
    --username "$AA_OPENCODE_S0_USERNAME" \
    --password-file "$AA_OPENCODE_S0_PASSWORD_FILE" \
    --server-data-root "$AA_OPENCODE_S0_DATA_ROOT" \
    --workspace-directory "$AA_OPENCODE_S0_WORKSPACE_DIRECTORY" \
    --server-binary "$AA_OPENCODE_S0_BINARY" \
    --expected-server-version "$AA_OPENCODE_S0_VERSION" \
    --state "$AA_OPENCODE_S0_EVIDENCE_DIR/pre-restart.json" \
    --output "$AA_OPENCODE_S0_EVIDENCE_DIR/eligibility-v1.json"
  ```

  Expected for v1.18.26: the pre-restart command atomically writes `PreStateV1(status="message_roundtrip_red")` containing the canonical `204/400/400` diagnostics, exits nonzero, makes no provider request, and the post-restart command refuses that state. A later candidate may close S0 only when pre writes `awaiting_restart`, both commands exit `0`, final status is `eligible`, and independent operator review matches every report digest to the exact invocation inputs while acknowledging the unverified restart-evidence level.

- [ ] **Step 5: Verify isolation and commit Task 0 only.**

  ```bash
  uv run pytest -q tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py
  uv run ruff check \
    scripts/opencode_structured_output_eligibility_probe.py \
    tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py
  uv run pyright \
    scripts/opencode_structured_output_eligibility_probe.py \
    tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py
  git add \
    scripts/opencode_structured_output_eligibility_probe.py \
    tests/agent_runtime/test_opencode_structured_output_eligibility_probe.py \
    tests/fixtures/opencode/v1.18.26-message-roundtrip-red.json
  git commit -m "test: probe OpenCode structured output eligibility"
  ```

  Expected: all local gates exit `0`; the fixture proves v1.18.26 is ineligible without provider use; `git diff --cached --name-only` contains exactly the three Task 0 files. Completing Task 0 does not make Checkpoint S0 green.

---

### Task 1: Freeze the provider-neutral structured activity and capability seam

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/attempts/structured_activity.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/__init__.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_structured_activity.py`

**Interfaces:** the immutable types and port in “Cross-plan interfaces”. Artifact/Kernel Task 5 consumes these exports and owns Feature/runtime requirement resolution.

- [ ] **Step 1: Add strict value-object tests.** Test canonical schema digest equality, exact schema/result byte bounds, sorted/unique exact-path/root/denial tuples, exact-file versus descendant semantics, deny-hole precedence, immutable candidate/digests, all closed dispatch/observation variants, ephemeral `StructuredActivityCallContext` generation/secret-view equality and exclusion from canonical projection, independent workspace invocation-scope/broker-binding/registration-receipt digests, the complete `StructuredActivityToolProductionQuiesced` identity/watermark/acknowledgement/digest invariants, dispatch-pending “proven not accepted” versus accepted/unknown receipt rules, closure-receipt invocation-scope/broker/generation/historical-fence/marker/command/injection-aggregate/network/digest invariants including canonical empty injection aggregate, and the provider-neutral pending/indeterminate close exceptions. Run:

  ```bash
  uv run pytest -q packages/framework/graph-engine/tests/attempts/test_structured_activity.py
  ```

  Expected failure: import error for `graph_engine.attempts.structured_activity`.

- [ ] **Step 2: Implement only the provider-neutral values and protocol.** Use frozen data/Pydantic models, existing canonical JSON/digest helpers, `JSONValue`, and `Protocol`. Reject an absent schema document, a digest mismatch, an oversized schema before any port call, intersecting paths, unknown observation status, a pending observation without an authentic quiescence receipt, a dispatch pending that proves neither non-acceptance/no namespace nor matching durable quiescence, independent broker-digest drift, or a closure with missing/noncanonical injection aggregate.

- [ ] **Step 3: Add the provider-neutral layering RED.** Inspect imports and canonical projections. Assert the new module imports no adapter, Product, or Capability package; contains no `OpenCode`, provider response-format, artifact document, projector, codec, serializer, finalizer, or materialization field; and excludes port/callable identity from every digest.

  ```bash
  uv run pytest -q packages/framework/graph-engine/tests/attempts/test_structured_activity.py \
    -k 'layering or canonical_projection'
  ```

  Expected failure: the activity module/exports do not exist.

- [ ] **Step 4: Export one seam.** Re-export the immutable request, workspace, binding, dispatch/observation unions, `StructuredActivityToolProductionQuiesced`, success candidate, raw-write closure, both close exceptions, ephemeral `StructuredActivityCallContext`, and `StructuredAgentActivityPort` from `graph_engine.attempts.__init__`. Do not add an executor, runtime binding, concrete adapter, document model, or file operation in this task.

- [ ] **Step 5: Prove layering and commit.**

  ```bash
  uv run pytest -q packages/framework/graph-engine/tests/attempts/test_structured_activity.py \
    -k 'structured_activity or layering or canonical_projection'
  uv run pyright packages/framework/graph-engine/graph_engine/attempts \
    packages/framework/graph-engine/tests/attempts/test_structured_activity.py
  uv run lint-imports
  git add \
    packages/framework/graph-engine/graph_engine/attempts/structured_activity.py \
    packages/framework/graph-engine/graph_engine/attempts/__init__.py \
    packages/framework/graph-engine/tests/attempts/test_structured_activity.py
  git commit -m "feat: define structured Agent activity seam"
  ```

### Task 2: Send the real OpenCode schema format and observe `info.structured`

**Files:**

- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/structured_output.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/protocol.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/reducer.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/__init__.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_protocol.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_prompt_admission.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py`

**Interfaces:** `OpenCodeJsonSchemaFormat`, `OpenCodeStructuredPromptBody`, `OpenCodeStructuredAgentActivityPort`, strict terminal assistant observer, shared session driver used by structured port and legacy handler.

- [ ] **Step 1: Add the exact admission-body test.** Construct one request with an authenticated schema and assert:

  ```python
  assert body["format"] == {"type": "json_schema", "schema": schema}
  assert "retryCount" not in body["format"]
  assert set(body) <= {"messageID", "parts", "agent", "model", "tools", "format"}
  assert canonical_json_text(schema) not in "\n".join(part["text"] for part in body["parts"])
  ```

  Also assert missing/drifted/oversized schema fails before `OpenCodeHttpClient.admit_message`.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_protocol.py \
    -k 'admission or schema'
  ```

  Expected failure: `OpenCodePromptAdmissionBody` forbids `format` and the production prompt embeds schema prose.

- [ ] **Step 2: Implement strict request models.** Add `format` only to the structured body builder; remove “return exactly one JSON object” and embedded-schema prose from that builder while preserving task, tool, workspace, and side-effect instructions. Leave the legacy builder callable only from legacy shadow.

- [ ] **Step 3: Add terminal observation tests.** A completed assistant envelope with `info.structured` succeeds. A 204 followed by no terminal assistant stays running. A text JSON object, prose substring, `StructuredOutput` tool input, `info.structured_output`, user-message structured field, incomplete assistant message, or assistant envelope with any `info.error` never succeeds.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_protocol.py \
    -k 'terminal or structured or admission_is_not_completion'
  ```

  Expected failure: current `structured_result_from_messages()` extracts prose/tool content and ignores `info.structured`.

- [ ] **Step 4: Implement the structured observer and local validation.** Select exactly one completed assistant message, require no error, read only `info.structured`, require a JSON object, apply canary/credential checks and the existing locked result-schema validator, and return an untrusted candidate/digests. Two different terminal structured candidates are indeterminate. Keep Kernel model validation out of this module.

- [ ] **Step 5: Refactor one session driver, not two dispatch engines.** `OpenCodeStructuredAgentActivityPort` and the legacy `OpenCodeHandler` share create/discover/bind/admit/observe primitives. The structured port implements only the provider-neutral methods. Do not have it return `OutputT`, materialize bytes, or call finalize.

- [ ] **Step 6: Run the adapter gate and commit.**

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_protocol.py \
    packages/adapters/agent-runtime-opencode/tests/test_prompt_admission.py \
    packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py
  uv run pyright packages/adapters/agent-runtime-opencode/agent_runtime_opencode
  git add \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/structured_output.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/protocol.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/reducer.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/handler.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/__init__.py \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_protocol.py \
    packages/adapters/agent-runtime-opencode/tests/test_prompt_admission.py \
    packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py
  git commit -m "feat: use OpenCode structured output protocol"
  ```

### Task 3: Close restart, repeated-read, and terminal-error semantics

**Files:**

- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/discovery.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/reducer.py`
- Create: `tests/helpers/opencode_fake_server.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/fake_server.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/harness.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_recovery.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_errors.py`

**Interfaces:** durable `OpenCodeActivityReference` binds server/certification/request/schema/prompt identity; re-observation adopts the same session/message; terminal error matrix below.

| Observation | Required result |
| --- | --- |
| 204 admission, busy/incomplete assistant, or open tool work | `running`, never success |
| transport failure before a session is bound | dispatch failure; no invented reference |
| transport/malformed/oversized response after bind | `indeterminate` |
| `StructuredOutputError`, `ContentFilterError`, `MessageOutputLengthError`, `ContextOverflowError` | terminal failed; no candidate |
| `MessageAbortedError` or session abort | canceled; no candidate |
| `APIError` | terminal failed with existing transient classifier; no candidate |
| terminal assistant without `info.structured`, wrong JSON type, schema rejection, credential/canary | invalid output; no candidate |
| same bound activity observed repeatedly | identical candidate/message/schema/observation digests |
| re-observation differs from a durable expected terminal digest | `indeterminate`; never overwrite or redispatch |

- [ ] **Step 1: Make fake server state detachable, importable, and its live identity configurable.** Put the reusable scripted HTTP server and persisted-state object in `tests/helpers/opencode_fake_server.py`; make the adapter-private `tests/fake_server.py` a thin compatibility re-export so existing adapter tests retain their imports and Product tests can import `tests.helpers.opencode_fake_server` without reaching into another package's private test tree. A new HTTP server and a new adapter instance must expose the same sessions/messages after process restart. Give that state an explicit immutable `server_version` used by `/global/health` instead of the current hard-coded `opencode-http-v1`; default ordinary protocol tests to a clearly synthetic version and let integration tests inject an exact promoted version. Persist user `info.format` and terminal `info.structured`; do not synthesize text JSON on the structured path. The fake advertises an identity only—the production adapter still authenticates that identity against the installed certification record.

- [ ] **Step 2: Add list/single-message/restart and quiescence-carrier tests.** Cover: admission → server restart → single-message identity read; terminal → server restart → message-list observation; adapter restart with the same durable reference; crash after terminal before Kernel result persistence; all cases assert one session create and one prompt POST. Extend the restartable fake state with the durable provider-neutral quiescence receipt/reference used later by the real boundary: running/pending observations must carry matching identity/watermarks; request accepted followed by response loss must rediscover the same activity, quiesce, and return dispatch pending with that receipt; a proven pre-acceptance failure may return the explicit no-namespace pending variant. Simulate a late tool arrival after each pending result and require it remain queued until exact reattachment. Acknowledgement-before-durable-receipt/process-death, watermark drift, wrong broker registration, and failed reattachment are indeterminate. Add one health test proving two detached server processes retain the configured `server_version`, and another proving a different configured version is observable as drift.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_recovery.py
  ```

  Expected failure: the fake has no restartable persisted state and the current reference does not bind structured schema/certification identity.

- [ ] **Step 3: Implement adopt/re-observe identity.** Bind `server_version`, certification digest, `agent_result_schema_digest`, expected user-message ID, prompt-body digest, stable activity reference, and broker binding/registration identities into `OpenCodeActivityReference`. Recovery reads the same session; it never calls session create or `prompt_async` after binding. Persist/adopt quiescence only through the boundary/broker durable owner introduced by Task 6; the adapter carries and verifies the provider-neutral value but does not invent an in-memory receipt. Compare any durable expected terminal or quiescence digest before returning repeated success/pending.

- [ ] **Step 4: Parameterize the complete error matrix.** Use the table above plus missing tool output, malformed tool arguments, duplicate conflicting terminal candidates, 400 message-list, 400 single-message, 404 formerly bound session, response limit, and malformed identity-bearing SSE. For every row assert outcome/status, retryability only where already defined, zero successful candidate, and redacted diagnostics.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_errors.py
  ```

  Expected failure: current reduction can accept text/tool payloads and lacks OpenCode structured error names.

- [ ] **Step 5: Implement minimal typed error reduction and rerun recovery suites.** Keep OpenCode `retryCount` absent and never infer a retry count from error payloads. Treat uncertain request acceptance, missing/malformed quiescence, or failed exact reattachment as indeterminate; never collapse it to ordinary pending or redispatch.

  ```bash
  uv run pytest -q \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_recovery.py \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_errors.py \
    packages/adapters/agent-runtime-opencode/tests/test_create_recovery.py \
    packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py \
    packages/adapters/agent-runtime-opencode/tests/test_observation.py
  ```

- [ ] **Step 6: Commit recovery/error closure.**

  ```bash
  git add \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/discovery.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/reducer.py \
    tests/helpers/opencode_fake_server.py \
    packages/adapters/agent-runtime-opencode/tests/fake_server.py \
    packages/adapters/agent-runtime-opencode/tests/harness.py \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_recovery.py \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_errors.py
  git commit -m "test: close OpenCode structured recovery matrix"
  ```

### Task 4: Gate the capability on a pinned certified OpenCode runtime

**Files:**

- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/certification.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-transport-v1.json`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/config.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/plugin.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/discovery.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/plugin-declaration.json`
- Modify: `packages/adapters/agent-runtime-opencode/pyproject.toml`
- Modify: `packages/products/assurance-product/assurance_product/source_catalog.py`
- Modify: `packages/products/assurance-product/assurance_product/binding_builder.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/resources/declarations/product.yaml`
- Modify: `packages/products/assurance-product/assurance_product/resources/declarations/deployment-plugin.yaml`
- Modify: `packages/products/assurance-product/assurance_product/product-declaration-opencode.json`
- Modify: `packages/products/assurance-product/pyproject.toml`
- Modify: `packages/products/assurance-product/README.md`
- Modify: `uv.lock`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_config.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/harness.py`
- Modify: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product.py`
- Modify: `tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-opencode-declaration.json`
- Modify: `tests/phase4/fixtures/bindings-opencode/plugin.yaml`
- Modify: `tests/phase4/test_six_wheel_composition.py`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/bindings.py`
- Modify: `examples/agent-runtime-fixture/agent_runtime_fixture/opencode-binding-declaration.json`
- Modify: `tests/agent_runtime/test_fixture_rebinding.py`
- Modify: `tests/product/test_workflow_module_security.py`
- Modify: `tests/product/test_product_providers.py`

**Interfaces:** `StructuredOutputTransportCertificationV1`; optional digest-bound certification reference in `OpenCodeAdapterConfig`; `capabilities_for(...)`; Product mapping `requires_structured_output -> opencode_structured_output`; adapter version `0.2.0`.

- [ ] **Step 1: Add negative-version and honest-advertisement tests.** Checkpoint S0 is a program-order prerequisite only; do not add its report or digest to Adapter config, installed resources, runtime resolution, `capabilities_for(...)`, or promotion input. Re-run the no-model `204 -> 200 list -> 200 single` sequence independently inside the production certification suite before any provider/model call. Fixed 1.18.4 and v1.18.26 raw reports have 204 admission but failed list/single-message checks and cannot validate as positive eligibility or certification. Missing production certification, unknown version, actual-health version drift, incomplete check set, or report digest drift yields no `opencode_structured_output`. A required request fails before session create/prompt POST.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py
  ```

  Expected failure: no certification model/capability resolver exists.

- [ ] **Step 2: Implement the transport certification model.** Require explicit passing checks for schema admission, terminal `info.structured`, adapter restart, controller-proven server restart, message-list, single-message, every deterministic transport/error class, repeated read, and the real boundary-plugin/tool/broker/sandbox probe. Keep version-level transport/fault evidence distinct from the 33 provider/model/schema compatibility rows inside one closed report. Bind adapter/server binary/version, disposable-deployment controller/profile/fault-script identities, boundary plugin/policy/profile digests, provider/model matrix, raw-report digest, and certification digest. `capabilities_for` returns either `frozenset({"opencode_structured_output"})` or `frozenset()`.

- [ ] **Step 3: Pin live identity and resolve the generic requirement before dispatch.** Extend config with an optional expected server version and certification digest for coexistence. `_observe_fingerprint` compares `/global/health` version and the installed certification before creating/adopting an unbound session. In Product `binding_builder.py`, map generic `requires_structured_output` to `opencode_structured_output` only when the selected handler is the authenticated OpenCode adapter and `capabilities_for(...)` contains that exact token; persist the resulting generic `satisfied_requirements` and adapter-capability-set digest in `AgentRuntimeBinding`. A missing capability or identity mismatch is configuration failure before session creation, not fallback.

- [ ] **Step 4: Bump all active OpenCode identities atomically.** Change only the OpenCode adapter package/plugin/source/Product optional dependency/Product declaration/deployment requirement from `0.1.0` to `0.2.0`; leave Cursor at `0.1.0`. This includes `tests/phase4/fixtures/bindings-opencode/plugin.yaml`, which currently pins the active OpenCode fixture. Update production `product.py` to stop assigning one shared runtime version to both adapters. Make the Phase 4 product fixture and neutral agent-runtime binding fixture choose the dependency version by adapter ID, then regenerate only their OpenCode declaration JSON; their Cursor declarations remain byte-for-byte at `0.1.0`. Replace adapter-test literals in `test_terminal_reduction.py` and `harness.py` with `ADAPTER_VERSION` (or the canonical reference builder) so replay fixtures cannot drift from the package identity. Regenerate/update `product-declaration-opencode.json`, run `uv lock`, and add assertions in the Phase 4 and agent-runtime fixture suites that OpenCode is `0.2.0` while Cursor is `0.1.0`. Historical benchmark manifests, logs, and evidence remain immutable and may continue to report the version that produced them. Keep the transport resource initially with no positive record; the release gate later replaces it only from a passing report.

- [ ] **Step 5: Verify and commit.**

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py \
    packages/adapters/agent-runtime-opencode/tests/test_config.py \
    packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py \
    tests/agent_runtime/test_fixture_rebinding.py \
    tests/phase4/test_six_wheel_composition.py \
    tests/product/test_workflow_module_security.py \
    tests/product/test_product_providers.py
  uv lock --check
  uv run pyright packages/adapters/agent-runtime-opencode/agent_runtime_opencode
  git add \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/certification.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-transport-v1.json \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/config.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/plugin.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/discovery.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/plugin-declaration.json \
    packages/adapters/agent-runtime-opencode/pyproject.toml \
    packages/products/assurance-product/assurance_product/source_catalog.py \
    packages/products/assurance-product/assurance_product/binding_builder.py \
    packages/products/assurance-product/assurance_product/product.py \
    packages/products/assurance-product/assurance_product/resources/declarations/product.yaml \
    packages/products/assurance-product/assurance_product/resources/declarations/deployment-plugin.yaml \
    packages/products/assurance-product/assurance_product/product-declaration-opencode.json \
    packages/products/assurance-product/pyproject.toml \
    packages/products/assurance-product/README.md \
    uv.lock \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py \
    packages/adapters/agent-runtime-opencode/tests/test_config.py \
    packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py \
    packages/adapters/agent-runtime-opencode/tests/harness.py \
    tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product.py \
    tests/phase4/fixtures/six-wheel-product/test_assurance_phase4_product/product-opencode-declaration.json \
    tests/phase4/fixtures/bindings-opencode/plugin.yaml \
    tests/phase4/test_six_wheel_composition.py \
    examples/agent-runtime-fixture/agent_runtime_fixture/bindings.py \
    examples/agent-runtime-fixture/agent_runtime_fixture/opencode-binding-declaration.json \
    tests/agent_runtime/test_fixture_rebinding.py \
    tests/product/test_workflow_module_security.py \
    tests/product/test_product_providers.py
  git commit -m "feat: gate OpenCode structured capability by version"
  ```

### Task 5: Certify the exact 33-contract provider/model/schema matrix

**Files:**

- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/conformance.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/deployment_control.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/conformance-transport-profile-v1.json`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/conformance-fault-script-v1.json`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_conformance.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`
- Modify: `packages/products/assurance-product/assurance_product/runtime_bindings.py`
- Modify: `packages/products/assurance-product/assurance_product/binding_builder.py`
- Modify: `packages/products/assurance-product/assurance_product/cli.py`
- Create: `packages/products/assurance-product/assurance_product/resources/schemas/binding-wheel-build-v1.schema.json`
- Create: `tests/product/test_opencode_structured_output_matrix.py`
- Modify: `tests/product/test_semantic_attempt_bindings.py`
- Modify: `tests/product/test_binding_builder_security.py`
- Modify: `tests/product/test_cli_bindings_build.py`

**Interfaces:** canonical `StructuredOutputCompatibilityRow` whose lookup key stays transport-only but whose row digest also binds sorted toolchain requirement IDs and network/command-secret modes/requirement IDs; `OpenCodeGateDeploymentController` JSON protocol; `TransportHarnessReportV1`; `BoundaryToolIntegrationReportV1`; `ProviderMatrixReportV1`; aggregate `StructuredOutputGateReportV1`; exact `python -m agent_runtime_opencode.conformance {export-catalog,run,promote}` CLI; closed `BindingWheelBuildRecordV1` plus canonical schema/serializer; `aa bindings build --structured-output-certification --structured-network-target-catalog --structured-command-secret-bindings --build-record ABS_PATH`; Boot exact-set, target-catalog/secret-handle closure, and pre-dispatch row resolver.

- [ ] **Step 1: Freeze the catalog test.** Resolve installed semantic contracts/bindings and assert exactly 33 rows with named owner counts **Execution 2 / Generation 14 / Healing 2 / Improvement 6 / Intake 4 / Quality 5**, every Feature contract requires structured output, and every row contains the exact canonical Agent-result schema digest/limits plus sorted toolchain requirement IDs, explicit network mode/sorted requirement IDs, and command-secret mode/full canonical requirement values (the transport row derives sorted requirement IDs from those values). Assert the exact toolchain mapping from the Capability plan: both Execution rows carry exactly `sut-api-test-runner-v1`, `sut-e2e-test-runner-v1`, `sut-fuzz-test-runner-v1`, and `sut-performance-benchmark-runner-v1` in canonical order; the other 31 rows—including all 14 Generation rows—carry an empty tuple. Assert exact `10 input_conditioned/23 none` network and `2 input_conditioned/31 none` command-secret partitions: all eight Generation reviewer/primary-codegen rows name only `sut-openapi-read-v1`; both Execution rows name backend/frontend test requirements and the full Task-4-owned `sut-admin-credential-v1` requirement; all other sets are empty. No row uses `provider_default`, fallback routing, wildcard fields, a generic shell/toolchain grant, or a broad shared runtime-target requirement.

  ```bash
  uv run pytest -q tests/product/test_opencode_structured_output_matrix.py -k catalog
  ```

  Expected failure: no compatibility row/report or matrix resolver exists.

- [ ] **Step 2: Implement canonical matrix construction.** Build rows from the authenticated 33-contract registry plus 33 Product runtime bindings. Deduplicate nothing by the current 19 legacy resource files: two contracts sharing schema bytes remain two contract rows. Add toolchain IDs plus the two mode/requirement families to canonical row metadata and row digest, export/run/promote/Boot exact equality, while leaving the frozen compatibility lookup key `(adapter, server, contract, provider/model, schema digest, schema/result limits)` unchanged. Include row/report digests in Product binding/build projections.

- [ ] **Step 3: Test the conformance runner, deployment-control protocol, and all three module-CLI commands.** Freeze an authenticated, disposable deployment-controller protocol with length-bounded canonical request/response frames: `describe`, `start(profile_digest)`, `set_fault(case_id, nonce)`, `clear_fault`, `restart(expected_instance_id)`, `status`, and `stop`. Every receipt binds controller implementation digest, candidate OpenCode binary digest/version, immutable deployment/profile/plugin/provider configuration digests, old/new process instance IDs, monotonic generation, timestamps, and the run nonce. The runner rejects a controller that reuses an instance ID/generation, changes binary/config, skips a fault transition, cannot prove stop/restart, or points at the normal provider endpoint. Controller trust is release-test-equipment trust only; do not claim attestation.

  With the restartable fake controller, run the version-level transport harness separately from the normal 33-row matrix. The transport harness creates/binds a session, restarts the actual server process through the controller, reconstructs the adapter, then proves list/single/repeated observation without a second prompt. It drives every frozen fault-script case deterministically through the controlled provider—structured-output, content-filter, output-length, context-overflow, aborted, API error, malformed/oversized/list/single failures—and requires exact reducer outcomes. The provider matrix posts each exact schema to the operator's normal endpoint, waits for terminal `info.structured`, and validates schema-owned probe values and result-size bounds; it does not pretend naturally occurring faults prove the transport suite. Make one row/fault/restart receipt drift and assert the aggregate report cannot be promoted.

  Test the two authoritative binding-build inputs here. `--structured-network-target-catalog ABS_PATH` accepts one owner-only, no-follow regular canonical JSON document matching the installed target-catalog schema; it rejects credentials/userinfo/query/fragment, unknown target classes/alias keys/secret-requirement acceptance, duplicate IDs, unbounded rows, and raw command policy. The builder runs the installed controlled resolver, freezes address sets/synthetic aliases/accepted-secret IDs/upstream-TLS policy digests, and embeds source plus resolved catalog/version/resolver digests in the generated binding wheel/ProductLock. `--structured-command-secret-bindings ABS_PATH` accepts only canonical requirement-ID→authorized-handle-ID rows, never values, source locators, or environment contents. Its key set must equal the union of Feature-declared command-secret requirement IDs; its mapped handle set, unioned with the preexisting adapter-only activity-handle set, must exactly equal `AgentRuntimeBinding.authorized_secret_handles`. Adapter-only handles are not command bindings; one command requirement may map one handle to its complete alias tuple; repeated legal handle identity is set-normalized and missing/extra handles fail. Both paths must be absolute, outside the SUT/worktree, owner-owned, non-symlink, non-group/world-writable, size-bounded, and explicit; no `.aa`, ambient environment, prompt, or Product default is a fallback. Change one catalog row, address/TLS pin, alias, accepted secret requirement, requirement/handle mapping, authorized union, or file identity and require binding/lock/Boot drift before Attempt creation.

  Freeze `--build-record ABS_PATH` and `BindingWheelBuildRecordV1` in the same RED. The path is explicit, absolute, outside the worktree/SUT, beneath an owner-only output/evidence directory, and must not already exist or be a symlink. The closed canonical record/schema contains exactly: format ID `binding-wheel-build-v1`; expected wheel filename and SHA-256; deployment manifest source digest; resolved Product-input digest; ProductLock digest; promoted structured-output certification source/resolved digest; structured-network target-catalog source/resolved digest; structured-command-secret-binding source/resolved digest; builder version/policy digest; and record digest. It contains no endpoint row, secret handle source/value, key, host path, timestamp, workflow identity, or nondeterministic field. Assert the generated schema golden equals the model schema, canonical bytes are stable, a field/digest/filename mutation fails, and failure before complete wheel fsync leaves neither a record nor a partial target. Workflow revision/run provenance remains outside this record and belongs to Capability Task 12's protected artifact handoff.

  Freeze the corresponding **conformance-run** flags here as well: `--network-target-catalog`, `--command-secret-bindings`, `--command-secret-source`, `--secret-generation-key-file`, and `--secret-generation-key-id`. The first two reuse the exact validators above. The synthetic source is an owner-only no-follow file containing only the exact authorized test handle/source identity/value required by the candidate boundary case; the key producer is Capability Task 11's sole production owner-only file/key-ID implementation. `run` constructs a candidate-only ephemeral Product binding from these values plus the unpromoted candidate identity, never installs/advertises it, exercises selected-target/one-shot-injection/quiescence/restart/rotation, and writes only value-free source/catalog/binding/key-ID/generation evidence digests. Reject missing/extra handles, key/source permission or identity drift, a source/key in the worktree/database/environment, candidate binding discovery as a production entry point, or any raw value/key/business endpoint in report bytes. Invoke `main(...)` with every new flag and parameterize omission/drift/rotation/restart so Task 7 cannot call an undeclared CLI.

  Also exercise the exact installed `assurance-boundary.mjs` through the controlled pinned server, not by importing it directly: its session-registration receipt must show builtin shell removed and only classified tools; an actual model/tool turn must invoke `assurance_exec`, cross the Product UDS broker, run in the qualified sandbox/composed command view, write one declared raw sentinel, reach one bound synthetic SUT target, fail to reach an unbound endpoint and typed/control sentinels, close raw writes, and return broker/view/network/closure receipts bound to the same plugin/policy/profile/binary/run nonce. Invoke `main(...)` for `export-catalog`, `run`, and `promote` with temporary paths and assert exact exit codes, atomic output, canonical bytes, refusal to overwrite installed records from a failed/incomplete/edited report, and absence of secret/canary/raw business-address bytes. Also execute `python -m agent_runtime_opencode.conformance --help` in the focused suite so Task 7 cannot depend on an undeclared entrypoint.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_conformance.py
  ```

  Expected failure: conformance CLI/report code is absent.

- [ ] **Step 4: Implement the frozen CLI plus fail-closed Boot and builder integration.** `conformance.py` exposes `main(argv: Sequence[str] | None = None) -> int` and a `__main__` guard. `export-catalog` resolves the installed 33-row catalog. `run` requires an explicit normal endpoint/secret, catalog, absolute controller command/socket, controller identity allow-record, transport/fault profile resources, boundary plugin plus sandbox/toolchain/network qualification records, frozen network-target catalog, value-free command-secret bindings, synthetic authorized source, production generation-key file/key ID, and output path; it runs three isolated sections—controlled transport/fault, controlled real-boundary integration with candidate-only ephemeral binding, and normal 33-row provider matrix—and always closes runtime ports/stops the disposable deployment in `finally`. `promote` accepts only one aggregate report with complete/pass transport, fault, provider, boundary, L7-network, quiescence, and one-shot-injection/no-leak/rotation-restart sections and atomically writes both installed records. All commands refuse repository-relative implicit evidence/controller/key/source/catalog paths, unqualified controller/binary/plugin/profile/network drift, redact secrets/business targets, and leave no partial output. `aa bindings build --structured-output-certification PATH --structured-network-target-catalog ABS_PATH --structured-command-secret-bindings ABS_PATH --build-record ABS_PATH` validates all four inputs, embeds the first three inputs' canonical source/resolved metadata (never secret values) in the generated installed binding wheel, binds their digests, and deterministically constructs `BindingWheelBuildRecordV1` from the exact same in-memory resolved values plus final wheel bytes. After wheel file and parent directory fsync, atomically install/fsync the canonical record at the explicit new path; on any earlier failure write no record. The record may be a distinct sibling of the wheel beneath the owner-only output directory, but must never equal/alias the wheel, an input, install directory, output directory itself, or another target, and no existing target may be overwritten. The latter two authority options are required whenever any selected contract has non-`none` network/command-secret mode and otherwise must be omitted or canonical-empty; `--build-record` is always required for a release-capable build. Boot requires exact row equality plus transport/boundary/sandbox/toolchain/network/catalog/command-secret-binding digests for every selected OpenCode contract before advertising capability. Missing/extra/drifted rows or sections fail; Cursor remains unaffected. The adapter rechecks only the selected structured-output row/boundary qualification before session creation; Product alone consumes the resolved target catalog and command-secret mapping.

- [ ] **Step 5: Run focused suites and commit.**

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_output_conformance.py \
    tests/product/test_opencode_structured_output_matrix.py \
    tests/product/test_semantic_attempt_bindings.py \
    tests/product/test_binding_builder_security.py \
    tests/product/test_cli_bindings_build.py
  uv run python -m agent_runtime_opencode.conformance --help
  uv run lint-imports
  git add \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/conformance.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/deployment_control.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/conformance-transport-profile-v1.json \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/conformance-fault-script-v1.json \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_conformance.py \
    packages/products/assurance-product/assurance_product/models.py \
    packages/products/assurance-product/assurance_product/runtime_bindings.py \
    packages/products/assurance-product/assurance_product/binding_builder.py \
    packages/products/assurance-product/assurance_product/cli.py \
    packages/products/assurance-product/assurance_product/resources/schemas/binding-wheel-build-v1.schema.json \
    tests/product/test_opencode_structured_output_matrix.py \
    tests/product/test_semantic_attempt_bindings.py \
    tests/product/test_binding_builder_security.py \
    tests/product/test_cli_bindings_build.py
  git commit -m "feat: certify OpenCode contract compatibility matrix"
  ```

### Task 6: Enforce typed-path deny holes at every OpenCode raw-write seam

**Dependency/order:** Task 6 runs after Task 4, Capability Tasks 1–2, Artifact/Kernel Task 5, and Artifact/Kernel Tasks 6–8. Artifact Task 4 defines the provider-neutral toolchain requirement value/registry; Capability Task 2 installs the exact four Product-owned semantic rows; Artifact Task 5 resolves those values and defines provider-neutral network values; Tasks 6–8 define the sole `AttemptSnapshotAdmissionPort`, immutable input snapshot/blob store, secret lifetime, and v2 identity consumed here. Run Task 6 before Artifact/Kernel Task 9 and before any Capability row flips from raw to typed. Use inventory-owned intended typed paths and network-target fixtures as non-authoritative attack fixtures, then rerun this task after Task 5 and Capability Tasks 3–9 against the final authenticated catalog/requirements.

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/structured_activity.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/workspace_binding.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/command_sandbox.py`
- Create: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/command_view.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_structured_workspace_access.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_raw_write_closure.py`
- Create: `packages/adapters/agent-runtime-opencode/tests/test_command_sandbox.py`
- Create: `packages/products/assurance-product/assurance_product/command_sandbox.py`
- Create: `packages/products/assurance-product/assurance_product/command_sandbox_broker.py`
- Create: `packages/products/assurance-product/assurance_product/command_execution_store.py`
- Create: `packages/products/assurance-product/assurance_product/command_view.py`
- Create: `packages/products/assurance-product/assurance_product/command_network.py`
- Create: `packages/products/assurance-product/assurance_product/command_secrets.py`
- Create: `packages/products/assurance-product/assurance_product/network_requirements.py`
- Create: `packages/products/assurance-product/assurance_product/network_targets.py`
- Create: `packages/products/assurance-product/assurance_product/toolchains.py`
- Create: `packages/products/assurance-product/assurance_product/resources/sandbox/structured-command-sandbox-v1.json`
- Create: `packages/products/assurance-product/assurance_product/resources/sandbox/structured-toolchain-python-runners-v1.json`
- Create: `packages/products/assurance-product/assurance_product/resources/sandbox/structured-network-egress-v1.json`
- Create: `packages/products/assurance-product/assurance_product/resources/schemas/structured-network-target-catalog-v1.schema.json`
- Create: `tests/product/test_structured_command_sandbox.py`
- Create: `tests/product/test_structured_command_toolchains.py`
- Create: `tests/product/test_structured_command_recovery.py`
- Create: `tests/product/test_structured_command_network.py`
- Create: `tests/product/test_structured_command_secrets.py`
- Create: `tests/product/test_structured_network_requirements.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-opencode.json`
- Modify by declaration regeneration: `packages/products/assurance-product/assurance_product/product-declaration-cursor.json`
- Modify: `packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py`
- Create: `scripts/qualify_structured_command_sandbox.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/products/assurance-product/assurance_product/opencode_agents.py`
- Modify: `packages/products/assurance-product/assurance_product/resources/opencode/assurance-boundary.mjs`
- Create: `packages/products/assurance-product/assurance_product/resources/opencode/workspace-binding-v2.schema.json`
- Modify: `tests/product/test_opencode_staging_boundary.py`

**Interfaces:** `aa-workspace-binding-v2`; exact activity-reference, broker-binding, broker-registration-receipt, input-snapshot/policy, nullable toolchain-profile/invocation-scope/network-access, sandbox/network backend/policy/qualification digests; `effective_raw_paths`; `effective_raw_roots`; `typed_output_denials`; Artifact-owned `StructuredToolchainRequirement`/registry, `StructuredToolchainInvocationSelection`/`BoundStructuredToolchainInvocationScope`, and `StructuredNetworkRequirementEntry` registry, provider-neutral requirement/resolved/bound network values, sole `ResolvedStructuredNetworkTargetCatalog`, `StructuredCommandBrokerRegistrationRequest`/port/session, and command-secret injection request/sink/entry/receipt/port; consumes Capability Task 2's Product-installed exact four-row toolchain-requirement contribution and installs the concrete qualified profile plus three-row network-requirement contribution, sole provider-neutral `ResolvedStructuredToolchainProfile`, `StructuredCommandNetworkPort`, L7-capable `LinuxNetworkNamespaceEgressGateway`, broker-registration implementation, and write-only injection implementation; immutable `StructuredCommandViewManifest`/`StructuredCommandViewReceipt`; authenticated `StructuredCommandSandboxPort`/policy digest; Product-owned `StructuredCommandSandboxBroker` session and installed `assurance_exec` OpenCode tool; durable tool-production-quiescence and `raw_writes_closed` generation/marker plus command/network/injection transcripts; closed v2 tool classification; v1 legacy compatibility. `toolchains.py` consumes `toolchain_requirements.py`, and `network_targets.py` reuses the core-owned catalog type; neither may define a second requirement/catalog type. Architecture tests pin their module owners.

- [ ] **Step 1: Add path-closure tests.** Assert exact raw paths, raw tree roots, and typed exact denials are sorted, finite, unique, and canonically typed; a typed-artifact-only request has both raw tuples empty; an exact raw file does not authorize `file/child` or a same-prefix sibling; only a declared tree root authorizes descendants; a typed child beneath that root is denied first; unresolved patterns or an untyped target fail before port dispatch. Assert the structured prompt separately advertises writable raw files/roots and never tells OpenCode to write typed paths.

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_workspace_access.py
  ```

  Expected failure: only v1 `allowed_outputs` exists and there is no typed deny-hole field.

- [ ] **Step 2: Implement and digest workspace binding v2.** Stamp stable `activity_reference_digest`, opaque `structured_command_broker_binding_digest`, `input_snapshot_digest`, `input_snapshot_policy_digest`, `structured_toolchain_profile_digest` and `structured_toolchain_invocation_scope_digest` (nullable together only for an empty requirement set), nullable `structured_command_secret_set_digest`, `sandbox_policy_digest`, `sandbox_qualification_digest`, nullable-together `structured_network_access_digest`, `structured_network_backend_digest`, `structured_network_policy_digest`, and `structured_network_qualification_digest`, all three path tuples, and existing session/Attempt/workspace identity plus broker registration receipt digest into both `StructuredWorkspaceAccess` and `aa-workspace-binding-v2`. The trusted plugin and broker require exact equality with the active dispatch/workspace/Product profile/invocation scope/catalog/gateway/broker-registration records; any missing, partial-null, or drifted digest fails before adapter dispatch, tool registration, native read, or command network setup. Route native read operations only through the immutable input-snapshot descriptor and never the live canonical project. Keep v1 parser/builder unchanged for legacy shadow. Make v2 mutation checks test typed exact denials, then exact raw-file equality, then raw-root descendant containment. Never convert an exact file into a prefix root or infer a root from `/**` text.

- [ ] **Step 3: Add security, composed-view, toolchain, and durable-command tests for every mutation channel and indirect command execution.** Parameterize native `write`, `edit`, `apply_patch`, `artifact_write`, the installed `assurance_exec` tool, a fake MCP writer, and a fake custom writer against a typed target. For v2, assert builtin `bash`/shell is absent or rejected before execution; no hook may merely rewrite its command and then let OpenCode run it locally. Assert the canonical and staged typed paths remain byte-identical/absent, and diagnostics say the path is Kernel-owned. In `test_command_sandbox.py`, execute apparently benign permitted commands whose test script/subprocess tries relative traversal, absolute paths, inherited file descriptors, symlinks, and a detached child against canonical typed output, staged typed output, workspace `control/`, raw blob store, Attempt journal, closure marker, and frozen promotion set. The OS sandbox must make every target absent or inaccessible while still permitting declared raw output and bounded scratch; after import, Kernel path/digest checks remain mandatory. In `test_structured_command_toolchains.py`, authenticate the exact four Product-owned semantic requirement rows already installed by Capability Task 2 and reject missing/extra/drifted/Feature/project rows. Resolve both Execution contracts to all four and all other 31 contracts to none. From `/workspace/project`, execute one deterministic fixture through each logical API, E2E, Fuzz, and Performance recipe over a composed view containing immutable SUT source/config plus only declared generated raw members at authenticated logical target paths. Assert `assurance_exec` accepts only `logical_recipe_id + typed recipe_parameters`, never argv; the request must equal one unused row of the Kernel-registered `BoundStructuredToolchainInvocationScope`, not merely be a subset. Assert closed-mapping equality, exact selected-test node IDs, bounded integer benchmark parameters, exactly one command per selected recipe, deterministic same-scope-to-same-derived-argv equality, the same relative imports/test discovery as the canonical layout, exact cwd/path mapping, no live project/venv/cache access, typed deny-hole absence, qualified executable/dependency closure, stable scope/view/payload/receipt digests, and identical recovery rebuild. Reject unknown/duplicate/cross-contract recipes, broadened or narrowed test IDs, mismatched/overflow parameters, model-supplied argv/executable/environment, and scope replay drift. Assert Generation cannot request any recipe and that no generic shell, formatter, LSP, Node, install, or undeclared command is accepted.

  In `test_structured_command_recovery.py`, crash at durable view publication, command prepare, before-launch, after the fsynced `launching` transition, after sandbox exit/output capture, during durable output-blob import, and after terminal record before broker response. Assert only `prepared` with a durable not-launched proof may launch on adoption; `completed`/`failed` is adopted and output import is idempotent; `launching` without a durable terminal supervisor record is permanently indeterminate and never redispatched; raw-write close blocks/maps through the provider-neutral indeterminate path. Test forged/replayed broker request, wrong Attempt/activity/workspace/generation/fence/policy/input/profile/view/network-access/backend/qualification digest, missing broker, timeout and lost response; none may fall back to local shell/network or import output. Add positive mixed-mode raw writes and a typed-only zero-write-tool test.

  In `test_structured_network_requirements.py`, install exactly the Product-owned shared rows `sut-openapi-read-v1`, `sut-test-backend-v1`, and `sut-test-frontend-v1` through the Artifact-owned registry and assert Product owner/provenance/entry digests, RegistrySet/ProductLock/graph-revision/dry-runtime parity, and rejection of missing/extra/duplicate/non-Product/project-loaded/drifted rows. The eight exact Generation plan-review/primary-codegen contracts resolve only the OpenAPI-read row; the two Execution contracts resolve backend/frontend rows; every other contract resolves an empty tuple. In `test_structured_command_network.py`, prove default-deny with all network fields `None`, then allow exact pre-key requirement→target selection rows through Product-issued aliases. Exercise one `product-full` input with concurrent OpenAPI/backend/frontend selections and prove each nested Feature receives only its declared rows; reject missing/extra/cross-requirement IDs and checkpoint replay drift. Deny raw URL/IP literals, alternate port/protocol/address family, unauthorized redirect, DNS rebinding, direct/proxy bypass, UDP/raw/unix sockets, registry egress, and loopback/private targets unless the exact catalog row authorizes them. The OpenAPI row must traverse the qualified Product L7 synthetic-origin proxy: allow canonical GET/HEAD/OPTIONS only under its bound OpenAPI path scope; deny POST/PUT/PATCH/DELETE, CONNECT/upgrade, encoded path escape, non-OpenAPI path, redirect expansion, wrong upstream Host/SNI, CA/pin/hostname-verification drift, and body/header leakage. Exercise Playwright-style subresources and pytest/Locust-style traffic only for selected Execution frontend/backend rows, enforce connection/byte/time limits, and prove command/network transcript equality across adoption. Network transcript/receipt binds requirement mapping plus method/path/subresource/upstream-TLS policy digests and aggregate outcomes without recording business URL paths/bodies. Test `none`, `required`, and `input_conditioned`; all eight Generation network rows use either explicit offline source or the exact read-only OpenAPI selection, never test targets or ambient `BASE_URL`/`API_BASE_URL`/mode variables after binding.

  In `test_structured_command_secrets.py`, cover Execution `offline_source + none`, `bound_sut + none` with zero injection, and authenticated `bound_sut + bound_command_secrets`; reject `offline_source + secret`, a secret missing for `requires_admin_auth=True`, and an extra secret for false. The selected `sut-admin-credential-v1` maps one authorized handle to the complete sorted alias tuple `("AA_ADMIN_PASSWORD", "QA_ADMIN_PASSWORD")` and exact accepted target-ID subset. Prove adapter/model/request/view/store/transcript cannot read/enumerate the value or obtain injection/sink capability. Kernel calls the Artifact-owned broker-registration port directly with the same lifetime's admission/injection ports; only the registered Product broker creates a command-ordinal sink and calls `inject_once(...)` under exact Attempt/activity-reference/workspace/ordinal/fence/broker/network/target-set/secret-entry/generation closure. A malicious adapter-created fake sink, wrong broker registration/ordinal, partial alias tuple, parallel flat receipt tuples, wrong/extra handle, same-class-but-wrong target, fence, generation, or unselected requirement resolves/writes zero values and launches nothing. Before workspace/activity work, reject oversize, invalid UTF-8, and NUL-containing env/file sources; HMAC still covers the untouched raw bytes. Assert direct/base64/hex/URL-safe reflection through stdout, stderr, raw files, structured result, receipt, or logs is rejected; injected commands receive a network subview containing only their exact accepted target IDs. Test registration-after-durable/crash-before-dispatch adoption, rotation before launch, prepared/launching/terminal cuts, exact-generation adoption, broker-before-secret close order, and no prompt/command/injection replay under rotation. Generation has no command-secret requirement or value access.

  ```bash
  uv run pytest -q tests/product/test_opencode_staging_boundary.py \
    tests/product/test_structured_command_toolchains.py \
    tests/product/test_structured_command_recovery.py \
    -k 'typed or mixed or workspace_binding_v2 or command_view or recovery'
  ```

  Expected failure: the v1 boundary treats only `allowed_outputs` as authority and ignores unknown MCP/custom tools.

- [ ] **Step 4: Implement closed v2 tool policy and the concrete command broker.** Allow only the installed, classified read/raw-write/executor-shell/internal `StructuredOutput` tool set. Deny unclassified MCP/custom tools for v2 sessions. Rewrite permitted raw mutations into staging exactly as today, but reject typed targets first. StructuredOutput remains usable and is not a filesystem authority. `StructuredCommandSandboxPort` launches commands in a separately contained filesystem/process boundary, authenticates its installed policy digest in the Product binding/workspace binding, imports only declared raw outputs through the same descriptor-safe path/size/mode checks, and destroys the sandbox after use.

  The first production backend is `LinuxBubblewrapStructuredCommandSandbox` in Product code. Unlike the existing Cursor helper, it must never use `--ro-bind / /`. Artifact Task 4 defines the provider-neutral `StructuredToolchainRequirement` value/registry; Capability Task 2's `toolchain_requirements.py` publishes the exact four semantic rows; Artifact Task 5 resolves authority IDs to complete rows and `ResolvedStructuredToolchainProfile`. Product `toolchains.py` consumes those immutable rows and owns only the concrete profile registry, resolving the initial `structured-toolchain-python-runners-v1` profile. Its canonical value binds exactly those four satisfied requirement values/digests, the concrete recipe-to-argv grammars, immutable runtime/dependency/image refs and digests, cleared allowlisted environment, command/cpu/memory/output limits, backend/policy ID, and qualification digest. Feature code sees only semantic requirement IDs and logical recipe inputs; it cannot provide a path, image, executable, argv prefix, environment, or profile. A profile with extra executable/recipe authority does not satisfy this closure.

  Before each command, `StructuredCommandViewBuilder` takes the raw-mutation gate, descriptor-scans only the currently declared/admitted raw staging members under the same path/entry/byte/mode bounds, and submits every present byte to the Product broker's **registration-owned** snapshot-admission capability; adapter context has no such attribute. Only after admission does the broker persist an Attempt-private `raw_artifact_bytes` ref and admitted `command_view_manifest`. The manifest references only the immutable input-snapshot ref plus those raw blob refs, never a mutable staging path. A runtime-owned synthetic `/workspace/project` is rebuilt from those refs, projecting each raw member at its bound logical repo path (including generated tests consumed by Execution), with writable mounts only for the current command's declared raw-output intent. All other SUT inputs are read-only; typed deny holes and runtime control are absent. The manifest records exact source-ref/logical-path/digest/size/mode mappings, raw generation, toolchain invocation-scope digest, broker registration/binding, injection aggregate, and nullable network-access/backend/policy/qualification digests. It never overlays or bind-mounts the live project. The sandbox executes with cwd `/workspace/project`; Product injects only the resolved toolchain environment plus nonsecret target aliases whose keys and synthetic origins are already bound in `BoundStructuredNetworkAccess`. On exit it kills/reaps the complete namespace before output import, descriptor-imports only broker-admitted declared raw-output refs, and emits an admitted `StructuredCommandViewReceipt` binding Attempt/activity/fence/command ordinal, broker registration/binding, input snapshot, prior raw generation, profile and invocation-scope digests, logical recipe ID, canonical typed parameters, Product-derived argv, sandbox/network policy/qualification, view manifest, alias-map digest, exit, injection/network transcript digests, and imported member digests. Recovery rebuilds and authenticates the same view/access solely from durable refs before reconciliation; mutable staging, ambient environment, DNS, and target catalog are never recaptured or re-resolved, and any drift is indeterminate/fatal rather than a new command dispatch.

  The authenticated `structured-command-sandbox-v1` policy permits only the exact read-only roots named by that resolved profile, the synthetic project view, tmpfs scratch, descriptor-owned raw-output mounts, and—only for nonempty bound access—the qualified network gateway endpoint; it does not mount host home, canonical project paths, workspace parent/control, typed staging, blob/journal roots, unrelated runtime sockets, host working directory, arbitrary `.venv`, or package caches. It uses new PID/user/mount/network namespaces, `--unshare-all`, `--die-with-parent`, no inherited file descriptors, bounded process/time/output quotas, and kills/reaps the complete namespace before importing outputs or declaring a command terminal. With no bound access it has no route/socket and network syscalls fail. With bound access, the namespace has no general default route. Product's `LinuxNetworkNamespaceEgressGateway` exposes only frozen target rows. For `sut-openapi-read-v1`, it is an L7 synthetic-origin proxy that terminates the sandbox side, canonicalizes method/path, forbids CONNECT/upgrade/non-read methods/non-OpenAPI paths/redirect escape, and rebuilds upstream TLS only under the bound CA-or-pin and hostname-verification policy; for Execution target rows it enforces their distinct backend/frontend/subresource policies. It rechecks SNI/Host/port/address pins and records bounded policy/outcome metadata. Direct IP, alternate DNS/proxy, package registries, and target expansion remain unreachable. Tool dependencies are immutable/offline with no install-on-demand. Executable paths and all mounted refs are exact and digest/provenance bound. Unsupported OS, missing/unqualified bubblewrap or L7/network gateway, user-namespace denial, policy/image/tool-root/profile/target/TLS-trust drift, or absent qualification record means the Product advertises no corresponding v2 capability; it must not silently invoke ordinary `subprocess`, host network, or the whole-root Cursor sandbox. Keep v1 behavior during shadow.

  Product `network_requirements.py` publishes exactly three Product-owned authenticated `StructuredNetworkRequirementEntry` rows—OpenAPI read, test backend, test frontend—through the core registry contribution seam; `product.py` and both regenerated declarations expose that contribution before any Capability contract resolution. ProductLock and Boot parity reject any other production row. Product `network_targets.py` loads only the authenticated installed/organization data catalog, runs the controlled resolver before executor binding, freezes address sets, accepted secret-requirement IDs, allowlisted nonsecret aliases, path/subresource and upstream-TLS trust policy digests, and emits the sole core-owned `ResolvedStructuredNetworkTargetCatalog`; Feature/InputT contributes only opaque target IDs and receives Product-created requirement→target selections. Product `command_network.py` implements `StructuredCommandNetworkPort` and the L7-capable `LinuxNetworkNamespaceEgressGateway`. It creates the isolated route/gateway, enforces bound requirement/access/limits, and emits a canonical metadata-only network transcript. Raw URL/host/port/DNS values from prompt, command argv, result, project config, or ambient environment never enlarge authority. Target credentials remain separate authorized secret handles and are never placed in the target catalog/transcript.

  Product `command_secrets.py` implements the Artifact-owned write-only injection and unforgeable sink protocols. Kernel never places either in adapter context: it calls the Product implementation of `StructuredCommandBrokerRegistrationPort.register_or_adopt(...)` with the stable activity reference and the same lifetime's admission/injection ports. For a command with a nonempty bound secret set, the registered broker authenticates the value-free canonical entry digest against dispatch/key/workspace, exact accepted target IDs against `BoundStructuredNetworkAccess`, and its own command-ordinal sink capability; after the durable `launching` transition and immediately before spawn it calls the registered injection port. The port writes strict UTF-8/no-NUL values only into the Product-owned one-shot launch environment/sealed descriptors and returns a metadata-only entry-structured receipt; no caller receives bytes. The sandbox sees only the complete installed alias tuple, never handle IDs or unrelated values. Empty sets perform zero injection calls. On teardown Product clears/destroys the sink and closes the broker session before closing the secret lifetime. Any reflection into output/persisted bytes is rejected by the registration-owned admission port, and an injected command receives only its exact target-ID network subview.

  `scripts/qualify_structured_command_sandbox.sh` runs the real malicious child/detached-process, composed API/E2E/Fuzz/Performance runner layouts for the exact four semantic recipes, L7 OpenAPI allow/deny/TLS-trust cases, exact Execution target egress, and synthetic one-shot command-secret suite on Ubuntu. It records binary/kernel/sandbox/toolchain/network/L7 proxy backend, policy, profile, image, controlled-resolver, upstream TLS-trust, broker-registration, and metadata-only injection-receipt/aggregate identities and allow/deny/restart/rotation/no-leak outcomes. The canonical report has a **required `one_shot_injection` section and section digest** covering fake-sink denial, complete-alias delivery, exact-target scope, zero-value persistence/reflection, generation rotation, same-generation restart/adoption, and broker-before-lifetime close; missing or drifted section/digest invalidates the whole qualification. It fails on any forbidden sentinel visibility, non-read/non-OpenAPI request, TLS trust bypass, registry egress, same-class wrong-target use, fake sink, secret reflection, detached namespace survival, semantic-requirement/profile drift, extra command authority, or inability to execute any qualified recipe. The records contain only qualification fixture IDs/digests and synthetic handle/alias/target metadata, never a secret or raw business address. CI installs pinned `bubblewrap` plus pinned L7 gateway/firewall dependencies, runs this qualification plus sandbox/toolchain/network/secret Product suites, and stores no secret/path payload. ProductLock/runtime binding, resolved target catalog, `BoundAttemptDispatch`/AttemptKey, workspace/activity/broker binding, command/transcript/closure receipt, and terminal Attempt receipt include the passing resolved-profile and sandbox/network/broker/injection policy/qualification digests. A production deployment on another platform must install and qualify separately versioned backends/profiles before enabling the corresponding capability; none is inferred or waived here.

  Connect OpenCode to that backend through one concrete control plane. For every v2 activity, Product opens a `0600` Unix-domain `StructuredCommandSandboxBroker` socket outside every Agent/sandbox mount. `register_or_adopt(...)` binds the stable pre-dispatch activity reference plus the complete Kernel-supplied `BoundStructuredToolchainInvocationScope` and delivers one opaque session capability directly to the trusted boundary plugin through Product-owned plugin registration—not through adapter context, prompt, request, or model data—while `StructuredWorkspaceAccess` carries the independent invocation-scope, broker-binding, and registration-receipt digests. `assurance-boundary.mjs` removes builtin shell and registers exactly one classified `assurance_exec` tool. That tool sends a length-bounded request containing Attempt key, activity reference, scope and both broker digests, workspace/input snapshot, generation/fence/ordinal, value-free command-secret entry digest, exact target-set/network/sandbox digests, one `logical_recipe_id`, its tagged typed `recipe_parameters`, and raw-output intent; it has no argv/executable/environment field and the session capability authenticates it. The broker requires exact equality with one not-yet-invoked bound scope row, rejects a second invocation of that recipe, and uses the qualified Product profile to deterministically derive concrete argv/environment internally. It validates active fence/raw generation/scope/both broker identities/secret entries/exact access; calls sandbox/network plus its registered Product-only admission/injection capabilities; imports output; and returns a canonical receipt binding both the logical request and derived argv. It rejects unknown/cross-contract/duplicate recipes, subset/superset test IDs, parameter mismatch/overflow, replayed ordinals, stale/closed/unregistered sessions, fake sinks, secret/target/access drift, malformed frames, and extra descriptors. Crash after registration/before dispatch adopts the same broker binding/registration receipt/scope/command namespace; socket loss after possible start blocks close until command/injection/network reconciliation. It never falls back to builtin execution or host network. Broker/session/gateway/sink shutdown precedes secret-lifetime teardown and durable-store close.

  Implement durable tool-production quiescence in that same trusted plugin/broker control plane. On a running/pending observation, or on dispatch uncertainty after the request might have been accepted, close new admission for the exact activity/broker/generation, allocate/record an ordinal for every already received or concurrently arriving tool call, reconcile terminal ordinals, queue later calls without acknowledging/executing them, fsync `StructuredActivityToolProductionQuiesced`, and only then return its full provider-neutral receipt. Recovery first re-registers/adopts the exact broker/generation and reattaches the plugin before polling or releasing queued work. A dispatch pending without this process is allowed only when the transport durably proves non-acceptance and no namespace/ordinal; request-accepted/response-lost must recover the stable activity reference and quiesce. Acknowledgement before durable receipt followed by process death, receipt watermark drift, or failed reattachment is indeterminate. Add race/E2E tests for running-pending late calls and dispatch-pending late calls, requiring one prompt, one namespace, stable ordinals, and at-most-once command/injection.

  `StructuredCommandExecutionStore` is a Product-owned, fenced, durable auxiliary store keyed by `(AttemptKey, activity_reference_digest, workspace, raw-write generation, command ordinal)`. Before any durable write or launch, the broker canonicalizes and exact-matches the logical recipe ID plus typed parameters to one unused registered scope row, deterministically derives/bounds argv and environment from the qualified Product profile, and canonicalizes raw intent, request, and view manifest; it passes those exact bytes through the admission port captured by its broker registration and stores only admitted Attempt-private blob refs/receipts plus nonsecret state metadata/digests. Declared outputs pass the same lifetime scanner before their blob refs enter a terminal record. Admission failure writes no `prepared` record and launches nothing. The store validates every blob's admission policy and exact generation; it persists invocation-scope/selection/recipe/parameter/derived-argv, both broker-binding and registration-receipt digests, value-free command-secret-entry-set, exact target-set, and injection-receipt digests, never alias values or a reusable verifier. Raw model text is never copied into an unscanned row or log.

  The immutable `command_payload_digest` binds Attempt/activity-reference/toolchain invocation-scope/broker-binding/**broker-registration-receipt**/workspace/generation/ordinal, admitted request/view refs, requirement/profile/selection/logical-recipe/canonical-parameter/derived-argv digests, sandbox/network access/backend/method-path-subresource/TLS-trust policy/qualification, nonsecret alias map, value-free canonical command-secret entry-set and exact target-set digests, and raw intent, but excludes mutable owner fence and every secret value. The record stores historical/current fences separately. Before launch the broker persists `prepared`, CAS/fsyncs `launching`, obtains the metadata-only entry-structured injection receipt under the anchored generation, and spawns once. Terminal state binds the same logical recipe/parameters/derived argv plus injection-entry aggregate, exit/output/view/network receipts, then imports idempotently. Fence N+1 may adopt/re-fence unchanged payload after proving N stale; N cannot launch/inject/connect/import/close/respond. Only `prepared` with durable no-start proof and exact generation may launch on adoption; terminal records adopt/import, and unresolved `launching` is permanently indeterminate. Test registration, prepared, launching, terminal-before-import, rotation, and response-loss cuts. Wrong payload/generation/fence/scope/recipe/parameters/derived argv/either broker digest/view/entry-set/exact-target/injection/network/output is integrity failure. Raw-write closure requires every selected recipe be invoked at most once in all outcomes and exactly once before a successful activity may close (a zero-row scope requires zero), every allocated ordinal terminal/imported, no live namespace/gateway, and binds canonical command transcript, ordered injection-entry receipt aggregate, invocation-scope and both broker digests, and nullable `structured_network_transcript_digest`; no value enters them. Any unresolved state makes close indeterminate. These records add no Kernel journal phase or LangGraph node.

- [ ] **Step 5: Implement and race-test terminal raw-write admission closure.** Put a per-binding mutation gate and monotonic closed generation in the v2 boundary. `close_raw_writes(...)` acquires the gate, authenticates the registered toolchain invocation-scope digest and exact successful recipe-use cardinality (each selected row once, with zero legal for a zero-row scope), proves every allocated command ordinal terminal/imported with its matching value-free injection receipt, every known command namespace killed/reaped, and every gateway connection terminal; computes and binds the exact invocation-scope digest, broker-binding digest, distinct broker-registration-receipt digest, canonical command transcript, mandatory ordered command-secret-injection aggregate (specified canonical empty aggregate for zero injections), and nullable `structured_network_transcript_digest`; waits for mutations already admitted through the boundary; writes/fsyncs the Attempt/activity/workspace/scope/broker/registration/generation-bound marker through the runtime-owned control directory; records the historical `closing_fence`; and returns `StructuredRawWriteClosure`. Same-generation replay returns the same immutable receipt. Every later native/shell/MCP/custom tool/network admission and every boundary-managed write rechecks the marker inside the gate immediately before mutation/connect. Test closure versus an in-flight managed write/connection in both schedules, unresolved command/injection/network state, scope/cardinality drift, either broker digest or aggregate/transcript drift, process restart, crash after marker/before receipt observation, stale fence, wrong session, and repeated close. The qualified sandbox must prevent its child namespace from surviving terminal import/close; separately, the test must not claim a JavaScript marker can date an arbitrary out-of-band syscall before the first successful raw capture. Kernel E2E proves mutation during/after successful capture fails before promotion and cannot change durable finalizer input or promoted bytes. Add the exact takeover case: fence N publishes the marker and crashes before Kernel event append; fence N+1 authenticates the same Attempt/activity/workspace/scope/broker/registration/generation and immutable marker/transcripts/aggregate, adopts the original closure, while fence N can no longer mutate/connect/append. Raise only the provider-neutral pending/indeterminate close exceptions for unresolved publication. Once closure returns, no new OpenCode mutation, command-secret injection, or network request is admitted; Kernel typed-install and immutable raw-snapshot tests remain green.

- [ ] **Step 6: Run boundary regressions and commit.**

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests/test_structured_workspace_access.py \
    packages/adapters/agent-runtime-opencode/tests/test_raw_write_closure.py \
    packages/adapters/agent-runtime-opencode/tests/test_command_sandbox.py \
    tests/product/test_structured_command_sandbox.py \
    tests/product/test_structured_command_toolchains.py \
    tests/product/test_structured_command_network.py \
    tests/product/test_structured_command_secrets.py \
    tests/product/test_structured_command_recovery.py \
    tests/product/test_structured_network_requirements.py \
    tests/product/test_opencode_staging_boundary.py \
    packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py \
    packages/adapters/agent-runtime-opencode/tests/test_binding_authority.py
  bash scripts/qualify_structured_command_sandbox.sh --help
  git add \
    packages/framework/graph-engine/graph_engine/attempts/structured_activity.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/workspace_binding.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/activity.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/command_sandbox.py \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/command_view.py \
    packages/adapters/agent-runtime-opencode/tests/test_structured_workspace_access.py \
    packages/adapters/agent-runtime-opencode/tests/test_raw_write_closure.py \
    packages/adapters/agent-runtime-opencode/tests/test_command_sandbox.py \
    packages/products/assurance-product/assurance_product/command_sandbox.py \
    packages/products/assurance-product/assurance_product/command_sandbox_broker.py \
    packages/products/assurance-product/assurance_product/command_execution_store.py \
    packages/products/assurance-product/assurance_product/command_view.py \
    packages/products/assurance-product/assurance_product/command_network.py \
    packages/products/assurance-product/assurance_product/command_secrets.py \
    packages/products/assurance-product/assurance_product/network_requirements.py \
    packages/products/assurance-product/assurance_product/network_targets.py \
    packages/products/assurance-product/assurance_product/toolchains.py \
    packages/products/assurance-product/assurance_product/resources/sandbox/structured-command-sandbox-v1.json \
    packages/products/assurance-product/assurance_product/resources/sandbox/structured-toolchain-python-runners-v1.json \
    packages/products/assurance-product/assurance_product/resources/sandbox/structured-network-egress-v1.json \
    packages/products/assurance-product/assurance_product/resources/schemas/structured-network-target-catalog-v1.schema.json \
    tests/product/test_structured_command_sandbox.py \
    tests/product/test_structured_command_toolchains.py \
    tests/product/test_structured_command_network.py \
    tests/product/test_structured_command_secrets.py \
    tests/product/test_structured_command_recovery.py \
    tests/product/test_structured_network_requirements.py \
    packages/products/assurance-product/assurance_product/product.py \
    packages/products/assurance-product/assurance_product/product-declaration-opencode.json \
    packages/products/assurance-product/assurance_product/product-declaration-cursor.json \
    packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py \
    scripts/qualify_structured_command_sandbox.sh \
    .github/workflows/ci.yml \
    packages/products/assurance-product/assurance_product/opencode_agents.py \
    packages/products/assurance-product/assurance_product/resources/opencode/assurance-boundary.mjs \
    packages/products/assurance-product/assurance_product/resources/opencode/workspace-binding-v2.schema.json \
    tests/product/test_opencode_staging_boundary.py
  git commit -m "feat: deny OpenCode writes to typed artifacts"
  ```

  The focused worktree gate above is portable and validates the qualification CLI contract without pretending macOS/unsupported hosts can qualify Linux isolation. The mandatory pinned Ubuntu+bubblewrap qualification job runs:

  ```bash
  bash scripts/qualify_structured_command_sandbox.sh \
    --sandbox-output "$AA_STRUCTURED_SANDBOX_QUALIFICATION" \
    --toolchain-output "$AA_STRUCTURED_TOOLCHAIN_QUALIFICATION" \
    --network-output "$AA_STRUCTURED_NETWORK_QUALIFICATION"
  ```

  All three output paths must be absolute, outside every worktree, new/empty, owner-only, and atomically written canonical reports. The script exits nonzero and leaves no passing record on unsupported host, missing bwrap/gateway dependencies, malicious-fixture visibility, composed-view/toolchain failure, target allow/deny/bypass failure, detached namespace survival, or identity drift. CI retains all three records as release artifacts; Task 7 must consume those exact absolute records and cannot skip or replace qualification locally.

### Task 7: Run and promote the real release gate, then close the adapter checkpoint

**Files:**

- Modify by promotion command: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-transport-v1.json`
- Modify by promotion command: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py`
- Modify: `tests/product/test_opencode_structured_output_matrix.py`
- Create: `tests/product/test_opencode_structured_binding_release.py`

**Interfaces:** real raw report → deterministic promotion → real `aa bindings build --build-record ABS_PATH` → deterministic wheel plus `BindingWheelBuildRecordV1` → isolated wheel install/Boot exact-closure proof; no manual positive record; complete adapter exit gate.

- [ ] **Step 1: Verify external gate inputs, disposable controller, and evidence ownership are explicit.** The operator provides `AA_OPENCODE_GATE_ENDPOINT` (normal 33-row provider/model endpoint), `AA_OPENCODE_GATE_SECRET_FILE`, `AA_OPENCODE_GATE_DEPLOYMENT`, absolute `AA_OPENCODE_GATE_CONTROLLER` plus `AA_OPENCODE_GATE_CONTROLLER_IDENTITY` for disposable candidate-binary/fault/restart control, absolute passing sandbox/toolchain/network qualification records, and an absolute `AA_OPENCODE_GATE_EVIDENCE_DIR` outside every Git worktree. The real boundary section additionally requires owner-only/no-follow **synthetic fixture** files `AA_OPENCODE_GATE_NETWORK_TARGET_CATALOG`, `AA_OPENCODE_GATE_COMMAND_SECRET_BINDINGS`, and `AA_OPENCODE_GATE_COMMAND_SECRET_SOURCE`, plus `AA_SECRET_GENERATION_KEY_FILE` and explicit nonempty `AA_SECRET_GENERATION_KEY_ID`. These fixture rows use only loopback test identities and the exact selected test handle/value; they are never installed or embedded as production authority. The later binding-build step separately requires deployment-owned `AA_STRUCTURED_NETWORK_TARGET_CATALOG` and `AA_STRUCTURED_COMMAND_SECRET_BINDINGS`, which may differ while satisfying the same installed requirement schemas. The key file satisfies the Product owner-only producer contract and is not environment content. The controller identity allow-record pins its implementation and candidate OpenCode binary/config digests; it must be capable of starting/stopping a disposable deployment with the installed boundary plugin and deterministic fault provider and may not target the normal endpoint. The secret, key, controller, identity/qualification, both catalog/binding sets, source, and evidence paths are permission-restricted; the named release operator owns raw-report/controller-log retention through the release-audit period and records them in the release ticket. Reject a relative, repository-contained, symlinked, group/world-writable, reused-nonempty, unowned, identity-drifted, or same-endpoint controller/evidence input before network access. Qualification records expose only fixture identities/digests, not business targets. Run `git status --short` first; expected output is empty. If no candidate binary/controller, qualified network backend, synthetic conformance authority, deployment binding authority, authorized synthetic secret source/key, or normal provider deployment is available, stop here: capability stays absent and Checkpoint S remains open.

- [ ] **Step 2: Export the authenticated 33-row catalog.**

  ```bash
  uv run python -m agent_runtime_opencode.conformance export-catalog \
    --deployment "$AA_OPENCODE_GATE_DEPLOYMENT" \
    --output "$AA_OPENCODE_GATE_EVIDENCE_DIR/catalog-v1.json"
  ```

  Expected: exit `0`, exactly 33 rows, canonical report digest, no secret bytes.

- [ ] **Step 3: Run the real asynchronous/recovery/provider matrix.**

  ```bash
  uv run python -m agent_runtime_opencode.conformance run \
    --normal-endpoint "$AA_OPENCODE_GATE_ENDPOINT" \
    --secret-file "$AA_OPENCODE_GATE_SECRET_FILE" \
    --deployment-controller "$AA_OPENCODE_GATE_CONTROLLER" \
    --controller-identity "$AA_OPENCODE_GATE_CONTROLLER_IDENTITY" \
    --sandbox-qualification "$AA_STRUCTURED_SANDBOX_QUALIFICATION" \
    --toolchain-qualification "$AA_STRUCTURED_TOOLCHAIN_QUALIFICATION" \
    --network-qualification "$AA_STRUCTURED_NETWORK_QUALIFICATION" \
    --network-target-catalog "$AA_OPENCODE_GATE_NETWORK_TARGET_CATALOG" \
    --command-secret-bindings "$AA_OPENCODE_GATE_COMMAND_SECRET_BINDINGS" \
    --command-secret-source "$AA_OPENCODE_GATE_COMMAND_SECRET_SOURCE" \
    --secret-generation-key-file "$AA_SECRET_GENERATION_KEY_FILE" \
    --secret-generation-key-id "$AA_SECRET_GENERATION_KEY_ID" \
    --boundary-plugin packages/products/assurance-product/assurance_product/resources/opencode/assurance-boundary.mjs \
    --catalog "$AA_OPENCODE_GATE_EVIDENCE_DIR/catalog-v1.json" \
    --output "$AA_OPENCODE_GATE_EVIDENCE_DIR/raw-report-v1.json"
  ```

  Expected: exit `0`; the controller proves an actual old-instance → new-instance server restart with unchanged candidate binary/config; every deterministic transport/error/repeated-read case passes; the real pinned server loads the exact boundary plugin and constructs a **candidate-only ephemeral binding** from the frozen catalog/bindings/source/key without advertising a production capability. It removes builtin shell, invokes `assurance_exec` through Product broker + qualified composed-view sandbox/L7 gateway, reaches one bound synthetic SUT target, denies one unbound endpoint plus typed/control targets, performs exact-target one-shot complete-alias injection, proves fake-sink/no-leak/value-encoding/rotation/same-generation-restart behavior, durably quiesces late tools across running-pending and accepted-response-lost dispatch-pending recovery, and closes raw writes with broker/registration/injection/command/network transcripts; 33/33 normal provider/model/schema rows pass within bounds. Any missing/drifted controller receipt, fault case, quiescence/boundary/network/injection receipt, required qualification section, or row exits nonzero and writes no promotable record.

- [ ] **Step 4: Promote only the passing report.**

  ```bash
  uv run python -m agent_runtime_opencode.conformance promote \
    --report "$AA_OPENCODE_GATE_EVIDENCE_DIR/raw-report-v1.json" \
    --transport-record packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-transport-v1.json \
    --matrix-record packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json
  ```

  Expected: exit `0`; both installed records bind adapter/server binary/version, controller/profile/fault-script and actual restart evidence digests, boundary plugin/tool-policy/broker-registration/sandbox/toolchain/L7-network/view/quiescence/closure digests, required one-shot-injection section/aggregate/no-leak/rotation-restart digests, raw-report digest, **synthetic fixture** target-catalog/secret-binding source identities, and exact 33 row digests. They do not authorize or bind a deployment's organization target catalog; that closure belongs to the generated binding wheel. The command refuses the known failing 1.18.4 and v1.18.26 reports, incomplete transport/fault/boundary/network/quiescence/injection sections, fake or unchanged restart generation, hand-edited report digests, missing rows, extra rows, or secret/canary/business-target content.

- [ ] **Step 4a: Update the promotion assertions from the generated records.** Make `test_structured_output_capability.py` and `test_opencode_structured_output_matrix.py` load the two command-produced records and assert their exact promoted adapter/server binary identity, controller-backed restart/fault evidence, real boundary integration digest, transport/report digest, and 33 row digests. Preserve explicit assertions that OpenCode 1.18.4 and v1.18.26 remain negative, prove the selected positive record came from the aggregate raw report, and reject a manually authored positive record with no promotable evidence. Do not paste a hand-written passing record into either test.

- [ ] **Step 4b: Build, install, and Boot the real production binding wheel.** This step depends on Capability Task 11's already committed production runtime/key/source bridge. The operator supplies absolute owner-only/no-follow `AA_OPENCODE_BINDINGS_MANIFEST`, new empty owner-only `AA_OPENCODE_BINDINGS_OUTPUT_DIR`, new empty owner-only `AA_OPENCODE_BINDINGS_INSTALL_DIR`, the deterministic expected `AA_OPENCODE_BINDINGS_WHEEL` path inside the output directory, and a new explicit absolute `AA_OPENCODE_BINDINGS_BUILD_RECORD` path outside the worktree/SUT. The deployment manifest references the selected OpenCode adapter/provider/model and adapter-only activity handles but contains no raw secret, endpoint catalog, generation key, or command-secret source. Run the actual installed CLI—no test builder shortcut:

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

  The build command verifies that the promoted matrix recursively authenticates the matching transport record and required boundary/sandbox/toolchain/L7-network/quiescence/one-shot-injection sections, embeds only source/resolved catalog and value-free requirement→handle identities, and emits exactly one deterministic wheel. It also atomically writes the explicit external `$AA_OPENCODE_BINDINGS_BUILD_RECORD` under the closed v1 schema with expected wheel filename and SHA-256 plus manifest, resolved Product-input, ProductLock, promoted certification, network-target-catalog, and command-secret-binding source/resolution digests, never raw catalog rows or values. Workflow run/revision provenance is deliberately not a builder-record field; Capability Task 12 authenticates it separately from the protected producer job and immutable Actions artifact handoff. Installation is always `--no-deps`: the isolated target may gain only this authenticated binding distribution, while graph-engine/Product/Feature dependencies resolve from the already verified current-revision `uv` workspace environment rather than an index or unrecorded wheel. The release test asserts that exact target-directory distribution set, adds only the isolated install directory to `sys.path`, discovers the generated entry point, composes Boot through the normal installed-product path, and requires exact equality for 33 rows, target catalog/resolver/TLS digests, command-secret bindings/authorized handle closure, Product requirement registry, broker/sandbox qualification, and ProductLock. It then reconstructs the runtime foundation with the external key/source and proves same-generation restart plus rotation rejection. Candidate conformance bindings are not accepted as installed production contributions. Missing/extra/drifted closure, wheel identity mismatch, an extra target distribution, or any secret/key bytes in wheel contents fails. The manifest, target/binding inputs, wheel/output/install directories, build record, raw report, secret source, and key remain external release artifacts and never enter Git. Capability Task 12's protected release workflow consumes the exact promoted records already committed by this Task 7 at its protected source revision and reruns only the exact build/install commands above; it must not rerun conformance or promotion. It uploads wheel, build record, and independently computed SHA-256 file as one immutable job artifact, then downloads and verifies them in its downstream Checkpoint job and passes explicit wheel/build-record/digest paths to the aggregate gate; no clean CI job may rely on this shell's prior installation state.

- [ ] **Step 5: Run focused and full gates.**

  ```bash
  uv run pytest -q packages/adapters/agent-runtime-opencode/tests
  uv run pytest -q tests/agent_runtime/test_opencode_conformance.py \
    tests/product/test_opencode_structured_output_matrix.py \
    tests/product/test_opencode_staging_boundary.py \
    tests/product/test_structured_command_sandbox.py \
    tests/product/test_structured_command_toolchains.py \
    tests/product/test_structured_command_network.py \
    tests/product/test_structured_command_secrets.py \
    tests/product/test_structured_command_recovery.py
  AA_OPENCODE_BINDINGS_INSTALL_DIR="$AA_OPENCODE_BINDINGS_INSTALL_DIR" \
    AA_OPENCODE_BINDINGS_WHEEL="$AA_OPENCODE_BINDINGS_WHEEL" \
    AA_OPENCODE_BINDINGS_BUILD_RECORD="$AA_OPENCODE_BINDINGS_BUILD_RECORD" \
    uv run pytest -q tests/product/test_opencode_structured_binding_release.py
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run lint-imports
  uv run pytest -q
  bash scripts/assurance_product_wheel_smoke_test.sh
  ```

  Expected: every command exits `0`; structured requests use one session/prompt, controller-backed server restart/repeated recovery does not redispatch, the pinned real boundary/tool path is qualified, and legacy shadow remains green.

- [ ] **Step 6: Prove forbidden names and scope are absent.**

  ```bash
  rg -n 'provider_schema|requires_provider_schema|provider-native structured|response_format' \
    packages/framework/graph-engine \
    packages/adapters/agent-runtime-contracts \
    packages/adapters/agent-runtime-opencode \
    packages/products/assurance-product
  ```

  Expected: no production-code matches. Test fixtures may mention forbidden names only in explicit rejection assertions.

- [ ] **Step 7: Commit the certified installed records.**

  ```bash
  git status --short
  git add \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-transport-v1.json \
    packages/adapters/agent-runtime-opencode/agent_runtime_opencode/resources/structured-output-matrix-v1.json \
    packages/adapters/agent-runtime-opencode/tests/test_structured_output_capability.py \
    tests/product/test_opencode_structured_output_matrix.py \
    tests/product/test_opencode_structured_binding_release.py
  git commit -m "chore: certify OpenCode structured output gate"
  ```

  Before staging, expected status contains only the two promoted installed records, the two updated record tests, and the new binding-release test; the external catalog/raw report/manifest/target catalog/secret bindings/source/key/wheel/install tree never appears in Git status. Preserve those external artifacts under their recorded release owner and retention policy—do not copy or delete them as part of this commit.

## Exit criteria

- [ ] Task 0/S0 evidence is isolated, non-promotable, and never satisfies `capabilities_for(...)`; only the later complete transport record plus exact 33-row matrix may advertise `opencode_structured_output`.
- [ ] `StructuredAgentActivityPort` is the only Kernel-facing adapter seam; no OpenCode type crosses into graph control, artifact materialization, or finalization.
- [ ] Every required request sends exact `format.type=json_schema` and authenticated `format.schema`; success comes only from terminal assistant `info.structured`.
- [ ] Server/adapter restart, list/single reads, repeated observation, and the full error matrix cannot duplicate a prompt or convert uncertainty/error into success.
- [ ] Running/pending and accepted-response-lost dispatch-pending paths carry authenticated durable tool-production quiescence, reattach the exact activity/broker/generation, and execute queued ordinals at most once; unproven acceptance/quiescence is indeterminate.
- [ ] `opencode_structured_output` is absent without a passing pinned transport record and an exact 33-row selected provider/model/schema/size matrix.
- [ ] V2 structured sessions expose only effective raw paths and deny typed/control/blob/journal paths through native writes, patch/edit, MCP, custom tools, and indirect executor code; executor commands run only in the authenticated OS sandbox and have no same-UID fallback.
- [ ] OpenCode 1.18.4 and v1.18.26 remain negative tests and are not promoted.
- [ ] The promoted record contains required L7 network and one-shot-injection/no-leak/rotation-restart evidence; the real post-promotion bindings build installs one external wheel whose Boot closure exactly matches the selected certification, deployment target catalog, command-secret bindings, and Product requirement registry.
- [ ] No new LangGraph node, artifact serializer/materializer, finalizer, validator binding, or transaction-tail behavior is introduced by this plan.
