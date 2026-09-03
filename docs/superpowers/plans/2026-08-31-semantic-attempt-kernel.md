# Semantic Attempt Kernel Implementation Plan

> **2026-09-02 continuation note:** this completed Attempt plan records the original baseline.
> Its provider-schema and `CompositeAttemptExecutor` references are superseded by
> [Permanent Raw Agent Runtime Cutover](../specs/2026-09-02-raw-agent-runtime-cutover-design.md)
> and [Raw Agent Runtime Closure](./2026-09-02-raw-agent-runtime-closure.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 99 deployment phase aliases with 41 immutable effectful semantic Attempt contracts, four proven-pure graph functions, and one reliable `AssuranceAttemptKernel`/`AttemptNodeFactory` seam, while retaining legacy aliases for shadow rollback until final drain.

**Architecture:** Core `graph-engine` owns provider-neutral Task contracts, resolved executors, stable Attempt identity, resolutions, workspace/resource/effect transaction, recovery, and the LangGraph node adapter. `agent-runtime-contracts` owns three-type Agent contracts and combines authenticated prepare/runtime/finalize entries into a core executor. Capability wheels own 33 Agent contracts and eight effectful Improvement Task contracts; four other legacy direct capability IDs are characterized as deterministic pure functions and do not enter the Kernel. Product owns exactly 33 deployment runtime bindings. Prepare → Agent runtime → finalize is one Attempt and one commit; validators and all six technical effects remain inside that transaction.

**Tech Stack:** Python 3.11, Pydantic v2, existing TaskHandler/TaskWorkspaceStore/activity/effect registries and append-only journal, LangGraph `1.2.11`, pytest, scripted fakes for crash cuts.

**Spec:** `docs/superpowers/specs/2026-08-31-python-native-langgraph-assurance-design.md`, sections 10–15 and 17–19, migration Phase 2, crash matrix, and Kernel tests.

---

## Global Constraints

- Follow the master interleave: Foundation Task 1 precedes Attempt Task 1; Foundation Tasks 2–7 then precede Attempt Tasks 2–6; Foundation Tasks 8–10 precede Attempt Tasks 7–10. Attempt Task 10 specifically requires Foundation Task 8's Boot seam.
- Work in the clean isolated worktree created for the plan suite. Never stage unrelated Capability/OpenCode changes from the original dirty worktree.
- Core must not import `agent_runtime_contracts`; the adapter constructs core `ResolvedAttemptContract` values.
- Keep 99 alias IDs, `expand_agent_job_slots`, existing Product slot bindings, and the legacy scheduler operational through shadow. This plan adds semantic resolution beside them and deletes nothing needed for rollback.
- Every shipped contract has a required ordered `validators` tuple. The migration baseline is explicitly `()` for every production contract because current Workflow nodes bind zero validators. Do not infer use from the 25-item registry and do not enable a production validator in this plan; the later Feature/Product test-only cloned-contract fixture is outside contributions and production counts.
- A validator sees the complete sealed staged set and authenticated bytes/input/output/evidence. Validation precedes durable prepare and promotion.
- The Kernel never selects the next graph node or sets Invocation terminal state.
- The initial implementation preserves the Invocation-wide pending barrier. Sibling pending writes can be durable, but no later superstep starts until the interrupted superstep closes.

Target layout:

```text
packages/framework/graph-engine/graph_engine/attempts/
├── __init__.py
├── contracts.py
├── context.py
├── keys.py
├── resolutions.py
├── resource_arbiter.py
├── kernel.py
└── node_factory.py

packages/adapters/agent-runtime-contracts/agent_runtime_contracts/
├── execution_contract.py
├── runtime_binding.py
└── attempt_executor.py

packages/capabilities/assurance-*/assurance_*/contracts/attempts.py
```

### Task 1: Define provider-neutral Attempt contracts, resolutions, and stable keys

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/attempts/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/contracts.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/context.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/resolutions.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/keys.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_contracts.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_resolutions.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_keys.py`

**Interfaces:** `TaskAttemptContract`, `AttemptExecutor`, `ResolvedAttemptContract`, `AttemptExecutionContext`, six closed `AttemptResolution` variants, `ReceiptRef`, validated `BusinessActivation`, `AttemptKey` and canonical key derivation.

- [ ] **Step 1: Write contract closure tests.**

```python
def test_task_contract_requires_explicit_validator_tuple() -> None:
    with pytest.raises(TypeError):
        TaskAttemptContract(
            contract_id="assurance.execution.run.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.run",
            input_model=RunInput,
            output_model=RunOutput,
            resources=ResourceClaims(),
            retry=AttemptRetryPolicy(max_attempts=1),
            timeout=AttemptTimeoutPolicy(seconds=60),
        )


def test_resolved_contract_digest_is_data_only() -> None:
    first = resolve_contract(contract, executor=executor_a)
    second = resolve_contract(contract, executor=executor_b)
    assert first.contract_digest == second.contract_digest
    assert "executor" not in first.canonical_projection()
```

The digest projection includes owner/contract/handler IDs, fully qualified model symbols, JSON-Schema digests, resource claims/templates, retry/timeout, and ordered validator IDs; it excludes callable identity and `repr`.

- [ ] **Step 2: Specify closed resolution invariants.**

Test these variants and fields:

```python
AttemptResolution = (
    CommittedTaskResult[OutputT]
    | RejectedTaskResult
    | PermanentTaskFailure
    | PendingTaskResult
    | IndeterminateTaskResult
    | CommittedEffectFailure
)
```

`CommittedEffectFailure` requires `writes_promoted=True` and a promotion receipt. Pending/indeterminate require a system wakeup/reconciliation reference and no committed output. Rejected/permanent failures cannot claim promotion.

- [ ] **Step 3: Specify semantic key behavior.**

Define `BusinessActivation` in `attempts/keys.py` as a frozen validated value object with `kind: Literal["root", "round", "trigger"]` and a nonempty bounded canonical `value`. Provide `one_shot()`, `for_round(index)` and `for_trigger(arrival_id)` constructors; reject negative rounds, blank/oversized values and noncanonical trigger IDs.

```python
def test_attempt_key_is_stable_across_technical_retry_and_replay() -> None:
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.for_round(2),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )
    replayed = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="a" * 64,
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        business_activation=BusinessActivation.for_round(2),
        contract_id="assurance.execution.agent.run.v1",
        validated_input=RunInput(change_id="chg-1"),
    )
    assert key == replayed


