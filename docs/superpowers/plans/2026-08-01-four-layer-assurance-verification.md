# Four-Layer Assurance Round-Trip and Runtime Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver contract-closed, topology-pinned, attempt-bound API/E2E/Fuzz/Performance assurance whose runtime, recovery, and benchmark claims are proven by committed physical evidence rather than prompt prose or pre-existing files.

**Architecture:** Keep the packaged YAML graph as the only control plane. Dark-ship strict wire models, input snapshots, physical-path resolution, candidate validators, durable effects, healing projections, and versioned semantics first; then write v6 root bindings and atomically activate all sixteen declared-only assurance contracts plus the graph-owned healing chain. Build eval policy/export and dark scorer/fixture inputs on those identities, prove the real GraphRuntime/recovery matrices, and only then activate benchmark scoring, datasets, and hard gates.

**Tech Stack:** Python 3.11, Pydantic v2, YAML workflow DSL, append-only JSONL ledgers, CAS-backed `TreeStore`, pytest, Ruff, Pyright, import-linter, uv, and Bash benchmark helpers.

## Global Constraints

- The authoritative design is `docs/superpowers/specs/2026-07-31-four-layer-assurance-verification-design.md`. Preserve D1-D18, the canonical layer order, every named wire identity, every compatibility boundary, and every failure owner in that document.
- The layer order is exactly `api`, `e2e`, `fuzz`, `performance`. A missing eval selection defaults to `("api", "e2e")`; an explicitly empty, duplicate, or unknown selection fails before fixture seeding or manifest capture.
- The packaged YAML graph remains the runtime control plane. Do not generate graph topology in Python, branch on assurance target names in the scheduler, or let the test harness return a next node.
- `StrictWireModel` means `ConfigDict(extra="forbid", strict=True, frozen=True)`. JSON arrays remain lists on the wire; do not silently coerce tuples, booleans, timestamps, paths, or numeric strings.
- Current packaged conformance and historical pinned classification are separate APIs. Current exactness never reclassifies v1-v5 roots; historical semantic discovery never depends on current graph/node IDs.
- After Task 10 freezes a named gate/topology/commit-safety semantic definition, never change that definition under the same semantic ID. Move a discovered fix before Task 10 or introduce an explicit next-version object plus old-version dispatcher and compatibility tests; unrelated edits in the same module do not alter descriptor bytes.
- Event schema v6 may begin in Task 11 only after gate, topology, and runtime-commit-safety objects can all be staged, loaded, inherited, verified on live/pinned replay, and registered in a closed phase-local consumer inventory. Task 17 implements and closes the evidence-export consumer before any scorer or benchmark activation.
- Do not select a new `precommit_validator` or `durable_effects` contract on a v5 root. The six validator-bearing contracts and four effect-bearing operation bindings activate only after v6 commit-safety semantics are live.
- The sixteen assurance agent contracts activate `read_isolation: declared_only` in one atomic task. At that same boundary, no declared-only agent receives `.git`, `.venv`, `node_modules`, `events.jsonl`, a coordinator ledger, or task-visible `.graph-runtime` control metadata.
- A human-readable Markdown summary is never path authority. Generated/reused test authority comes only from the strict layer-specific manifest, input snapshot, frozen write set, plan/case mapping, physical-path resolver, and successful candidate-validation receipt.
- Every declared-only `task_attempt_started` references an already persisted input snapshot; its success event repeats the same snapshot ID. Lock deferral is scheduling state, not a failed attempt, and consumes no attempt number or budget.
- Precommit validation runs after artifact ingest and write-set freeze but before `task_attempt_succeeded`. Failure leaves no success event, superstep commit, canonical-tree change, synchronized publication, or domain event.
- Durable-effect intents live in the same success ledger record as the successful candidate. Successors remain blocked until the superstep is committed and every intent is acknowledged against a verified domain event.
- Retry timing for durable effects is stored in a coordinator-owned atomic sidecar independent from the contended progression lock. It does not create task attempts and cannot busy-loop.
- Physical evidence comparison uses pinned tree roots and normalized repository-relative paths. No validator, fixer, policy, exporter, or scorer compares authority from a raw `WriteEntry.logical_path` root name.
- Eval write evidence is content-aware and `lstat`-based, never symlink-following. Worktree capture fails deterministically above 250,000 entries or 4 GiB of hashed regular-file bytes. Git porcelain remains diagnostic only.
- Write-mode change resolution always uses the configured active `changes` root. Archive-only, path escape, symlink escape, missing/tampered configuration, or ambiguous resolution fails before runtime and never broadens policy.
- Historical v1-v5 ledgers stay readable. Pending v4/v5 assurance codegen/fixer/effect-bearing work stops with typed reason `legacy_commit_safety_semantics_unbound`; it is not authorized by a topology receipt alone.
- The only exit from that blocked active legacy root is the audited D18 supersede transition. It must fence the complete descendant subtree and may consume one exact replacement authorization at most once.
- API/E2E automatic codegen healing uses graph-owned authority, approval, record, and aggregate-safety operations. Agents never append the host ledger or invoke `aa heal record-apply`.
- The normalized `HealingEpisodeProjection` is the sole version-aware consumer of legacy allocation pairs and v2 allocation/apply events. All other healing, guard, history, and retro consumers use the projection.
- Every mutation test declares one expected owner, stable code, and locator and proves unrelated outcomes stay canonical. A downstream failure does not satisfy an upstream mutation case.
- Integration tests construct the real packaged `GraphRuntime`, journal, tree store, freeze/apply pipeline, nested child graphs, and deterministic adapter. CI does not require an OpenCode server.
- Preserve existing complete L2/L3 benchmark tiers. New workflow-codegen datasets use independent plan-ready/codegen-pending tiers that contain no selected assurance/codegen completion or generated test fallback.
- The three hard benchmark metrics are `current_assurance_chain_rate`, `current_codegen_attempt_rate`, and `selected_test_write_rate`; all four codegen suites require each metric to be at least `1.0` with zero allowed regression.
- Write every behavior test first, run it, and observe the named failure before production changes. Run every Python command through `uv run`.
- Do not assume the worktree or index is clean. One integrator serializes every `git add` and `git commit`; subagents may edit disjoint files but never stage or commit.

## Locked Interface Clarifications

The design fixes the semantics; this plan fixes one implementation vocabulary so parallel tasks cannot introduce equivalent-but-incompatible APIs.

1. Add `StrictWireModel` to `assurance_agent/artifacts/models/common.py`. Artifact, snapshot, receipt, effect, eval-evidence, and retry-sidecar models import this one base. `workflow.core` may depend downward on `artifacts`; `artifacts` never imports `workflow`.
2. Add `assurance_agent/verification/generated_files.py` as the only layer-to-summary/manifest/private-root/model registry:

   ```python
   @dataclass(frozen=True, slots=True)
   class LayerGeneratedFilesContract:
       layer: LayerName
       summary_path: str
       manifest_path: str
       private_test_root: str
       model_id: str


   def get_generated_files_contract(layer: str) -> LayerGeneratedFilesContract:
       """Return one exact contract or raise ValueError for an unknown layer."""
   ```

   The four manifest paths are `change:codegen/{layer}-generated-files.json`; the Performance summary remains `change:codegen/performance-codegen-summary.md` and its private root is `tests/perf`.
3. Add `assurance_agent/workflow/graph/task_inputs.py` with the exact `TaskInputSnapshotEntryV1`, `TaskInputSnapshotV1`, and `PlanFixerRuntimeContextV1` fields from design D13. Its public boundary is:

   ```python
   def capture_task_input_snapshot(
       *,
       invocation_id: str,
       task: ExecutableTask,
       attempt_id: str,
       workspace: TaskWorkspace,
       contract: ExecutionContract,
       runtime_context: PlanFixerRuntimeContextV1 | None,
   ) -> tuple[str, bytes]:
       """Return the CAS identity and canonical snapshot bytes."""


   def load_task_input_snapshot(store: TreeStore, snapshot_id: str) -> TaskInputSnapshotV1:
       """Load, digest-check, and strictly validate one snapshot object."""
   ```

4. `WorkspaceBackend.create(...)` receives `sidecar_root: Path` and returns a `TaskWorkspace` whose control manifest is outside `project_root`. `TaskWorkspace.from_materialized_root(...)` receives that sidecar manifest explicitly. Legacy workspaces may request convenience Git; declared-only workspaces may not.
5. Add `assurance_agent/workflow/graph/evidence_paths.py` with the D15 `ResolvedEvidencePath` model and this pure resolver:

   ```python
   def resolve_evidence_path(
       *,
       logical_path: str,
       tree_roots: Mapping[str, str],
       current_change_repo_path: str,
   ) -> ResolvedEvidencePath:
       """Resolve all aliases and classify one pinned physical path."""
   ```

   Current `WriteSet` objects carry canonical `base_tree_roots`, copied from and verified against `base_tree_id` during freeze and covered by `write_set_id`. Historical write sets may omit the map but are ineligible for current D15/D16 scoring. Validators and scorers pass only this write-set-bound map; no public API accepts an unbound live filesystem root map.
6. Add `assurance_agent/workflow/graph/precommit.py` with `PrecommitValidationContext`, `CandidateValidationReceiptV1`, a closed validator registry, and:

   ```python
   def validate_candidate(
       validator_id: str,
       context: PrecommitValidationContext,
   ) -> CandidateValidationReceiptV1:
       """Dispatch one registered validator and return its canonical receipt."""


   def verify_candidate_receipt(
       receipt: CandidateValidationReceiptV1,
       context: PrecommitValidationContext,
   ) -> None:
       """Recompute every binding or raise CandidateValidationError."""
   ```

   Registry IDs are exactly `generated_files_candidate/v1` and `codegen_fix_candidate/v1`. Validation writes both canonical decision payload bytes and the receipt to CAS; `decision_payload_sha256` names those exact bytes, and evidence export carries them as a `blob` object rather than treating a receipt digest as self-contained evidence.
7. Add `assurance_agent/workflow/graph/durable_effects.py` with `DurableEffectIntentV1`, strict payload dispatch, deterministic effect-ID derivation, `DurableEffectAcknowledgementV1`, and:

   ```python
   def reconcile_effect(
       intent: DurableEffectIntentV1,
       context: DurableEffectContext,
   ) -> DurableEffectAcknowledgementV1:
       """Append or reuse one verified domain event by registered kind."""
   ```

   Registered kinds are exactly `healing_allocation/v2`, `fixer_proposal_approved/v1`, and `heal_record_apply/v2`.
8. Add `assurance_agent/workflow/graph/effect_retry.py` with the exact D14 `EffectRetryStateV1` wire fields and an `EffectRetryStore` exposing compare-and-swap `load`, `schedule_next`, and `clear_if_acknowledged` methods. In the same pre-semantics task, add strict `RootTerminalFenceStateV1` plus `RootEffectFenceStore.guard/prepare_terminal/commit_terminal/abort_prepared`; every schedule/reconcile path takes this root guard first and rejects a prepared/committed terminal fence. The seam is dormant until D18 consumes it, and canonical RFC 3339 UTC parsing is explicit.
9. Add `assurance_agent/workflow/graph/runtime_commit_safety.py`. Its semantic ID is `runtime_commit_safety/v1`; its manifest inventory is the exact validator, context/receipt, path/diff-safety, effect, reconciler, acknowledgement, and retry-sidecar dependency/consumer set. Add `historical_topology_safety/v1` in `topology_semantics.py`; gate semantics gains canonical manifest bytes rather than a digest-only API.
10. A v6 definition binding always carries six non-empty fields: gate object ID/digest, topology object ID/digest, and commit-safety object ID/digest. The same values appear in the root start event, child start events, projections, pinned requests, definition snapshots, compatibility checks, and evidence export.
11. Add `assurance_agent/workflow/graph/historical_roles.py` for `DiscoveredHistoricalLayerRoles` and `DiscoveredHistoricalAssuranceRoles`. `ResolvedPinnedDefinition.historical_roles` carries the one discovered manifest from `load_pinned_execution_definition(...)`; `FrozenDefinitionBinding.historical_roles` carries the same value into replay/trace/eval consumers. Historical compilation, selection, replay binding, compatibility-audit triggering, trace projection through the frozen binding, and v6 classification consume that carrier; none performs a second name lookup.
12. Keep the existing v4 and v5 display classifiers frozen in a legacy module. Only v6 with a verified `historical_topology_safety/v1` object calls the semantic CFG/dominance/truth-table classifier.
13. `find_current_assurance_conformance_issues(schema)` consumes only the workflow schema. `find_current_healing_conformance_issues(schema, contracts)` additionally consumes the decoded `ExecutionContractCatalog` because validator/effect ownership lives there. `compile_packaged_workflow` passes both explicitly; neither validator loads packaged resources itself.
14. Add `assurance_agent/workflow/healing/projection.py`; `HealingEpisodeProjection` normalizes legacy baseline/allocation pairs, v2 combined allocations, and legacy/v2 record events. Direct raw-name matching is permitted only in the event codec, this projector, and versioned fixtures.
15. `ExecutionContract` gains `precommit_validator: str | None = None` and `durable_effects: tuple[DurableEffectKind, ...] = ()`. Unknown IDs/kinds, missing manifest membership, duplicates, and wrong producer-kind bindings fail during contract/catalog compilation.
16. `TaskAttemptSucceededEvent` gains optional historical-compatible fields `input_snapshot_id`, `runtime_context_sha256`, `candidate_validation_receipt_id`, and `durable_effects`. A current contract makes the relevant fields mandatory through runtime validation; historical event models are not retrofitted.
17. `TaskSchedulingDeferredEvent`, `DurableEffectAcknowledgedEvent`, the durable-effect integrity terminal event, topology compatibility receipt event, three healing v2 domain events, and `GraphInvocationSupersededEvent` are strict discriminated event variants. Exact duplicates fold idempotently; identity-equal payload drift is corruption.
18. Add `assurance_agent/eval/selection.py`:

   ```python
   def normalize_selected_layers(
       *,
       test_type: object = MISSING,
       test_types: object = MISSING,
   ) -> tuple[LayerName, ...]:
       """Resolve one raw suite boundary value into canonical layer order."""
   ```

   `MISSING` is a private sentinel exported only for runner tests. `execute_attempt(...)` accepts `selected_layers: tuple[LayerName, ...]`; downstream consumers never receive the raw suite value.
19. Replace porcelain authority with strict `WorktreeManifestV1`, `WorktreeManifestEntryV1`, `WriteDiffV1`, and `WritePolicyV1` models in `eval/write_scan.py`. Persist `write-manifest-before.json`, `write-manifest-after.json`, `write-diff.json`, and `write-policy.json`; keep the two porcelain files only as diagnostics.
20. Add pure change-location seams to `change_location.py`: `parse_change_roots`, `probe_change_location_candidates`, and `decide_change_location`. The existing `resolve_change` wrapper delegates to them. Eval persists `change-location-config.yaml` and `change-location.json` using the exact D17 models.
21. Add `assurance_agent/eval/evidence_export.py`. Its schema ID is `root_execution_closure/v1`; the exporter reads the source ledger once, includes the current root descendant closure plus exact referenced snapshots/write sets/blobs/receipts/semantics and D18 authorization event when applicable, and rejects both missing and extra objects during replay.
22. Add `assurance_agent/verification/generated_entries.py` in two stages. Task 6 defines strict API/E2E/Fuzz/Performance plan-to-case-to-target mapping extraction consumed by precommit; Task 18 adds the AST behavior classifier consumed by the scorer. Runtime validation and the scorer share mapping/path helpers, but only the scorer runs behavioral AST obligations.
23. `GraphRuntime.supersede(...)` and `aa workflow supersede` consume a typed eligibility result whose stable reason is `legacy_commit_safety_semantics_unbound`. The replacement start fields are an all-or-none pair: `supersedes_invocation_id` and `replacement_authorization_id`.
24. The v6-only activation is one commit. It updates the sixteen skills, contracts, ingest catalog, four codegen outputs, healing graph, exact personas, current conformance compiler hooks, validators/effects, and declared-only switch together. Earlier tasks may add dormant code and tests but may not select those mechanisms in packaged YAML.

## File Structure

### New production files

- `assurance_agent/artifacts/models/generated_files.py` — strict four-layer generated-file manifests.
- `assurance_agent/artifacts/models/healing_codegen.py` — strict fixer authority, intent, approval, target record, and aggregate safety artifacts.
- `assurance_agent/verification/generated_files.py` — exact four-layer manifest/summary/private-root registry.
- `assurance_agent/verification/generated_entries.py` — shared mapping and layer-specific behavioral test classifier.
- `assurance_agent/workflow/graph/task_inputs.py` — attempt-bound input snapshots and plan-fixer runtime context.
- `assurance_agent/workflow/graph/evidence_paths.py` — pinned physical-path/alias resolver.
- `assurance_agent/workflow/graph/precommit.py` — validator registry, context, receipts, and dispatch.
- `assurance_agent/workflow/graph/durable_effects.py` — inline effect registry, intent, acknowledgement, and dispatch.
- `assurance_agent/workflow/graph/effect_retry.py` — independent retry-sidecar store and backoff projection.
- `assurance_agent/workflow/graph/runtime_commit_safety.py` — canonical `runtime_commit_safety/v1` manifest.
- `assurance_agent/workflow/graph/topology_analysis.py` — pure CFG, dominance, reachability, and finite truth-table helpers.
- `assurance_agent/workflow/graph/assurance_conformance.py` — structured current four-layer diagnostics.
- `assurance_agent/workflow/graph/healing_conformance.py` — structured current healing diagnostics.
- `assurance_agent/workflow/graph/historical_roles.py` — shared historical semantic role discovery manifest.
- `assurance_agent/workflow/graph/historical_topology_v6.py` — v6 historical safety classifier.
- `assurance_agent/workflow/graph/topology_semantics.py` — canonical `historical_topology_safety/v1` manifest.
- `assurance_agent/workflow/graph/resume_compatibility.py` — v4/v5 audit receipt and typed live-resume guard.
- `assurance_agent/workflow/graph/supersede.py` — subtree eligibility, terminal fence, and replacement authorization.
- `assurance_agent/workflow/graph/assurance_personas.py` — exact sixteen-target persona registry.
- `assurance_agent/workflow/healing/projection.py` — normalized legacy/v2 healing episode projection.
- `assurance_agent/workflow/healing/effects.py` — strict healing effect payloads, domain-event builders, and reconcilers.
- `assurance_agent/eval/selection.py` — one raw-suite-to-layer normalization boundary.
- `assurance_agent/eval/change_location_evidence.py` — D17 strict records and replay verification.
- `assurance_agent/eval/evidence_export.py` — bounded root execution closure export/replay.
- `assurance_agent/eval/scorers/current_codegen.py` — current-chain, attempt, and selected-write scoring.

