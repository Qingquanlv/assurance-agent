# Task 4 Report — Close Phase 5 security, fault, replay, property, and repository gates

## Status: DONE_WITH_CONCERNS

Local Phase 5 security, fault, replay, property, and repository gates are
implemented, executable, and committed. Task 3 live OpenCode admission was
not fabricated. Step 4 combined-suite failures and the committed-HEAD smoke
failure are recorded as Change-local returns and were not silently patched.

## What I implemented

Took ownership of the existing Task 4 drafts and closed original Phase 5
Task 26 as a **closed node-ID gate** over existing lower-level tests:

- `tests/phase5/conformance.py`: immutable `SECURITY_GATE_NODE_IDS`,
  `FAULT_GATE_NODE_IDS`, `REPOSITORY_GATE_NODE_IDS`; Task 26 fault-row
  evidence (`direct` / `superseded` / `gap`); `audit_gate_nodes`;
  `run_gate_nodes`; honest Task 3 admission state.
- `tests/phase5/test_phase5_final_security_gate.py`: source authentication,
  adapter confinement, secret redaction.
- `tests/phase5/test_phase5_final_fault_gate.py`: provider-state-loss replay,
  crash/fault recovery, STOP/interrupt, coverage/healing, Task 26 row map.
- `tests/phase5/test_phase5_final_repository_gate.py`: graph reachability,
  wheel isolation, unique 131-node audit, Task 3 admission honesty.
- `tests/phase5/test_replay_properties.py`: publish replay matches the
  uninterrupted projection for every ordered crash subset.
- `scripts/assurance_product_wheel_smoke_test.sh`: isolated selected-plugin
  closure, source-drift, extra-binding, and foreign-binding scenarios.

Owned helper fixes (not Change-local product patches):

1. Rematched `generated-declaration-contribution-mismatch` from the
   nonexistent
   `test_generated_provider_rejects_declaration_contribution_mismatch`
   to the existing
   `test_generated_provider_contributes_exactly_99_aliases`.
2. Isolated `run_gate_nodes` by source file so one polluted `sys.modules`
   cannot fail a later file's node. This is gate-runner robustness, not a
   Change-local test patch.

Task 3 admission: local file (gitignored) is
`blocked_by_execution_approval`. Helper reports
`release_disposition=blocked` and never `admitted`.

Fault coverage remains truthful: 78 direct, 5 superseded, 2 gaps
(`opencode-terminal-before-restart`,
`opencode-provider-state-deleted-after-receipt`).
`release_complete` is `False`.

## What I tested and results

Focused GREEN (helpers present):

- Cheap gate / audit / admission / replay / smoke-contract:
  `13 passed` in 1.63s.
- Security + repository execute gates: `2 passed` in 78s.
- Cross-file isolation helper: `1 passed` in 9.68s.
- Fault execute gate: `1 passed` in 223s.

## TDD Evidence (RED then GREEN)

**Helpers (owned gate files vs committed conformance):**

- RED: restored `HEAD` `conformance.py`, ran 8 cheap gate tests:
  `8 failed` with `AttributeError` for `SECURITY_GATE_NODE_IDS`,
  `FAULT_GATE_NODE_IDS`, `PHASE5_FAULT_IDS`, `PHASE5_FAULT_EVIDENCE`,
  `REPOSITORY_GATE_NODE_IDS`, `audit_gate_nodes`,
  `all_final_gate_node_ids`, `phase5_opencode_admission_state`.
- GREEN: restored helpers; same cheap tests plus audit/replay/smoke-contract
  passed (`13 passed`).

**Cross-file isolation helper:**

- RED: `test_run_gate_nodes_isolates_cross_file_binding_pollution` failed
  because one pytest subprocess ran
  `test_generated_provider_contributes_exactly_99_aliases` then
  `test_forged_alias_target_fails_closed` (`SourceSnapshotError` on
  leftover `assurance_product_bindings_*`).
- GREEN: `run_gate_nodes` now executes one subprocess per source file;
  the isolation test passed.

## Step 4 suite

Command:

```bash
uv run pytest packages/agent-runtime-contracts/tests packages/agent-runtime-opencode/tests packages/agent-runtime-cursor/tests tests/phase4 tests/phase5 -q
```

