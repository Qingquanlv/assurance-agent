# Raw Agent Runtime Closure Implementation Plan

> **Status:** implemented historical plan; amended 2026-09-04.
>
> **Scope rule:** this plan closes the existing OpenCode/Attempt seam. It does not create a second artifact pipeline, deployment platform, or Workflow layer.
>
> **Amendment:** the former R5 live-provider gate was cancelled and removed by
> [Checkpoint R Removal Design](../specs/2026-09-04-checkpoint-r-removal-design.md). R1–R4 remain
> implementation history; no task below authorizes recreating that gate.

**Goal:** make Raw Agent execution the permanent implementation for all 33 Agent contracts so Product cutover can continue from T5b without waiting for OpenCode provider-structured output.

**Architecture:** the installed Feature contract owns `InputT`, `AgentResultT`, `OutputT`, prepare/finalize handlers, resources, validators, and file rules. The Product selects an authenticated OpenCode adapter/provider/model binding. The adapter appends the JSON Schema generated from `AgentResultT` to a normal prompt, accepts one terminal assistant JSON object, and validates it locally. OpenCode may write only authorized raw workspace files. The existing `AssuranceAttemptKernel` then validates the finalized output and performs seal, ordered validators, durable prepare, promote/recover, effects, and receipt.

**No topology change:** all work remains behind the existing Attempt node. No LangGraph node or edge is added.

**Counts frozen by this plan:** 33 distinct Agent contracts, 34 Agent occurrences, 8 distinct non-Agent Improvement Task contracts, 41 distinct semantic Attempt contracts, and 43 Attempt occurrences.

---

## Permanent transaction

```text
LangGraph Attempt node
  -> prepare(validated input)
  -> OpenCode root session in isolated workspace
       prompt = business instructions + context + AgentResult JSON Schema
       output = authorized raw files + one assistant JSON object
  -> parse exact assistant JSON
  -> validate AgentResultT locally
  -> finalize(prepared, AgentResultT, actual workspace files)
  -> validate OutputT
  -> existing AssuranceAttemptKernel transaction tail
       seal -> validators -> durable prepare -> promote/recover
       -> six effects when declared -> receipt
```

The Schema constrains the assistant result object. It does not prove that generated files are semantically correct. Feature finalizers and Kernel validators continue to inspect the actual files.

---

## Explicit non-goals

- Do not call OpenCode `format.type = "json_schema"` or read `info.structured`.
- Do not add `ArtifactContract`, typed artifact slots, a JSON/YAML materializer, materialization manifests, or materialization receipts.
- Do not add a custom OpenCode Schema-writer plugin.
- Do not add OCI image assembly, a bundled Python runtime, a wheelhouse, a global deployment registry, a new launcher, or a supply-chain attestation system.
- Do not introduce in-toto/SLSA or a new release-certification service.
- Do not move existing runtime modules merely to improve directory aesthetics.
- Do not generalize a new interface until at least two current callers require it.
- Do not change graph topology, `join:any`, subgraphs, routing, interrupts, effects, or public entrypoint names in this plan.

---

### Task R1: Replace provider-schema policy with a local result contract

**Files:**

- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/models.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/runtime_binding.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/schema.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Test: `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- Test: `packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py`
- Test: `packages/adapters/agent-runtime-contracts/tests/test_schema.py`

**Steps:**

1. Add failing tests showing that an Agent contract always exposes the JSON Schema and digest derived from its installed `agent_result_model`, independent of provider capabilities.
2. Remove `requires_provider_schema`, `provider_schema`, structured-output negotiation, and the corresponding capability error from active contracts and bindings.
3. Keep one result delivery mode: `assistant_json_local_v1`. Provider and model remain separate canonical fields; never parse a combined provider/model string.
4. Validate and digest the exact parsed JSON payload before Pydantic defaults or serializers can normalize it. Then validate the same payload as `AgentResultT`.
5. Keep any current model-validation context required by an installed result model, such as Generation capability leaves, as a Feature-owned input to validation. Do not create a generic project-loadable Schema registry.
6. Run:

```bash
uv run pytest \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py \
  packages/adapters/agent-runtime-contracts/tests/test_schema.py -q