def test_new_business_round_changes_attempt_key() -> None:
    assert key_for(round=2) != key_for(round=3)
```

The actual test repeats full keyword arguments. Use `one_shot()` for a one-shot node, `for_round()` for an ordinary loop, or `for_trigger(current_trigger.arrival_id)` for a join-driven activation. Do not use LangGraph task/checkpoint IDs, random values, timestamps, or process identity.

- [ ] **Step 4: Run failures, implement immutable models, rerun.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_contracts.py \
  packages/framework/graph-engine/tests/attempts/test_resolutions.py \
  packages/framework/graph-engine/tests/attempts/test_keys.py
```

Expected before implementation: import errors. Validate `select()` output into `InputT`, serialize with the existing canonical JSON function, and include its digest in the key.

- [ ] **Step 5: Run static checks and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_contracts.py \
  packages/framework/graph-engine/tests/attempts/test_resolutions.py \
  packages/framework/graph-engine/tests/attempts/test_keys.py
uv run pyright packages/framework/graph-engine/graph_engine/attempts
git add \
  packages/framework/graph-engine/graph_engine/attempts/__init__.py \
  packages/framework/graph-engine/graph_engine/attempts/contracts.py \
  packages/framework/graph-engine/graph_engine/attempts/context.py \
  packages/framework/graph-engine/graph_engine/attempts/resolutions.py \
  packages/framework/graph-engine/graph_engine/attempts/keys.py \
  packages/framework/graph-engine/tests/attempts/test_contracts.py \
  packages/framework/graph-engine/tests/attempts/test_resolutions.py \
  packages/framework/graph-engine/tests/attempts/test_keys.py
git commit -m "feat: define semantic Attempt contracts and identity"
```

### Task 2: Resolve authenticated Attempt contributions into the core registry

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/models.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/contributions.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/registries.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/lock.py`
- Create: `packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`

**Interfaces:** immutable descriptor/realized `attempt_contracts`, `AttemptContractRegistry`, owner/dependency/handler/validator/model closure, contract projections in Product lock.

- [ ] **Step 1: Test descriptor/realized and owner closure.**

```python
def test_realized_attempt_contracts_must_match_descriptor_digests() -> None:
    with pytest.raises(PluginContractError, match="attempt contract"):
        realize_plugin(descriptor_with(contract_digest="a" * 64), contribution_with("b" * 64))


def test_contract_cannot_bind_foreign_handler_without_dependency_authority() -> None:
    with pytest.raises(RegistryConflictError, match="owner"):
        build_attempt_registry([intake_contract_using_generation_handler])
```

Also reject duplicate IDs, unsorted/duplicate validators, missing handler, missing validator, missing schema/model identity, configuration-tree contracts, and extra realized contracts.

- [ ] **Step 2: Prove ProductLock v3 and registry digests change while legacy v2 stays immutable.**

Change one model-schema digest, resource claim, timeout, or ordered validator ID and assert the Attempt registry and `ProductLock` v3 digests change. Swap only callable objects behind the same authenticated entry and assert the data projection remains stable because source digest already authenticates code. Re-run the `InvocationLock` v2 golden and assert its bytes do not change.

- [ ] **Step 3: Run focused tests and implement registry/projections.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py
```

Expected before implementation: missing registry type/fields. Implement with core data only; do not import `agent_runtime_contracts`.

- [ ] **Step 4: Verify composition and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/composition
uv run lint-imports
git add \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/composition/models.py \
  packages/framework/graph-engine/graph_engine/composition/contributions.py \
  packages/framework/graph-engine/graph_engine/composition/registries.py \
  packages/framework/graph-engine/graph_engine/composition/lock.py \
  packages/framework/graph-engine/tests/composition/test_attempt_contract_registry.py \
  packages/framework/graph-engine/tests/composition/test_lock_model.py
git commit -m "feat: authenticate semantic Attempt contract registry"
```

### Task 3: Upgrade Agent contracts and build the composite executor

**Files:**

- Rewrite: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py`
- Create: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/runtime_binding.py`
- Create: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/plugin_kit.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_models.py`
- Create: `packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`
- Create: `packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/protocol.py`
- Modify: `packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_config.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/test_observation.py`

**Interfaces:** `AgentExecutionContract[InputT, AgentResultT, OutputT]`, `AgentRuntimeBinding`, `CompositeAttemptExecutor`, provider schema capability negotiation and typed phase bundle.

- [ ] **Step 1: Write the three-model contract test.**

```python
def test_agent_result_and_finalize_output_are_distinct_contracts() -> None:
    contract = AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        requires_provider_schema=True,
        resources=claims,
        retry=retry,
        timeout=timeout,
        validators=(),
    )
    assert contract.agent_result_model is not contract.output_model