### New tests and helpers

- `tests/helpers_assurance_contract.py`
- `tests/fixtures/assurance/api-contract/**`
- `tests/unit/artifacts/test_generated_files.py`
- `tests/unit/artifacts/test_healing_codegen.py`
- `tests/unit/verification/test_assurance_contract_round_trip.py`
- `tests/unit/verification/test_assurance_contract_mutations.py`
- `tests/unit/verification/test_generated_entries.py`
- `tests/unit/workflow/graph/test_assurance_topology_mutations.py`
- `tests/unit/workflow/graph/test_healing_topology_mutations.py`
- `tests/unit/workflow/graph/test_historical_roles.py`
- `tests/unit/workflow/graph/test_topology_semantics.py`
- `tests/unit/workflow/graph/test_runtime_commit_safety.py`
- `tests/unit/workflow/graph/test_task_input_snapshot.py`
- `tests/unit/workflow/graph/test_evidence_paths.py`
- `tests/unit/workflow/graph/test_precommit_validation.py`
- `tests/unit/workflow/graph/test_durable_effects.py`
- `tests/unit/workflow/graph/test_effect_retry.py`
- `tests/unit/workflow/graph/test_resume_compatibility.py`
- `tests/unit/workflow/graph/test_supersede.py`
- `tests/unit/healing/test_episode_projection.py`
- `tests/unit/eval/test_selection.py`
- `tests/unit/eval/test_change_location_evidence.py`
- `tests/unit/eval/test_evidence_export.py`
- `tests/unit/eval/test_codegen_scorer.py`
- `tests/integration/test_four_layer_codegen_only.py`
- `tests/integration/test_four_layer_resume.py`
- `tests/integration/test_codegen_fixer_record.py`

### Modified production areas

- `assurance_agent/_resources/skills/aa-{api,e2e,fuzz,performance}-{plan,plan-reviewer,codegen}/SKILL.md` plus API/E2E plan-fixer and codegen-fixer skills — structured closure and graph-owned state authority.
- `assurance_agent/_resources/schemas/{workflow-schema.yaml,execution-contracts.yaml,ingest-artifact-catalog.yaml}` — v6-only hard outputs, validators, effects, isolation, and healing topology.
- `assurance_agent/_resources/opencode/agents/{aa-doc-author,aa-reviewer,aa-test-author}.md` — exact bounded edit floors.
- `assurance_agent/artifacts/models/{common.py,__init__.py}`, `assurance_agent/artifacts/registry.py`, and `assurance_agent/workflow/graph/{ingest.py,ingest_catalog.py}` — strict model registration and runtime ingest.
- `assurance_agent/workflow/core/{events.py,graph_events.py,migrate_events.py}` — v6 binding fields, snapshots, deferrals, receipts/effects, healing v2, and supersede events.
- `assurance_agent/workflow/graph/{contracts.py,models.py,workspace.py,scheduler.py,finalize.py,planner.py,checkpoint.py,status.py,compiler.py,definition_pinning.py,replay_schema.py,replay_binding.py,runtime.py}` — runtime authority and compatibility plumbing.
- `assurance_agent/workflow/graph/handlers/{agent.py,operation.py,plan_checks.py}`, `assurance_agent/workflow/graph/agent_api.py`, `assurance_agent/workflow/driver/{runtime_factory.py,opencode_adapter.py,driver_state.py}` — attempt preparation, exact personas/directories, graph-owned healing operations, and v6 start/resume.
- `assurance_agent/workflow/healing/{allocation.py,safety.py}`, `assurance_agent/workflow/orchestration/healing_state.py`, and `assurance_agent/retro/workflow_history.py` — normalized healing projection and durable domain writes.
- `assurance_agent/change_location.py`, `assurance_agent/eval/{runner.py,executor.py,write_scan.py,fixtures.py,types.py}`, and `assurance_agent/eval/scorers/{codegen.py,shared.py}` — one selection, content evidence, bounded export, and current-chain scoring.
- `benchmark/vue-fastapi-admin/eval-fixtures/{tiers,fixture-lock.json}`, `eval/datasets/workflow-*-codegen/*.yaml`, and `eval/suites/workflow-*-codegen.yaml` — truthful codegen-pending benchmark inputs and hard metrics.
- `assurance_agent/commands/workflow_cmd.py`, `docs/schemas.md`, and release notes — supersede CLI and compatibility documentation.

## Execution Preflight

- [ ] Confirm the working branch and baseline:

  ```bash
  git branch --show-current
  git rev-parse HEAD
  git status --short
  git diff --cached --name-only
  ```

  Expected: branch `codex/capability-traceability-integration`, no unrelated tracked changes, and an empty index. Do not stash, discard, or absorb another session's files.
- [ ] Run the current focused baseline before Task 1:

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_contract_round_trip.py \
    tests/unit/verification/test_layer_assurance_round_trip.py \
    tests/unit/workflow/graph/test_replay_schema.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_fixtures.py
  ```

  Expected: all existing tests pass. Record failures as baseline defects before changing files.
- [ ] Before every commit inspect `git diff --cached --name-only`, the complete cached diff, and `git diff --cached --check`. The staged paths must exactly match that task's declared files.
- [ ] For every Python-changing task run Ruff on the changed Python files and the full `uv run pyright` before commit. Focused pytest alone is not an intermediate green gate.
- [ ] Run tasks in numbered order. Tasks 4-10 dark-ship runtime primitives; Task 11 is the only v6 writer flip; Task 15 is the only packaged assurance activation; Tasks 18-19 remain dark; Task 22 is the only benchmark/scorer activation.

---

### Task 1: Add Strict Generated-File and Healing Wire Models Without Runtime Activation

**Files:**
- Modify: `assurance_agent/artifacts/models/common.py`
- Create: `assurance_agent/artifacts/models/generated_files.py`
- Create: `assurance_agent/artifacts/models/healing_codegen.py`
- Modify: `assurance_agent/artifacts/models/__init__.py`
- Create: `assurance_agent/verification/generated_files.py`
- Create: `tests/unit/artifacts/test_generated_files.py`
- Create: `tests/unit/artifacts/test_healing_codegen.py`
- Modify: `tests/unit/artifacts/test_registry.py`

**Interfaces:**
- Produces: `StrictWireModel`, `GeneratedFileEntryV1`, `GeneratedFilesV1`, four layer-specific generated-file models, `FixerAuthorityV1`, `CodegenFixApplyIntentV1`, API/E2E intent models, `FixerProposalApprovalReceiptV1`, target apply-summary/safety models, aggregate safety model, and `LayerGeneratedFilesContract`.
- Preserves: the runtime ingest catalog and packaged graph remain unchanged; these models are importable but not yet selected by a path or execution contract.
- Consumes: `LayerName`, canonical JSON digest helpers, and existing healing proposal identifiers.

- [ ] **Step 1: Add failing strict-shape and canonical-order tests**

  Cover all four path-specific layer literals, `extra` rejection, strict booleans/integers, duplicate/unsorted paths, duplicate/unsorted `case_ids`, traversal, backslashes, empty applied proposal sets, non-empty reasons for `no_op`/`skipped`, and API payload rejection by the E2E model. Assert two serializations of each valid document are byte-identical.

  ```python
  def test_api_generated_files_rejects_e2e_layer() -> None:
      payload = valid_generated_files_payload(layer="e2e", repo_path="tests/e2e/test_login.py")
      with pytest.raises(ValidationError):
          ApiGeneratedFilesV1.model_validate(payload)


  def test_applied_intent_requires_canonical_nonempty_proposals_and_paths() -> None:
      payload = valid_fix_intent_payload(outcome="applied")
      payload["proposal_ids"] = []
      with pytest.raises(ValidationError):
          ApiCodegenFixApplyIntentV1.model_validate(payload)
  ```

- [ ] **Step 2: Run the new model tests and observe import/model failures**

  ```bash
  uv run pytest -q \
    tests/unit/artifacts/test_generated_files.py \
    tests/unit/artifacts/test_healing_codegen.py
  ```

  Expected: collection fails because the new models and registry do not exist.
- [ ] **Step 3: Implement the strict base, models, validators, and layer registry**

  Use the exact D16 fields for generated files. Keep human summary paths in `LayerGeneratedFilesContract`, not in model validators. Enforce normalized POSIX repository paths, canonical unique order, SHA-256 wire format, outcome-specific fixer invariants, and exact API/E2E target literals.
- [ ] **Step 4: Export models without registering runtime paths**

  Update `artifacts/models/__init__.py`; add registry tests asserting the new model types are available but `match_artifact("codegen/api-generated-files.json")` retains the pre-activation result.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/artifacts/test_generated_files.py \
    tests/unit/artifacts/test_healing_codegen.py \
    tests/unit/artifacts/test_registry.py
  uv run ruff check \
    assurance_agent/artifacts/models/common.py \
    assurance_agent/artifacts/models/generated_files.py \
    assurance_agent/artifacts/models/healing_codegen.py \
    assurance_agent/verification/generated_files.py \
    tests/unit/artifacts/test_generated_files.py \
    tests/unit/artifacts/test_healing_codegen.py
  uv run pyright
  ```

  Expected: all commands pass and no runtime catalog/schema file changed.
- [ ] **Step 6: Commit the dormant model boundary**

  ```bash
  git add assurance_agent/artifacts/models/common.py \
    assurance_agent/artifacts/models/generated_files.py \
    assurance_agent/artifacts/models/healing_codegen.py \
    assurance_agent/artifacts/models/__init__.py \
    assurance_agent/verification/generated_files.py \
    tests/unit/artifacts/test_generated_files.py \
    tests/unit/artifacts/test_healing_codegen.py \
    tests/unit/artifacts/test_registry.py
  git commit -m "feat(artifacts): define strict assurance output models"
  ```

### Task 2: Build Independent Canonical Fixtures and a Structural Skill Contract Reader

**Files:**
- Create: `tests/fixtures/assurance/api-contract/**`
- Modify: `tests/fixtures/assurance/e2e-contract/**`
- Modify: `tests/fixtures/assurance/fuzz-contract/**`
- Modify: `tests/fixtures/assurance/performance-contract/**`
- Create: `tests/helpers_assurance_contract.py`
- Create: `tests/unit/verification/test_assurance_contract_round_trip.py`
- Create: `tests/unit/verification/test_assurance_contract_mutations.py`
- Modify: `tests/unit/verification/test_fuzz_performance_contract_fixtures.py`

**Interfaces:**
- Produces: `SkillContractSections`, `parse_skill_contract_sections(path)`, canonical per-layer bundle builders, and `AssuranceContractObservation` test DTO.
- Consumes: production `PlanReviewAuthoring`, `PlanReview`, applicability, mechanical checks, gate, precondition, `TaskWorkspace`, and artifact-ingest APIs.
- Defers: packaged skill text, contracts, personas, workflow YAML, and activation-only assertions are not changed in this task. Do not commit skipped or expected-failure tests; Task 15 adds the assertions that require the activated runtime.

- [ ] **Step 1: Add the independent API fixture and normalize all four fixture roots**

  Each root must contain one exact automated case, complete layer plans, the layer summary, authoring/runtime reviews, `.aa/config.yaml`, `.aa/data-knowledge.yaml`, `tests/testdata/domain/account.py`, declared layer adapter/support files, and no checks/codegen output. Fuzz uses the final Case ID/Test Function/Target File mapping plus Schema Acquisition; Performance uses the final Case ID/Task Method/Target File mapping. Assert no fixture imports or symlinks another fixture or benchmark directory.
- [ ] **Step 2: Add a structural Markdown section reader**

  Parse only exact `## Inputs`, `## Outputs`, `## State Authority`, and `## Runtime Context` headings plus backticked logical paths and required/optional/conditional groups. Reject duplicate headings, malformed path rows, unknown group names, and a section interrupted by an unexpected heading. Do not parse general prose.
- [ ] **Step 3: Add failing fixture/production boundary tests**

  Parameterize the fixed matrix:

  | layer | l1_path | shared_factory | assert_ideal | capability_keys |
  |---|---|---|---|---|
  | api | pass | pass | pass | pass |
  | e2e | pass | pass | pass | pass |
  | fuzz | pass | pass | not_applicable | pass |
  | performance | pass | pass | not_applicable | pass |

  Assert all four check IDs exist in production order, Fuzz/Performance carry `check_not_in_profile`, and a dynamic empty layer carries four `layer_not_applicable` entries. Record observations at authoring, runtime, freeze, applicability, mechanical, wire, gate, precheck, and codegen-workspace boundaries.
- [ ] **Step 4: Add owner-specific mutation cases**

  Include one mutation each for missing skill input, undeclared read, forbidden write, wrong review layer/change, stale checks, malformed N/A, missing capability, and wrong summary/manifest layer. Each row declares `owner`, `code`, and `locator`; assert all non-owner observations equal the canonical fixture. Task 4 adds snapshot substitution once that wire API exists.
- [ ] **Step 5: Run the fixture/helper tests**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py
  uv run ruff check tests/helpers_assurance_contract.py tests/unit/verification
  uv run pyright
  ```

  Expected: every collected fixture, structural-reader, observation, and mutation-owner test passes with no skip/expected-failure marker.
- [ ] **Step 6: Commit the observing harness foundation**

  ```bash
  git add tests/fixtures/assurance tests/helpers_assurance_contract.py \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py
  git commit -m "test(assurance): add independent four-layer contract fixtures"
  ```

### Task 3: Add Typed Compiler Diagnostics and Dark-Ship Current Conformance

**Files:**
- Create: `assurance_agent/workflow/graph/topology_analysis.py`
- Create: `assurance_agent/workflow/graph/assurance_conformance.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `assurance_agent/workflow/graph/replay_schema.py`
- Modify: `tests/unit/workflow/graph/test_compiler.py`
- Create: `tests/unit/workflow/graph/test_assurance_topology_mutations.py`
- Modify: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`

**Interfaces:**
- Produces: `CompileDiagnostic`, typed `CompileError.diagnostics`, `AssuranceConformanceIssue`, pure CFG/dominance/truth-table helpers, and `find_current_assurance_conformance_issues(...)`.
- Preserves: `validate_current_assurance_activation(...)` as deterministic string-rendering compatibility wrapper; packaged compiler does not invoke the new strict current validator until Task 15.
- Consumes: decoded `WorkflowSchemaV2` models, existing expression AST, gate/route definitions, and exact packaged release domains.

- [ ] **Step 1: Add typed diagnostic transport tests**

  Assert `CompileError` preserves a sorted immutable tuple, `str(exc)` renders deterministic human detail, and `_pinned_reason_for_compile_error(...)` switches on category/code rather than substrings. Cover `historical_ingest_identity`, `historical_contract_identity`, and `workflow_validation` without changing existing reason outcomes.
- [ ] **Step 2: Add the full current assurance mutation table**

  Use Pydantic `model_copy` on the decoded packaged schema. Cover every mutation in design §8.7: selection truth table, run modes, preflight, reviewer/mechanical/gate/precheck uniqueness, evidence reads/aliases, fail-closed atoms and precedence, bypass edges/routes, interrupt bindings/actions/allowlist, remediation returns, and generation join. Positive controls reorder commutative operands and list literals while preserving the finite truth table.
- [ ] **Step 3: Build the approved assurance target schema in test code**

  Start from the decoded packaged schema and use Pydantic `model_copy` to add the approved API/E2E capability atoms without editing YAML. Run every mutation against that valid target model, and assert the unmodified pre-activation package remains on its compatibility validator until Task 15.
- [ ] **Step 4: Run tests and observe missing diagnostics/conformance functions**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py
  ```

  Expected: collection or assertions fail at the new typed APIs, not because packaged YAML was changed.
- [ ] **Step 5: Implement generic topology analysis and current assurance validation**

  Evaluate predicates over the complete finite domain and treat unknown params/builtins or parse failure as a mismatch. Compute reachability and dominance over ordinary edges and each route outcome. Return sorted structured issues; do not return a next node or mutate the schema.
- [ ] **Step 6: Route existing compile identity failures through typed diagnostics**

  Keep generic and historical compiler behavior stable. Adapt only the compatibility string wrapper and pinned reason mapper; leave the strict packaged hooks dormant for Task 15.
- [ ] **Step 7: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/workflow/driver/test_runtime_factory_compilation.py
  uv run ruff check assurance_agent/workflow/graph tests/unit/workflow/graph
  uv run pyright
  ```

  Expected: all tests pass and `compile_packaged_workflow` still compiles the pre-activation packaged graph through its old compatibility call.
- [ ] **Step 8: Commit typed conformance without activation**

  ```bash
  git add assurance_agent/workflow/graph/topology_analysis.py \
    assurance_agent/workflow/graph/assurance_conformance.py \
    assurance_agent/workflow/graph/compiler.py \
    assurance_agent/workflow/graph/definition_pinning.py \
    assurance_agent/workflow/graph/replay_schema.py \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py
  git commit -m "feat(graph): add typed assurance conformance diagnostics"
  ```

### Task 4: Move Declared-Only Workspaces to Sidecars and Bind Attempt Input Snapshots

**Files:**
- Create: `assurance_agent/workflow/graph/task_inputs.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/graph/agent_api.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Create: `tests/unit/workflow/graph/test_task_input_snapshot.py`
- Modify: `tests/unit/workflow/graph/test_workspace.py`
- Modify: `tests/unit/workflow/graph/test_read_isolation.py`
- Modify: `tests/unit/workflow/graph/test_scheduler.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/integration/test_trace_recovery_workflows.py`

**Interfaces:**
- Produces: the snapshot/context APIs from Locked Interface Clarification 3, sidecar-backed `TaskWorkspace`, `TaskSchedulingDeferredEvent`, and start/success snapshot/context fields.
- Preserves: existing non-declared workspaces, historical events without the new fields, and packaged contracts that have not yet enabled declared-only isolation.
- Consumes: contract claims/digests, pinned tree roots, reserved attempt identity, lease registry, deterministic scheduler clock/backoff, and audited plan-review resume data.

- [ ] **Step 1: Add strict snapshot/context model tests**

  Assert canonical unique physical entries, complete aliases/matched claims/origins, file/symlink field rules, base/materialized tree bindings, digest verification, automatic-healing null human fields, human-approved exact interrupt/action/reason/digest fields, and wrong-target/tree/review rejection.
- [ ] **Step 2: Add sidecar visibility and alias tests**

  Materialize one declared-only workspace with aliased `project`/`repo` roots. Snapshot bytes, modes, and link metadata of host `.git`, `.venv`, `node_modules`, event/ledger/coordinator/runtime targets. From the real attempt root, try to read, traverse, and write every former-link location and assert each is unavailable; then prove every host snapshot is byte-for-byte and metadata-identical. The external sidecar still reopens the workspace, and one physical test file has both logical aliases in one snapshot entry.