```

**Exit:** local result Schema and validation work without any provider-structured capability field; no active Agent contract can require provider-structured output.

**Commit:** `feat: make agent results locally validated`

---

### Task R2: Make the OpenCode raw adapter strict

**Files:**

- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/handler.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/reducer.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/protocol.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/workspace_binding.py`
- Test: `packages/adapters/agent-runtime-opencode/tests/test_prompt_admission.py`
- Test: `packages/adapters/agent-runtime-opencode/tests/test_terminal_reduction.py`
- Test: `packages/adapters/agent-runtime-opencode/tests/test_observation.py`
- Test: `packages/adapters/agent-runtime-opencode/tests/test_binding_authority.py`

**Steps:**

1. Add failing prompt tests proving the result Schema is rendered from the authenticated contract, appended after business instructions/context, and sent as ordinary prompt text without OpenCode `format`.
2. Require one root OpenCode session per stable Attempt key. Preserve the selected adapter, provider, and model in the existing activity evidence.
3. Replace permissive candidate extraction with a closed terminal-message parser. A successful terminal response contains exactly one non-empty `text` part holding one JSON object, one `step-start`, one successful `step-finish`, and optional `patch` metadata. Reject extra text, multiple result objects, tool/file/reasoning/unknown parts, errors, and truncation.
4. Validate the raw JSON against the R1 result contract and return the validated result plus bounded digests. Do not trust a model-reported file manifest as proof of workspace contents.
5. Keep workspace discovery fail-closed. The isolated execution root must not load project-local `opencode.json/jsonc`, `.opencode/**`, `.agents/**`, `AGENTS.md`, `CLAUDE.md`, or `CONTEXT.md`; add one real-server discovery regression test when the official binary is available.
6. Run:

```bash
uv run pytest packages/adapters/agent-runtime-opencode/tests -q
```

**Exit:** the adapter accepts only the closed Raw Agent response shape, produces local validation evidence, and does not depend on OpenCode Structured Output.

**Commit:** `feat: enforce strict raw opencode results`

---

### Task R3: Close prepare/runtime/finalize for all Agent contracts

**Files:**

- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py`
- Modify: `packages/products/assurance-product/assurance_product/agent_contracts.py`
- Modify: `packages/products/assurance-product/assurance_product/runtime_bindings.py`
- Modify: `packages/products/assurance-product/assurance_product/opencode_agents.py`
- Modify: the existing Agent contract/finalizer modules under each of:
  - `packages/capabilities/assurance-execution/`
  - `packages/capabilities/assurance-intake/`
  - `packages/capabilities/assurance-generation/`
  - `packages/capabilities/assurance-quality/`
  - `packages/capabilities/assurance-healing/`
  - `packages/capabilities/assurance-improvement/`
- Test: `packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`
- Test: existing `tests/test_attempt_contracts.py`, `tests/test_contracts.py`, and `tests/test_workflow_module.py` files in the six Capability wheels
- Test: `tests/product/test_agent_execution_contracts.py`
- Test: `tests/product/test_semantic_attempt_bindings.py`
- Test: `tests/product/test_binding_coverage.py`

**Steps:**

1. Add failing executor tests for the exact order `prepare -> runtime -> AgentResultT validation -> finalize -> OutputT validation` and for failure before finalization when the raw result is invalid.
2. Replace the provider-negotiating Composite executor with `ResolvedRawAgentExecutor`. Keep a single graph-facing `TaskAttemptContract`; do not split phases into LangGraph nodes.
3. Pass finalizers one closed bundle containing validated input, prepared value, validated Agent result, bounded run evidence, and read-only access to the authorized raw workspace.
4. Migrate the 33 installed Agent contracts across the six wheels. Finalizers continue reading and validating actual JSON/YAML/source/Markdown files where their business contract requires it. Delete only duplicate parsing or shape checks made redundant by validated `AgentResultT`; retain semantic, cross-file, policy, Eval, and validator checks.
5. Bind exactly one Product runtime row to each semantic Agent contract ID. Reject missing, duplicate, extra, wrong-owner, wrong-adapter, and contract-digest-drifted rows. Do not resolve through the 99 legacy phase aliases.
6. Leave the eight non-Agent Improvement Task contracts on their existing deterministic execution path.
7. Run the focused contract tests and the six `test_workflow_module.py` suites.

**Exit:** 33/33 contracts resolve directly, 34/34 Agent occurrences use the Raw executor, and no Agent contract negotiates provider Schema support.

**Commit:** `feat: bind raw agent execution contracts`

---

### Task R4: Prove transaction recovery and security using existing primitives

**Files:**

- Modify only as required: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify only as required: `packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py`
- Modify only as required: `packages/framework/graph-engine/graph_engine/runtime/activity.py`
- Modify only as required: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Modify: relevant OpenCode adapter recovery/security tests
- Modify: existing Attempt Kernel recovery/fault tests

**Steps:**

1. Reuse the current Attempt journal, fenced activity identity, isolated workspace, and recovery entrypoints. Add fields only when a failing test proves the current record cannot distinguish safe replay from duplicate dispatch.
2. Persist the minimum recovery facts: stable Attempt key, adapter/provider/model identity, OpenCode session/message identity, prompt/result digest, terminal status, and existing workspace/commit receipts. Do not persist unbounded transcripts or secret values.
3. Add crash tests at: before session creation, after session creation but before prompt acknowledgment, after prompt acknowledgment, after terminal result but before finalize, after finalize but before seal, and across the existing prepare/promote/effect cuts.
4. Prove recovery adopts the recorded session and never sends a second prompt after acknowledgment. Cancellation and reconcile remain idempotent and fenced.
5. Prove unauthorized writes, symlink/path escape, project-instruction discovery, secret leakage, malformed terminal parts, and result/file disagreement fail before promote.
6. Exercise the existing six Effect kinds without changing their ownership or order:
   - healing: `allocation.v2`, `heal-apply.v2`, `proposal-approved.v1`
   - improvement: `archive.v1`, `delivery.v1`, `promotion.v1`

**Exit:** Raw Agent execution has the same Attempt atomicity, recovery, fencing, effect, and receipt guarantees as the current Kernel transaction.

**Commit:** `test: prove raw agent transaction recovery`

---

### Task R5: Historical Checkpoint R implementation — removed 2026-09-04

> **Archival snapshot:** R5 was implemented before the project cancelled Checkpoint R. The actions
> below record what happened; they are not executable instructions and must not be recreated.

R5 added the Product inventory/cutover tests, CI integration, and candidate-bound live OpenCode
check. At the time it recorded 33 contracts, 33 bindings, an undercounted 34 Agent occurrences, 41
semantic contracts, and 43 Attempt occurrences; later graph-owned inventory corrected the counts to
35 and 44. It also:

1. ran the Raw adapter, six Capability contract, Attempt recovery/effect, Product composition, and
   repository suites;
2. reverified the four T5a non-Agent roots on `langgraph-v1` while leaving ten roots on `legacy-v2`;
3. recorded candidate commit, ProductLock, GraphRevision, adapter/provider/model, local Schema
   validation, raw-file validation, one-session recovery, and absence of Structured Artifact code;
4. deleted the obsolete Structured Output eligibility probe while preserving its research note; and
5. handed the migration to Product T5b with a candidate-specific live check planned for every later
   tranche.

The former exit condition was a green Checkpoint R. On 2026-09-04 its workflow, script, support
harness, marker, fixture, and manifest were removed. Product continuation now uses the ordinary
repository gate and focused deterministic Raw Agent, Attempt recovery, and Product lifecycle tests.
T5a remains accepted and Product T5b is next.

---

## Dependency order

```text
R1 local result contract
  -> R2 strict OpenCode adapter
  -> R3 33-contract integration
  -> R4 recovery/security proof
  -> Product T5b -> T5c -> T5d -> T6-T10
```

R1 tests and the non-overlapping parts of R2 may be developed together, but commits merge in the
order above. R3–R4 are sequential because each consumes the preceding contract.

## Completion criteria

- All 33 Agent contracts use prompt-carried JSON Schema and local result validation.
- OpenCode Structured Output is neither required nor advertised as a migration gate.
- OpenCode writes only authorized raw files; Capability finalizers validate actual file contents.
- The existing Attempt transaction and all six Effect kinds retain their guarantees.
- There are no new LangGraph nodes and no Structured Artifact Pipeline implementation.
- Product cutover can proceed from T5b under the original migration plan.