```

- [ ] **Step 2: Test exact runtime-binding closure.**

`AgentRuntimeBinding` contains contract ID, runtime handler ID, provider, model, policy and secret handles only. It cannot carry Feature resources, validators, prepare/finalize handlers, input/result/output models, or skill/profile authority.

- [ ] **Step 3: Test composite dataflow and schema negotiation.**

```python
async def test_composite_executor_passes_typed_phase_bundle_to_finalize() -> None:
    output = await executor.execute(validated_input, context)
    assert prepare.seen_input == validated_input
    assert runtime.seen_schema == CaseDesignAgentResult.model_json_schema()
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert output == expected_output


async def test_required_provider_schema_fails_closed_when_adapter_lacks_it() -> None:
    with pytest.raises(StructuredOutputCapabilityError):
        await executor_without_schema.execute(validated_input, context)
```

Always validate runtime output locally even when provider enforcement is advertised.

The current OpenCode adapter injects schema text into prompts and validates observations locally; it does not use a provider-enforced response-format API. Add `AgentRuntimeCapabilities(provider_schema=False)` and test that honest value. Do not claim provider enforcement or carry a private OpenCode source fork. A later pinned OpenCode/adapter upgrade may flip the capability only after an integration test proves the schema travels through the provider API and malformed provider output is rejected before finalize. Until that test passes, every contract with `requires_provider_schema=True` remains a cutover blocker.

- [ ] **Step 4: Cover the two eliminated Intake fanouts.**

Add primary and repair tests named `test_case_design_composite_attempt_preserves_both_legacy_prepare_consumers` and `test_case_design_repair_composite_attempt_preserves_both_legacy_prepare_consumers`. Each proves the prepared value reaches runtime input and the prepared value plus validated Agent result reach finalize. These replace, rather than recreate, legacy `min_matches: 2` phase fanout.

- [ ] **Step 5: Run failing tests, implement, and verify.**

```bash
uv run pytest -q packages/adapters/agent-runtime-contracts/tests
uv run pyright packages/adapters/agent-runtime-contracts/agent_runtime_contracts
uv run lint-imports
```

Expected before implementation: new imports or model fields fail; after implementation all exit `0` and core remains independent.

- [ ] **Step 6: Commit the adapter contract.**

```bash
git add \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/__init__.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/execution_contract.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/runtime_binding.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts/plugin_kit.py \
  packages/adapters/agent-runtime-contracts/tests/test_models.py \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py \
  packages/adapters/agent-runtime-contracts/tests/test_runtime_binding.py \
  packages/adapters/agent-runtime-opencode/agent_runtime_opencode/protocol.py \
  packages/adapters/agent-runtime-opencode/agent_runtime_opencode/observation.py \
  packages/adapters/agent-runtime-opencode/tests/test_config.py \
  packages/adapters/agent-runtime-opencode/tests/test_observation.py
git commit -m "feat: compose one typed Agent Attempt"
```

### Task 4: Define 41 Feature-owned Attempt contracts and freeze four pure functions

**Files:**

- Create: `packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py`
- Create: `packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py`
- Create: `packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py`
- Create: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py`
- Create: `packages/capabilities/assurance-intake/assurance_intake/contracts/decisions.py`
- Create: `packages/capabilities/assurance-generation/assurance_generation/contracts/decisions.py`
- Create: `packages/capabilities/assurance-healing/assurance_healing/contracts/decisions.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/__init__.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/__init__.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/contracts/__init__.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/__init__.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/contracts/__init__.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/contracts/__init__.py`
- Create: `packages/capabilities/assurance-intake/tests/test_attempt_contracts.py`
- Create: `packages/capabilities/assurance-generation/tests/test_attempt_contracts.py`
- Create: `packages/capabilities/assurance-execution/tests/test_attempt_contracts.py`
- Create: `packages/capabilities/assurance-quality/tests/test_attempt_contracts.py`
- Create: `packages/capabilities/assurance-healing/tests/test_attempt_contracts.py`
- Create: `packages/capabilities/assurance-improvement/tests/test_attempt_contracts.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin.py`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/operations/workflow_state.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/workflow_state.py`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/operations/workflow_state.py`

**Interfaces:** exactly 33 Agent contracts and eight distinct direct Task contracts as immutable owner contributions; four additional legacy direct IDs remain authenticated handlers during coexistence but become ordinary typed graph functions; zero implicit validator bindings.

- [ ] **Step 1: Freeze the exact Agent catalog and counts.**

Tests assert per-owner counts: Intake 4, Generation 14, Execution 2, Quality 5, Healing 2, Improvement 6. Preserve current semantic IDs:

```python
EXPECTED_AGENT_COUNTS = {
    "assurance.intake": 4,
    "assurance.generation": 14,
    "assurance.execution": 2,
    "assurance.quality": 5,
    "assurance.healing": 2,
    "assurance.improvement": 6,
}
assert sum(EXPECTED_AGENT_COUNTS.values()) == 33
```

Every contract names concrete Pydantic input/result/output models, prepare/finalize handlers, resources, retry, timeout, and `validators=()`.

- [ ] **Step 2: Freeze and classify the 12 legacy direct-task candidates.**

Characterization proves these four IDs are deterministic, perform no filesystem/network/effect/activity work, and require no durable evidence; retain their Pydantic input/output behavior but do not publish `TaskAttemptContract` values for the Python graph:

```text
assurance.generation.complete
assurance.generation.review-round.advance
assurance.healing.repair-round.advance
assurance.intake.review-round.advance
```