- [ ] **Step 3: Add scheduler ordering and lock-deferral tests**

  Inject crash cuts after snapshot CAS but before started append and after started append. Assert no started event can reference a missing object, retry uses the ledger-derived attempt number, and start/success IDs match. Hold a real synchronized project lock and assert `task_scheduling_deferred` contains no attempt ID/number, no failed event, no budget use, deterministic ordinal/backoff, restart suppression before due time, and one first attempt after release.
- [ ] **Step 4: Run tests and observe current ordering/visibility failures**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_workspace.py \
    tests/unit/workflow/graph/test_read_isolation.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/integration/test_trace_recovery_workflows.py
  ```

  Expected: failures show task-visible control metadata, started-before-snapshot ordering, and started+failed lock conflicts.
- [ ] **Step 5: Implement sidecar materialization and snapshot capture without narrowing the wave lock**

  Preserve `execute -> _project_lock_scope -> _execute_wave -> _begin_attempt`: the synchronized project lock still covers the whole wave and is acquired before any attempt reservation. Within `_begin_attempt`, reserve identity/lease, materialize/filter/inject the skill bundle, construct typed runtime context when required, capture/store the snapshot, append the started event, then dispatch. Thread `sidecar_root` through every `WorkspaceBackend.create(...)` implementation/test double and pass the explicit sidecar manifest into every `TaskWorkspace.from_materialized_root(...)` call, including trace-recovery integration helpers. Clean unreachable sidecars/CAS references after pre-start crashes without deleting reachable objects.
- [ ] **Step 6: Fold and verify new event fields**

  Require the snapshot object before accepting a declared-only start; require success to repeat the same IDs/digests. Fold the highest exact deferral ordinal, reject payload conflict, and keep a deferred task unselectable before `next_retry_at` without committing its superstep.
- [ ] **Step 7: Render the plan-fixer Runtime Context prompt block**

  Bind the canonical bytes/digest to `AgentRequest`; reject prompt/context mismatch before adapter dispatch. Do not materialize the context or ledger. Keep this path dormant until Task 15 contracts name the runtime injection.
- [ ] **Step 8: Add AST consumer-set guards**

  At this stage require `input_snapshot_id` in attempt preparation, start, success, and fold, and require `runtime_context_sha256` in plan-fixer preparation, prompt rendering, start, success, fold, and resume. Store the expected consumer symbols as a closed test inventory; Tasks 8, 17, and 18 extend it when fixer authority, export, and scorer consumers are added. Do not accept filename substring matches.
- [ ] **Step 9: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_workspace.py \
    tests/unit/workflow/graph/test_read_isolation.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/integration/test_trace_recovery_workflows.py
  uv run ruff check assurance_agent/workflow/core/graph_events.py assurance_agent/workflow/graph tests/unit/workflow/graph
  uv run pyright
  ```

  Expected: all focused tests pass; current packaged agent execution retains legacy visibility because no contract has been flipped.
- [ ] **Step 10: Commit the attempt-input authority seam**

  ```bash
  git add assurance_agent/workflow/graph/task_inputs.py \
    assurance_agent/workflow/graph/workspace.py \
    assurance_agent/workflow/graph/contracts.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/scheduler.py \
    assurance_agent/workflow/graph/agent_api.py \
    assurance_agent/workflow/graph/handlers/agent.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_workspace.py \
    tests/unit/workflow/graph/test_read_isolation.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/integration/test_trace_recovery_workflows.py
  git commit -m "feat(graph): bind declared task input snapshots"
  ```

### Task 5: Resolve Evidence by Pinned Physical Identity

**Files:**
- Create: `assurance_agent/workflow/graph/evidence_paths.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Create: `tests/unit/workflow/graph/test_evidence_paths.py`
- Modify: `tests/unit/workflow/graph/test_workspace.py`

**Interfaces:**
- Produces: `ResolvedEvidencePath`, `resolve_evidence_path(...)`, a read-only `TreeStore.tree_roots(tree_id) -> Mapping[str, str]` accessor, and canonical `WriteSet.base_tree_roots` covered by `write_set_id`.
- Consumes: `WriteSet.base_tree_id`, safe normalized tree roots, current change repository path, and one logical write entry.
- Defers: validators, fixer authority, eval policy, export, and scorer switch to this resolver in later tasks.

- [ ] **Step 1: Add positive alias/ownership tests**

  Cover all four private roots when a write serializes as `project:tests/...` while `project` and `repo` both map to `.`. Assert one physical path, repository-relative `tests/...`, aliases containing both roots, and ownership `repo`. Cover a current-change output and assert the more-specific `current_change` ownership.
- [ ] **Step 2: Add fail-closed path tests over observable pinned inputs**

  Cover missing pinned root map, a map that disagrees with `base_tree_id` at freeze/load, traversal, absolute paths, another change, ambiguous non-nested containment, and a physical path with no unique ownership. Assert typed `EvidencePathError` codes. Because the pure resolver never opens or follows symlinks, assert that a pinned symlink entry/target cannot rewrite the lexical physical identity; tree ingestion, not this API, rejects unsafe tree entries. A historical write set without roots remains readable but cannot satisfy current evidence validation.
- [ ] **Step 3: Run tests and observe private/single-alias limitations**

  ```bash
  uv run pytest -q tests/unit/workflow/graph/test_evidence_paths.py tests/unit/workflow/graph/test_workspace.py
  ```

  Expected: the new module/accessor is missing and the current lexical `_canonical_logical` result cannot retain aliases.
- [ ] **Step 4: Implement the pure resolver and safe root accessor**

  Reuse workspace normalization, but return all equivalent aliases in canonical order. During freeze copy the verified base-tree roots into the write set before calculating `write_set_id`. Determine ownership by normalized physical containment in `current_change > repo > project` order. Never open live filesystem paths or follow symlinks.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q tests/unit/workflow/graph/test_evidence_paths.py tests/unit/workflow/graph/test_workspace.py
  uv run ruff check assurance_agent/workflow/graph/evidence_paths.py assurance_agent/workflow/graph/workspace.py tests/unit/workflow/graph/test_evidence_paths.py
  uv run pyright
  ```

  Expected: all alias and failure controls pass.
- [ ] **Step 6: Commit the shared physical identity seam**

  ```bash
  git add assurance_agent/workflow/graph/evidence_paths.py \
    assurance_agent/workflow/graph/workspace.py \
    tests/unit/workflow/graph/test_evidence_paths.py \
    tests/unit/workflow/graph/test_workspace.py
  git commit -m "feat(graph): resolve evidence by pinned physical path"
  ```

### Task 6: Add Candidate Validation Receipts and Both Assurance Validators

**Files:**
- Create: `assurance_agent/workflow/graph/precommit.py`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/finalize.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/graph/ingest.py`
- Modify: `assurance_agent/verification/generated_files.py`
- Create: `assurance_agent/verification/generated_entries.py`
- Create: `tests/unit/verification/test_generated_entries.py`
- Create: `tests/unit/workflow/graph/test_precommit_validation.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_scheduler.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/unit/workflow/graph/test_ingest.py`

**Interfaces:**
- Produces: `PrecommitValidationContext`, `CandidateValidationReceiptV1`, `CandidateValidationError`, strict per-layer mapping extraction, validator registry, `validate_candidate(...)`, `verify_candidate_receipt(...)`, `generated_files_candidate/v1`, and `codegen_fix_candidate/v1`.
- Consumes: a verified D13 input snapshot, pinned definition/policy identities, current committed tree, typed outputs, frozen `WriteSet`, D15 physical paths, plan/case mapping, fixer proposal/authority, and optional approval receipt.
- Preserves: `ExecutionContract.precommit_validator` defaults to `None`; packaged contracts do not select either validator before Task 15.

- [ ] **Step 1: Lock the validation context and receipt wire contract**

  Add strict tests for this boundary:

  ```python
  class PrecommitValidationContext(StrictWireModel):
      root_invocation_id: str
      invocation_id: str
      task_id: str
      attempt_id: str
      target: str
      base_tree_id: str
      current_tree_id: str
      input_snapshot_id: str
      contract_digest: str
      policy_object_id: str
      policy_digest: str
      gate_attempt_id: str | None
      interrupt_id: str | None
      output_digests: dict[str, str]
      write_set_id: str
      definition_semantics: dict[str, str]


  class CandidateValidationReceiptV1(StrictWireModel):
      schema_version: Literal["1"]
      validator_id: str
      validator_semantics_digest: str
      root_invocation_id: str
      invocation_id: str
      task_id: str
      attempt_id: str
      input_snapshot_id: str
      output_digests: dict[str, str]
      write_set_id: str
      decision_payload_sha256: str
  ```

  Require canonical output-key order, non-empty identities, exact context/receipt equality, and CAS digest verification.
- [ ] **Step 2: Add generated-file reconciliation mutation tests**

  First lock structural mapping extraction: API/E2E use Case ID/Test Function/Target File, Fuzz uses Test Function Mapping plus Schema Acquisition, and Performance uses Case ID/Task Method/Target File. Reject a missing, duplicated, interrupted, malformed, wrong-layer, or unmapped table. Then cover generated/updated regular-file add/content-modify, reused input-snapshot binding, full repository test/testdata write-set equality, plan Target Files and case mapping, exact case IDs, selected private-root restriction, and physical aliases. Reject summary-only, wrong layer/change, duplicate, omitted or extra writes, support-file credit, shared-builder reuse, wrong digest/disposition, delete, symlink/kind change, mode-only change, no-op modify, unmapped reuse, stale snapshot, and cross-attempt/tree/write-set substitution.
- [ ] **Step 3: Add codegen-fix candidate mutation tests**

  Require target-specific intent, proposal and fixer-authority subsets, exact claimed/test-write equality, regular-file add/content-modify only, baseline before-digest rules, and current approval receipt for high-risk proposals. Reject product edits, unrelated tests, assertion expected-value weakening, skip/xfail insertion, another target, stale proposal/authority/tree, missing high-risk approval, no-op with writes, and applied with zero writes.
- [ ] **Step 4: Add scheduler no-commit tests**

  For each validator, feed a shape-valid but cross-artifact-invalid candidate. Assert no `task_attempt_succeeded`, no `superstep_committed`, no canonical-tree change, no publication, no candidate receipt, and a single `invalid_output` failure owned by the validator. A valid candidate records one CAS receipt ID on success.
- [ ] **Step 5: Run tests and observe the missing registry/receipt path**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_ingest.py
  ```

  Expected: failures stop at missing contract field, validator dispatch, and success receipt binding.
- [ ] **Step 6: Implement the closed registry and validator algorithms**

  Run validators only after strict ingest and freeze. Derive mapping relations from typed production plans/cases rather than manifest-provided IDs. Use `resolve_evidence_path(...)` for every write. Store canonical decision bytes and receipt in CAS; make receipt verification recompute all identities and validator semantics.
- [ ] **Step 7: Integrate precommit between freeze and success append**

  Extend `_PreparedAttempt`/`_SettledAttempt` with the verified input snapshot and receipt. Reject unknown validator IDs at contract load. Keep a contract with no validator byte-for-byte compatible and prevent a receipt on such an attempt.
- [ ] **Step 8: Fold and validate receipt identity**

  Accept the optional historical field, but require it for a contract that names a validator. Reject a missing CAS object, wrong validator, output map, snapshot, attempt, tree, write set, or semantics digest before the task can be treated as committed.
- [ ] **Step 9: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_ingest.py
  uv run ruff check assurance_agent/workflow/graph/precommit.py assurance_agent/workflow/graph assurance_agent/verification/generated_entries.py tests/unit/workflow/graph tests/unit/verification/test_generated_entries.py
  uv run pyright
  ```

  Expected: both validator suites pass and the packaged catalog still selects neither validator.
- [ ] **Step 10: Commit dormant precommit validation**

  ```bash
  git add assurance_agent/workflow/graph/precommit.py \
    assurance_agent/workflow/graph/contracts.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/finalize.py \
    assurance_agent/workflow/graph/scheduler.py \
    assurance_agent/workflow/graph/ingest.py \
    assurance_agent/verification/generated_files.py \
    assurance_agent/verification/generated_entries.py \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_ingest.py
  git commit -m "feat(graph): validate mapped candidates before commit"
  ```

### Task 7: Add Inline Durable Effects, Acknowledgements, and Independent Retry State

**Files:**
- Create: `assurance_agent/workflow/graph/durable_effects.py`
- Create: `assurance_agent/workflow/graph/effect_retry.py`
- Modify: `assurance_agent/workflow/graph/contracts.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/graph/planner.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/status.py`
- Create: `tests/unit/workflow/graph/test_durable_effects.py`
- Create: `tests/unit/workflow/graph/test_effect_retry.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_scheduler.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`

**Interfaces:**
- Produces: `DurableEffectKind`, `DurableEffectIntentV1`, `DurableEffectContext`, `DurableEffectAcknowledgementV1`, effect registry, deterministic effect ID, `EffectRetryStateV1`, `EffectRetryStore`, strict `RootTerminalFenceStateV1`, `RootEffectFenceStore`, and generic reconciliation/recovery.
- Consumes: committed successful attempts, hard-output digests, domain-event journal API, progression locks, scheduler clock, and pinned reconciler semantics.
- Preserves: `ExecutionContract.durable_effects` defaults empty; existing handlers return no intents; the production registry has no healing entries until Task 8 supplies their strict payloads/reconcilers; no packaged operation selects an effect before Task 15.

- [ ] **Step 1: Add strict intent/acknowledgement tests**

  Use a test-local registry instance with one strict payload/reconciler. Require canonical unique effect IDs, exact payload digest, producer/task/attempt identity, reconciler digest, domain source sequence, and canonical domain-event digest. Reject opaque/malformed payloads and acknowledgement without a matching inline intent. Assert the production registry remains empty in this task.
- [ ] **Step 2: Add producer contract cardinality tests**

  Compile a test catalog against the test-local registry. A contract declaring one kind must receive exactly one matching intent from `TaskResult`; zero, duplicate, extra, wrong kind, wrong producer, or unstable effect ID is `invalid_output` before success. A contract declaring no kinds rejects all intents.
- [ ] **Step 3: Add named crash-cut tests**

  Inject cuts before success, after success before superstep commit, after commit before domain append, after domain append before acknowledgement, and after acknowledgement before successor planning. Once success is recorded, assert no handler reinvocation; the logical action has one committed result, one domain event, one acknowledgement, and one successor.
- [ ] **Step 4: Add real-lock retry-sidecar and terminal-fence tests**

  Hold the progression lock used by a test reconciler across multiple runtime restarts. Assert the independent sidecar advances one ordinal only when due, suppresses before due time, uses capped deterministic backoff, does not create attempts, and is inert after acknowledgement. Concurrent due reconcilers yield one CAS winner. Independently race `schedule_next`/reconcile against `prepare_terminal` and `commit_terminal`: every path acquires the root guard before any progression lock, a prepared fence suppresses new retry state, a committed fence rejects it permanently, and aborting an uncommitted preparation restores eligibility. Keep this generic seam unused by packaged runtime until Task 13.
- [ ] **Step 5: Run tests and observe the current success-only planner seam**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/integration/test_graph_runtime_faults.py
  ```

  Expected: failures show no inline intents, no acknowledgement state, and successors planned from success before reconciliation.
- [ ] **Step 6: Implement strict effect dispatch and same-record success serialization**

  Add `TaskResult.durable_effects`; validate them before constructing one `TaskAttemptSucceededEvent`. Keep the full canonical intents in that record. Do not append a separate pending record.
- [ ] **Step 7: Gate successors on committed-and-acknowledged state**

  Extend projection/planner readiness so a successful task with an uncommitted superstep or unacknowledged effect is not a completed predecessor. Exact duplicate acknowledgements are idempotent; conflicting acknowledgement/domain identity raises graph integrity failure.
- [ ] **Step 8: Add recovery and terminal integrity behavior**

  In `_reach_recovery_barrier`, repair pending writes/publications first, then reconcile unacknowledged effects, then materialize/plan. Every effect/retry entrypoint already runs under `RootEffectFenceStore.guard` and refuses prepared/committed terminal state. Retryable lock/I/O errors persist sidecar state; permanent model/digest/domain conflict records `durable_effect_integrity_failed` and terminally stops the invocation.
