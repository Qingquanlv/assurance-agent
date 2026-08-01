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