They account for 16 of the 25 direct occurrences. The remaining eight IDs are effectful Improvement contracts and account for nine Attempt occurrences:

```text
assurance.improvement.retro-collect-v3
assurance.improvement.reconcile-improvements
assurance.improvement.evaluate-memory-improvement
assurance.improvement.export-change-improvement
assurance.improvement.apply-improvement-auto-review
assurance.improvement.apply-improvement-review
assurance.improvement.apply-memory-improvement
assurance.improvement.rollback-memory-improvement
```

`assurance.improvement.evaluate-memory-improvement` is not the pure offline benchmark Eval comparator. It occurs in both standalone `improvement-evaluate` and `improvement-apply`, returns `MemoryEvalReceipt`, and emits registered effect kind `assurance.improvement.effect.delivery.v1` with payload discriminator `memory_eval`; both occurrences therefore use the Kernel.

Move the three sets of pure models/transforms into owner-local `contracts/decisions.py`; make the existing legacy `operations/workflow_state.py` handlers delegate to those functions while coexistence continues. Tests assert the pure functions reject invalid inputs and return the same canonical output as their legacy handlers without touching a spy `TaskContext`. Tests assert the other eight resolve as `TaskAttemptContract` values with `validators=()`.

- [ ] **Step 3: Prove registration is not binding.**

Across six tests assert 25 validators remain registered, the 41 effectful contracts are complete, and every validator tuple is explicitly empty. Add a negative constructor test showing omission is invalid and a registry test showing unused registered validators are legal.

- [ ] **Step 4: Move surviving semantic values without deleting legacy exports.**

Define new contracts in `contracts/attempts.py`; make current `contracts/workflow.py` import/re-expose data needed by the legacy Runtime so shadow remains green. Add contract projections to the authenticated plugin contribution/declaration. Do not change Workflow YAML or alias bindings in this Task.

- [ ] **Step 5: Run all six contract suites.**

```bash
uv run pytest -q \
  packages/capabilities/assurance-intake/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-generation/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-execution/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-quality/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-healing/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-intake/tests/test_workflow_module.py \
  packages/capabilities/assurance-generation/tests/test_workflow_module.py \
  packages/capabilities/assurance-execution/tests/test_workflow_module.py \
  packages/capabilities/assurance-quality/tests/test_workflow_module.py \
  packages/capabilities/assurance-healing/tests/test_workflow_module.py \
  packages/capabilities/assurance-improvement/tests/test_workflow_module.py
```

Expected: all new and legacy suites pass.

- [ ] **Step 6: Commit the Feature contract catalog.**

```bash
git add \
  packages/capabilities/assurance-intake/assurance_intake/contracts/attempts.py \
  packages/capabilities/assurance-generation/assurance_generation/contracts/attempts.py \
  packages/capabilities/assurance-execution/assurance_execution/contracts/attempts.py \
  packages/capabilities/assurance-quality/assurance_quality/contracts/attempts.py \
  packages/capabilities/assurance-healing/assurance_healing/contracts/attempts.py \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/attempts.py \
  packages/capabilities/assurance-intake/assurance_intake/contracts/decisions.py \
  packages/capabilities/assurance-generation/assurance_generation/contracts/decisions.py \
  packages/capabilities/assurance-healing/assurance_healing/contracts/decisions.py \
  packages/capabilities/assurance-intake/assurance_intake/contracts/__init__.py \
  packages/capabilities/assurance-generation/assurance_generation/contracts/__init__.py \
  packages/capabilities/assurance-execution/assurance_execution/contracts/__init__.py \
  packages/capabilities/assurance-quality/assurance_quality/contracts/__init__.py \
  packages/capabilities/assurance-healing/assurance_healing/contracts/__init__.py \
  packages/capabilities/assurance-improvement/assurance_improvement/contracts/__init__.py \
  packages/capabilities/assurance-intake/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-generation/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-execution/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-quality/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-healing/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-intake/assurance_intake/plugin.py \
  packages/capabilities/assurance-generation/assurance_generation/plugin.py \
  packages/capabilities/assurance-execution/assurance_execution/plugin.py \
  packages/capabilities/assurance-quality/assurance_quality/plugin.py \
  packages/capabilities/assurance-healing/assurance_healing/plugin.py \
  packages/capabilities/assurance-improvement/assurance_improvement/plugin.py \
  packages/capabilities/assurance-intake/assurance_intake/plugin-declaration.json \
  packages/capabilities/assurance-generation/assurance_generation/plugin-declaration.json \
  packages/capabilities/assurance-execution/assurance_execution/plugin-declaration.json \
  packages/capabilities/assurance-quality/assurance_quality/plugin-declaration.json \
  packages/capabilities/assurance-healing/assurance_healing/plugin-declaration.json \
  packages/capabilities/assurance-improvement/assurance_improvement/plugin-declaration.json \
  packages/capabilities/assurance-intake/assurance_intake/operations/workflow_state.py \
  packages/capabilities/assurance-generation/assurance_generation/operations/workflow_state.py \
  packages/capabilities/assurance-healing/assurance_healing/operations/workflow_state.py
git diff --cached --name-only
```

Confirm the staged list is exactly the 36 paths above, then:

```bash
git commit -m "feat: publish semantic Attempt contracts"
```

### Task 5: Bind exactly 33 Product runtimes while retaining 99 aliases

**Files:**