- [ ] **Step 9: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/integration/test_graph_runtime_faults.py
  uv run ruff check assurance_agent/workflow/graph assurance_agent/workflow/core/graph_events.py tests/unit/workflow/graph
  uv run pyright
  ```

  Expected: all generic effect/retry/crash tests pass with no packaged producer activated.
- [ ] **Step 10: Commit the generic durable-effect protocol**

  ```bash
  git add assurance_agent/workflow/graph/durable_effects.py \
    assurance_agent/workflow/graph/effect_retry.py \
    assurance_agent/workflow/graph/contracts.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/scheduler.py \
    assurance_agent/workflow/graph/planner.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/status.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_scheduler.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/integration/test_graph_runtime_faults.py
  git commit -m "feat(graph): reconcile inline durable effects"
  ```

### Task 8: Normalize Healing Events and Dark-Ship Graph-Owned Healing Operations

**Files:**
- Create: `assurance_agent/workflow/healing/projection.py`
- Create: `assurance_agent/workflow/healing/effects.py`
- Create: `assurance_agent/workflow/graph/healing_conformance.py`
- Modify: `assurance_agent/workflow/core/events.py`
- Modify: `assurance_agent/workflow/healing/allocation.py`
- Modify: `assurance_agent/workflow/healing/safety.py`
- Modify: `assurance_agent/workflow/orchestration/healing_state.py`
- Modify: `assurance_agent/retro/workflow_history.py`
- Modify: `assurance_agent/workflow/graph/handlers/operation.py`
- Modify: `assurance_agent/workflow/graph/durable_effects.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Create: `tests/unit/healing/test_episode_projection.py`
- Modify: `tests/unit/healing/test_allocation.py`
- Modify: `tests/unit/healing/test_record_apply.py`
- Modify: `tests/unit/test_healing_state.py`
- Modify: `tests/unit/retro/test_workflow_history.py`
- Create: `tests/unit/workflow/graph/test_healing_topology_mutations.py`
- Modify: `tests/unit/workflow/graph/test_durable_effects.py`
- Modify: `tests/unit/workflow/graph/test_task_input_snapshot.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Create: `tests/integration/test_codegen_fixer_record.py`

**Interfaces:**
- Produces: `HealingEpisodeProjection`, `HealingAllocationEffectV2`, `FixerProposalApprovedEffectV1`, `HealRecordApplyEffectV2`, three strict domain events/reconcilers, `HealingConformanceIssue`, `find_current_healing_conformance_issues(...)`, and code-owned allocate/authority/approval/dispatch/record/combine operations.
- Consumes: proposal, baseline, codegen attempt/manifest/input-snapshot/write-set authority, candidate validation receipt, audited interrupt/resume, current target fragments, and the generic effect protocol.
- Preserves: legacy event models/readers and current packaged healing topology. New operations are registered but unreachable until Task 15.

- [ ] **Step 1: Add legacy/v2/mixed projection tests**

  Assert a legacy baseline+allocation pair, one v2 combined allocation, and a non-conflicting mixed ledger produce the same baseline and ordered logical allocations. The first v2 allocation yields `attempts_used == 1`; later allocation reuses the exact baseline ID. Equivalent duplicate logical keys deduplicate; payload/ID/baseline conflict is integrity failure.
- [ ] **Step 2: Pin every healing consumer to the normalized projection**

  Test `derive_healing_state`, `derive_guard_context`, budget/allocation guards, record safety, graph guards, and retro workflow history against legacy-only, v2-only, and mixed ledgers. Add an AST consumer-set test allowing raw legacy event-name matching only in `core/events.py`, `healing/projection.py`, and versioned fixture tests.
- [ ] **Step 3: Add authority/approval/record operation tests**

  Prove `allocate` hard-outputs entry baseline plus fixer authority and returns one allocation effect. Authority binds generated/updated write-set after digests or valid mapped private-root reuse from the input snapshot; imported codegen returns `unverified_imported_codegen`. Approval binds proposal/authority/baseline/policy/source interrupt/tree. Record verifies target intent and candidate receipt, emits target summary+safety fragment and one record effect. Combine consumes exactly the active target fragments.
- [ ] **Step 4: Add API-only, E2E-only, and both-active integration controls**

  Drive the new operations directly through the real scheduler without changing packaged topology. Assert hard-output sets, effect acknowledgement, active record join inputs, one aggregate safety file, and no inactive-target requirement.
- [ ] **Step 5: Add the current healing mutation table against an in-test target schema**

  Build the approved healing topology and contracts as decoded Pydantic fixtures without changing packaged YAML. Cover authority/approval dominance, audited interrupt, one-target dispatch, both validator IDs, record-node join, aggregate safety, hard outputs, and all four producer/effect bindings. Each mutation asserts `category="healing_conformance"` and a stable code/owner/locator; positive controls cover low-risk, approved high-risk, API-only, E2E-only, and both-active paths.
- [ ] **Step 6: Run tests and observe legacy direct-write failures**

  ```bash
  uv run pytest -q \
    tests/unit/healing/test_episode_projection.py \
    tests/unit/healing/test_allocation.py \
    tests/unit/healing/test_record_apply.py \
    tests/unit/test_healing_state.py \
    tests/unit/retro/test_workflow_history.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/integration/test_codegen_fixer_record.py
  ```

  Expected: failures identify direct legacy filtering and direct host-ledger writes in allocation/record apply.
- [ ] **Step 7: Implement the normalized projector and migrate consumers**

  Keep legacy codecs unchanged. Dispatch both versions in one projector and expose normalized baseline, allocations, approvals, target records, and active guard context. Remove every downstream raw-name filter permitted only by the AST guard.
- [ ] **Step 8: Implement strict effect payloads and idempotent reconcilers**

  Build deterministic domain IDs/keys from the exact fields in design §5.3/D14. Reconcile exact-key/exact-payload as no-op; treat same-key drift as ledger corruption. Register exactly `healing_allocation/v2`, `fixer_proposal_approved/v1`, and `heal_record_apply/v2` by effect kind, not operation target. Replace Task 7's production-registry-empty assertion with the exact three kind/reconciler set while still proving no packaged contract selects one before Task 15.
- [ ] **Step 9: Implement graph-owned operation handlers without routing them**

  Register `fixer-authority-ready`, `record-fixer-approval`, `fixer-dispatch`, `record-codegen-fix-apply`, and `combine-fixer-safety` in `default_operations()`. Update `test_default_operations_registry_has_exact_keys()` in this same task; Task 15 later adds activation-specific reachability assertions. Extend the D13 field-consumer inventory with fixer-authority snapshot/context verification. Finalize the scheduler compatibility hook now: it runs only for the exact known pre-activation pinned allocation-contract digest with `durable_effects == ()`. Task 15 changes only the packaged contract selection, not this frozen consumer.
- [ ] **Step 10: Implement structured healing conformance without compiler activation**

  Reuse pure topology analysis, inspect the decoded target contracts for validator/effect bindings, and return sorted structured findings. Keep `compile_packaged_workflow` on its pre-activation compatibility path until Task 15.
- [ ] **Step 11: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/healing/test_episode_projection.py \
    tests/unit/healing/test_allocation.py \
    tests/unit/healing/test_record_apply.py \
    tests/unit/test_healing_state.py \
    tests/unit/retro/test_workflow_history.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/integration/test_codegen_fixer_record.py
  uv run ruff check assurance_agent/workflow/healing assurance_agent/workflow/graph/healing_conformance.py assurance_agent/workflow/orchestration/healing_state.py assurance_agent/retro/workflow_history.py tests/unit/healing tests/unit/workflow/graph/test_healing_topology_mutations.py
  uv run pyright
  ```

  Expected: all legacy/v2/mixed and dormant-operation tests pass; packaged YAML still references the old route.
- [ ] **Step 12: Commit normalized healing and dormant operations**

  ```bash
  git add assurance_agent/workflow/healing/projection.py \
    assurance_agent/workflow/healing/effects.py \
    assurance_agent/workflow/graph/healing_conformance.py \
    assurance_agent/workflow/core/events.py \
    assurance_agent/workflow/healing/allocation.py \
    assurance_agent/workflow/healing/safety.py \
    assurance_agent/workflow/orchestration/healing_state.py \
    assurance_agent/retro/workflow_history.py \
    assurance_agent/workflow/graph/handlers/operation.py \
    assurance_agent/workflow/graph/durable_effects.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/graph/scheduler.py \
    tests/unit/healing/test_episode_projection.py \
    tests/unit/healing/test_allocation.py \
    tests/unit/healing/test_record_apply.py \
    tests/unit/test_healing_state.py \
    tests/unit/retro/test_workflow_history.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/integration/test_codegen_fixer_record.py
  git commit -m "feat(healing): normalize episodes and stage graph-owned records"
  ```

### Task 9: Separate Historical Role Discovery from Current Conformance

**Files:**
- Create: `assurance_agent/workflow/graph/historical_roles.py`
- Create: `assurance_agent/workflow/graph/historical_topology_v6.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `assurance_agent/workflow/graph/replay_schema.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/eval/specialty_replay.py`
- Create: `tests/unit/workflow/graph/test_historical_roles.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/workflow/graph/test_replay_schema.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `tests/unit/workflow/graph/test_four_layer_replay.py`
- Modify: `tests/unit/workflow/graph/test_compiler.py`
- Modify: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`
- Create: `tests/unit/eval/test_specialty_replay.py`

**Interfaces:**
- Produces: the D10 `DiscoveredHistoricalLayerRoles`/`DiscoveredHistoricalAssuranceRoles` manifest, `ResolvedPinnedDefinition.historical_roles`/`FrozenDefinitionBinding.historical_roles` carriers, structured discovery issues, and the v6 semantic classifier.
- Preserves: frozen v1-v3 compatibility, frozen v4 display path, and byte-for-byte frozen v5 display outcomes including known false negatives.
- Consumes: pinned schema, pinned contracts/catalog/profile, graph references, aliases, operation/skill contracts, artifacts, routes, dependencies, and pure topology-analysis helpers.

- [ ] **Step 1: Freeze v4/v5 golden display results**

  Capture marker-free, safe-wired, and known-bypass fixtures under their current v4/v5 dispatch. Assert exact pairs `("legacy_v4_unbound", false)` and `("legacy_v5_unbound", false)` for `(semantics_id, semantics_bound)` plus frozen status; these goldens must remain unchanged after v6 implementation.
- [ ] **Step 2: Add historical discovery positive controls**

  Consistently rename every graph, node, alias, and equivalent predicate in a safe pinned graph. Assert one manifest with stable canonical digest drives historical compile, layer selection, `_bind_assurance_invocation`, trace binding, and classification without current IDs.
- [ ] **Step 3: Add v6 safety mutations**

  Cover unknown builtin/unbounded parameter, duplicate role, ambiguous alias, safe activation narrowing, direct codegen bypass, skip/recovery bypass, missing precondition dominance, unaudited remediation, mixed safe/unsafe path, and zero markers. Expected statuses are exactly `wired`, `partial`, or `legacy_unwired` with stable discovery/safety codes.
- [ ] **Step 4: Run tests and observe current-name coupling**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_historical_roles.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_replay_schema.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/graph/test_four_layer_replay.py \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/eval/test_specialty_replay.py
  ```

  Expected: renamed safe graphs fail current name lookup and v5 is incorrectly routed through the newest classifier.
- [ ] **Step 5: Implement one semantic discovery manifest**

  Discover lineages once in `load_pinned_execution_definition(...)` from references/contracts/artifacts/routes/dependencies. Return fail-closed issues for missing/duplicate/ambiguous roles. Make roles a required `HistoricalCompileContext` input, attach them to `ResolvedPinnedDefinition`, preserve them in `FrozenDefinitionBinding`, and pass that same value into compilation, selection, replay/trace binding, audit-trigger, classifier, and `eval/specialty_replay.py`; remove its second `evaluate_layer_selection(...)` call plus every other name-based rediscovery helper. Direct compiler controls construct explicit fixture roles and prove a `partial` historical classification remains reportable rather than becoming a current compile gate.
- [ ] **Step 6: Freeze legacy dispatch and add v6 classifier**

  Move the existing v4/v5 behavior behind explicit version functions without improving it. The v6 classifier asks the one-sided safety question over the pinned finite domain and uses CFG dominance rather than current names. It may report `partial`; it is replay authorization evidence, not a current compile validator.
- [ ] **Step 7: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_historical_roles.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_replay_schema.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/graph/test_four_layer_replay.py \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/eval/test_specialty_replay.py
  uv run ruff check assurance_agent/workflow/graph/historical_roles.py assurance_agent/workflow/graph/historical_topology_v6.py assurance_agent/workflow/graph/definition_pinning.py assurance_agent/workflow/graph/replay_schema.py assurance_agent/workflow/graph/replay_binding.py assurance_agent/eval/specialty_replay.py
  uv run pyright
  ```

  Expected: rename/equivalence controls pass, bypasses are partial, and all v4/v5 goldens remain frozen.
- [ ] **Step 8: Commit the historical semantic boundary**

  ```bash
  git add assurance_agent/workflow/graph/historical_roles.py \
    assurance_agent/workflow/graph/historical_topology_v6.py \
    assurance_agent/workflow/graph/definition_pinning.py \
    assurance_agent/workflow/graph/replay_schema.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/graph/compiler.py \
    assurance_agent/eval/specialty_replay.py \
    tests/unit/workflow/graph/test_historical_roles.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_replay_schema.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/graph/test_four_layer_replay.py \
    tests/unit/workflow/graph/test_compiler.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/eval/test_specialty_replay.py
  git commit -m "refactor(graph): separate historical assurance semantics"
  ```

### Task 10: Build the Three Closed Semantics Manifests

**Files:**
- Create: `assurance_agent/workflow/graph/topology_semantics.py`
- Create: `assurance_agent/workflow/graph/runtime_commit_safety.py`
- Modify: `assurance_agent/workflow/orchestration/gate_semantics.py`
- Modify: `assurance_agent/workflow/graph/precommit.py`
- Modify: `assurance_agent/workflow/graph/durable_effects.py`
- Modify: `assurance_agent/workflow/graph/effect_retry.py`
- Modify: `assurance_agent/workflow/healing/effects.py`
- Create: `tests/unit/workflow/graph/test_topology_semantics.py`
- Create: `tests/unit/workflow/graph/test_runtime_commit_safety.py`
- Modify: `tests/unit/workflow/orchestration/test_gate_semantics_manifest.py`

**Interfaces:**
- Produces: canonical descriptor bytes/object digest APIs for `plan_gate_semantics/v1`, `historical_topology_safety/v1`, and `runtime_commit_safety/v1`.
- Consumes: exact code/dependency inventories from Tasks 3, 6, 7, 8, and 9.
- Defers: root/child definition fields and event schema v6 are implemented in Task 11.

- [ ] **Step 1: Add manifest canonicalization tests**

  Assert each semantic ID, runtime/schema version, sorted dependency/consumer inventory, canonical bytes, object digest, and semantic digest. The gate manifest must now be recoverable bytes, not a digest-only function.
- [ ] **Step 2: Add closed consumer-set tests**

  Use AST/import inspection to require every registered validator/effect kind, the retry sidecar, the complete root guard/prepared/committed terminal-fence protocol, and every declared helper exactly once in the commit-safety inventory. Require topology discovery/CFG/dominance/truth-table/status/runtime-version dependencies in the topology inventory. Reject an unregistered implementation or a listed-but-unconsumed field. Digest named semantic definitions/descriptors, not unrelated bytes from a mutable containing module, so a later caller of the frozen fence API does not change `runtime_commit_safety/v1`.
- [ ] **Step 3: Add mutate-each-dependency tests**

  Parameterize every inventory member, replace its canonical source digest with a distinct valid digest, and assert the aggregate semantic digest changes. Also assert reordering keys or source files without byte changes does not alter canonical output.
- [ ] **Step 4: Run tests and observe missing canonical objects**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/orchestration/test_gate_semantics_manifest.py \
    tests/unit/workflow/graph/test_topology_semantics.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  ```

  Expected: gate bytes API and both new manifests are missing.
- [ ] **Step 5: Implement canonical closed manifests**

  Derive dependency source identities deterministically and keep inventory constants next to their registry. Do not introspect installed package paths at runtime or accept caller-supplied dependency lists.
- [ ] **Step 6: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/orchestration/test_gate_semantics_manifest.py \
    tests/unit/workflow/graph/test_topology_semantics.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  uv run ruff check assurance_agent/workflow/orchestration/gate_semantics.py assurance_agent/workflow/graph/topology_semantics.py assurance_agent/workflow/graph/runtime_commit_safety.py tests/unit/workflow/graph
  uv run pyright
  ```

  Expected: all three manifests have stable canonical bytes and closed mutate-each coverage.
- [ ] **Step 7: Commit the semantics objects**

  ```bash
  git add assurance_agent/workflow/graph/topology_semantics.py \
    assurance_agent/workflow/graph/runtime_commit_safety.py \
    assurance_agent/workflow/orchestration/gate_semantics.py \
    assurance_agent/workflow/graph/precommit.py \
    assurance_agent/workflow/graph/durable_effects.py \
    assurance_agent/workflow/graph/effect_retry.py \
    assurance_agent/workflow/healing/effects.py \
    tests/unit/workflow/graph/test_topology_semantics.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py \
    tests/unit/workflow/orchestration/test_gate_semantics_manifest.py
  git commit -m "feat(graph): define closed runtime semantics manifests"
  ```

### Task 11: Pin All Three Semantics Objects and Flip New Roots to Event Schema V6

**Files:**
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/core/migrate_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/definition_pinning.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/driver/runtime_factory.py`
- Modify: `tests/unit/workflow/graph/test_policy_digest_event.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/workflow/graph/test_checkpoint.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `tests/unit/workflow/driver/test_runtime_factory_compilation.py`
- Modify: `tests/unit/test_events.py`
- Modify: `tests/helpers_graph_v3.py`
- Modify: `tests/integration/test_graph_interrupt_v3.py`
- Modify: `tests/integration/test_graph_runtime.py`
- Create: `tests/fixtures/workflow/graph-events-v6.jsonl`

**Interfaces:**
- Produces: v6 `PinnedDefinitionRequest`, `InvocationDefinitionBinding`, root/child event fields, staged semantic object paths, full verification, and exact child inheritance.
- Consumes: canonical gate/topology/commit-safety bytes/digests from Task 10 and the assurance profile/policy/schema/contracts/catalog identities already pinned.
- Preserves: v1-v5 parsing, migration, projection, and live/report compatibility; no v1-v5 root receives synthetic semantic identities.

- [ ] **Step 1: Add v6 model all-or-none tests**

  At the pure event-model layer, require the six v6 fields all-or-none and keep v1-v5 fixtures model-valid without them. Put unknown semantic ID, object-byte/digest mismatch, and child/root identity checks in `verify_pinned_definitions(...)` and the production append path, where the object store is available; do not introduce a `workflow.core -> workflow.graph/storage` dependency.
- [ ] **Step 2: Add staging, write-once, and tamper tests**

  Stage all three canonical objects under the root definition bundle. Assert digest-addressed write-once paths, exact byte reload, root request identity, and rejection when bytes, object IDs, or semantic digests are changed independently.
- [ ] **Step 3: Add inheritance and consumer-field coverage**

  Cover assurance child, layer-cycle child, retro child, issue-review child, and improvement-review child. Assert the exact six fields in request creation, root/child start serialization, projection, child inheritance, pinned load, live compatibility, replay compatibility, snapshot definition context, and validator/effect dispatch. Register the phase-local evidence-export consumer obligation for Task 17 instead of importing a not-yet-existing exporter. A non-assurance child must inherit without invoking assurance classification.
- [ ] **Step 4: Add version-dispatch compatibility tests**

  Assert v1-v3 stay report-only; v4 reports `legacy_v4_unbound/false`; v5 reports `legacy_v5_unbound/false`; and only v6 with loaded, digest-verified topology bytes reports `historical_topology_safety/v1/true` and calls the new classifier. Unknown or unverified v6 semantic bytes fail before definition-dependent recovery.
- [ ] **Step 5: Run tests and observe missing v6 fields/staging**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_policy_digest_event.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/driver/test_runtime_factory_compilation.py \
    tests/unit/test_events.py \
    tests/integration/test_graph_interrupt_v3.py \
    tests/integration/test_graph_runtime.py
  ```

  Expected: new v6 binding assertions fail while all old fixture assertions remain green.
- [ ] **Step 6: Extend definition requests, snapshots, and projections**

  Add exact object ID/digest pairs to request/binding/event/projection models, `request_for_compiled`, `bind_root_definitions`, `inherit_child_definitions`, `stage_pinned_definitions`, `verify_pinned_definitions`, replay/load request builders, and `_same_definition_epoch`. Load bytes and recompute digests; never trust event strings alone.
- [ ] **Step 7: Update migration and event goldens**

  Permit schema version 6 only with complete bindings. Keep existing v1-v5 fixture bytes and expected folds unchanged; add separate v6 goldens rather than rewriting old roots.