Result: **6 failed, 1380 passed, 1 skipped, 1 warning** in 2242s.

Failures recorded by test node ID (all pass in isolation; combined-suite
isolation leaks in dirty Change-local tests — not patched):

- `tests/phase5/test_binding_coverage.py::test_cursor_resolution_repeats_and_keeps_finalize_null`
- `tests/phase5/test_graph_binding_audit.py::test_graph_bindings_are_closed_and_inventoried[cursor]`
- `tests/phase5/test_graph_binding_audit.py::test_graph_has_no_runtime_or_phase4_agent_targets[cursor]`
- `tests/phase5/test_graph_intake_and_triplets.py::test_workflow_compiles_under_both_product_providers[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_has_exact_provider_and_binding_closure[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_selects_exact_plugin_and_product_identity[cursor]`

Isolation rerun of those six node IDs: `6 passed` in 38s.

## Smoke

Command: `bash scripts/assurance_product_wheel_smoke_test.sh`

Ran from committed HEAD: **yes** (`a1c0c806bf056c78445a015beb0bac50335b9f3f`).

Exit code: **1**

Change-local return (not patched): committed `assurance_intake` wheel
fails `aa-next bindings build` with

```text
ImportError: cannot import name 'MinimumCoverageMatrixAuthoring' from 'assurance_intake.contracts'
```

Dirty `packages/assurance-intake/assurance_intake/contracts/__init__.py`
likely exports the name; `git archive HEAD` does not. Wheel archives
themselves printed `WHEEL_ARCHIVES_OK` before the bindings-build failure.

## Files changed (this task commit)

- `tests/phase5/conformance.py`
- `tests/phase5/test_phase5_final_security_gate.py`
- `tests/phase5/test_phase5_final_fault_gate.py`
- `tests/phase5/test_phase5_final_repository_gate.py`
- `tests/phase5/test_replay_properties.py`
- `scripts/assurance_product_wheel_smoke_test.sh`

`test_replay_properties.py` is required by the fault-gate replay category
and by original Phase 5 Task 26. ~91 unrelated dirty/untracked files were
left unstaged.

## Change-local defects refused to patch (node IDs)

Combined-suite isolation (pass alone, fail in Step 4):

- `tests/phase5/test_binding_coverage.py::test_cursor_resolution_repeats_and_keeps_finalize_null`
- `tests/phase5/test_graph_binding_audit.py::test_graph_bindings_are_closed_and_inventoried[cursor]`
- `tests/phase5/test_graph_binding_audit.py::test_graph_has_no_runtime_or_phase4_agent_targets[cursor]`
- `tests/phase5/test_graph_intake_and_triplets.py::test_workflow_compiles_under_both_product_providers[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_has_exact_provider_and_binding_closure[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_selects_exact_plugin_and_product_identity[cursor]`

Also isolation-fragile when paired with binding-builder (gate runner now
isolates by file; the underlying leak remains):

- `tests/phase5/test_composition_authority.py::test_forged_alias_target_fails_closed`

Smoke / committed-source import (no pytest node):

- `assurance_intake.operations.finalize` → missing
  `MinimumCoverageMatrixAuthoring` on committed
  `assurance_intake.contracts`.

Explicit modeled gaps (not invented as passing):

- `opencode-terminal-before-restart`
- `opencode-provider-state-deleted-after-receipt`

26 mapped gate nodes exist only in dirty Change-local files (not in
`HEAD` at dispatch). The gates consume that working tree; those files
were not staged.

## Self-review findings

- Completeness: brief Step 1 categories are present; Task 3 release
  claims stay `blocked` / `requires_task3_evidence_validation`.
- Quality: manifests are `MappingProxyType`; audit forbids skip/xfail;
  `run_gate_nodes` requires exact JUnit pass counts.
- Discipline: only the six Task 4 files were staged; no OpenCode/Cursor
  live commands; no fabricated admission; no new workspace/publish
  behavior.
- Testing: RED/GREEN recorded for helpers and isolation; Step 4 and
  smoke recorded with node IDs / exit code.
- `conformance.py` is large (~980 lines) because the plan listed it as
  the single helper module. One-responsibility concern noted below.

## Concerns