- Create: `packages/products/assurance-product/assurance_product/runtime_bindings.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Modify: `packages/products/assurance-product/assurance_product/agent_contracts.py`
- Create: `tests/product/test_semantic_attempt_bindings.py`
- Modify: `tests/product/test_agent_execution_contracts.py`
- Modify: `tests/product/test_graph_binding_audit.py`

**Interfaces:** one `AgentRuntimeBinding` per Agent contract, no extras; Product cannot override Feature resources/validators/models; semantic and alias maps coexist.

- [ ] **Step 1: Write exact set-equality tests.**

```python
def test_product_has_exactly_one_runtime_binding_per_agent_contract() -> None:
    contracts = all_feature_agent_contracts()
    assert len(contracts) == 33
    assert set(AGENT_RUNTIME_BINDINGS) == set(contracts)
    assert len(AGENT_RUNTIME_BINDINGS) == 33


def test_semantic_bindings_coexist_with_legacy_aliases_during_shadow() -> None:
    composition = resolve_assurance_composition(request)
    assert len(composition.semantic_attempt_contracts) == 41
    assert len(LEGACY_AGENT_PHASE_ALIASES) == 99
```

- [ ] **Step 2: Test forbidden Product authority.**

Try to supply validators/resources/output model in a runtime binding and assert Pydantic rejects the extra fields. Resolve a binding whose runtime handler owner is outside authenticated Product dependency closure and assert Boot rejects it.

- [ ] **Step 3: Implement bindings from existing deployment values.**

Use current Product provider/model/policy/secret selection, keyed by semantic contract ID rather than phase alias. Resolve each Agent contract plus binding through `CompositeAttemptExecutor` into core `ResolvedAttemptContract`. Resolve the eight Improvement direct contracts without Product runtime binding; the four pure functions are not present in this registry.

- [ ] **Step 4: Verify old and new closure.**

```bash
uv run pytest -q \
  tests/product/test_semantic_attempt_bindings.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_graph_binding_audit.py \
  tests/product/test_product_composition.py
uv run lint-imports
```

Expected: 33 runtime bindings, 41 resolved contracts, and 99 aliases are all asserted while legacy composition still compiles.

- [ ] **Step 5: Commit Product semantic bindings.**

```bash
git add \
  packages/products/assurance-product/assurance_product/runtime_bindings.py \
  packages/products/assurance-product/assurance_product/product.py \
  packages/products/assurance-product/assurance_product/agent_contracts.py \
  tests/product/test_semantic_attempt_bindings.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_graph_binding_audit.py
git commit -m "feat: resolve exact Product Agent runtime bindings"
```

### Task 6: Seal immutable candidate bytes and enrich validator context

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_validator_context.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_task_workspace.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py`

**Interfaces:** immutable `SealedFile`/`SealedWriteSet`, durable `PreparedWorkspaceRef`, enriched immutable `ValidationContext`, and ordered validator runner. Existing validators can adapt without becoming bound.

- [ ] **Step 1: Write full staged-set and immutability tests.**

```python
async def test_seal_authenticates_complete_candidate_bytes(provider, binding) -> None:
    sealed = await provider.seal(binding)
    assert tuple(item.path for item in sealed.files) == tuple(sorted(all_staged_paths))
    assert all(sha256(item.content).hexdigest() == item.after_sha256 for item in sealed.files)
    mutate_staged_file()
    with pytest.raises(TaskWorkspaceViolation, match="drifted after sealing"):
        await provider.prepare(binding, sealed)


async def test_promotion_consumes_durable_sealed_bytes_not_mutated_stage(provider, binding) -> None:
    sealed = await provider.seal(binding)
    prepared = await provider.prepare(binding, sealed)
    mutate_or_remove_live_stage()
    receipt = await provider.promote(prepared)
    assert canonical_file.read_bytes() == sealed.files[0].content
    assert receipt.sealed_digest == sealed.sealed_digest


def test_validator_context_contains_authenticated_semantic_values() -> None:
    assert context.task_input == validated_input.model_dump(mode="json")
    assert context.task_output == validated_output.model_dump(mode="json")
    assert context.evidence_refs == expected_evidence
    assert context.write_set.sealed_digest == sealed.sealed_digest
```

- [ ] **Step 2: Test ordering/rejection/exception behavior.**

Run validator IDs in contract tuple order. First rejection stops promotion and returns `RejectedTaskResult`; an exception becomes typed permanent/internal failure according to policy, never an implicit accept. Empty tuple performs zero calls and remains explicit.

- [ ] **Step 3: Run tests, implement descriptor-pinned byte reads, rerun.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py
```

Expected before implementation: missing enriched fields/API. Introduce the following responsibilities:

```python
@dataclass(frozen=True, slots=True)
class SealedFile:
    path: str
    before_sha256: str | None
    before_mode: int | None
    after_sha256: str
    after_mode: int
    content: bytes


@dataclass(frozen=True, slots=True)
class SealedWriteSet:
    files: tuple[SealedFile, ...]
    sealed_digest: str


class WorkspaceProvider(Protocol):
    async def open_or_create(
        self, attempt_key: AttemptKey, claims: ResourceClaims
    ) -> TaskWorkspaceBinding: ...
    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet: ...
    async def prepare(
        self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet
    ) -> PreparedWorkspaceRef: ...
    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...
    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt: ...
```

Read initial sealed bytes through authenticated directory descriptors, not unpinned paths. `prepare` durably persists/authenticates those bytes. Promotion consumes only `PreparedWorkspaceRef`, never the mutable stage tree.

- [ ] **Step 4: Verify and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py
git add \
  packages/framework/graph-engine/graph_engine/plugin_api.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/tests/attempts/test_validator_context.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py
git commit -m "feat: validate authenticated sealed candidate bytes"
```