- [ ] **Step 8: Flip current root/import writers from 5 to 6**

  Change both `GraphRuntime` root creation/import paths and `runtime_factory.one_definition_resolver` only after Steps 1-7 pass. The append boundary first verifies all three staged objects, then constructs/appends the v6 event. Update `tests/helpers_graph_v3.py`, `test_graph_interrupt_v3.py`, and `test_graph_runtime.py` so fresh-root assertions require v6 plus six complete fields while explicit legacy helpers continue to construct v5 fixtures.
- [ ] **Step 9: Run the compatibility matrix and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_policy_digest_event.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_replay_schema.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/graph/test_four_layer_replay.py \
    tests/unit/workflow/driver/test_runtime_factory_compilation.py \
    tests/unit/test_events.py \
    tests/integration/test_graph_interrupt_v3.py \
    tests/integration/test_graph_runtime.py
  uv run ruff check assurance_agent/workflow/core assurance_agent/workflow/graph assurance_agent/workflow/driver/runtime_factory.py tests/unit/workflow
  uv run pyright
  ```

  Expected: new roots are v6 with complete verified bindings; v1-v5 goldens and non-assurance children pass unchanged.
- [ ] **Step 10: Commit the v6 definition epoch**

  ```bash
  git add assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/core/migrate_events.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/graph/compiler.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/definition_pinning.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/driver/runtime_factory.py \
    tests/unit/workflow/graph/test_policy_digest_event.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_checkpoint.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/driver/test_runtime_factory_compilation.py \
    tests/unit/test_events.py \
    tests/helpers_graph_v3.py \
    tests/integration/test_graph_interrupt_v3.py \
    tests/integration/test_graph_runtime.py \
    tests/fixtures/workflow/graph-events-v6.jsonl
  git commit -m "feat(graph): bind runtime semantics in event schema v6"
  ```

### Task 12: Audit Pending V4/V5 Assurance Paths and Block Unbound Commit Safety

**Files:**
- Create: `assurance_agent/workflow/graph/resume_compatibility.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/driver/driver_state.py`
- Create: `tests/unit/workflow/graph/test_resume_compatibility.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`

**Interfaces:**
- Produces: `TopologyCompatibilityReceiptV1`, append-only receipt event, `ResumeCompatibilityDecision`, and stable reason `legacy_commit_safety_semantics_unbound`.
- Consumes: exact pinned v4/v5 definition bundle, Task 9's historical role manifest/classifier, staged v6 topology semantics used for the audit, reconstructed v4/v5 profile, reachable remaining task set, outstanding succeeded-but-uncommitted write sets/publications/effects, and current validator/effect-bearing contract registry.
- Preserves: report/terminal-only remaining work may continue under old rules; unsafe/unreconstructable topology still fails before planning; no old root is rewritten.

- [ ] **Step 1: Add safe/bypass audit-trigger tests**

  Use safe and dangerous v4/v5 pending roots whose frozen display classifier includes a known false negative. Assert the audit trigger comes from discovered reachable assurance roles, not display status. Safe topology appends/reuses one bound receipt; bypass or unreconstructable v4 profile cannot.
- [ ] **Step 2: Add commit-safety sufficiency tests**

  A topology receipt authorizes report/terminal-only work but never pending codegen, codegen-fixer, allocation/approval/record effect operations, any current validator/effect-bearing task, or recovery of a succeeded-but-uncommitted assurance write/publication/effect. Add v4/v5 `success-before-superstep` and `success-before-publication` controls. Those return the exact typed reason before handler dispatch, write recovery that depends on current semantics, or effect reconciliation.
- [ ] **Step 3: Add receipt integrity/idempotency tests**

  Bind root, exact pinned bundle, discovered-role digest, topology semantic object/digest, audit result, reachable set digest, and event source sequence. Exact replay is idempotent; same identity/different payload and receipt from another root are corruption.
- [ ] **Step 4: Run tests and observe the current permissive resume path**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/integration/test_graph_runtime_faults.py
  ```

  Expected: pending legacy codegen currently reaches current handler code after definition loading.
- [ ] **Step 5: Implement audit and typed recovery barrier**

  Place compatibility evaluation before any definition-dependent recovery/dispatch in `_reach_recovery_barrier`. Build the receipt only from the pinned bundle plus staged audit semantics. Surface the typed decision through driver status without parsing exception text.
- [ ] **Step 6: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/integration/test_graph_runtime_faults.py
  uv run ruff check assurance_agent/workflow/graph/resume_compatibility.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/driver/driver_state.py tests/unit/workflow/graph/test_resume_compatibility.py
  uv run pyright
  ```

  Expected: safe report-only roots continue, every remaining commit-safety-bearing legacy path stops with the stable reason, and no handler is called.
- [ ] **Step 7: Commit legacy resume authorization**

  ```bash
  git add assurance_agent/workflow/graph/resume_compatibility.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/driver/driver_state.py \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/integration/test_graph_runtime_faults.py
  git commit -m "feat(graph): audit legacy assurance resume safety"
  ```

### Task 13: Add Audited Legacy-Root Supersede and Single-Use V6 Replacement

**Files:**
- Create: `assurance_agent/workflow/graph/supersede.py`
- Modify: `assurance_agent/workflow/core/graph_events.py`
- Modify: `assurance_agent/workflow/graph/models.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/status.py`
- Modify: `assurance_agent/workflow/driver/driver_state.py`
- Modify: `assurance_agent/commands/workflow_cmd.py`
- Create: `tests/unit/workflow/graph/test_supersede.py`
- Modify: `tests/integration/test_cli_workflow_v2.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`

**Interfaces:**
- Produces: `SupersedeAction`, `SupersedeEligibility`, `GraphInvocationSupersededEvent`, subtree digest/fence, `GraphRuntime.supersede(...)`, replacement authorization consumption, and `aa workflow supersede`.
- Consumes: latest active root, typed legacy-block decision, descendant invocation closure, lease/open-attempt/superstep/write/publication/effect/retry state, Task 7's already-frozen `RootEffectFenceStore`, current v6 staged request, resolved canonical params, operator identity, and reason.
- Preserves: normal active-root and `restart: once` guards. Only the exact unused replacement authorization bypasses them.

- [ ] **Step 1: Add CLI shape and eligibility tests**

  Accept exactly `--change`, `--invocation`, `--action rerun-v6|stop`, non-empty `--who`, non-empty `--reason`, and optional JSON `--params` only for rerun. Reject child ID, other change/entrypoint, non-latest or unrelated terminal root, non-blocked root, stop with params, invalid params, or a staged request that is not v6.
- [ ] **Step 2: Add complete subtree quiescence and retry-fence tests**

  Reject any live lease, open/running attempt, prepared/uncommitted superstep or write set, pending synchronized publication, unacknowledged effect, active retry sidecar, or concurrent child creation anywhere in the canonical descendant closure. Consume the frozen root guard/prepare/commit protocol from Task 7; `GraphRuntime.supersede(...)`, effect reconciliation, and `EffectRetryStore.schedule_next(...)` all follow the already-tested guard-before-progression order. Race a due retry against supersede and prove neither can create sidecar state after preparation/commit or deadlock. Assert a real v6 root pinned before this task retains byte-identical `runtime_commit_safety/v1` bytes/digest and passes compatibility across the new caller.
- [ ] **Step 3: Add terminal-fence tests**

  After supersede, root and all descendants project stopped/superseded for scheduling; direct child resume returns the stable superseded result; recovery does not adopt a descendant; late worker success/publication/effect acknowledgement is rejected by the fence. Existing status renderers may say `stopped`, but the typed audit event remains queryable.
- [ ] **Step 4: Add replacement/crash/concurrency tests**

  Cover before append, after append before new root start, after root start before return, two concurrent exact commands, conflicting params/reason, generic run without authorization, import-checkpoint, another entrypoint, and second consumption. Exact retry must start or resume at most one replacement root bound to the superseded root, authorization, params digest, and staged definition-request digest.
- [ ] **Step 5: Run tests and observe the permanent-active-root behavior**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  ```

  Expected: command/API are absent and the ordinary active guard prevents a fresh v6 root.
- [ ] **Step 6: Implement two durable transactions for fence and authorization consumption**

  Stage and validate the exact current v6 request before terminalizing. First acquire `RootEffectFenceStore.guard`, write a prepared terminal-fence CAS, then acquire the progression lock, rescan eligibility, and durably append one deterministic supersede event/authorization; commit the terminal fence before releasing the root guard. A crash/retry resolves a prepared state from the authoritative event: commit it when the event exists, otherwise the same command may safely abort it. Second, under a new progression transaction, atomically consume that authorization and append or recover the one replacement root-start pair. A crash between transactions is intentional and retryable; do not re-enter the progression lock. Load the already-staged request by digest on retry.
- [ ] **Step 7: Wire the CLI to typed results**

  Never infer eligibility from error text. Return stable nonzero exits for ineligible/conflicting requests and the new/resumed invocation ID for rerun. `stop` produces no replacement authority.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  uv run ruff check assurance_agent/workflow/graph/supersede.py assurance_agent/workflow/graph/runtime.py assurance_agent/commands/workflow_cmd.py tests/unit/workflow/graph/test_supersede.py
  uv run pyright
  ```

  Expected: every named crash/concurrency cut converges to one fence and at most one replacement root.
- [ ] **Step 9: Commit the audited operator exit**

  ```bash
  git add assurance_agent/workflow/graph/supersede.py \
    assurance_agent/workflow/core/graph_events.py \
    assurance_agent/workflow/graph/models.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/status.py \
    assurance_agent/workflow/driver/driver_state.py \
    assurance_agent/commands/workflow_cmd.py \
    tests/unit/workflow/graph/test_supersede.py \
    tests/integration/test_cli_workflow_v2.py \
    tests/integration/test_graph_runtime_faults.py
  git commit -m "feat(workflow): add audited legacy root supersede"
  ```

### Task 14: Dark-Ship Exact Persona Mapping and Prove Attempt Workspace Requests

**Files:**
- Create: `assurance_agent/workflow/graph/assurance_personas.py`
- Modify: `assurance_agent/workflow/driver/opencode_adapter.py`
- Modify: `tests/unit/driver/test_opencode_adapter.py`

**Interfaces:**
- Produces: exact dormant `ASSURANCE_PERSONA_BY_TARGET`, `expected_assurance_persona(target)`, and request-directory continuity tests.
- Consumes: the sixteen exact skill targets, `AgentRequest.workspace_root`, packaged persona documents, and mocked OpenCode create/prompt/status transport.
- Preserves: current handler routing and packaged persona permissions. Task 15 switches the handler to the registry and tightens persona documents in the atomic activation; no external OpenCode dependency enters CI.

- [ ] **Step 1: Add the exact target/persona table tests**

  Require planners plus API/E2E plan fixers to `aa-doc-author`, four reviewers to `aa-reviewer`, and four codegen plus API/E2E codegen fixers to `aa-test-author`. Reject a missing target, extra assurance-like target, and schema persona mismatch. Do not change handler fallback in this task.
- [ ] **Step 2: Add request-directory continuity tests**

  Mock transport and assert session creation, prompt dispatch, every status poll, and reconnect use exactly `AgentRequest.workspace_root`. Reject a response/status request made with host root, change root, or a prior attempt's directory.
- [ ] **Step 3: Run tests and observe any request-directory gaps**

  ```bash
  uv run pytest -q \
    tests/unit/driver/test_opencode_adapter.py
  ```

  Expected: the new mapping tests fail until the registry exists; directory tests pass only if create, prompt, status, and reconnect all use the attempt root.
- [ ] **Step 4: Implement the dormant mapping and request validation**

  Define the exact registry without importing it from `AgentHandler` yet. Preserve current OpenCode calls while making directory propagation explicit and tested at every request boundary.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/driver/test_opencode_adapter.py
  uv run ruff check assurance_agent/workflow/graph/assurance_personas.py assurance_agent/workflow/driver/opencode_adapter.py tests/unit/driver/test_opencode_adapter.py
  uv run pyright
  ```

  Expected: exact persona and directory tests pass for all sixteen targets.
- [ ] **Step 6: Commit dormant persona mapping and adapter coverage**

  ```bash
  git add assurance_agent/workflow/graph/assurance_personas.py \
    assurance_agent/workflow/driver/opencode_adapter.py \
    tests/unit/driver/test_opencode_adapter.py
  git commit -m "feat(agent): define assurance personas and pin attempt directories"
  ```

### Task 15: Atomically Activate V6 Assurance Contracts, Manifests, and Healing Topology

**Files:**
- Modify: `assurance_agent/_resources/schemas/workflow-schema.yaml`
- Modify: `assurance_agent/_resources/schemas/execution-contracts.yaml`
- Modify: `assurance_agent/_resources/schemas/ingest-artifact-catalog.yaml`
- Modify: `assurance_agent/_resources/skills/aa-api-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-plan-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-api-codegen-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-plan-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-e2e-codegen-fixer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-fuzz-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-plan/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-plan-reviewer/SKILL.md`
- Modify: `assurance_agent/_resources/skills/aa-performance-codegen/SKILL.md`
- Modify: `assurance_agent/_resources/opencode/agents/aa-doc-author.md`
- Modify: `assurance_agent/_resources/opencode/agents/aa-reviewer.md`
- Modify: `assurance_agent/_resources/opencode/agents/aa-test-author.md`
- Modify: `assurance_agent/artifacts/registry.py`
- Modify: `assurance_agent/workflow/graph/ingest_catalog.py`
- Modify: `assurance_agent/workflow/graph/compiler.py`
- Modify: `assurance_agent/workflow/graph/handlers/agent.py`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L3-run-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L3-run-done.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/api-generated-files.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/e2e-generated-files.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/fuzz-generated-files.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/performance-generated-files.json`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json`
- Modify: `tests/unit/artifacts/test_registry.py`
- Modify: `tests/unit/verification/test_assurance_contract_round_trip.py`
- Modify: `tests/unit/verification/test_assurance_contract_mutations.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_ingest.py`
- Modify: `tests/unit/workflow/graph/test_packaged_schema_compiles.py`
- Modify: `tests/unit/workflow/graph/test_assurance_topology_mutations.py`
- Modify: `tests/unit/workflow/graph/test_healing_topology_mutations.py`
- Modify: `tests/unit/workflow/graph/test_canonical_schema_v2.py`
- Modify: `tests/unit/workflow/graph/test_resume_compatibility.py`
- Modify: `tests/unit/workflow/graph/test_read_isolation.py`
- Modify: `tests/unit/workflow/graph/test_task_runner.py`
- Modify: `tests/unit/test_opencode_register.py`
- Modify: `tests/unit/test_skills_slimming.py`
- Modify: `tests/unit/test_fuzz_performance_skills.py`
- Modify: `tests/unit/test_skills_content.py`
- Modify: `tests/unit/eval/test_fixtures.py`
- Modify: `tests/unit/eval/test_eval_import_replay.py`
- Modify: `tests/integration/test_codegen_fixer_record.py`
- Modify: `tests/integration/test_eval_workflow_run_synth.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`

**Interfaces:**
- Produces: the complete current v6 contract bundle: sixteen declared-only skills, exact contract parity, four generated-file hard outputs, two validator contract sets, four durable-effect producers, typed plan-fixer contexts, strict current assurance/healing compilation, and the graph-owned healing topology.
- Consumes: every dormant primitive from Tasks 1-14 and the v6 root binding from Task 11.
- Preserves: the public graph remains the only router; API/E2E automatic healing remains available only with physical authority; imported unverified codegen stops explicitly; existing complete L2/L3 success/run paths remain compatible.

- [ ] **Step 1: Add the activation-only structural and runtime assertions**

  Extend the green Task 2 harness to require exact `## Inputs`, `## Outputs`, `## State Authority`, and plan-fixer `## Runtime Context` sections; bidirectional read/write/authorization closure; typed platform-only exemptions; real declared-only workspace visibility; and zero forbidden state/command tokens across all sixteen skills. Migrate the existing slimming/content tests from their old exact heading tuples and `workflow-state.yaml` prose expectations to canonical headings plus `owner: graph_ledger` and `agent_state_writes: forbidden`; preserve their semantic responsibility assertions.
- [ ] **Step 2: Repair all sixteen skill contracts**

  Set `owner: graph_ledger` and `agent_state_writes: forbidden`; remove `workflow-state.yaml`, `phases.`, plan-fixer `events.jsonl`, codegen collection claims, and codegen-fixer `aa heal record-apply`. Use `tests/testdata/domain/**`. Require Fuzz plans to emit the strict Test Function Mapping plus Schema Acquisition fields and Performance plans to emit Task Mapping with Target File; reviewers consume the same structure. Add exact generated-file manifests to four codegen Outputs, typed Runtime Context to two plan fixers, and only target intent plus authorized tests to two codegen-fixer Outputs.
- [ ] **Step 2a: Activate exact persona authority and permission floors**

  Make `AgentHandler` validate the schema-selected persona through `ASSURANCE_PERSONA_BY_TARGET` for all sixteen targets. Tighten packaged persona documents to deny-first edit policy and `external_directory: deny`; `aa-test-author` permits only the four private roots plus `tests/testdata/**`, never broad `**tests/**`. Keep non-assurance routing unchanged.
- [ ] **Step 3: Close all sixteen execution contracts and enable isolation**

  Add only structurally declared inputs; reject `repo:**`, cross-change, sibling-layer, or broad product/test reads. Make writes and authorization cover the same agent-owned logical set plus typed platform exemptions. Set `read_isolation: declared_only` on exactly the sixteen targets. API/E2E planner writes are their four fixed plan/summary files plus one exact conditional knowledge proposal.
- [ ] **Step 4: Register strict artifacts and hard outputs**

  Add four path-specific generated-file catalog/model entries and distinct fixer authority, API/E2E intent, approval, API/E2E target summary/safety, and aggregate safety entries. Change each codegen node to require summary plus manifest. Change fixer/record/combine outputs to the exact sets in design §5.3. Update `tests/unit/artifacts/test_registry.py` from its pre-activation Task 1 expectation to the exact activated path/model set; do not retain a brittle registry-length shortcut.
- [ ] **Step 4a: Migrate complete benchmark imports in the same activation unit**

  Freeze one strict valid manifest for each existing sample codegen output and add it to all six complete import chains (`L2-{api,e2e,fuzz,performance}-codegen-seed`, `L3-run-seed`, and `L3-run-done`) wherever the selected codegen node now requires summary+manifest. Recompute `fixture-lock.json`. Seed and import every old L2/L3 tier through production fixture/import code, including a synthesized workflow run, so the schema/output activation cannot break existing complete histories. This is compatibility migration only: do not add pending tiers, switch datasets/suites, or activate benchmark metrics here.
