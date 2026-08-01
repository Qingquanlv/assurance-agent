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