### Task 7: Implement durable resource arbitration

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/attempts/resource_arbiter.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_resource_arbiter.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/context.py`

**Interfaces:** `ResourceArbiterPort`, `ResourceAuthorizationStorePort`, append-only `ResourceAuthorizationRecord`, durable acquire/adopt/release keyed by Attempt key and fence; read/write/exclusive conflict matrix.

- [ ] **Step 1: Specify the conflict matrix and idempotent adoption.**

```python
@pytest.mark.parametrize(("left", "right", "conflicts"), [
    (reads("qa/a"), reads("qa/a"), False),
    (reads("qa/a"), writes("qa/a"), True),
    (writes("qa/a"), writes("qa/a/b"), True),
    (exclusive("qa"), reads("qa/a"), True),
    (exclusive("qa/a"), exclusive("qa/b"), False),
    (writes("qa/a"), writes("qa/b"), False),
])
def test_resource_conflict_matrix(left, right, conflicts, arbiter) -> None:
    assert arbiter.claims_conflict(left, right) is conflicts


async def test_replay_adopts_same_authorization_but_stale_fence_cannot_use_it() -> None:
    first = await arbiter.acquire(attempt_key, claims, fencing_token=4)
    replay = await arbiter.adopt(attempt_key, fencing_token=5)
    assert replay.authorization_id == first.authorization_id
    with pytest.raises(StaleFencingToken):
        await arbiter.release(attempt_key, fencing_token=4)
```

- [ ] **Step 2: Run failure, implement durable records, and test parallelism.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_resource_arbiter.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
```

Expected: missing modules. Resolve templates only from validated task input. Persist acquire/adopt/release as canonical compare-and-swap records in `persistence/resource_authorization.py` using the same fenced append-only storage primitive as the journals; a process-local lock alone is forbidden. Acquire before external dispatch. A conflict always returns `PendingTaskResult` with a durable resource-wakeup reference; there is no undeclared per-contract wait policy and therefore no missing digest field. Non-conflicting Attempts proceed concurrently. Restart tests reconstruct the arbiter/store and prove the full handoff: fence 4 acquires, lease loss lets fence 5 atomically adopt the same authorization, fence 4 can neither use nor release it, terminal/failure recovery by fence 5 releases it exactly once, and a previously blocked Attempt then acquires and progresses. A newer fence may adopt an authorization only for the same Attempt key; conflicting Attempts remain blocked until durable release.

- [ ] **Step 3: Commit arbitration.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_resource_arbiter.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
git add \
  packages/framework/graph-engine/graph_engine/attempts/context.py \
  packages/framework/graph-engine/graph_engine/attempts/resource_arbiter.py \
  packages/framework/graph-engine/graph_engine/persistence/resource_authorization.py \
  packages/framework/graph-engine/tests/attempts/test_resource_arbiter.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
git commit -m "feat: arbitrate durable Attempt resource claims"
```

### Task 8: Extract the reliable workspace transaction into AssuranceAttemptKernel

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/events.py`
- Create: `packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py`
- Create: `packages/framework/graph-engine/tests/persistence/test_attempt_journal.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/task_workspace.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/activity.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/ledger.py`

**Interfaces:** async `AttemptKernelPort.execute_or_recover`; `AttemptJournalPort` and durable adapter; closed Attempt journal events/snapshot with compare-and-swap append; adoption of current TaskWorkspaceStore/activity/promotion primitives; fence checks at every irreversible boundary.

- [ ] **Step 1: Define and test the Attempt journal state machine.**

The closed event set covers open, resource authorization, activity prepare/dispatch/bind/terminal observation, commit prepare, promotion, effect intent/apply/receipt, system-interrupt issue/issuance-anchor/completion-anchor, terminal, and resource release. Define `AttemptJournalPort.load/append/ensure_durable` here using the Task 1 `AttemptKey` and this Task's event/snapshot types; implement its durable adapter on the same append-only storage/fence primitive as the checkpoint journal without giving it any Workflow-transition API. Test identical append replay, different batch at the same expected revision conflict, gap/corruption rejection, stale-fence rejection, and `ensure_durable`. `SystemInterruptIssued` includes generation/ordinal/envelope digest. `SystemInterruptIssuanceAnchored` proves the interrupt-bearing checkpoint but does not retire it; only the explicit Task 10 bridge's matching `SystemInterruptCompletionCheckpointed` event retires the active generation after a successful resumed-node checkpoint.

```bash
uv run pytest -q packages/framework/graph-engine/tests/persistence/test_attempt_journal.py
```

Expected: fail because Attempt journal events and CAS implementation are absent.

- [ ] **Step 2: Write one complete happy-path test.**

Assert the exact trace:

```python
assert trace == [
    "adopt_or_create",
    "authorize_resources",
    "begin_workspace",
    "execute",
    "validate_output",
    "seal_candidate",
    "run_validators",
    "durable_prepare",
    "promote",
    "settle_effects",
    "publish_receipt",
]
assert isinstance(result, CommittedTaskResult)
```

- [ ] **Step 3: Write crash-window tests before implementation.**

Parameterize faults before durable prepare, after prepare/before promotion, during multi-file promotion, after promotion/before receipt, and after receipt/before graph checkpoint. Replaying the same key must return the same receipt, never rerun committed mutation, and reject input/contract/revision drift.

- [ ] **Step 4: Test activity adoption and fences.**

An in-flight recoverable handler is reconciled/adopted with the same key. Fence is checked before external dispatch, durable prepare, promotion, effect application, and terminal receipt. After lease loss, the old runner can observe completion but cannot commit; the new runner adopts it.