- [ ] **Step 5: Select validators and effects only on v6-bound contracts**

  Set `generated_files_candidate/v1` on exactly four codegen contracts and `codegen_fix_candidate/v1` on exactly two codegen-fixer contracts. Bind allocation to `healing_allocation/v2`, approval record to `fixer_proposal_approved/v1`, and API/E2E record operations to `heal_record_apply/v2`. `compile_packaged_workflow(schema, contracts)` must receive a non-null decoded catalog, validate the exact producer-to-validator/effect mapping, and fail closed when `contracts is None`; historical compilation remains readable/reportable and does not call current conformance. Task 12 alone blocks pending v1-v5 dispatch with `legacy_commit_safety_semantics_unbound`.
- [ ] **Step 5a: Activate without mutating the frozen allocation compatibility seam**

  Task 8 already froze the versioned compatibility reconciler for the exact pre-activation allocation-contract digest with `durable_effects == ()`; do not edit that scheduler/effect semantic definition here. This lets a root created in Tasks 11-12 cross Task 13 and this activation with byte-identical pinned `runtime_commit_safety/v1`. The newly selected packaged allocation contract persists exclusively through `healing_allocation/v2`, and no current success path may call `commit_healing_allocation_ledger(...)`. Add that real old-v6 root plus recovery controls on both sides of allocation success and current-package negative reachability of the compatibility hook.
  Re-run the Task 12 barrier against the newly activated packaged catalog: pending v4/v5 codegen, fixer, allocation, approval, and record work must return `legacy_commit_safety_semantics_unbound` before dispatch/recovery, while report/terminal-only work remains compatible.
- [ ] **Step 6: Replace the healing graph atomically**

  Encode exactly:

  ```text
  allocate -> fixer-authority-ready
    pass -> fixer-proposal-approval
      pass -> fixer-dispatch -> fix-api -> record-api \
                              -> fix-e2e -> record-e2e -> fixer-join
      needs_human_review -> fixer-approval-interrupt
        approve_and_apply -> record-fixer-approval -> fixer-proposal-approval
        stop -> complete-failed
    stop -> complete-failed
  fixer-join -> combine-fixer-safety -> safety
  ```

  `fixer-dispatch` has no hard outputs and fans out through exactly two proposal guards. Join mode is `all_active` over record nodes, not fixer nodes. High risk cannot pass without the exact graph-owned approval receipt.
  In the packaged graph, parameterize allocation, high-risk approval, and each active target record across success-before-superstep, superstep-before-domain, domain-before-ack, and ack-before-successor boundaries. Use the real effect registry and retry store; exact restarts produce one event/ack and never reinvoke a recorded successful handler.
- [ ] **Step 7: Complete current four-layer topology fixes**

  Add independent API/E2E capability atoms to codegen preconditions, retain fail-closed gate/precondition ordering, exact aliases/routes/interrupts/remediation, and all-active generation join. Invoke both structured current conformance validators only from `compile_packaged_workflow`; generic/project/historical compilation must not call them.
  Update `test_canonical_schema_v2.py` from the old direct allocate-to-fixer topology to the exact approval/dispatch/record/join graph so no stale topology assertion survives activation.
- [ ] **Step 8: Enforce no-host-link/no-convenience-Git execution**

  Task 4 already made host-link and convenience-Git behavior conditional on `read_isolation`; activation now comes only from the sixteen contract flips. In `AgentHandler`, also switch persona selection to Task 14's exact registry. Repeat real read/traverse/write attempts against `.git`, `.venv`, `node_modules`, ledger, coordinator, and runtime-control former locations and prove their host bytes/modes/link metadata stay unchanged; every start/success binds one valid snapshot.
- [ ] **Step 9: Run the atomic activation suite**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/artifacts/test_registry.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_ingest.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_canonical_schema_v2.py \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_read_isolation.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/unit/test_opencode_register.py \
    tests/unit/test_skills_slimming.py \
    tests/unit/test_fuzz_performance_skills.py \
    tests/unit/test_skills_content.py \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_eval_import_replay.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/integration/test_eval_workflow_run_synth.py \
    tests/integration/test_graph_runtime_faults.py
  uv run ruff check assurance_agent tests/helpers_assurance_contract.py tests/unit/verification tests/unit/workflow/graph
  uv run pyright
  uv run lint-imports
  ```

  Expected: all activation tests pass together; packaged allocation/approval/record success-to-commit-to-domain-to-ack cuts converge; old complete L2/L3 imports load their frozen manifests; no packaged contract can advertise isolation without snapshot/no-host enforcement or select runtime safety without a v6 binding.
- [ ] **Step 10: Inspect the one activation diff as a release unit**

  Verify the cached diff contains every listed skill/schema/catalog/runtime hook plus only the six compatibility tier manifests, four frozen generated-file manifests, fixture lock, and their import regressions from the eval/benchmark area. It must not contain pending tiers, dataset/suite switches, or scorer activation. Reject a partial staged set.
- [ ] **Step 11: Commit the atomic v6 activation**

  ```bash
  git add assurance_agent/_resources/schemas/workflow-schema.yaml \
    assurance_agent/_resources/schemas/execution-contracts.yaml \
    assurance_agent/_resources/schemas/ingest-artifact-catalog.yaml \
    assurance_agent/_resources/skills/aa-api-plan/SKILL.md \
    assurance_agent/_resources/skills/aa-api-plan-reviewer/SKILL.md \
    assurance_agent/_resources/skills/aa-api-plan-fixer/SKILL.md \
    assurance_agent/_resources/skills/aa-api-codegen/SKILL.md \
    assurance_agent/_resources/skills/aa-api-codegen-fixer/SKILL.md \
    assurance_agent/_resources/skills/aa-e2e-plan/SKILL.md \
    assurance_agent/_resources/skills/aa-e2e-plan-reviewer/SKILL.md \
    assurance_agent/_resources/skills/aa-e2e-plan-fixer/SKILL.md \
    assurance_agent/_resources/skills/aa-e2e-codegen/SKILL.md \
    assurance_agent/_resources/skills/aa-e2e-codegen-fixer/SKILL.md \
    assurance_agent/_resources/skills/aa-fuzz-plan/SKILL.md \
    assurance_agent/_resources/skills/aa-fuzz-plan-reviewer/SKILL.md \
    assurance_agent/_resources/skills/aa-fuzz-codegen/SKILL.md \
    assurance_agent/_resources/skills/aa-performance-plan/SKILL.md \
    assurance_agent/_resources/skills/aa-performance-plan-reviewer/SKILL.md \
    assurance_agent/_resources/skills/aa-performance-codegen/SKILL.md \
    assurance_agent/_resources/opencode/agents/aa-doc-author.md \
    assurance_agent/_resources/opencode/agents/aa-reviewer.md \
    assurance_agent/_resources/opencode/agents/aa-test-author.md \
    assurance_agent/artifacts/registry.py \
    assurance_agent/workflow/graph/ingest_catalog.py \
    assurance_agent/workflow/graph/compiler.py \
    assurance_agent/workflow/graph/handlers/agent.py \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L3-run-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L3-run-done.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/api-generated-files.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/e2e-generated-files.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/fuzz-generated-files.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/codegen/performance-generated-files.json \
    benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json \
    tests/unit/artifacts/test_registry.py \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_ingest.py \
    tests/unit/workflow/graph/test_packaged_schema_compiles.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_canonical_schema_v2.py \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_read_isolation.py \
    tests/unit/workflow/graph/test_task_runner.py \
    tests/unit/test_opencode_register.py \
    tests/unit/test_skills_slimming.py \
    tests/unit/test_fuzz_performance_skills.py \
    tests/unit/test_skills_content.py \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_eval_import_replay.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/integration/test_eval_workflow_run_synth.py \
    tests/integration/test_graph_runtime_faults.py
  git commit -m "feat(assurance): activate v6 contract-closed runtime"
  ```

### Task 16: Unify Eval Layer Selection and Define Replayable Change Location

**Files:**
- Create: `assurance_agent/eval/selection.py`
- Create: `assurance_agent/eval/change_location_evidence.py`
- Modify: `assurance_agent/change_location.py`
- Modify: `assurance_agent/eval/runner.py`
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/types.py`
- Create: `tests/unit/eval/test_selection.py`
- Create: `tests/unit/eval/test_change_location_evidence.py`
- Modify: `tests/unit/test_change_location.py`
- Modify: `tests/unit/eval/test_runner.py`
- Modify: `tests/unit/eval/test_executor.py`
- Modify: `tests/integration/test_eval_cli.py`

**Interfaces:**
- Produces: `normalize_selected_layers(...)`, pure change-root parsing/probing/decision, `ChangeLocationCandidateV1`, `ChangeLocationEvidenceV1`, and canonical selection fields in `ExecutionResult`/`execution.json`.
- Consumes: raw suite `test_type`/`test_types` by key presence, `.aa/config.yaml` bytes, caller-supplied before-manifest leaf facts, `preference="active"`, and safe repository containment.
- Preserves: generic workflow CLI params remain validated by the workflow schema; only the eval suite boundary accepts scalar/comma/list forms.

- [ ] **Step 1: Add the complete selection normalization table**

  Cover omitted, single scalar, comma scalar, YAML list, reverse order, all fifteen non-empty subsets, empty string/list, duplicate, unknown, and both keys present. Assert the exact canonical tuple and that a falsey explicit value never receives the default.
- [ ] **Step 2: Add runner/executor/CLI single-resolution tests**

  Pass a list-form suite through both `run_suite` and a real `aa eval run`; assert fixture validation, runtime params, executor result, and `execution.json` all receive the same canonical tuple value. Remove every downstream stringification/reparse path and the `parse_single_test_type` assumption; Task 17 makes policy consume this tuple.
- [ ] **Step 3: Add pure change-location decision tests**

  Cover default and non-default configured roots, changes/archive coexistence selecting changes, archive-only rejection, missing active leaf, absolute/parent roots, symlink candidates, path escape, another change, missing/extra candidates, and tampered config digest. Candidate probes use `lstat` kind/mode and before-manifest leaf presence.
- [ ] **Step 4: Add strict decision-model replay tests over supplied leaf facts**

  Construct `ChangeLocationEvidenceV1` from exact config bytes, safe candidate probes, and an explicit set of before-manifest leaves. Reparse the bytes, recompute the decision, and require byte-identical evidence. Do not wire executor persistence until Task 17 owns the actual content manifest.
- [ ] **Step 5: Run tests and observe truthiness/string/symlink failures**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py -k list_form_selection
  ```

  Expected: current runner stringifies lists, defaults falsey values, and live `is_dir()` cannot produce replayable candidate evidence.
- [ ] **Step 6: Implement one normalization boundary and typed executor input**

  Resolve before fixture seeding. Change `execute_attempt` to accept only `selected_layers`; construct graph `params.test_types` as a list in canonical order. Reject any call that tries to pass unresolved raw selection downstream.
- [ ] **Step 7: Split pure change resolution from the existing wrapper**

  Keep `resolve_change(...)` public behavior by delegating to parsed roots, safe probes, and decision. Expose the strict evidence builder/replayer as pure functions that require caller-supplied before-manifest facts. Task 17 wires copied config and evidence before policy/runtime.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py -k list_form_selection
  uv run ruff check assurance_agent/eval/selection.py assurance_agent/eval/change_location_evidence.py assurance_agent/change_location.py assurance_agent/eval/runner.py assurance_agent/eval/executor.py tests/unit/eval
  uv run pyright
  ```

  Expected: all selection forms converge once, and pure change-location evidence replays exactly from explicit leaf facts.
- [ ] **Step 9: Commit the eval selection/location boundary**

  ```bash
  git add assurance_agent/eval/selection.py \
    assurance_agent/eval/change_location_evidence.py \
    assurance_agent/change_location.py \
    assurance_agent/eval/runner.py \
    assurance_agent/eval/executor.py \
    assurance_agent/eval/types.py \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/test_change_location.py \
    tests/unit/eval/test_runner.py \
    tests/unit/eval/test_executor.py \
    tests/integration/test_eval_cli.py
  git commit -m "feat(eval): normalize layers and define change evidence"
  ```

### Task 17: Replace Porcelain Authority with Content Manifests and Export the Root Evidence Closure

