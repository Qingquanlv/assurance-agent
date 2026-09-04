# Attempt Runtime Production Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one current-version, OpenCode-only Product runtime in which all 41 semantic contracts execute through the durable Attempt transaction.

**Architecture:** Python LangGraph remains the sole Workflow authority and `AssuranceAttemptKernel.execute_or_recover(...)` remains the sole Attempt transaction boundary. Product constructs current SQLite persistence, workspace, activity, raw Agent/Task executors, and one Effect state port under the live runner fence; old Runtime/data readers and Cursor are deleted rather than routed or disabled. Effect composition keeps one authenticated static `EffectRegistry` and injects a bound call context at settlement time.

**Tech Stack:** Python 3.11, Pydantic v2, LangGraph 1.2.11, SQLite/aiosqlite, uv workspace, pytest, Ruff, Pyright, and import-linter.

**Spec:** `docs/superpowers/specs/2026-09-03-attempt-runtime-production-closure-design.md`

**2026-09-04 amendment:** Phase P is implemented. The former P14 protected live-provider gate was
cancelled and removed by `docs/superpowers/specs/2026-09-04-checkpoint-r-removal-design.md`. P1–P13
remain implementation history; no task below authorizes recreating P14.

**Deferred companion spec:** `docs/superpowers/specs/2026-09-02-attempt-kernel-internal-refactor-design.md` constrains the later behavior-preserving Phase I plan; by its own decision point, it does not authorize a pre-Phase-P code decomposition here.

## Global Constraints

- Work only in `/Users/lvqingquan/agent/assurance-agent/.worktrees/attempt-kernel-internal-refactor` on `codex/attempt-kernel-internal-refactor`.
- Do not implement the Phase I Kernel decomposition from `2026-09-02-attempt-kernel-internal-refactor-design.md`; this plan ends at Phase P.
- Keep all 14 public roots on Python LangGraph. Do not restore Workflow YAML, a lowering layer, scheduler, shadow runtime, or entrypoint selector.
- OpenCode is the only Product Agent runtime. Delete Cursor integration; do not leave an offline-only provider, fail-red shell, adapter enum, provider switch, fixture, benchmark, or packaging extra.
- Accept only schema IDs and versions explicitly locked by the candidate ProductLock. Do not migrate, backfill, quarantine, route, or specially diagnose data from an older release.
- Raw Agent is permanent: one OpenCode root session, ordinary prompt-carried Schema instructions, exactly one assistant JSON object, local `AgentResultT` validation, and deterministic file-reading finalizers.
- Do not add OpenCode `format.json_schema`, Structured Artifact materialization, provider-schema negotiation, or child sessions.
- Keep one static authenticated `EffectRegistration(handler)` / `EffectRegistry`. Do not add `EffectStoreContract`, handler factories, runtime profiles, a factory registry, or a second runtime registry.
- Keep the immutable commit order: validate output/intents → journal output/intents → seal → Validators → durable prepare → promote/recover → Effects → terminal receipt → resource release → `ResourcesReleased` proof.
- All mutating invocation objects are created after runner-lease acquisition and carry that exact positive fencing token. Remove every default fence.
- Production paths may not use pickle, memory-only stores, `_DeferredPhase`, `_DeferredTaskExecutor`, `_UnusedWorkspace`, `fixture-model`, scripted Kernel results, or untyped placeholder objects.
- Preserve current same-version crash recovery, current GraphRevision reopening, binding aliases, archive recovery, and OpenCode poll/list fallback; those are not historical compatibility.
- Use `uv run ...` for Python commands. Each task follows red → green → focused checks → commit and leaves the branch testable.

## Dependency Order

```text
P1 remove Cursor
  └─> P2 single Product identity ─> P3 current-only framework records
                                   └─> P4/P5 strict Capability artifacts

P3 ─> P6 SQLite Attempt + authorization
P3 ─> P7 one Effect registry + SQLite Effect state
P6 ─> P8 executed-result contract ─> P9 journal-backed activity
P7 + P8 + P9 ─> P10 real Agent/Task executors
P6 + P7 + P10 ─> P11 lease-bound Product assembly
P11 ─> P12 Kernel recovery/terminal release
P12 ─> P13 lifecycle and packaging closure ─> ordinary repository gate
```

## Target File Ownership

```text
packages/framework/graph-engine/graph_engine/
├── application/application.py       # lease-bound execution factory protocol/use
├── attempts/
│   ├── contracts.py                 # AuthorizedAttemptScope and executed result contract
│   ├── activity.py                  # journal-backed activity port only
│   ├── events.py                    # current Attempt event versions
│   ├── kernel.py                    # unchanged public coordinator/transaction authority
│   └── production_{host,worker}.py  # host isolation and synchronous activity bridge
├── effects/
│   ├── apply.py                     # existing settler
│   └── state.py                     # EffectStatePort and bound EffectCallContext
└── persistence/
    ├── attempt_journal.py
    └── resource_authorization.py

packages/products/assurance-product/assurance_product/
├── invocation_identity.py           # one current identity handshake
├── runtime_ports.py                 # Product composition and lease-bound binding
├── runtime_bindings.py              # 33 real OpenCode + eight real Task executors
├── sqlite_attempt_store.py
├── sqlite_resource_authorization.py
└── sqlite_effect_state.py
```

The following have no replacement directory: `packages/adapters/agent-runtime-cursor/`, legacy evidence/runtime readers, old Capability artifact models/aliases, and Effect factory registries.

---

### Task P1: Delete Cursor and collapse Product to one Agent provider

**Files:**

- Delete: `packages/adapters/agent-runtime-cursor/`
- Delete: `packages/products/assurance-product/assurance_product/product-declaration-cursor.json`
- Delete: `tests/product/fixtures/deployment/cursor.yaml`
- Delete: `tests/phase4/fixtures/bindings-cursor/`
- Delete: `benchmark/agent-runtime-phase3/run-cursor.sh`
- Delete: `benchmark/assurance-product/run-cursor.sh`
- Delete: `benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-cursor.sh`
- Delete: `examples/agent-runtime-fixture/agent_runtime_fixture/cursor-binding-declaration.json`
- Delete: `examples/agent-runtime-fixture/manifests/cursor.json`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.importlinter`
- Modify: `packages/products/assurance-product/pyproject.toml`
- Modify: `packages/products/assurance-product/assurance_product/{__init__,models,product,source_catalog,runtime_bindings,cli}.py`
- Modify: `packages/products/assurance-product/assurance_product/resources/declarations/{product.yaml,deployment-plugin.yaml,deployment.schema.json}`
- Modify: `examples/agent-runtime-fixture/{pyproject.toml,agent_runtime_fixture/bindings.py}`
- Modify: `benchmark/agent-runtime-phase3/{README.md,manifest.json,run_item.py}`
- Modify: `benchmark/assurance-product/{eval.py,projection.py,run_item.py}`
- Modify: `benchmark/vue-fastapi-admin/benchmark/{cursor-loop-helpers.sh,run-workflow-loop.sh,run-workflow-loop-opencode-openai.sh,watch-loop.sh,run_with_hard_timeout.py}`
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`
- Modify: `README.md`
- Modify: `packages/products/assurance-product/README.md`
- Test: `tests/product/test_product_providers.py`
- Test: `tests/product/test_product_packaging.py`
- Test: `tests/product/test_binding_builder.py`
- Test: `tests/product/test_wheel_smoke_contract.py`

**Interfaces:**

- Produces: one installed Product entry point, `assurance-opencode`; `product_source_catalog()` and Product declaration/building no longer accept an adapter selector.
- Removes: `AdapterName`, `CursorBindingV1`, `AssuranceCursorProductProvider`, Cursor secret resolution, and Cursor runtime handler identity.

- [ ] **Step 1: Add failing single-provider tests.**

```python
def test_product_exposes_only_opencode_provider() -> None:
    providers = {
        item.name
        for item in importlib.metadata.entry_points(group="graph_engine.products")
        if item.dist and item.dist.name == "assurance-product"
    }
    assert providers == {"assurance-opencode"}


def test_deployment_rejects_cursor_binding() -> None:
    document = valid_deployment_document()
    document["runtime_plugin_id"] = "runtime.cursor"
    document["adapter_binding"] = {"schema_version": "1"}
    with pytest.raises(ValidationError):
        DeploymentBindingsV1.model_validate(document)
```