- [ ] **Step 5: Run failures and implement by moving, not duplicating, transaction logic.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py
```

Expected: missing Kernel. Extract reusable workspace/activity/journal operations from `runtime/scheduler.py` behavior; do not have the Kernel call the legacy planner/scheduler or write legacy Workflow terminal events.

- [ ] **Step 6: Verify legacy recovery remains green.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_activity_recovery.py \
  packages/framework/graph-engine/tests/runtime/test_scheduler.py
```

- [ ] **Step 7: Commit Kernel extraction.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/attempts/events.py \
  packages/framework/graph-engine/graph_engine/persistence/attempt_journal.py \
  packages/framework/graph-engine/graph_engine/runtime/task_workspace.py \
  packages/framework/graph-engine/graph_engine/runtime/activity.py \
  packages/framework/graph-engine/graph_engine/runtime/ledger.py \
  packages/framework/graph-engine/tests/attempts/test_kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py
git commit -m "feat: execute recoverable semantic Attempts"
```

### Task 9: Move all six technical effects inside the Attempt transaction

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/effects/__init__.py`
- Create: `packages/framework/graph-engine/graph_engine/effects/contracts.py`
- Create: `packages/framework/graph-engine/graph_engine/effects/apply.py`
- Create: `packages/framework/graph-engine/graph_engine/effects/recovery.py`
- Retain and import directly: `packages/framework/graph-engine/graph_engine/json_schema.py`
- Retain unchanged until Product Task 9 deletes the compatibility layer: `packages/framework/graph-engine/graph_engine/runtime/json_schema.py`
- Modify: `packages/framework/graph-engine/graph_engine/runtime/effects.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_effects.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py`

**Interfaces:** Kernel-local effect state machine for exactly six registered kinds; idempotency key derived from Attempt/effect ordinal; pending/indeterminate/committed-failure resolutions.

- [ ] **Step 1: Freeze the exact kind set.**

```python
EXPECTED_EFFECT_KINDS = {
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.healing.effect.proposal-approved.v1",
    "assurance.improvement.effect.archive.v1",
    "assurance.improvement.effect.delivery.v1",
    "assurance.improvement.effect.promotion.v1",
}
assert set(effect_registry.entries) == EXPECTED_EFFECT_KINDS
```

Explicitly assert `improvement-evaluate/export/apply/rollback` are graph names, not effect kinds.

- [ ] **Step 2: Cover apply/reconcile outcomes and crash cuts.**

Parameterize the apply/reconcile/crash matrix over every member of `EXPECTED_EFFECT_KINDS`; a set-equality test alone is insufficient. For each kind, cover applied, transient, pending, not-applied, permanent failure, invalid receipt, and unknown outcome and assert the correct resolution. For each kind, crash after file promotion but before/during effect return and prove reconciliation does not repeat promotion. A permanent effect failure returns `CommittedEffectFailure(writes_promoted=True)`.

- [ ] **Step 3: Run failures, implement the extracted state machine, rerun.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py
```

Expected before implementation: missing effect package/new Kernel behavior. The repository already owns the reusable validator at `graph_engine.json_schema`, and `runtime/json_schema.py` is already a thin compatibility re-export. Make both the new effect package and legacy `runtime/effects.py` import the retained top-level module directly; leave the existing wrapper unchanged until Product drain. Reuse registered handlers/policies/schema validation; persist apply/reconcile states in Attempt journal, not graph state. This makes deletion of `graph_engine.runtime` incapable of stranding the new effect engine on a legacy import.

- [ ] **Step 4: Verify coexistence with the legacy settle loop.**

Legacy aliases still use `runtime/effects.py`; semantic Kernel contracts use only the new in-Attempt protocol. Tests prove one Attempt cannot be settled by both. Do not delete the old loop until Product drain.

- [ ] **Step 5: Commit effect transaction.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/effects/__init__.py \
  packages/framework/graph-engine/graph_engine/effects/contracts.py \
  packages/framework/graph-engine/graph_engine/effects/apply.py \
  packages/framework/graph-engine/graph_engine/effects/recovery.py \
  packages/framework/graph-engine/graph_engine/runtime/effects.py \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py
git commit -m "feat: settle durable effects inside Attempts"
```

### Task 10: Implement the owner-scoped Attempt node factory and interrupt replay

**Dependency:** Foundation Task 8 is complete and exposes the Boot-internal resolver plus owner-scoped build context.

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/attempts/node_factory.py`
- Create: `packages/framework/graph-engine/graph_engine/attempts/checkpoint_bridge.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_node_factory.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py`
- Create: `packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/kernel.py`
- Modify: `packages/framework/graph-engine/graph_engine/boot/boot.py`

**Interfaces:** `AttemptNodeFactory.attempt`, owner-scoped `CapabilityBuildContext.attempt`, resolution-to-state mapping, durable `pending_generation` and interrupt ordinal replay, plus `AttemptCheckpointObserver(CheckpointAnchorObserverPort)`.

- [ ] **Step 1: Test the only graph-facing effect seam.**

```python
async def test_committed_resolution_publishes_typed_output_and_receipt() -> None:
    node = factory.attempt(
        contract,
        semantic_node_id="execution.run",
        activation=select_activation,
        select=select_input,
        publish=publish_output,
    )
    update = await node(state, runtime=runtime)
    assert kernel.seen_key == expected_semantic_key
    assert update == {"execution": output, "receipts": (receipt_ref,)}