- Task 3 live OpenCode admission remains incomplete; release is blocked.
- Step 4 did not go green in one process; six cursor composition/audit
  nodes fail only under combined-suite pollution.
- Smoke from committed HEAD fails on a Change-local intake export gap.
- Two OpenCode restart/provider-delete rows remain explicit gaps.
- Several Task 26 mappings point at dirty, not-yet-committed Change-local
  tests; a clean checkout of this commit alone cannot audit those nodes.
- `tests/phase5/conformance.py` now owns manifests, evidence, audit, run,
  and admission in one file (plan-listed, but large).

## Fix round 1 — `generated-declaration-contribution-mismatch`

Independent review found this row mapped as `direct` to the happy-path
99-alias test. The product already fail-closes: generated
`DeploymentPlugin.contribute()` calls `validate_contribution()` and raises
`PluginContractError` when declaration IDs disagree with the live
contribution. No new workspace/publish behavior was added.

Resolution (option 1): added a Phase 5 node that drops one generated
declaration binding ID, leaves the live contribution intact, and asserts
`PluginContractError` / `binding declarations disagree`. Rematched the
row to that node. Counts stay 78 direct / 2 gaps;
`release_complete` stays `False`.

New node:

`tests/phase5/test_generated_declaration_mismatch.py::test_generated_provider_rejects_declaration_contribution_mismatch`

The new file is Task 4-owned so dirty Change-local
`tests/phase5/test_binding_builder.py` was left unstaged.

### TDD Evidence (RED then GREEN)

**RED** — rematch only; node file absent:

```bash
uv run pytest tests/phase5/test_phase5_final_fault_gate.py::test_original_task26_fault_rows_have_an_exact_closed_node_mapping tests/phase5/test_phase5_final_fault_gate.py::test_fault_evidence_classification_is_truthful_and_release_remains_blocked tests/phase5/test_phase5_final_repository_gate.py::test_all_final_gate_nodes_are_unique_auditable_and_collectable -q
```

```text
FAILED tests/phase5/test_phase5_final_fault_gate.py::test_original_task26_fault_rows_have_an_exact_closed_node_mapping
FAILED tests/phase5/test_phase5_final_repository_gate.py::test_all_final_gate_nodes_are_unique_auditable_and_collectable
2 failed, 1 passed, 1 warning in 0.24s
FileNotFoundError: .../tests/phase5/test_generated_declaration_mismatch.py
```

**GREEN** — added the fail-closed node:

```bash
uv run pytest tests/phase5/test_generated_declaration_mismatch.py::test_generated_provider_rejects_declaration_contribution_mismatch tests/phase5/test_phase5_final_fault_gate.py::test_original_task26_fault_rows_have_an_exact_closed_node_mapping tests/phase5/test_phase5_final_fault_gate.py::test_fault_evidence_classification_is_truthful_and_release_remains_blocked tests/phase5/test_phase5_final_repository_gate.py::test_all_final_gate_nodes_are_unique_auditable_and_collectable -q
```

```text
4 passed, 1 warning in 1.38s
```

### Covering tests

Files:

- `tests/phase5/test_generated_declaration_mismatch.py`
- `tests/phase5/test_phase5_final_fault_gate.py`
- `tests/phase5/test_phase5_final_security_gate.py`
- `tests/phase5/test_phase5_final_repository_gate.py`

Command:

```bash
uv run pytest tests/phase5/test_phase5_final_fault_gate.py tests/phase5/test_phase5_final_security_gate.py tests/phase5/test_phase5_final_repository_gate.py -q
```

Output:

```text
.............                                                            [100%]
13 passed, 1 warning in 318.85s (0:05:18)
```

The warning is the pre-existing `CompiledWorkflow.schema` field-name
shadow in `assurance_kernel`.

### Self-review

- Completeness: only the Important mismatch row was rematched; Minors
  were not touched; Task 3 admission was not fabricated; no live
  providers; no Change-local product patch.
- Quality: the mapped node injects declaration/contribution disagreement
  and asserts fail-closed rejection.
- Discipline: staged files are the rematch, the new node, and this
  report appendix.
- Residual: original Step 4 / smoke / Task 3 / explicit-gap concerns
  remain.