- [ ] **Step 2: Run the tests and confirm the current dual-provider surface fails them.**

Run: `uv run pytest -q tests/product/test_product_providers.py tests/product/test_product_packaging.py tests/product/test_binding_builder.py`

Expected: failure showing the Cursor entry point/model/declaration still exists.

- [ ] **Step 3: Remove the exact Cursor package and Product branches.**

Delete the paths listed above. Make OpenCode fields concrete instead of retaining one-value adapter unions. Keep the installed entry-point name `assurance-opencode`, but remove adapter arguments from internal Product source/declaration functions.

- [ ] **Step 4: Collapse examples, benchmarks, and smoke tests to OpenCode.**

Rename `cursor-loop-helpers.sh` to `loop-helpers.sh` only for helpers still used by both OpenCode loop scripts. Remove adapter comparison axes from benchmark manifests and projections. Do not rename OpenCode pagination variables named `cursor`.

- [ ] **Step 5: Regenerate the workspace lock and run focused checks.**

```bash
uv lock
uv sync --dev
uv run pytest -q tests/product/test_product_providers.py \
  tests/product/test_product_packaging.py \
  tests/product/test_binding_builder.py \
  tests/product/test_wheel_smoke_contract.py
uv run ruff check pyproject.toml packages/products/assurance-product examples/agent-runtime-fixture
bash scripts/assurance_product_wheel_smoke_test.sh
```

- [ ] **Step 6: Commit.**

```bash
git add -A
git commit -m "refactor: remove cursor product integration"
```

### Task P2: Replace runtime selection with one current Invocation identity

**Files:**

- Create: `packages/products/assurance-product/assurance_product/invocation_identity.py`
- Create: `tests/product/test_invocation_identity.py`
- Modify: `packages/products/assurance-product/assurance_product/application.py`
- Modify: `packages/products/assurance-product/assurance_product/change_workspace.py`
- Modify: `packages/products/assurance-product/assurance_product/models.py`
- Modify: `packages/products/assurance-product/assurance_product/revision_registry.py`
- Modify: `packages/products/assurance-product/assurance_product/status.py`
- Modify: `packages/products/assurance-product/assurance_product/product.py`
- Delete: `tests/product/shadow_harness.py`
- Delete: `tests/product/test_entrypoint_cutover.py`
- Delete: `tests/product/test_langgraph_shadow_parity.py`
- Delete: `tests/product/test_validator_shadow_parity.py`
- Delete: `tests/product/test_historical_v2_export_archive.py`
- Delete: `tests/product/test_legacy_drain_gate.py`
- Modify: `tests/product/test_runtime_selection_security.py`
- Modify: `tests/product/test_revision_retention.py`
- Modify: `tests/product/test_application_export_archive.py`
- Modify: `tests/product/test_cli_status_and_lock.py`
- Modify: `tests/product/test_change_workspace_paths.py`

**Interfaces:**

- Produces: `InvocationIdentityRecord`, `identity_path()`, `load_identity()`, `write_initializing()`, and `complete_initialized()`.
- Produces: `RevisionRegistry.bind(invocation_id, revision_id)` and `revision_for(invocation_id) -> str`.
- Removes: `RuntimeKind`, `ENTRYPOINT_RUNTIME_CUTOVER`, `SelectionRecord`, runtime discriminators, `backfill_legacy`, drain authorization, and legacy lifecycle rendering.

- [ ] **Step 1: Write strict current identity tests.**

```python
def test_identity_has_no_runtime_discriminator() -> None:
    record = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-1",
        entrypoint="intake",
        root_input_digest="a" * 64,
        product_lock_digest="b" * 64,
        revision_id="c" * 64,
    )
    assert "runtime" not in record.model_dump(mode="json")


@pytest.mark.parametrize("runtime", ["legacy-v2", "langgraph-v1"])
def test_identity_rejects_runtime_field(runtime: str) -> None:
    with pytest.raises(ValidationError):
        InvocationIdentityRecord.model_validate({**valid_identity_document(), "runtime": runtime})
```

- [ ] **Step 2: Run the new test and current lifecycle tests to establish the failing baseline.**

Run: `uv run pytest -q tests/product/test_invocation_identity.py tests/product/test_runtime_selection_security.py tests/product/test_revision_retention.py`

Expected: import failure for `invocation_identity` and selection/legacy assertions from existing tests.

- [ ] **Step 3: Move the crash-safe two-phase handshake into `invocation_identity.py`.**

Use `ConfigDict(frozen=True, extra="forbid")`. Serialize exactly the seven fields shown in the test. Store records under `ChangePaths.langgraph_identities`; reject symlinks, unknown fields, missing versions, non-canonical bytes, identity drift, and phase regression. Preserve only the current `.pending` + atomic-replace crash recovery.

- [ ] **Step 4: Delete runtime branching from Product Application and revision retention.**

Replace `load_selection` calls with `load_identity`, remove all legacy projection/read/export/archive paths, and make status/lock/export/archive consume only current LangGraph state. Rename `coexistence_graph_manifest()` to `product_graph_manifest()`. Change revision binding payloads to exactly `invocation_id` and `revision_id`.

- [ ] **Step 5: Tighten the current workspace layout.**

Replace the `selections` directory with `identities`. Remove `invocations` and `ledger` from `_LANGGRAPH_DIRECTORY_NAMES` and `ChangeWorkspace.initialize()` allowlists. Unknown control entries fail with the existing current-layout error before mutation; do not identify or route their former format.

- [ ] **Step 6: Run focused lifecycle tests and commit.**

```bash
uv run pytest -q tests/product/test_invocation_identity.py \
  tests/product/test_runtime_selection_security.py \
  tests/product/test_revision_retention.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_cli_status_and_lock.py \
  tests/product/test_change_workspace_paths.py
uv run pyright packages/products/assurance-product/assurance_product
git add -A
git commit -m "refactor: keep one current invocation identity"
```

### Task P3: Delete framework historical readers and serialization fallbacks

**Files:**

- Delete: `packages/framework/graph-engine/graph_engine/evidence/legacy_v2.py`
- Delete: `packages/framework/graph-engine/tests/evidence/test_legacy_v2_reader.py`
- Delete: `packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json`
- Modify: `packages/framework/graph-engine/graph_engine/evidence/__init__.py`
- Modify: `packages/framework/graph-engine/graph_engine/composition/{__init__,lock,contributions}.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/workspace.py`
- Modify: `packages/framework/graph-engine/graph_engine/effects/{__init__,contracts}.py`
- Modify: `packages/framework/graph-engine/tests/composition/test_lock_model.py`
- Modify: `packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py`
- Modify: `packages/framework/graph-engine/tests/boot/test_graph_revision.py`
- Modify: `tests/architecture/legacy_import_inventory.py`

**Interfaces:**

- Produces: `authenticate_composition_lock(lock: ProductLock, ...)` accepting only the current lock model.
- Removes: `HistoricalProductManifest`, `InvocationLock`, optional/omitted Attempt registry fields, legacy workspace construction names, and legacy settlement refusal types.

- [ ] **Step 1: Add strict ProductLock/descriptor tests.**

```python
def test_product_lock_requires_attempt_registry_fields() -> None:
    document = current_product_lock_document()
    document["registry_digests"].pop("attempt_contracts")
    with pytest.raises(ValidationError):
        ProductLock.model_validate(document)


def test_historical_invocation_lock_is_not_a_composition_lock() -> None:
    with pytest.raises((TypeError, ValidationError, CompositionLockError)):
        authenticate_composition_lock(load_old_invocation_lock_fixture())
```

