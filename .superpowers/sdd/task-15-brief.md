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