**Files:**
- Create: `assurance_agent/eval/evidence_export.py`
- Modify: `assurance_agent/eval/change_location_evidence.py`
- Modify: `assurance_agent/eval/write_scan.py`
- Modify: `assurance_agent/eval/executor.py`
- Modify: `assurance_agent/eval/scorers/shared.py`
- Modify: `assurance_agent/workflow/graph/workspace.py`
- Modify: `tests/unit/eval/test_write_scan.py`
- Modify: `tests/unit/eval/test_change_location_evidence.py`
- Create: `tests/unit/eval/test_evidence_export.py`
- Modify: `tests/unit/eval/test_executor.py`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/workflow/graph/test_contracts.py`
- Modify: `tests/unit/workflow/graph/test_task_input_snapshot.py`
- Modify: `tests/unit/workflow/graph/test_policy_snapshot_runtime.py`
- Modify: `tests/unit/workflow/graph/test_runtime_commit_safety.py`

**Interfaces:**
- Produces: `WorktreeManifestEntryV1`, `WorktreeManifestV1`, `WriteDiffEntryV1`, `WriteDiffV1`, `WritePolicyV1`, strict `ExecutionEvidenceV1`, content capture/diff/replay APIs, `RootEventSliceEventV1`, `RootEventSliceV1`, `EvidenceExportObjectV1`, `EvidenceExportManifestV1`, and export/replay functions.
- Consumes: canonical selected layers, D17 resolved current-change path/evidence, full source ledger read once, root invocation ID, root/descendant definitions/events, snapshots, contexts, receipts, write sets, blobs, and semantics objects.
- Preserves: porcelain files for diagnostics and `_copy_sut_tests` for human inspection; neither earns authority or current-write credit.

- [ ] **Step 1: Add strict fail-closed content-manifest tests**

  Traverse directories but serialize only regular-file and symlink leaves; bind normalized relative path, kind, mode, size, content digest or symlink target using `lstat` without following. Exclude exactly `.git/**` and the external attempt/evidence directory. Require canonical UTF-8 byte ordering plus exact aggregate entry/file-byte counts. Reject escape, duplicate path, extra/coerced fields, inconsistent kind fields, socket/FIFO/device leaves, permission/read/stat failure, identity/kind race between stat and read, more than 250,000 entries, or more than 4 GiB hashed bytes.
- [ ] **Step 2: Add full content-diff coverage**

  Detect add/delete, byte modify, chmod, file-to-symlink, symlink-target change, clean tracked, pre-dirty, untracked, and ignored paths. An unchanged pre-dirty path must not be attributed. Preserve before/after evidence even when porcelain text is identical or omits an ignored file. Replay must recompute the complete canonical diff from both manifests and require byte-exact equality with `write-diff.json`; forged-empty, omitted, extra, reordered, or wrong-reason entries fail evidence integrity before policy scoring.
- [ ] **Step 3: Add exact selected-layer policy and graph-authority parity tests**

  For all fifteen selections and reverse-order inputs, require canonical bytes allowing only current change, graph locks/publications, `tests/testdata` and selected private roots. Reject all-changes, sibling tests, product code, memory, arbitrary runtime metadata, another change, archive root, and `eval/out/runs/**` inside the attempt SUT. For each subset, derive the selected execution-contract write-claim union and prove `WritePolicyV1` makes the same selected/sibling/shared decisions; any drift fails the shared parity test.
- [ ] **Step 4: Add the root event-slice model tests**

  Use two interleaved roots in one valid ledger. Require `export_seq` exactly `1..N`, strictly increasing `source_seq` with permitted gaps, production graph-event validation, exact root descendant closure, and optional one exact D18 supersede event only when its authorization is consumed by this root.
- [ ] **Step 5: Add transitive object-closure and D15 root-map tests**

  For v6, export exact root/child definition bindings, individual graph/contracts/catalog/policy/profile/three semantics objects, selected codegen snapshots/contexts/receipts/write sets, the receipt-referenced canonical validation-decision payload as a digest-bound `blob`, and blobs for bound plan/case/hard outputs/add-modify files. For v1-v5, export only object kinds actually pinned by that historical schema; when a v4/v5 compatibility receipt is present, additionally export its exact staged audit-semantics object. Never reconstruct a missing historical object from the current executable. Verify every current write set carries `base_tree_roots`, that the map matches `base_tree_id` and participates in `write_set_id`, and that offline `project:`/`repo:` alias resolution uses only that exported map. Reject missing/tampered/extra decision blobs or root maps, unreferenced objects, duplicate identity, path escape, digest/size mismatch, ancestry escape, deleted/reordered events, wrong source metadata, or an unrelated sentinel object copied from the store.
- [ ] **Step 5a: Add strict execution-envelope tests**

  Require `selected_layers`, `selection_normalizer_version`, `write_policy_schema_version`, change ID, safely resolved repository-relative active-change root, and root invocation ID for both fresh and import-checkpoint execution. A failure before root establishment records null and receives no current credit. Bind the copied config/change-location evidence, both `write-manifest-*.json` files, diff, policy, source-ledger metadata, root slice, and export manifest by path/digest/size; reject extra/coerced/missing fields and any envelope/object disagreement.
- [ ] **Step 6: Run tests and observe porcelain/wide-copy limitations**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  ```

  Expected: ignored/content/kind mutations are invisible and no bounded runtime evidence closure exists.
- [ ] **Step 7: Implement content capture, D17 persistence, diff, and strict policy**

  Capture `write-manifest-before.json` independently after fixture seeding and before runtime. Copy the exact config, build/replay D17 evidence from that manifest, then construct policy from the selected tuple and active change. On unsafe/archive-only resolution, persist infrastructure evidence but no writable policy or root. Capture `write-manifest-after.json` regardless of runtime result; persist canonical files and bind every digest/size plus both schema-version fields in strict `execution.json`. Compute forbidden writes only from validated `WriteDiffV1` plus `WritePolicyV1`.
- [ ] **Step 8: Implement one-pass root evidence export**

  Read and digest the full source ledger once, select closure by parent invocation lineage, then traverse the schema-version-specific explicit object references. Do not recursively export trees. Export each current receipt's canonical decision bytes and each current write set's bound `base_tree_roots`; verify their digests before recording them. Preserve historical object sparsity and receipt-bound audit semantics exactly. Preserve the exact source metadata/pairs in `execution.json`; failure before a root leaves root ID null and cannot receive current credit. Extend the D13 and D10/D14 field-consumer inventories with snapshot/context, receipt-decision, D15 root-map, and all three current semantics object/digest export reads.
- [ ] **Step 9: Make shared evidence integrity fail closed**

  Require strict execution, D17, manifests/diff/policy, root slice, export manifest, and every referenced object. Recompute the full diff from the two bound manifests and compare canonical bytes rather than trusting a persisted count/path list; a forged empty diff over a forbidden manifest change is integrity failure. Missing write evidence or object bytes scores integrity zero; never synthesize forbidden count zero from absence.
- [ ] **Step 10: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  uv run ruff check assurance_agent/eval/write_scan.py assurance_agent/eval/change_location_evidence.py assurance_agent/eval/evidence_export.py assurance_agent/eval/executor.py assurance_agent/eval/scorers/shared.py tests/unit/eval
  uv run pyright
  ```

  Expected: every manifest/export mutation fails at evidence integrity and all policy subsets produce stable bytes.
- [ ] **Step 11: Commit content-aware bounded evidence**

  ```bash
  git add assurance_agent/eval/evidence_export.py \
    assurance_agent/eval/change_location_evidence.py \
    assurance_agent/eval/write_scan.py \
    assurance_agent/eval/executor.py \
    assurance_agent/eval/scorers/shared.py \
    assurance_agent/workflow/graph/workspace.py \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_executor.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_policy_snapshot_runtime.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py
  git commit -m "feat(eval): export content-bound runtime evidence"
  ```

### Task 18: Dark-Ship Selected-Test Behavior and Current-Chain Scoring

**Files:**
- Modify: `assurance_agent/verification/generated_entries.py`
- Create: `assurance_agent/eval/scorers/current_codegen.py`
- Modify: `assurance_agent/eval/scorers/shared.py`
- Modify: `tests/unit/verification/test_generated_entries.py`
- Create: `tests/unit/eval/test_codegen_scorer.py`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/verification/test_fuzz_performance_contract_fixtures.py`
- Modify: `tests/unit/workflow/graph/test_task_input_snapshot.py`

**Interfaces:**
- Produces: typed plan mapping extraction, `GeneratedEntryDecision`, four AST classifiers, current root/branch/cycle attempt binding, `CurrentLayerCodegenEvidence`, and callable calculations for the three future hard metrics.
- Consumes: verified execution/D17/policy/export evidence, pinned historical role manifest, epoch-closed ledger attempts, D16 manifest/receipt/snapshot/write set/blobs, content diff, plan/case mappings, and selected layers.
- Preserves: the live `eval/scorers/codegen.py` registration and existing syntax/secret/summary/evidence verdicts. This task is dark: no dataset or suite can request the three new metrics until Task 22 activates them after runtime/recovery coverage.

- [ ] **Step 1: Extend the strict mappings with behavioral classification inputs**

  Reuse Task 6's strict Fuzz Test Function Mapping/Schema Acquisition and Performance Task Mapping/Target File records. Extend those typed mapping objects with the closed behavioral policy inputs required by the scorer; retain fail-closed missing/duplicate/interrupted/malformed behavior and do not change the canonical plan wire shape.
- [ ] **Step 2: Add independent positive/negative AST corpora**

  API requires a mapped `test_*` request through a declared client and a non-constant response-dependent assertion. E2E requires navigation, interaction, and page/locator-dependent expect/assert. Fuzz requires bound schema/parametrize, generated case consumption, and `call_and_validate` or one registered equivalent. Performance requires a mapped method on `HttpUser`/`FastHttpUser`, `@task`, and `self.client` request in that method.
- [ ] **Step 3: Reject pass-shaped but behaviorless code**

  Parameterize assignment-only, bare return, `assert True`, constant comparison, decorator-only function, helper-only file, unmapped symbol, wrong case, API request without dependent assertion, E2E navigation without interaction/assertion, Fuzz without bound schema call, and Locust task without request or outside a User subclass.
- [ ] **Step 4: Add epoch-closed current-chain tests**

  For each selected layer, bind root, assurance child, branch, cycle generation, trees, applicability/preflight, reviewer, mechanical, gate, precheck, and codegen. Applicable requires the full chain and no plan attempt in codegen-only; inapplicable requires current N/A/skip evidence and absence of reviewer/codegen. Mixing byte-identical evidence from two cycles must fail.
- [ ] **Step 5: Add manifest/receipt/write attribution tests**

  Require current summary+manifest output digests, successful `generated_files_candidate/v1` receipt, input snapshot, non-empty write set, selected private-root `test_entry` add/content-modify, after/blob/final-manifest digest equality, content-diff attribution, zero sibling writes, and zero forbidden policy writes. Delete/chmod/shared/support/summary-only/pre-existing test cases score zero.
- [ ] **Step 6: Lock aggregation semantics**

  `current_assurance_chain_rate` divides by every selected layer. `current_codegen_attempt_rate` and `selected_test_write_rate` divide by selected applicable layers and return `0.0` when none apply. Cover one applicable+one inapplicable, two applicable with one write, and a successful branch from another root.
- [ ] **Step 7: Run tests and observe presence/syntax false positives**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  ```

  Expected: current scorer gives credit to pre-existing/`assert True` syntax and lacks lineage/receipt checks.
- [ ] **Step 8: Implement shared mappings/classifiers and current evidence binding**

  Load plan/case bytes only from the attempt-bound snapshot/exported blobs. Use the shared layer registry and physical resolver. Extend the D13 field-consumer inventory with scorer snapshot/context reads. Keep registered client/schema-call forms closed policy data with direct mutation coverage; do not match arbitrary call-name substrings.
- [ ] **Step 9: Replay policy before scoring writes**

  Strictly reconstruct D17 location and `WritePolicyV1` from canonical selected layers. Require byte-identical persisted policy before classifying any write. Any evidence/policy mismatch sets all three hard metrics to zero.
- [ ] **Step 10: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  uv run ruff check assurance_agent/verification/generated_entries.py assurance_agent/eval/scorers tests/unit/verification/test_generated_entries.py tests/unit/eval/test_codegen_scorer.py
  uv run pyright
  ```

  Expected: direct calls credit only current, mapped, behaviorally meaningful, receipt-bound selected test writes; the live scorer still exposes no new hard metric.
- [ ] **Step 11: Commit the dark current-chain scorer**

  ```bash
  git add assurance_agent/verification/generated_entries.py \
    assurance_agent/eval/scorers/current_codegen.py \
    assurance_agent/eval/scorers/shared.py \
    tests/unit/verification/test_generated_entries.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/unit/verification/test_fuzz_performance_contract_fixtures.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py
  git commit -m "feat(eval): stage current behavioral codegen evidence"
  ```

### Task 19: Prepare Truthful Codegen-Pending Tiers Without Activating Them

**Files:**
- Modify: `assurance_agent/eval/fixtures.py`
- Modify: `assurance_agent/eval/types.py`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L1-assurance-input-ready.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-pending.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-pending.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/config.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/api.py`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/e2e/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review-summary.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-checks.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review.json`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review-summary.md`
- Create: `benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-checks.json`
- Modify: `benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json`
- Modify: `tests/unit/eval/test_fixtures.py`

**Interfaces:**
- Produces: `TierManifest.repo_paths`, `TierManifest.expected_layers`, selected-role-aware full-ancestry validation, one independent common base, and four dormant plan-ready/codegen-pending tiers.
- Consumes: locked benchmark fixture paths/digests, canonical selected layers, truthful L1 domain capability, exact current plan/case inputs, and the structured historical-role vocabulary.
- Preserves: current workflow-codegen dataset/suite references and live scorer registration; existing complete L2/L3 tiers remain importable after the dynamic review/check seeder is removed.

- [ ] **Step 1: Add full-ancestry pending-tier rejection tests**

  Reject every selected assurance-chain role by structured role identity: applicability, branch wrapper, review-cycle wrapper, reviewer, mechanical, gate, precheck, codegen, and generation-join. Also reject review/check JSON, codegen summary/manifest, mapped target test/stub, and resets that mark selected work done. Cover every role/artifact directly and inherited from a parent; permit only declared config, adapter, conftest, and reusable support.
- [ ] **Step 2: Add locked `repo_paths` safety and capability tests**

  Copy `.aa/config.yaml`, `.aa/data-knowledge.yaml`, and reusable support only from `fixture-lock.json` into the isolated attempt SUT. Reject absolute/parent/symlink paths and digest mismatch. Make `tests/testdata/domain/api.py` expose the real capability with product dependencies imported lazily inside the factory, so module and symbol import smoke passes in the isolated fixture. If lazy import is impossible, lock the smallest real dependency closure; never install fake `app.*` modules.
- [ ] **Step 3: Freeze complete-tier review/check evidence, then remove dynamic seeding**

  Add and lock the six exact Fuzz/Performance review/check files above, import them from their existing complete L2 tiers, and seed every old L2/L3 tier successfully. Only then delete the exact `_ensure_assurance_seed_artifacts(...)` helper. `expected_layers` remains validation metadata and never supplies params or resets.
- [ ] **Step 4: Add independent pending tiers and repair source mappings**

  Build `L1-assurance-input-ready` without complete-tier ancestry and add four child pending tiers. Repair Fuzz/Performance case and plan mappings to the real API entity/capability. Assert expanded pending chains contain no selected completion or mapped target fallback while complete chains retain summaries, manifests, tests, and frozen reviews/checks. Do not switch a dataset or suite yet.
- [ ] **Step 5: Run tests and observe current helper/ancestry gaps**

  ```bash
  uv run pytest -q tests/unit/eval/test_fixtures.py
  ```

  Expected: the current fixture implementation still relies on dynamic assurance seeding and cannot reject hidden selected wrappers/artifacts across ancestry.
- [ ] **Step 6: Implement metadata, role-aware validation, and locked copying**

  Expand the complete `extends` chain before copying/importing. Validate `paths`, `repo_paths`, imports, and resets against the one resolved selected tuple and structured role map. Recompute `fixture-lock.json` with the repository helper, never by manual digest editing.
- [ ] **Step 7: Run focused and static gates**

  ```bash
  uv run pytest -q tests/unit/eval/test_fixtures.py
  uv run ruff check assurance_agent/eval/fixtures.py assurance_agent/eval/types.py tests/unit/eval/test_fixtures.py
  uv run pyright
  ```

  Expected: all four dormant pending tiers validate and seed as plan-ready/codegen-pending; every existing complete L2/L3 tier preserves complete import behavior without a dynamic helper.
- [ ] **Step 8: Commit dormant truthful fixture inputs**

  ```bash
  git add assurance_agent/eval/fixtures.py assurance_agent/eval/types.py \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L1-assurance-input-ready.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-api-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-e2e-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-pending.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-fuzz-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/tiers/L2-performance-codegen-seed.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/config.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/.aa/data-knowledge.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/tests/testdata/domain/api.py \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/e2e/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/fuzz/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/cases/system/performance/case.yaml \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/fuzz-codegen-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/plans/performance-codegen-plan.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-review-summary.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/fuzz-plan-checks.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review.json \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-review-summary.md \
    benchmark/vue-fastapi-admin/eval-fixtures/samples/eval-sample-001/review/performance-plan-checks.json \
    benchmark/vue-fastapi-admin/eval-fixtures/fixture-lock.json \
    tests/unit/eval/test_fixtures.py
  git commit -m "test(eval): stage truthful codegen-pending fixtures"
  ```

### Task 20: Drive the Real Four-by-Two and Multi-Layer GraphRuntime Matrix

**Files:**
- Create: `tests/helpers_four_layer_runtime.py`
- Create: `tests/integration/test_four_layer_codegen_only.py`
- Modify: `tests/integration/test_api_e2e_assurance_flow.py`
- Modify: `tests/integration/test_fuzz_performance_assurance_flow.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`
- Modify: `assurance_agent/workflow/graph/precommit.py`
- Modify: `assurance_agent/workflow/graph/task_inputs.py`

**Interfaces:**
- Produces: a deterministic target/attempt-aware adapter, coordinator-only snapshot fault injector, test-only `BarrierNodeRunner`, real packaged runtime fixture, eight base cells, stale-evidence cases, and representative multi-layer/join coverage.
- Consumes: `build_graph_runtime`, the production import-checkpoint path, packaged schema/contracts/catalog/personas, real journal/tree/workspace/freeze/apply/child invocation pipeline, and canonical layer outputs.
- Preserves: the adapter supplies agent output only; coordinator evidence faults are separate test seams. Neither helper implements gates, topology, planner decisions, or write authorization.

- [ ] **Step 1: Build a deterministic adapter keyed by exact target and attempt**

  It writes valid strict outputs to the provided attempt workspace and can inject one named malformed/missing/forbidden output mutation. Record every target/persona/workspace request so tests can prove no plan dispatch in codegen-only and no unselected branch dispatch. Put snapshot-CAS/start-reference corruption behind a separate coordinator fault injector because an agent cannot mutate that authority.
- [ ] **Step 2: Add the four applicable base cells**

  For API/E2E/Fuzz/Performance individually, use the production import-checkpoint path to import only bootstrap/plan-ready predecessors; selected branch/reviewer/mechanical/gate/precheck/codegen/join roles remain pending. Then run `execute` in `codegen-only` mode with `run_tests=false`. Assert current applicability, reviewer, mechanical, gate, precheck, and codegen attempts; Fuzz/Performance parent preflight; no plan or `operation:run-tests` attempt; summary+manifest+receipt+snapshot+write set; correct private test write; and completed branch/join-to-END.
- [ ] **Step 3: Add the four inapplicable base cells**

  Assert current applicability, four N/A checks, skipped gate/precheck, no reviewer/codegen attempt, no generated output/write, and completed branch/join. A valid case set with only another layer or no selected automated case is N/A. Malformed case YAML or non-boolean `automation.required` is an applicability error; wrong/missing/malformed N/A stops rather than becoming skip.
- [ ] **Step 4: Add stale/pre-existing artifact cases**

  Seed pass-shaped old review/check/summary/test bytes from another root/cycle and assert they cannot replace current producer attempts. Cover reviewer success with no review output; mechanical failure/no committed checks despite stale checks; missing manifest; shape-valid manifest/write-set mismatch; forged selected-role `task_imported`; and coordinator-injected stale snapshot/reference. Each fails at its named ingest/precommit/precheck owner with no commit. The positive control has each current producer rewrite byte-identical canonical bytes and passes because attempt/snapshot/receipt/write-set ownership is fresh.
- [ ] **Step 5: Add representative multi-layer selections**

  Cover default API+E2E, API+Fuzz+Performance with mixed applicable/inapplicable layers, all four applicable, and one selected layer with all siblings unselected. Assert unselected absence, one canonical params tuple/policy, and each active branch's complete chain. Hold the last applicable codegen with a blocking adapter; separately hold an inapplicable gate result before commit with test-only `BarrierNodeRunner`. In both cases `generation-join` must wait, then start exactly once after release, and no execution successor runs.
- [ ] **Step 6: Run tests and observe any runtime-only seams**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py
  ```

  Expected: every failing assertion names a production boundary; no helper fallback is permitted.
- [ ] **Step 7: Apply only owner-local production fixes exposed by the real matrix**

  Add each discovered case to its owner-specific mutation suite before fixing it, then change the actual owner (`runtime`, replay binding, scheduler, snapshot, or precommit) rather than the harness. If the owner is a Task 10 semantic definition, move the correction into the pre-freeze task or add a formally versioned manifest/dispatcher; never silently change v1 bytes. Do not add target-specific scheduler dispatch or generate topology in Python.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py
  uv run ruff check tests/helpers_four_layer_runtime.py tests/integration/test_four_layer_codegen_only.py assurance_agent/workflow/graph/replay_binding.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/scheduler.py assurance_agent/workflow/graph/precommit.py assurance_agent/workflow/graph/task_inputs.py
  uv run pyright
  ```

  Expected: all eight cells and three multi-layer controls pass through the real runtime.
- [ ] **Step 9: Commit the real runtime matrix and necessary fixes**

  ```bash
  git add tests/helpers_four_layer_runtime.py \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_api_e2e_assurance_flow.py \
    tests/integration/test_fuzz_performance_assurance_flow.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/scheduler.py \
    assurance_agent/workflow/graph/precommit.py \
    assurance_agent/workflow/graph/task_inputs.py
  git commit -m "test(runtime): prove four-layer codegen-only execution"
  ```

### Task 21: Complete Resume, Remediation, Healing, and Named Crash-Cut Coverage

**Files:**
- Create: `tests/integration/test_four_layer_resume.py`
- Modify: `tests/integration/_graph_fault_worker.py`
- Modify: `tests/integration/test_graph_runtime_faults.py`
- Modify: `tests/integration/test_codegen_fixer_record.py`
- Modify: `tests/unit/workflow/graph/test_resume_v3.py`
- Modify: `tests/unit/workflow/graph/test_replay_binding.py`
- Modify: `assurance_agent/workflow/graph/runtime.py`
- Modify: `assurance_agent/workflow/graph/checkpoint.py`
- Modify: `assurance_agent/workflow/graph/replay_binding.py`
- Modify: `assurance_agent/workflow/graph/scheduler.py`

**Interfaces:**
- Produces: the complete design §12 resume/recovery matrix and any minimal generic recovery fixes it exposes.
- Consumes: real packaged runtime, deterministic adapter, restart clock/crash hooks, pinned v4/v5/v6 bundles, manual/knowledge interrupts, effect retry sidecar, and D18 supersede.
- Preserves: recorded successful handlers are never reinvoked; a graph wrapper resumes its existing child rather than creating a duplicate.

- [ ] **Step 1: Add ordinary restart seam cases**

  For each of API, E2E, Fuzz, and Performance, crash independently after reviewer, mechanical, gate, and precheck; resume the same root and assert only the next pending node runs. Cover wrapper crash after child creation/before wrapper success and require reuse of that child invocation.
- [ ] **Step 2: Add remediation routes**

  API/E2E automatic plan fixer creates a new assurance epoch and returns to review. Restart before the fixer attempt, after fixer success/before write-set commit, after apply/before review replanning, and after the second mechanical producer/before its gate; old-epoch evidence never authorizes codegen.

  Fuzz/Performance `fix_and_proceed` records an audited plan tree change and returns to review. Reject no-op revision, extra path, conflicting base digest, reused transition ID, wrong source-gate attempt, and sibling-layer edit, then exercise `revision_target_objects`, `manual_plan_revision_append`, and applicable resume ordinals. Knowledge remediation refreshes L1-dependent mechanical/gate evidence but absent, no-op, stale, or unrelated promotion stops. `accept_risk` requires a declared action, exact interrupt/source-gate binding, no smuggled revision, still-valid mandatory capability/evidence, and a pinned declared route; capability removal and stale/wrong-tree evidence stop at precheck. `stop` terminates without precheck/codegen.
- [ ] **Step 3: Add codegen-fixer healing routes with exact event cardinality**

  Cover API-only, E2E-only, both-active, high-risk approval, low-risk direct pass, imported-unverified codegen, wrong/stale authority, and active-record join. Each allocation emits one `healing_attempt_allocated_v2`; low risk emits zero and high risk exactly one `fixer_proposal_approved` (effect kind `fixer_proposal_approved/v1`); each active target emits one `heal_record_apply_v2`, so both-active emits two; each episode emits one aggregate safety result.
- [ ] **Step 4: Add every exact subprocess/SIGKILL cut ID**

  Extend `_graph_fault_worker.py` with a packaged-four-layer builder and `(structural_path, node_id, occurrence)` selector. Parameterize the exact design IDs: `before_attempt_started`, `after_attempt_started`, `handler_before_success`, `snapshot_created_before_started`, `started_with_snapshot_before_handler`, `candidate_after_freeze_before_validate`, `candidate_after_validate_before_success`, `candidate_validation_rejected`, `target_success_before_commit`, `target_superstep_committed`, `tree_pointer_superstep`, `canonical_materialization`, `checkpoint_snapshot_write`, `sync_apply_pending`, `sync_ack_pending`, `revision_target_objects`, `manual_plan_revision_append`, every applicable `graph_resumed_ordinal_<n>`, `child_started_before_wrapper_success`, and `child_pending_before_wrapper_success`.

  Also parameterize `fixer_approval_after_resume_before_operation`, `fixer_approval_before_success_line`, `fixer_approval_after_success_before_superstep_commit`, `fixer_approval_after_superstep_commit_before_domain_event`, `fixer_approval_after_domain_event_before_ack`, and `fixer_approval_after_ack_before_gate`; `allocate_before_success_line`, `allocate_after_success_before_superstep_commit`, `allocate_after_superstep_commit_before_domain_event`, `allocate_after_domain_event_before_ack`, and `allocate_after_ack_before_successor`; plus `heal_record_before_success_line`, `heal_record_after_success_before_superstep_commit`, `heal_record_after_superstep_commit_before_domain_event`, `heal_record_after_domain_event_before_ack`, and `heal_record_after_ack_before_successor`. Name the otherwise textual §12.7 real-lock seam `effect_retry_lock_contended` and pin it in worker/tests. Every durable cut kills the subprocess and reconstructs a fresh runtime. Hold/release the real progression lock across restarts and assert due-time sidecar behavior with no duplicate attempt/domain event.
- [ ] **Step 5: Add pinned compatibility and D18 exits**

  V6 compatible bundles resume the exact pinned graph/contracts/catalog without injecting a current node. Independently mutate gate/profile/topology/commit-safety semantics and ingest models; pending-fixer and already-validated-receipt resumes also reject validator/reconciler drift before recovery. V4/v5 report-only work continues; pending codegen/fixer/effect work stops with the stable reason. Cover `supersede_before_append`, `supersede_after_append_before_root_start`, `supersede_after_root_start_before_return`, concurrent exact replacement, and terminal stop; rerun-v6 is consumed once and direct child resume remains fenced.
- [ ] **Step 6: Run tests and observe duplicate-child/recovery gaps**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_resume.py \
    tests/integration/_graph_fault_worker.py \
    tests/integration/test_graph_runtime_faults.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/unit/workflow/graph/test_resume_v3.py \
    tests/unit/workflow/graph/test_replay_binding.py
  ```

  Expected: failures, if any, identify one generic recovery seam rather than layer-specific routing.
- [ ] **Step 7: Apply minimal generic recovery fixes**

  Preserve the recovery order: definition-independent manual repair, exact bundle/semantic compatibility, pending write/publication, unacknowledged effects, materialization, then planner. Bind wrapper recovery to the existing child ID from checkpoint namespace/ledger and reject duplicate child creation. A fix to a frozen semantic definition follows the global version/move-earlier rule; this task cannot mutate v1 meaning in place.
- [ ] **Step 8: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_resume.py \
    tests/integration/test_graph_runtime_faults.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/unit/workflow/graph/test_resume_v3.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    tests/unit/workflow/graph/test_supersede.py
  uv run ruff check tests/integration/test_four_layer_resume.py tests/integration/_graph_fault_worker.py assurance_agent/workflow/graph/runtime.py assurance_agent/workflow/graph/checkpoint.py assurance_agent/workflow/graph/replay_binding.py assurance_agent/workflow/graph/scheduler.py
  uv run pyright
  ```

  Expected: every restart/crash route converges, with one child, one committed result, and no handler rerun after recorded success.
- [ ] **Step 9: Commit recovery-complete coverage**

  ```bash
  git add tests/integration/test_four_layer_resume.py \
    tests/integration/_graph_fault_worker.py \
    tests/integration/test_graph_runtime_faults.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/unit/workflow/graph/test_resume_v3.py \
    tests/unit/workflow/graph/test_replay_binding.py \
    assurance_agent/workflow/graph/runtime.py \
    assurance_agent/workflow/graph/checkpoint.py \
    assurance_agent/workflow/graph/replay_binding.py \
    assurance_agent/workflow/graph/scheduler.py
  git commit -m "test(runtime): complete assurance recovery matrix"
  ```

### Task 22: Activate the Current Scorer, Pending Datasets, and Hard Gates

**Files:**
- Modify: `assurance_agent/eval/scorers/codegen.py`
- Modify: `assurance_agent/eval/types.py`
- Modify: `eval/datasets/workflow-api-codegen/WAC-001.yaml`
- Modify: `eval/datasets/workflow-e2e-codegen/WEEC-001.yaml`
- Modify: `eval/datasets/workflow-fuzz-codegen/WFUZ-001.yaml`
- Modify: `eval/datasets/workflow-performance-codegen/WPER-001.yaml`
- Modify: `eval/suites/workflow-api-codegen.yaml`
- Modify: `eval/suites/workflow-e2e-codegen.yaml`
- Modify: `eval/suites/workflow-fuzz-codegen.yaml`
- Modify: `eval/suites/workflow-performance-codegen.yaml`
- Modify: `tests/unit/eval/test_scorers.py`
- Modify: `tests/unit/eval/test_suite_load_all.py`
- Modify: `tests/unit/eval/test_fixtures.py`
- Modify: `tests/integration/test_eval_cli.py`

**Interfaces:**
- Produces: the single live switch from dormant current-chain calculations/pending tiers to four workflow-codegen datasets and suites that hard-gate the same three metrics.
- Consumes: green Task 20 real-runtime matrix, green Task 21 recovery matrix, Task 18 scorer, Task 19 validated pending tiers, and the strict Task 17 execution/evidence envelope.
- Preserves: complete L2/L3 run/full datasets, existing non-codegen scorer metrics, and the canonical one-time layer selection/policy replay boundary.

- [ ] **Step 1: Add failing atomic-activation assertions**

  Require each workflow-codegen dataset to reference exactly its `L2-*-codegen-pending` tier and each suite to register all three current metrics. For every metric require regression `{direction: higher_is_better, max_regression: 0.0}` and a hard threshold encoded in the real schema as `op: gte` plus `value: 1.0`. Removing or weakening any field fails loading; a deterministic result with one metric at `0.0` yields a failed verdict.
- [ ] **Step 2: Prove activation prerequisites are green**

  ```bash
  uv run pytest -q \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_four_layer_resume.py \
    tests/integration/test_codegen_fixer_record.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_fixtures.py
  ```

  Expected: runtime, recovery, direct scorer, and dormant pending tiers pass before any live registration changes.
- [ ] **Step 3: Switch scorer, datasets, and suites in one diff**

  Register `current_assurance_chain_rate`, `current_codegen_attempt_rate`, and `selected_test_write_rate` in the live codegen scorer; switch all four datasets to their matching pending tier; add exact hard thresholds/regression policy to all four suites. Reject a staged set that changes only one of these three surfaces.
- [ ] **Step 4: Run policy replay and real CLI matrix**

  Through `tests/integration/test_eval_cli.py`, invoke real `aa eval run` command handling with the deterministic real-runtime adapter for one import-checkpoint sample per API/E2E/Fuzz/Performance suite, plus one no-tier/no-import fresh-root control. Assert the appropriate import/fresh root ID, list/scalar selection normalization, fixture validation, runtime params, D17 policy, execution envelope, exported evidence, live metric keys, and final verdict all agree. Re-run the fifteen selection-subset policy/scorer table and one tampered-policy negative; no OpenCode server is used.
- [ ] **Step 5: Run focused and static gates**

  ```bash
  uv run pytest -q \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_scorers.py \
    tests/integration/test_eval_cli.py -k codegen
  uv run ruff check assurance_agent/eval/scorers/codegen.py assurance_agent/eval/types.py tests/unit/eval tests/integration/test_eval_cli.py
  uv run pyright
  ```

  Expected: all four live suites consume only pending inputs and fail unless all three replayed current-chain metrics equal `1.0`.
- [ ] **Step 6: Commit benchmark activation**

  ```bash
  git add assurance_agent/eval/scorers/codegen.py assurance_agent/eval/types.py \
    eval/datasets/workflow-api-codegen/WAC-001.yaml \
    eval/datasets/workflow-e2e-codegen/WEEC-001.yaml \
    eval/datasets/workflow-fuzz-codegen/WFUZ-001.yaml \
    eval/datasets/workflow-performance-codegen/WPER-001.yaml \
    eval/suites/workflow-api-codegen.yaml \
    eval/suites/workflow-e2e-codegen.yaml \
    eval/suites/workflow-fuzz-codegen.yaml \
    eval/suites/workflow-performance-codegen.yaml \
    tests/unit/eval/test_scorers.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/unit/eval/test_fixtures.py \
    tests/integration/test_eval_cli.py
  git commit -m "feat(eval): activate four-layer evidence gates"
  ```

### Task 23: Publish Compatibility Semantics and Run the Release Gate

**Files:**
- Modify: `docs/schemas.md`
- Modify: `README.md`
- Modify: `docs/eval.md`
- Create: `docs/release-notes/2026-08-four-layer-assurance.md`
- Create: `tests/unit/test_docs_contract.py`

**Interfaces:**
- Produces: operator/user documentation for v6 bindings, declared-only inputs, generated-file authority, durable effects, legacy resume narrowing, supersede, multi-layer codegen-only, D17 evidence, and hard benchmark metrics.
- Consumes: final implemented CLI help, wire models, reason codes, and suite schemas.
- Preserves: no documentation promise exceeds tested OpenCode configuration/request binding; third-party sandbox enforcement is explicitly outside CI proof.

- [ ] **Step 1: Add failing documentation-contract assertions**

  Require exact schema IDs, six v6 semantic fields, both validator IDs, three effect kinds, legacy block reason, supersede command/actions, four selected layer values/default, evidence-export algorithm, and three hard metrics. Reject stale text claiming single-layer codegen-only, summary-based authority, or automatic imported-codegen healing.
- [ ] **Step 2: Run the documentation test and observe missing contracts**

  ```bash
  uv run pytest -q tests/unit/test_docs_contract.py
  ```

  Expected: assertions fail until documentation is updated.
- [ ] **Step 3: Document compatibility and operational exits**

  State that v1-v5 remain parseable/displayable, topology receipts are not commit-safety proof, report-only legacy work may continue, pending assurance commit work stops, and `aa workflow supersede` is the sole audited rerun-v6/stop exit. Document intentional imported-codegen healing narrowing.
- [ ] **Step 4: Run all focused feature suites**

  ```bash
  uv run pytest -q \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/verification/test_assurance_contract_mutations.py \
    tests/unit/workflow/graph/test_assurance_topology_mutations.py \
    tests/unit/workflow/graph/test_healing_topology_mutations.py \
    tests/unit/workflow/graph/test_task_input_snapshot.py \
    tests/unit/workflow/graph/test_precommit_validation.py \
    tests/unit/workflow/graph/test_durable_effects.py \
    tests/unit/workflow/graph/test_effect_retry.py \
    tests/unit/workflow/graph/test_topology_semantics.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py \
    tests/unit/workflow/graph/test_resume_compatibility.py \
    tests/unit/workflow/graph/test_supersede.py \
    tests/unit/eval/test_selection.py \
    tests/unit/eval/test_change_location_evidence.py \
    tests/unit/eval/test_write_scan.py \
    tests/unit/eval/test_evidence_export.py \
    tests/unit/eval/test_codegen_scorer.py \
    tests/unit/eval/test_fixtures.py \
    tests/unit/eval/test_suite_load_all.py \
    tests/integration/test_four_layer_codegen_only.py \
    tests/integration/test_four_layer_resume.py \
    tests/integration/test_codegen_fixer_record.py
  uv run pytest -q tests/integration/test_eval_cli.py -k codegen
  ```

  Expected: all focused suites pass.
- [ ] **Step 5: Run the complete repository release gate**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run lint-imports
  uv run pytest -q
  bash scripts/packaging_smoke_test.sh
  ```

  Expected: all six commands pass without an OpenCode server.
- [ ] **Step 6: Run mechanical architecture and contract scans**

  ```bash
  ! rg -n 'workflow-state\.yaml|phases\.|aa heal record-apply' \
    assurance_agent/_resources/skills/aa-{api,e2e,fuzz,performance}-*
  ! rg -n 'events\.jsonl' \
    assurance_agent/_resources/skills/aa-api-plan-fixer \
    assurance_agent/_resources/skills/aa-e2e-plan-fixer
  uv run pytest -q \
    tests/unit/verification/test_assurance_contract_round_trip.py \
    tests/unit/workflow/graph/test_contracts.py \
    tests/unit/workflow/graph/test_runtime_commit_safety.py \
    -k 'declared_only_exact_count or exact_validator_contract_set or exact_effect_contract_set or consumer_set'
  ```

  Expected: forbidden token scans return no match; declared-only occurs exactly sixteen times; validator/effect consumer-set tests confirm exact six/four contract sets.
- [ ] **Step 7: Inspect final history and diff**

  ```bash
  git status --short
  git log --oneline --decorate -23
  git diff origin/main...HEAD --stat
  git diff origin/main...HEAD --check
  ```

  Expected: no unexpected tracked changes, no whitespace errors, and the commit sequence follows Tasks 1-23 with Task 15 as the runtime-contract activation and Task 22 as the later benchmark activation.
- [ ] **Step 8: Commit documentation after all gates pass**

  ```bash
  git add docs/schemas.md docs/eval.md docs/release-notes/2026-08-four-layer-assurance.md \
    README.md tests/unit/test_docs_contract.py
  git commit -m "docs(assurance): publish v6 runtime evidence semantics"
  ```

## Requirement Coverage Map

| Requirement | Implementation tasks | Primary proving suites |
|---|---:|---|
| D1 one graph control plane/observing harness | 2, 15, 20 | contract round-trip; real GraphRuntime matrix |
| D2 structural skill/contract closure | 2, 15 | contract round-trip and mutations |
| D3 current structured conformance | 3, 15 | assurance/healing topology mutations |
| D4 separate historical classifier | 9, 11 | historical roles, replay schema/binding |
| D5 multi-layer codegen-only | 16, 20 | selection unit table; multi-layer runtime |
| D6 attempt-bound freshness | 4, 17, 18, 20 | snapshots, export, scorer, stale runtime cases |
| D7 real runtime/deterministic adapter | 20, 21 | codegen-only and resume integrations |
| D8 one mutation owner | 2, 3, 6, 15, 18 | contract/topology/precommit/scorer mutation tables |
| D9 selected/current write authority | 5, 16, 17, 18 | physical paths, D17, policy replay |
| D10 pinned topology semantics | 9-12 | manifests, v6 binding, legacy audit |
| D11 declared-read agents/personas | 4, 14, 15 | snapshot/read isolation/persona/directory tests |
| D12 content-aware write evidence | 17 | content manifest/diff suite |
| D13 attempt-bound inputs/deferrals | 4 | snapshot, scheduler, checkpoint suites |
| D14 precommit/effects/retry | 6-8, 10-12, 15, 21 | validation, effects, retry, recovery |
| D15 physical evidence identity | 5, 6, 17, 18 | evidence-path and cross-consumer tests |
| D16 strict generated authority | 1, 6, 15, 18 | model, precommit, ingest, scorer suites |
| D17 replayable change location | 16-18 | location evidence, policy, scorer replay |
| D18 audited legacy exit | 12, 13, 17, 21, 23 | compatibility, supersede, export, recovery, docs |
| Healing authority/projection | 1, 7, 8, 15, 21 | episode projection, record integration, crash cuts |
| Benchmark truth/hard metrics | 16-22 | fixture, runtime/recovery, suite loader, scorer, eval CLI suites |

## Final Acceptance Checklist

- [ ] Four independent canonical fixtures cross authoring, runtime model, freeze, applicability, mechanical checks, JSON round-trip, real plan gate, real precheck, and codegen workspace with the fixed check matrix.
- [ ] All sixteen skill inputs/outputs/state authority sections are structurally closed against exact reads/writes/authorization, use declared-only isolation, and expose no ambient runtime/coordinator paths.
- [ ] Every current assurance/healing mutation fails packaged compilation with the expected structured category/code/owner/locator; generic, project, explicit, and pinned graphs retain their intended compiler path.
- [ ] V6 roots and every child bind verified gate/topology/commit-safety object IDs and digests; v1-v5 display/replay behavior stays frozen and never receives synthetic bindings.
- [ ] Every declared-only attempt starts only after its complete snapshot/context exists, repeats the identity at success, and treats lock contention as durable non-attempt scheduling deferral.
- [ ] Shape-valid but cross-artifact-invalid codegen/fixer candidates produce no success, commit, tree change, publication, or domain event.
- [ ] Durable effects survive every success/commit/domain/ack cut, use independent due-time retry state, and block successors until exact acknowledgement.
- [ ] API/E2E healing follows authority -> approval -> intent -> record -> active join -> aggregate safety with one normalized legacy/v2 projection and no agent-side ledger write.
- [ ] Multi-layer codegen-only preserves the API/E2E default, supports every non-empty subset, enforces selected/current write policy, and waits at the all-active generation join.
- [ ] D17 configuration/location, content manifests/diff/policy, event slice, definition objects, snapshots, receipts, write sets, and blobs form one bounded replayable evidence closure with no missing or extra object.
- [ ] API/E2E/Fuzz/Performance selected tests satisfy their exact behavioral AST obligations; pre-existing, trivial, support-only, shared-only, deletion-only, or summary-only artifacts earn zero selected-write credit.
- [ ] The eight real base cells, representative multi-layer runs, ordinary restarts, remediation routes, healing routes, effect contention, pinned replay, and D18 crash/concurrency cuts all converge without duplicate handler/child/domain events.
- [ ] Four codegen datasets use only codegen-pending tiers; complete L2/L3 run fixtures retain their complete ancestry/behavior after frozen manifest/review migration; all four suites hard-gate the three current-chain metrics at `1.0` with zero regression.
- [ ] Documentation names the intentional legacy/imported-codegen narrowing and the only audited operator exit.
- [ ] Ruff lint, Ruff format check, Pyright, import-linter, full pytest, and packaging smoke all pass.

---

This sequence keeps every dangerous mechanism dormant until its semantics and recovery readers exist, makes the v6 binding complete before activation, and turns the final benchmark verdict into a replay of committed runtime evidence rather than a directory-presence heuristic.