def test_owner_context_rejects_foreign_contract_and_node_site_authority() -> None:
    with pytest.raises(ContractOwnershipError):
        intake_context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign",
            activation=activation,
            select=select,
            publish=publish,
        )
    with pytest.raises(TypeError):
        intake_context.attempt(
            own_id,
            semantic_node_id="intake.own",
            activation=activation,
            handler_id="injected",
            select=select,
            publish=publish,
        )
```

- [ ] **Step 2: Test every resolution mapping.**

Committed calls `publish`; rejected/permanent/committed-effect-failure produce typed failure updates; pending/indeterminate emit system interrupts; none writes terminal status directly. Human interrupts are absent from this factory.

Add activation-selector tests: a one-shot replay returns the same key, a new business round changes it, two distinct current-trigger arrival IDs in the same epoch produce distinct keys, and replay of one arrival ID reuses the key. Reject an empty or noncanonical activation before Kernel dispatch.

- [ ] **Step 3: Test stable interrupt ordinal/generation replay.**

```python
async def test_resume_replays_issued_interrupt_before_kernel_reentry() -> None:
    first = await invoke_until_interrupt(node, state)
    assert first.value["pending_generation"] == 1
    journal.mark_kernel_now_committed(attempt_key)
    resumed = await resume_after_crash_before_checkpoint(node, first)
    assert trace[:2] == ["interrupt:generation=1:ordinal=0", "kernel:recover"]
    assert resumed["result"] == committed_output
```

Also crash after resume/before node checkpoint, multiple pending generations, and technical retry. The node must reissue every durable prior interrupt at the same ordinal before re-entering Kernel; it cannot conditionally skip it because external state changed.

- [ ] **Step 4: Bridge both checkpoint phases back to the Attempt journal.**

System-interrupt payloads include an issuance marker `{kind: "system_interrupt_issued", attempt_key, generation, ordinal, envelope_digest}`. `AnchoredCheckpointer` persists extracted markers in its outbox in the same transaction as checkpoint bytes. `AttemptCheckpointObserver.on_anchored(notice)` validates marker/checkpoint/invocation identity and idempotently appends `SystemInterruptIssuanceAnchored`; that event proves exposure but leaves `pending_generation` active.

When the resumed node has replayed its issued calls and Kernel returns a non-pending resolution, it merges `publish()` with the reserved `assurance_checkpoint_markers` replacement channel from Foundation Task 4. The batch has one `{kind: "system_interrupt_completed", attempt_key, generation, ordinal, envelope_digest}` entry for every active generation replayed during this node invocation, in ordinal order. A pending write containing that partial update never retires anything; only the later `aput` anchor for the merged successful resumed-node checkpoint creates the completion notice. The observer atomically appends the matching `SystemInterruptCompletionCheckpointed` batch, and only those events retire `SystemInterruptIssued`. A fresh success writes an empty batch; public output and `select()` omit the reserved channel. Enforce the frozen active-generation bound before another interrupt is issued.

Test both crash cuts independently: before/after issuance-observer delivery and before/after completion-observer delivery. Add pending generation 1 → resume → pending generation 2 → resume → commit, crash and duplicate-delivery variants, and assert both generations retire while an unrelated generation remains. Also test mismatched digest/generation, a human interrupt with no Attempt marker, and later re-entry after completion to prove no stale ordinal is replayed. Recovery redelivers notices, keeps an issuance-only generation active, and never retires the wrong generation.

- [ ] **Step 5: Prove Invocation-wide pending barrier.**

With two parallel nodes, make one Kernel return pending and one commit. Assert the sibling pending write is anchored, the sibling is not rerun, and no next superstep begins until the interrupted node resumes.

- [ ] **Step 6: Run focused tests, implement, and run the Attempt gate.**

```bash
uv run pytest -q \
  packages/framework/graph-engine/tests/attempts/test_node_factory.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py
uv run pytest -q packages/framework/graph-engine/tests/attempts
uv run pyright packages/framework/graph-engine/graph_engine/attempts
uv run lint-imports
```

Expected: all exit `0` after implementation.

- [ ] **Step 7: Commit node integration.**

```bash
git add \
  packages/framework/graph-engine/graph_engine/attempts/node_factory.py \
  packages/framework/graph-engine/graph_engine/attempts/checkpoint_bridge.py \
  packages/framework/graph-engine/graph_engine/attempts/kernel.py \
  packages/framework/graph-engine/graph_engine/boot/boot.py \
  packages/framework/graph-engine/tests/attempts/test_node_factory.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_replay.py \
  packages/framework/graph-engine/tests/attempts/test_system_interrupt_checkpoint_bridge.py
git commit -m "feat: adapt semantic Attempts to LangGraph nodes"
```

## Attempt Kernel exit gate

- [ ] Run `uv run pytest -q packages/framework/graph-engine/tests/attempts packages/adapters/agent-runtime-contracts/tests tests/product/test_semantic_attempt_bindings.py`.
- [ ] Run all six Feature `test_attempt_contracts.py` plus all six legacy `test_workflow_module.py`; both semantic and rollback surfaces pass.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run lint-imports`.
- [ ] Review a machine-readable catalog dump: 33 Agent contracts, eight direct Attempt contracts, four pure direct functions, 33 runtime bindings, 41 resolved contracts, 25 registered validators, zero bound validators, six effect kinds, and 99 still-live legacy phase aliases.
- [ ] Request code review focused on canonical input keys, provider schema negotiation plus local validation, sealed candidate bytes, validator ordering, crash-after-promotion behavior, effect reconciliation, fence checks, and system-interrupt ordinal replay.