- [ ] **Step 2: Run strict lock and workspace tests and confirm compatibility paths are exercised.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/composition/test_lock_model.py packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py`

Expected: strict tests fail because missing Attempt fields and old temporary names are accepted.

- [ ] **Step 3: Remove historical lock/evidence models and omit serializers.**

Require `RegistryDigests.attempt_contracts` and `RegistryProjections.attempt_contracts` explicitly. Remove `_parse_locked_manifest` fallback, `HistoricalProductManifest`, `InvocationLock`, `_omit_empty_attempt_contracts`, and matching `PluginDescriptor`/contribution serializers. Regenerate current golden digests instead of accepting both byte shapes.

- [ ] **Step 4: Remove old workspace and Effect compatibility.**

Delete `_legacy_receipt_temporary_name`, `_legacy_pending_construction_prefix`, their recovery candidates, `LEGACY_SETTLEMENT`, `DualSettlementError`, and `refuse_legacy_settlement`. Keep current temporary-file recovery and current in-Attempt settlement.

- [ ] **Step 5: Run framework checks and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/composition/test_lock_model.py \
  packages/framework/graph-engine/tests/runtime/test_task_workspace_faults.py \
  packages/framework/graph-engine/tests/boot/test_graph_revision.py
uv run lint-imports
uv run pyright packages/framework/graph-engine/graph_engine
git add -A
git commit -m "refactor: remove historical framework formats"
```

### Task P4: Make Intake and Generation artifacts current-schema-only

**Files:**

- Modify: `packages/capabilities/assurance-intake/assurance_intake/contracts/{__init__,review,cases,explore}.py`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/{personas/explorer.md,prompts/case-design.md,prompts/explore.md}`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/result-contracts/case-review.v1.schema.json`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/schemas/{case-authoring.v1.schema.json,case-review.v1.schema.json,qa-change.v1.schema.json}`
- Modify: `packages/capabilities/assurance-intake/assurance_intake/resources/skills/{aa-case-design/SKILL.md,aa-case-repair/SKILL.md,aa-case-reviewer/SKILL.md,aa-explore/SKILL.md}`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/{__init__,reviews}.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/personas/reviewer.md`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/{result-contracts/plan-review.v1.schema.json,schemas/plan-review.v1.schema.json}`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/resources/skills/{aa-api-plan-reviewer/SKILL.md,aa-e2e-plan-reviewer/SKILL.md,aa-fuzz-plan-reviewer/SKILL.md,aa-performance-plan-reviewer/SKILL.md}`
- Modify: `packages/capabilities/assurance-intake/tests/{test_contracts,test_agent_skills,test_validators}.py`
- Modify: `packages/capabilities/assurance-generation/tests/{test_contracts,test_plan_review,test_planning,test_agent_skills}.py`

**Interfaces:**

- Produces: one `ReviewDecision = Literal["pass", "needs_fix", "needs_human_review", "reject"]` in both capabilities.
- Produces: one strict Case/Explore read model equal to the current authoring contract.
- Removes: `approved`, `changes_requested`, `action`/`instructions` repair aliases, permissive historical read models, and old prompt vocabulary.

- [ ] **Step 1: Add rejection tests for former review and repair shapes.**

Before changing a reader, add an explicit expected `(schema_id, schema_version, schema_digest)` mapping for every installed Intake and Generation artifact used by the ProductLock test fixture; assert exact mapping equality so a second accepted version cannot be added silently.

```python
@pytest.mark.parametrize("decision", ["approved", "changes_requested"])
def test_case_review_rejects_old_decisions(decision: str) -> None:
    with pytest.raises(ValidationError):
        CaseReviewResultV1.model_validate({**valid_case_review(), "decision": decision})


@pytest.mark.parametrize("old_key", ["action", "instructions"])
def test_auto_fix_requires_edits(old_key: str) -> None:
    with pytest.raises(ValueError, match="edits"):
        normalized_auto_fix_edits({old_key: ["replace value"]})


def test_plan_review_rejects_approved() -> None:
    with pytest.raises(ValidationError):
        PlanReview.model_validate({**valid_plan_review(), "decision": "approved"})
```

- [ ] **Step 2: Run Intake and Generation contract tests and confirm old forms still pass.**

Run: `uv run pytest -q packages/capabilities/assurance-intake/tests/test_contracts.py packages/capabilities/assurance-generation/tests/test_contracts.py packages/capabilities/assurance-generation/tests/test_plan_review.py`

Expected: the new rejection assertions fail.

- [ ] **Step 3: Collapse review decisions and repair fields.**

Make `_PASS_DECISIONS = frozenset({"pass"})` and `_FIX_DECISIONS = frozenset({"needs_fix"})`; remove every conditional accepting the former values. `normalized_auto_fix_edits()` reads only a non-empty `edits: list[str]`. Keep current public outcomes unchanged.

- [ ] **Step 4: Collapse permissive Case and Explore readers into the current strict models.**

Require the current authoring fields and exact schema version on every read. Remove separate base/read variants that exist only to accept older documents. Update installed JSON Schemas, result-contract resources, prompts, personas, and Skills so the model is never instructed to emit a removed field or value.

- [ ] **Step 5: Run both capability suites and commit.**

```bash
uv run pytest -q packages/capabilities/assurance-intake/tests \
  packages/capabilities/assurance-generation/tests
uv run pyright packages/capabilities/assurance-intake packages/capabilities/assurance-generation
git add packages/capabilities/assurance-intake packages/capabilities/assurance-generation
git commit -m "refactor: require current intake and generation artifacts"
```

### Task P5: Make Quality and Healing artifacts current-schema-only

**Files:**

- Modify: `packages/capabilities/assurance-quality/assurance_quality/contracts/{__init__,baseline,coverage,trace,issues,report,pr_metrics,sufficiency}.py`
- Modify: `packages/capabilities/assurance-quality/assurance_quality/resources/schemas/{adversarial-yield.v1.schema.json,fact-baseline.v1.schema.json,trace.v2.schema.json}`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/operations/status.py`
- Modify: `packages/capabilities/assurance-quality/tests/{test_contracts,test_coverage,test_trace,test_issues,test_report,test_metrics,test_quality_characterization}.py`
- Modify: `packages/capabilities/assurance-healing/tests/{test_contracts,test_proposal,test_safety}.py`

**Interfaces:**

- Produces: `TraceProjectionDocument` accepting only explicit schema version `"2"` and `IssueReconcileStatusDocument` accepting only `"2.0"`.
- Produces: report schema `"1.1"`, required fingerprint preimage, current structured coverage/baseline/metrics fields, and current Healing event projections.
- Removes: V1 aliases/unions, missing-version injection, field aliases, historical setup links, old event kinds, and `form="legacy"`.

- [ ] **Step 1: Add strict version and required-field tests.**

First freeze the exact `(schema_id, schema_version, schema_digest)` mapping for every installed Quality and Healing artifact in the ProductLock test fixture, then add the former-shape rejection cases.

```python
@pytest.mark.parametrize("document", [trace_v1_document(), trace_without_version()])
def test_trace_accepts_only_explicit_v2(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TraceProjectionDocument.model_validate(document)


def test_issue_reconcile_accepts_only_v2() -> None:
    with pytest.raises(ValidationError):
        IssueReconcileStatusDocument.model_validate(issue_reconcile_v1_document())


def test_problem_fingerprint_requires_preimage() -> None:
    with pytest.raises(ValidationError):
        ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
```

- [ ] **Step 2: Run focused tests and confirm old models are still accepted.**

Run: `uv run pytest -q packages/capabilities/assurance-quality/tests/test_trace.py packages/capabilities/assurance-quality/tests/test_issues.py packages/capabilities/assurance-quality/tests/test_report.py packages/capabilities/assurance-healing/tests/test_contracts.py`

Expected: new rejection tests fail against V1 unions, defaults, and Healing fallback projections.

- [ ] **Step 3: Delete old Quality variants and aliases.**

Keep `TraceProjectionV2`, `TraceGapV2`, and `IssueReconcileStatusV2` as the unqualified public names. Require `TraceTestRef.test_name`, current integrity values, current timestamp source, report `1.1` issues, structured coverage rows, current baseline authoring fields, current metrics setup reference, and `ProblemFingerprint.preimage`. Remove discriminated unions whose only purpose was reading former versions.

- [ ] **Step 4: Delete Healing event fallback.**

Project only current installed event kinds and exact current keys. Missing `record_key`, old fixer event names, or former fields raise the current schema error; never synthesize a `legacy:` key or `form="legacy"`.

- [ ] **Step 5: Run Quality/Healing suites and commit.**

```bash
uv run pytest -q packages/capabilities/assurance-quality/tests \
  packages/capabilities/assurance-healing/tests
uv run pyright packages/capabilities/assurance-quality packages/capabilities/assurance-healing
git add packages/capabilities/assurance-quality packages/capabilities/assurance-healing
git commit -m "refactor: require current quality and healing artifacts"
```

### Task P6: Add canonical SQLite Attempt and resource-authorization stores

**Files:**

- Create: `packages/products/assurance-product/assurance_product/sqlite_attempt_store.py`
- Create: `packages/products/assurance-product/assurance_product/sqlite_resource_authorization.py`
- Create: `tests/product/test_sqlite_attempt_store.py`
- Create: `tests/product/test_sqlite_resource_authorization.py`
- Modify: `packages/products/assurance-product/assurance_product/sqlite_checkpointer.py`
- Modify: `packages/products/assurance-product/assurance_product/runtime_ports.py`
- Modify: `packages/framework/graph-engine/graph_engine/persistence/{attempt_journal,resource_authorization}.py`
- Modify: `packages/framework/graph-engine/tests/persistence/{test_attempt_journal,test_resource_authorization_store}.py`
- Modify: `tests/product/test_sqlite_checkpointer.py`

**Interfaces:**

- Produces: `SqliteAttemptJournal(backend)` implementing `AttemptJournalPort`.
- Produces: `SqliteResourceAuthorizationStore(backend)` implementing `ResourceAuthorizationStorePort`.
- Produces: `AssuranceSqliteBackend.install_observers(...)`, `seal_observers()`, and a checkpointer factory that rejects use before sealing.
- Removes: Product `DurableAttemptJournal` and its `attempts.pkl` file.

- [ ] **Step 1: Write SQLite round-trip, CAS, fence, and restart tests.**

```python
async def test_attempt_journal_survives_backend_restart(workspace, attempt_key) -> None:
    async with open_sqlite_checkpointer(workspace) as backend:
        journal = SqliteAttemptJournal(backend)
        await journal.append(
            attempt_key,
            (
                AttemptOpened(
                    contract_digest="b" * 64,
                    input_digest="c" * 64,
                    graph_revision="d" * 64,
                    invocation_id="inv-1",
                    public_entrypoint="intake",
                    semantic_node_id="intake.explore",
                ),
            ),
            expected_revision=0,
            fencing_token=3,
        )
        await journal.ensure_durable(attempt_key)
    async with open_sqlite_checkpointer(workspace) as reopened:
        snapshot = await SqliteAttemptJournal(reopened).load(attempt_key)
        assert snapshot is not None
        assert snapshot.revision == 1
        assert snapshot.fencing_token == 3


async def test_resource_store_rejects_stale_release(sqlite_resource_store) -> None:
    await append_acquire(sqlite_resource_store, fence=4)
    with pytest.raises(StaleFencingToken):
        await append_release(sqlite_resource_store, fence=3)
```

- [ ] **Step 2: Run the new tests and confirm the Product adapters do not exist.**

Run: `uv run pytest -q tests/product/test_sqlite_attempt_store.py tests/product/test_sqlite_resource_authorization.py tests/product/test_sqlite_checkpointer.py`

Expected: import failures for both SQLite adapters.

- [ ] **Step 3: Add current canonical tables and adapters.**

Add `assurance_attempt_batches` keyed by `(attempt_key_digest, revision)` and `assurance_resource_authorizations` keyed by monotonic revision. Store canonical JSON bytes, explicit schema version, fence, and record digest. Use the backend's shared `synchronous=FULL` connection and async transaction lock; every append uses `BEGIN IMMEDIATE`, exact replay is idempotent, and divergent replay/gaps/stale fences raise existing typed integrity errors.

- [ ] **Step 4: Break the observer construction cycle without a second backend.**

Open/configure/setup SQLite first, construct `SqliteAttemptJournal`, install `(AttemptCheckpointObserver(journal),)`, then seal observers. Reject observer mutation after sealing and reject `checkpointer(identity)` before sealing. Remove `open_sqlite_checkpointer(observers=...)` once all callers use the ordered API.

- [ ] **Step 5: Verify durability and commit.**

```bash
uv run pytest -q tests/product/test_sqlite_attempt_store.py \
  tests/product/test_sqlite_resource_authorization.py \
  tests/product/test_sqlite_checkpointer.py \
  packages/framework/graph-engine/tests/persistence/test_attempt_journal.py \
  packages/framework/graph-engine/tests/persistence/test_resource_authorization_store.py
uv run pyright packages/products/assurance-product/assurance_product \
  packages/framework/graph-engine/graph_engine/persistence
git add packages/products/assurance-product/assurance_product \
  packages/framework/graph-engine/graph_engine/persistence \
  packages/framework/graph-engine/tests/persistence tests/product
git commit -m "feat: persist attempts and authorizations in sqlite"
```

### Task P7: Replace captured Effect stores with one call-context seam

**Files:**

- Create: `packages/framework/graph-engine/graph_engine/effects/state.py`
- Create: `packages/framework/graph-engine/tests/effects/test_effect_state.py`
- Create: `packages/products/assurance-product/assurance_product/sqlite_effect_state.py`
- Create: `tests/product/test_sqlite_effect_state.py`
- Modify: `packages/framework/graph-engine/graph_engine/{plugin_api.py,effects/__init__.py,effects/apply.py}`
- Modify: `packages/capabilities/assurance-healing/assurance_healing/{plugin.py,contracts/effects.py,effects/__init__.py,effects/common.py,effects/allocation.py,effects/apply.py,effects/approval.py}`
- Delete: `packages/capabilities/assurance-healing/assurance_healing/effects/store.py`
- Modify: `packages/capabilities/assurance-improvement/assurance_improvement/{plugin.py,contracts/effects.py,effects/__init__.py,effects/common.py,effects/archive.py,effects/delivery.py,effects/promotion.py}`
- Delete: `packages/capabilities/assurance-improvement/assurance_improvement/effects/store.py`
- Modify: the four installed receipt Schema files covering all six Effect kinds: the three `assurance_healing/resources/schemas/*-receipt*.schema.json` files and `assurance_improvement/resources/schemas/improvement-effect-receipt.v1.schema.json`
- Modify: `packages/framework/graph-engine/tests/attempts/{test_kernel_effects,test_kernel_effect_recovery}.py`
- Modify: `packages/capabilities/assurance-healing/tests/{test_effects,test_recovery_faults}.py`
- Modify: `packages/capabilities/assurance-improvement/tests/test_effects.py`

**Interfaces:**

- Produces: `EffectStatePort`, `EffectStateObservation`, and `EffectCallContext` bound to Effect kind, settlement key, and live fence.
- Changes: `DurableEffectHandler.apply(intent, context)` and `reconcile(intent, context)`.
- Produces: `SQLiteEffectState(backend)` plus a test-only `MemoryEffectState`.
- Preserves: the current `EffectRegistration(handler)`, `EffectEntry`, contribution authentication, and single `EffectRegistry`.
- Covers exactly: `assurance.healing.effect.allocation.v2`, `assurance.healing.effect.heal-apply.v2`, `assurance.healing.effect.proposal-approved.v1`, `assurance.improvement.effect.archive.v1`, `assurance.improvement.effect.delivery.v1`, and `assurance.improvement.effect.promotion.v1`.

- [ ] **Step 1: Write one-registry and two-key state tests.**

```python
async def test_context_prevents_settlement_key_substitution(memory_effect_state) -> None:
    context = bind_effect_call(
        state=memory_effect_state,
        effect_kind="assurance.improvement.effect.delivery.v1",
        settlement_key="a" * 64,
        fencing_token=7,
    )
    await context.commit(
        business_key="delivery:IMP-1",
        intent_digest="b" * 64,
        payload={"kind": "memory_apply"},
        receipt={"idempotency_key": "delivery:IMP-1", "settlement_key": "a" * 64},
    )
    record = await memory_effect_state.observe(
        effect_kind="assurance.improvement.effect.delivery.v1",
        settlement_key="a" * 64,
        business_key="delivery:IMP-1",
        intent_digest="b" * 64,
        fencing_token=7,
    )
    assert record.status == "committed"


def test_frozen_composition_exposes_one_effect_registry(composition) -> None:
    assert isinstance(composition.registries.effects, EffectRegistry)
    assert not hasattr(composition, "effect_factory_registry")
    assert not hasattr(composition, "runtime_effect_registry")
```

- [ ] **Step 2: Run Effect tests and confirm handlers still capture Capability stores.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/effects/test_effect_state.py packages/capabilities/assurance-healing/tests/test_effects.py packages/capabilities/assurance-improvement/tests/test_effects.py`

Expected: missing state/context API and constructor failures after tests instantiate stateless handlers.

- [ ] **Step 3: Implement the bound context and SQLite state adapter.**

The full Product-private port receives `(effect_kind, settlement_key, business_key, intent_digest, payload, receipt, fencing_token)`. Capability-visible context exposes `settlement_key` read-only and accepts only business key, digest, payload, and receipt. Enforce unique `(effect_kind, settlement_key)` and `(effect_kind, business_key)`, exact receipt reuse, intent drift conflict, and stale-fence rejection under `BEGIN IMMEDIATE`.

- [ ] **Step 4: Make all six handlers static and context-driven.**

Instantiate handlers without stores in each plugin. Keep payload validation, business-key formula, external action, and receipt building in the Capability. Add `settlement_key` to current receipt models/Schemas and delete delivery's `_kernel_settlement_key` alignment branch. Modify `AttemptEffectSettler` to create one bound context per ordinal from its injected `EffectStatePort`.

- [ ] **Step 5: Prove apply/reconcile and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/effects \
  packages/framework/graph-engine/tests/attempts/test_kernel_effects.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py \
  packages/capabilities/assurance-healing/tests/test_effects.py \
  packages/capabilities/assurance-healing/tests/test_recovery_faults.py \
  packages/capabilities/assurance-improvement/tests/test_effects.py \
  tests/product/test_sqlite_effect_state.py
uv run pyright packages/framework/graph-engine/graph_engine/effects \
  packages/capabilities/assurance-healing \
  packages/capabilities/assurance-improvement \
  packages/products/assurance-product/assurance_product/sqlite_effect_state.py
git add packages/framework/graph-engine packages/capabilities/assurance-healing \
  packages/capabilities/assurance-improvement packages/products/assurance-product tests/product
git commit -m "feat: inject durable effect call context"
```

### Task P8: Make executor results durable before commit processing

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/{__init__,contracts,context,events,kernel}.py`
- Modify: `packages/framework/graph-engine/graph_engine/effects/apply.py`
- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/attempt_executor.py`
- Modify: `packages/framework/graph-engine/tests/attempts/{test_contracts,test_kernel,test_kernel_recovery,test_kernel_raw_agent_recovery}.py`
- Modify: `packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`

**Interfaces:**

- Produces: `AuthorizedAttemptScope(execution: AttemptExecutionContext, workspace: TaskWorkspaceBinding)`.
- Produces: `TerminalReceiptRef`, `ExecutedAttemptResult[OutputT]`, `ExecutorResolution`, and `ExecutorStepResult[OutputT]`.
- Changes: `AttemptExecutor.execute(validated_input, scope)` and `reconcile(validated_input, scope, snapshot)` return `ExecutorStepResult`.
- Changes: current `ActivityTerminalObserved` carries validated output plus optional source host-receipt identity/digest; output and `EffectIntentRecorded` events append in one CAS batch.

- [ ] **Step 1: Write the atomic observed-result test.**

```python
async def test_output_and_effect_intents_share_one_journal_revision(kernel_fixture) -> None:
    intent = EffectIntent(kind=DELIVERY_KIND, payload=valid_delivery_payload())
    kernel_fixture.executor.result = ExecutedAttemptResult(
        output=ValidOutput(value="done"),
        effects=(intent,),
        source_terminal_receipt=TerminalReceiptRef(
            identity_digest="a" * 64,
            receipt_digest="b" * 64,
        ),
    )
    await kernel_fixture.run_until_cut("after_observed_result")
    records = kernel_fixture.journal.records(kernel_fixture.attempt_key)
    assert [event.kind for event in records[-1].events] == [
        "activity_terminal_observed",
        "effect_intent_recorded",
    ]
```

- [ ] **Step 2: Run the focused tests and confirm the old executor returns only `OutputT`.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/attempts/test_contracts.py packages/framework/graph-engine/tests/attempts/test_kernel.py packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py`

Expected: imports for `AuthorizedAttemptScope`/`ExecutedAttemptResult` fail and Effect intents are not in the observed batch.

- [ ] **Step 3: Introduce the closed result contract and migrate test executors.**

Committed resolutions are not part of `ExecutorStepResult`. Agent and Task fakes must return `ExecutedAttemptResult` on success and one of `RejectedTaskResult`, `PermanentTaskFailure`, `PendingTaskResult`, or `IndeterminateTaskResult` otherwise. The Kernel constructs the authorized scope only after resource grant and workspace authentication.

- [ ] **Step 4: Persist the observed batch before seal.**

Validate `OutputT`, every Effect kind/payload Schema, and source receipt identity before one append containing `ActivityTerminalObserved` followed by ordinal `EffectIntentRecorded` events. On replay, rebuild the same typed output/intents from the journal. Remove Effect intent discovery/recording from `AttemptEffectSettler`; it consumes only already persisted intents.

- [ ] **Step 5: Run framework/adapter tests and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts \
  packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py
uv run pyright packages/framework/graph-engine/graph_engine/attempts \
  packages/adapters/agent-runtime-contracts/agent_runtime_contracts
git add packages/framework/graph-engine packages/adapters/agent-runtime-contracts
git commit -m "feat: persist executed attempt results before commit"
```

### Task P9: Make the Attempt journal the only activity authority

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/{activity,host_protocol,host_receipts,production_host,production_worker}.py`
- Modify: `packages/framework/graph-engine/graph_engine/plugin_api.py`
- Modify: `packages/framework/graph-engine/tests/runtime/{activity-events-v2.golden.json,test_activity_models,test_activity_port,test_activity_recovery,test_host_protocol,test_host_receipts,test_production_host,test_production_host_faults,test_production_host_security}.py`
- Modify: `packages/framework/graph-engine/tests/attempts/test_kernel_raw_agent_recovery.py`
- Modify: `packages/adapters/agent-runtime-opencode/tests/{test_create_recovery,test_fault_matrix,test_prompt_admission,test_observation}.py`

**Interfaces:**

- Produces: `JournalBackedTaskActivityPort` implementing the existing synchronous `TaskActivityPort` against `AttemptJournalPort`.
- Produces: `create_production_task_execution_host(...) -> TaskExecutionHost`; Product never imports `_ProductionTaskExecutionHost` or mutates it after creation.
- Changes: current `TaskActivityRpcIdentity`, `TaskHostCallIdentity`, and terminal receipt bind Attempt key digest, authorization ID, fence, phase, workspace, request, GraphRevision, ProductLock, and host implementation.
- Removes: production use/export of `LedgerTaskActivityPort` and `bind_invocation_runtime()`.

- [ ] **Step 1: Add a host test proving activity RPC commits to the Attempt journal.**

```python
async def test_host_activity_rpc_uses_attempt_journal(production_host_fixture) -> None:
    result = await production_host_fixture.execute_one_opencode_call()
    snapshot = await production_host_fixture.attempt_journal.load(
        production_host_fixture.attempt_key
    )
    assert result.outcome is not None
    assert snapshot is not None
    assert snapshot.activity_state == "bound"
    assert not production_host_fixture.legacy_ledger_path.exists()
```

- [ ] **Step 2: Run activity/host tests and confirm the host opens `LedgerTaskActivityPort`.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/runtime/test_activity_port.py packages/framework/graph-engine/tests/runtime/test_production_host.py packages/framework/graph-engine/tests/runtime/test_host_receipts.py`

Expected: the new fixture observes no journal activity and current host construction requires post-construction binding.

- [ ] **Step 3: Implement the synchronous journal-backed bridge.**

Construct the port with exact Attempt/RPC identity, `AttemptJournalPort`, live-fence assertion, owner event loop, and remaining deadline. `snapshot`, `mark_dispatch_started`, and `bind` submit async journal work through `asyncio.run_coroutine_threadsafe` and return only after commit. The OpenCode handler is the only writer of the provider admission fingerprint; remove the Kernel's generic fingerprint append.

- [ ] **Step 4: Seal production host dependencies at construction.**

Add `create_production_task_execution_host()` taking runtime authorization, authenticated handlers/import roots, `TaskWorkspaceStore`, terminal receipt store, journal-backed activity factory, and invocation root. Delete mutable `bind_invocation_runtime`. Put host receipts under `.runtime/activities/<invocation>/receipts/`, separate from workspace promotion receipts.

- [ ] **Step 5: Bump and strictly validate current host/RPC identity versions.**

Do not parse prior wire records. Before each activity mutation or terminal receipt acceptance, compare Attempt, authorization, fence, request, workspace, GraphRevision, ProductLock, handler, and host identities. Add negative cases for every mismatched field and for stale fences.

- [ ] **Step 6: Run recovery/security tests and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/runtime \
  packages/framework/graph-engine/tests/attempts/test_kernel_raw_agent_recovery.py \
  packages/adapters/agent-runtime-opencode/tests/test_create_recovery.py \
  packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py \
  packages/adapters/agent-runtime-opencode/tests/test_prompt_admission.py \
  packages/adapters/agent-runtime-opencode/tests/test_observation.py
uv run pyright packages/framework/graph-engine/graph_engine/attempts \
  packages/adapters/agent-runtime-opencode/agent_runtime_opencode
git add packages/framework/graph-engine packages/adapters/agent-runtime-opencode
git commit -m "feat: journal external activity through attempt state"
```

### Task P10: Resolve all Agent and Task contracts to real staged executors

**Files:**

- Modify: `packages/adapters/agent-runtime-contracts/agent_runtime_contracts/{__init__,execution_contract,attempt_executor,models,plugin_kit,workspace}.py`
- Modify: `packages/products/assurance-product/assurance_product/{agent_contracts,opencode_agents,runtime_bindings}.py`
- Modify: Capability `contracts/attempts.py` and prepare/finalize handler modules under all six `packages/capabilities/assurance-*` wheels.
- Modify: `packages/adapters/agent-runtime-contracts/tests/{test_attempt_executor,test_models,test_runtime_binding}.py`
- Modify: all six Capability `tests/test_attempt_contracts.py` files.
- Modify: `tests/product/{test_agent_execution_contracts,test_semantic_attempt_bindings,test_binding_coverage,test_opencode_staging_boundary,test_candidate_execution_isolation}.py`

**Interfaces:**

- Produces: `AgentPhaseWriteClaims(prepare, runtime, finalize)` as three sorted, disjoint installed path-claim tuples included in `AgentExecutionContract.canonical_projection()`.
- Produces: 33 `ResolvedRawAgentExecutor` values and eight deterministic `ResolvedAttemptContract` values, all implementing the P8 executor protocol.
- Removes: `_DeferredPhase`, `_DeferredTaskExecutor`, `_DEFAULT_MODEL`, `_catalog_binding()`, process-local `declared_effects`, `StructuredOutputCapabilityError`, and `negotiate_provider_schema`.

- [ ] **Step 1: Add phase ordering, binding, and write-claim tests.**

```python
async def test_raw_executor_uses_three_disjoint_staging_phases(raw_executor_fixture) -> None:
    result = await raw_executor_fixture.execute()
    assert raw_executor_fixture.phase_log == ["prepare", "runtime", "finalize"]
    assert isinstance(result, ExecutedAttemptResult)
    assert raw_executor_fixture.prepare_delta <= set(raw_executor_fixture.claims.prepare)
    assert raw_executor_fixture.runtime_delta <= set(raw_executor_fixture.claims.runtime)
    assert raw_executor_fixture.finalize_delta <= set(raw_executor_fixture.claims.finalize)
    assert not (
        raw_executor_fixture.prepare_delta
        & raw_executor_fixture.runtime_delta
        | raw_executor_fixture.prepare_delta
        & raw_executor_fixture.finalize_delta
        | raw_executor_fixture.runtime_delta
        & raw_executor_fixture.finalize_delta
    )


def test_runtime_registry_contains_exact_semantic_contracts(runtime_registry) -> None:
    assert len(runtime_registry) == 41
    assert sum(is_agent_contract(item.contract) for item in runtime_registry.values()) == 33
    assert not any(type(item.executor).__name__.startswith("_Deferred") for item in runtime_registry.values())
```

- [ ] **Step 2: Run adapter/Product contract tests and confirm deferred executors remain.**

Run: `uv run pytest -q packages/adapters/agent-runtime-contracts/tests/test_attempt_executor.py tests/product/test_agent_execution_contracts.py tests/product/test_semantic_attempt_bindings.py tests/product/test_opencode_staging_boundary.py`

Expected: phase claims are absent and Product resolution yields deferred executors.

- [ ] **Step 3: Add phase claims to all 33 installed contracts.**

Require three sorted/disjoint claim tuples whose union is a subset of the resolved Attempt write claims. Include them in the complete Agent contract digest. Migrate prepare/finalize handlers that write through `TaskContext.project_root` to the authenticated staging writer/root. `ReadOnlyRawWorkspace` must expose descriptor-pinned reads without a public raw `Path`.

Delete the unused provider-Schema negotiation types and exports from `plugin_kit.py`; Raw Agent contracts always carry prompt instructions plus an installed local result Schema and never branch on provider capabilities.

- [ ] **Step 4: Implement host-backed prepare/runtime/finalize and deterministic Task adapters.**

Each phase gets a distinct `task_id = digest(AttemptKey, phase, handler_id)` and exact host identity. Runtime alone receives OpenCode activity and secrets. Each phase measures before/after staging state, validates its delta, and persists it in the source host receipt. On success, return P8 `ExecutedAttemptResult`; on non-success, return the declared typed failure.

- [ ] **Step 5: Resolve bindings only from authenticated Product data.**

Use the current `DeploymentBindingsV1`, Capability binding entries, ProductLock projections, and installed handler registry. Require exact provider/model/policy/resource/secret/handler/contract digests. Remove environment/default model rebuilding and reject missing, duplicate, extra, wrong-owner, or drifted rows before graph execution.

- [ ] **Step 6: Run the six contract suites and Product integration tests.**

```bash
uv run pytest -q packages/adapters/agent-runtime-contracts/tests \
  packages/capabilities/assurance-execution/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-intake/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-generation/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-quality/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-healing/tests/test_attempt_contracts.py \
  packages/capabilities/assurance-improvement/tests/test_attempt_contracts.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_semantic_attempt_bindings.py \
  tests/product/test_binding_coverage.py \
  tests/product/test_opencode_staging_boundary.py \
  tests/product/test_candidate_execution_isolation.py
uv run pyright packages/adapters/agent-runtime-contracts packages/products/assurance-product
```

- [ ] **Step 7: Commit.**

```bash
git add packages/adapters/agent-runtime-contracts packages/capabilities \
  packages/products/assurance-product tests/product
git commit -m "feat: drive all semantic attempt executors"
```

### Task P11: Construct invocation runtime only under the live runner lease

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/application/{application,runtime_context}.py`
- Modify: `packages/framework/graph-engine/graph_engine/boot/boot.py`
- Modify: `packages/products/assurance-product/assurance_product/{application,runtime_ports,sqlite_checkpointer}.py`
- Modify: `packages/framework/graph-engine/tests/application/test_application.py`
- Modify: `packages/framework/graph-engine/tests/persistence/{test_anchored_checkpointer,test_checkpoint_recovery}.py`
- Modify: `tests/product/{runtime_composition,test_product_runtime_ports,test_langgraph_initial_handshake,test_langgraph_sqlite_restart,test_cli_fail_closed}.py`

**Interfaces:**

- Produces: `InvocationBoundExecution(artifact, runtime_context)` and `InvocationBoundExecutionFactory.bind(runner_lease) -> InvocationBoundExecution`.
- Changes: mutating `AssuranceApplication.start`, `start_and_run`, `run`, and `resume` accept the execution factory instead of prebuilt artifact/context.
- Produces: `ProductRuntimePorts.open(workspace, composition, invocation, authorization, reachable_contract_ids)` and its `execution_factory(...)`.
- Removes: default fence `1`, `_TolerantAttemptFactory`, `_scripted_input`, `test_kernel_resolutions`, `_UnusedWorkspace`, and untyped secret/workspace placeholders.

- [ ] **Step 1: Write a lease-order test.**

```python
async def test_execution_binding_observes_the_acquired_fence(application_fixture) -> None:
    factory = RecordingExecutionFactory()
    await application_fixture.application.run(
        invocation_id="inv-1",
        execution_factory=factory,
    )
    assert factory.bound_fences == [application_fixture.lease.current("inv-1").fencing_token]
    assert factory.bind_count == 1


async def test_missing_fence_fails_before_kernel(product_ports_fixture) -> None:
    with pytest.raises(ValueError, match="fencing token"):
        await product_ports_fixture.bind_without_runner_lease()
    assert product_ports_fixture.kernel_call_count == 0
```

- [ ] **Step 2: Run application/Product runtime tests and confirm artifact/context are currently prebuilt.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/application tests/product/test_product_runtime_ports.py tests/product/test_langgraph_initial_handshake.py tests/product/test_cli_fail_closed.py`

Expected: factory API is absent and the Product path can create fence-1 placeholders.

- [ ] **Step 3: Add the lease-bound framework protocol.**

Inside `_hold_lease`, call `bind()` exactly once, verify equality among lease/checkpointer/context/config/stored tokens, and close/recover the bound checkpointer outbox before lease release. Status uses a strictly read-only view whose mutation methods fail; it does not acquire a fence to repair state.

- [ ] **Step 4: Rebuild ProductRuntimePorts in the normative order.**

Authenticate current ProductLock/GraphRevision/identity/authorization; open ChangeWorkspace and shared SQLite; construct P6 journals and sealed observer set; construct workspace, terminal receipts, secrets/network, P7 Effect state, P9 host, and P10 executors; preflight the selected root; then construct the Kernel. Only after runner lease acquisition create the exact-fence checkpointer, AttemptNodeFactory, 14-root BootArtifact, and runtime context.

- [ ] **Step 5: Add selected-root preflight.**

Derive reachable contracts from the authenticated Python graph inventory. Resolve all 41 contract specifications for compilation, but require OpenCode endpoint/credentials only when the selected root reaches an Agent contract. Missing required ports fail before invocation graph mutation. Add spies proving `aa compile` does not open invocation SQLite, resolve a secret, construct a worker/host, acquire a runner lease, or contact OpenCode.

- [ ] **Step 6: Run application, checkpoint, and Product composition tests.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/application \
  packages/framework/graph-engine/tests/persistence/test_anchored_checkpointer.py \
  packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py \
  tests/product/test_product_runtime_ports.py \
  tests/product/test_langgraph_initial_handshake.py \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_cli_fail_closed.py
uv run pyright packages/framework/graph-engine/graph_engine/application \
  packages/products/assurance-product/assurance_product
```

- [ ] **Step 7: Commit.**

```bash
git add packages/framework/graph-engine packages/products/assurance-product tests/product
git commit -m "feat: bind product execution under runner lease"
```

### Task P12: Close Kernel recovery and terminal resource release

**Files:**

- Modify: `packages/framework/graph-engine/graph_engine/attempts/{events,kernel,resolutions,resource_arbiter}.py`
- Modify: `packages/framework/graph-engine/graph_engine/attempts/{node_factory,checkpoint_bridge}.py`
- Modify: `packages/framework/graph-engine/tests/attempts/{test_kernel,test_kernel_recovery,test_fault_matrix,test_resource_arbiter}.py`
- Modify: `packages/framework/graph-engine/tests/attempts/{test_kernel_raw_agent_recovery,test_kernel_effect_recovery,test_system_interrupt_checkpoint_bridge,test_system_interrupt_replay}.py`
- Modify: `tests/product/{test_langgraph_sqlite_restart,test_replay_properties,test_cli_sqlite_system_interrupt}.py`

**Interfaces:**

- Preserves: the single public `AssuranceAttemptKernel.execute_or_recover(...) -> AttemptResolution` boundary and existing LangGraph interrupt mapping.
- Changes: a terminal replay is returnable only when the journal contains terminal state, the authorization store proves the grant absent, and a matching `ResourcesReleased` event is durable.
- Preserves: the transaction trace through observed output/intent durability, seal, ordered Validators, durable prepare, promote/recover, six-Effect settlement, and terminal receipt.

- [ ] **Step 1: Add the three terminal-release recovery tests.**

```python
async def test_terminal_state_with_active_grant_is_cleaned_before_replay(kernel_fixture) -> None:
    await kernel_fixture.crash_after("terminal_durable")
    assert await kernel_fixture.authorization_store.is_active(kernel_fixture.attempt_key)
    result = await kernel_fixture.restart()
    assert isinstance(result, CommittedTaskResult)
    assert not await kernel_fixture.authorization_store.is_active(kernel_fixture.attempt_key)
    assert kernel_fixture.snapshot().released is True


async def test_released_grant_without_proof_appends_proof_on_restart(kernel_fixture) -> None:
    await kernel_fixture.crash_after("authorization_released")
    result = await kernel_fixture.restart()
    assert isinstance(result, CommittedTaskResult)
    assert kernel_fixture.snapshot().released is True


async def test_release_proof_with_active_grant_fails_integrity(kernel_fixture) -> None:
    await kernel_fixture.install_impossible_release_state()
    with pytest.raises(AttemptIntegrityError, match="release proof"):
        await kernel_fixture.restart()
```

- [ ] **Step 2: Run the Kernel recovery tests and confirm terminal/event publication currently precedes the authorization-store release.**

Run: `uv run pytest -q packages/framework/graph-engine/tests/attempts/test_kernel.py packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py packages/framework/graph-engine/tests/attempts/test_fault_matrix.py`

Expected: the new cuts cannot distinguish terminal durability, grant release, and release-proof durability.

- [ ] **Step 3: Split terminal completion into the three durable cuts.**

Append and durably flush the terminal event first. Then assert the live fence and release the exact authorization ID. Finally prove the grant is absent, append `ResourcesReleased`, durably flush it, and only then construct/return the terminal resolution. Exact replay of every completed cut is idempotent. A stale runner cannot release or prove a newer grant.

- [ ] **Step 4: Complete the section 8 crash matrix.**

Cover resource acquire/adopt, workspace open, deterministic prepare, OpenCode create/bind/admit/observe, host receipt, finalize, observed output/Effect batch, seal, Validator reject, durable prepare, promotion uncertainty, every Effect apply/reconcile state, all three terminal-release cuts, and graph-checkpoint anchoring. At each restart assert stable Attempt key, no duplicate prompt/promotion/Effect/terminal/release, and the correct pending, indeterminate, rejected, permanent, committed, or committed-Effect-failure resolution.

- [ ] **Step 5: Verify interrupt anchoring and the normative happy-path order.**

Update the happy-path trace assertion to include `record_terminal`, `release_resources`, and `record_release_proof` after `settle_effects`. Prove human interrupts remain pure nodes, system pending/indeterminate interrupts restart the same Attempt node, and a terminal result cannot reach the LangGraph checkpoint before release proof.

- [ ] **Step 6: Run focused recovery checks and commit.**

```bash
uv run pytest -q packages/framework/graph-engine/tests/attempts \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_replay_properties.py \
  tests/product/test_cli_sqlite_system_interrupt.py
uv run pyright packages/framework/graph-engine/graph_engine/attempts
git add packages/framework/graph-engine tests/product
git commit -m "feat: prove resource release before terminal replay"
```

### Task P13: Close current lifecycle, security, and packaging surfaces

**Files:**

- Create: `tests/architecture/test_attempt_runtime_production_closure.py`
- Modify: `tests/architecture/legacy_import_inventory.py`
- Modify: `tests/product/{test_cli_langgraph_lifecycle,test_application_export_archive,test_archive_after_publish,test_publish_recovery}.py`
- Modify: `tests/product/{test_export_security,test_candidate_execution_isolation,test_opencode_staging_boundary,test_change_runtime_layout}.py`
- Modify: `tests/product/{test_product_packaging,test_product_providers,test_wheel_smoke_contract,test_phase5_final_repository_gate,test_phase5_final_security_gate}.py`
- Modify: `packages/products/assurance-product/assurance_product/{application,cli,status,export,archive,revision_registry}.py`
- Modify: `README.md`
- Modify: `packages/products/assurance-product/README.md`
- Modify: `scripts/{graph_engine_smoke_test,assurance_capability_wheel_smoke_test,assurance_product_wheel_smoke_test}.sh`

**Interfaces:**

- Produces: one restartable lifecycle for `start`, `run`, `resume`, `status`, `lock show`, `export`, and `archive`, using only the current identity, ProductLock, GraphRevision, SQLite stores, and receipt chain.
- Removes: remaining runtime selectors, drain/backfill/shadow vocabulary, scripted production execution, pickle, memory-only production Effect stores, provider factories, and compatibility readers.
- Preserves: same-version archive recovery, binding aliases, current GraphRevision reopening, and OpenCode poll/list fallback.

- [ ] **Step 1: Add the production-closure architecture test.**

```python
def test_production_tree_contains_no_deleted_runtime_surface(repository) -> None:
    forbidden = {
        "CursorBindingV1",
        "LegacyRuntimeRecord",
        "ENTRYPOINT_RUNTIME_CUTOVER",
        "LedgerTaskActivityPort",
        "_DeferredPhase",
        "_DeferredTaskExecutor",
        "_TolerantAttemptFactory",
        "_UnusedWorkspace",
        "fixture-model",
        "negotiate_provider_schema",
    }
    assert repository.production_symbol_hits(forbidden) == {}
    assert repository.production_pickle_imports() == ()
    assert repository.effect_factory_registry_definitions() == ()
```

Keep the scanner rooted in installed production packages and declarations; fixtures and explicit old-shape rejection payloads are checked by their own tests rather than hidden with substring exceptions.

- [ ] **Step 2: Run lifecycle/architecture tests and record every remaining obsolete hit.**

```bash
uv run pytest -q tests/architecture/test_attempt_runtime_production_closure.py \
  tests/product/test_cli_langgraph_lifecycle.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_product_packaging.py
```

Expected: failure lists the remaining placeholder, compatibility, and packaging surfaces by file and symbol.

- [ ] **Step 3: Remove the listed dead surfaces without replacement layers.**

Delete unused branches, models, constants, exports, entry points, dependencies, fixture distributions, and source-manufactured release/checkpoint evidence. Rename tests and user-facing text that still imply runtime coexistence. Do not add a generic provider layer or a compatibility facade.

- [ ] **Step 4: Prove the complete lifecycle across process reopen.**

Drive a deterministic non-Agent root through `start` and `run`, reopen for `status` and `lock show`, exercise a system interrupt through `resume`, reach achieved, export, interrupt archive publication, and recover archive in a fresh Product process. At each command authenticate the same identity, ProductLock, GraphRevision, checkpoint anchor, Attempt receipt, and fence; reject tampered paths, links, modes, digests, secret handles, and network policy before mutation.

- [ ] **Step 5: Rebuild and inspect all distributions.**

```bash
uv lock
uv sync --dev
bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh
```

Assert the built metadata contains the OpenCode adapter and current schemas, and contains no Cursor distribution/extra/declaration, old reader, placeholder executor, or Effect factory registry.

- [ ] **Step 6: Run focused security/lifecycle tests and commit.**

```bash
uv run pytest -q tests/architecture \
  tests/product/test_cli_langgraph_lifecycle.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_archive_after_publish.py \
  tests/product/test_publish_recovery.py \
  tests/product/test_export_security.py \
  tests/product/test_candidate_execution_isolation.py \
  tests/product/test_opencode_staging_boundary.py \
  tests/product/test_change_runtime_layout.py \
  tests/product/test_product_packaging.py \
  tests/product/test_product_providers.py \
  tests/product/test_wheel_smoke_contract.py
git add -A
git commit -m "refactor: close current product runtime surfaces"
```

### Task P14: Historical protected Checkpoint R implementation — removed 2026-09-04

> **Archival snapshot:** P14 was implemented before the project cancelled Checkpoint R. The design
> and assets summarized below record what existed; they are not executable instructions and must not
> be recreated.

P14 created `.github/workflows/checkpoint-r.yml`, `scripts/checkpoint_r.sh`, the Product checkpoint
support/live suites, the deployment fixture, the `checkpoint_r_live` marker, and a candidate-bound CI
evidence manifest. Its implemented contract was:

1. freeze exact inventories of 33 Agent contracts, 33 bindings, 35 Agent occurrences, 41 semantic
   contracts, and 44 Attempt occurrences;
2. preflight a clean `GITHUB_SHA`, official OpenCode binary/version/digest, protected provider auth,
   locked provider/model, Product secret handles, and all 33 matrix rows;
3. execute every Agent row through `ProductRuntimePorts`, a real runner lease, installed contracts,
   the raw executor, and the real Attempt Kernel—never through a direct adapter shortcut;
4. record source/wheel and binding digests, OpenCode/provider/model identity, ProductLock,
   GraphRevision, policy/resources/secrets, Attempt/source-host/promotion/Effect receipts, release
   proof, and CI identity;
5. reject missing, duplicate, skipped, xfailed, cancelled, waived, timed-out, cross-candidate, or
   stale evidence;
6. add deterministic Task, recovery, security, lifecycle, packaging, and all three wheel-smoke proofs
   to the same immutable candidate; and
7. keep credential-free CI separate from the protected self-hosted live-provider job.

The former release rule required both the ordinary repository gate and `Checkpoint R / exact
candidate` to pass on one unchanged commit. On 2026-09-04 the protected workflow, script, support
harness, marker, fixture, and candidate manifest were removed. Phase P source acceptance now uses
Ruff, format, Pyright, import-lint, full pytest, the three wheel smoke tests, and focused deterministic
production-port/recovery suites. These checks intentionally do not certify an external
OpenCode/provider/model combination.

## Completion Criteria

- [ ] Cursor and all runtime/data compatibility surfaces named in the spec are deleted, not hidden behind switches.
- [ ] One current Invocation identity pins one ProductLock and GraphRevision for all lifecycle commands.
- [ ] All 41 semantic contracts resolve to real executors; the 35 Agent and 44 total occurrences keep stable Attempt keys.
- [ ] Every Agent uses prepare → one OpenCode root session → strict assistant JSON/local Schema validation → deterministic finalize.
- [ ] Output and Effect intents are journaled before seal; Validators, durable prepare, promotion, all six Effects, terminal receipt, authorization release, and release proof retain the normative order.
- [ ] SQLite is authoritative for checkpoints, Attempt events, resource authorization, and Effect state; restart duplicates no external or irreversible action.
- [ ] One static authenticated `EffectRegistry` serves six stateless handlers through bound `EffectCallContext`; no store/factory/runtime-registry chain exists.
- [ ] Current old-shape rejection, path/write/security, interrupt, lifecycle, packaging, lint, format, type, import, test, and three wheel-smoke checks pass.
- [ ] The full repository gate, three wheel smoke tests, and focused deterministic production-port,
  recovery, security, and lifecycle suites are green.
- [ ] Phase I Kernel internal refactoring remains unimplemented; its separate design note is reconsidered only after this plan's Phase P gate passes.
