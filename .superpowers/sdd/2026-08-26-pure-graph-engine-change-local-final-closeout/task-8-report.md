# Task 8 Report — Delete the Legacy `assurance_agent` Distribution

## Status: DONE_WITH_CONCERNS

The `assurance_agent/` source tree is gone. Production metadata no longer
names that package. `importlib.util.find_spec("assurance_agent")` is `None`.
Capability/product wheels and `packages/assurance-kernel/` were not deleted.
Task 3 live OpenCode admission was not fabricated.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Committed HEAD at start: `a6734643d98ef8e010d8305cbcd95a7acdb1fb5c`
- Phase 4 inventory: `.superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction/phase6-deletion.txt` (240 `assurance_agent/` paths) plus `ownership.yaml`

## What I implemented

- Deleted the entire `assurance_agent/` tree.
- Removed `assurance_agent` from root `pyproject.toml` Pyright include and
  from `.importlinter` root packages, layer contracts, and forbidden lists.
- `uv.lock` had no `assurance-agent` package after Task 7; no lock rebuild.
- Replaced `tests/conftest.py` so collection no longer imports the deleted
  package. Deleted untracked `tests/phase6/test_conftest_product_guard.py`.
- Deleted three characterization tests that imported the deleted package.
- Removed the live legacy hook-comparison test from
  `tests/phase4/test_product_hooks_parity.py`.
- Phase 4 collectors now use frozen ownership tables instead of the live
  agent tree. Missing agent roots expand to empty.
- Residual evidence for Phase 6 Task 8 records this deletion.

## TDD Evidence (RED then GREEN)

**RED** — source, import, and conftest still resolved `assurance_agent`:

```bash
uv run pytest tests/phase6/test_assurance_agent_deleted.py -q
```

```text
FAILED tests/phase6/test_assurance_agent_deleted.py::test_assurance_agent_source_and_metadata_are_absent
FAILED tests/phase6/test_assurance_agent_deleted.py::test_assurance_agent_is_not_importable
FAILED tests/phase6/test_assurance_agent_deleted.py::test_conftest_does_not_import_assurance_agent
3 failed, 1 passed, 1 warning in 0.34s
```

Expected RED causes: `assurance_agent/` existed; `find_spec` returned a
source `ModuleSpec`; `tests/conftest.py` imported `assurance_agent.product`.
The passing test is the inventory mapping (replacement test or `obsolete`).

**Replacement tests before deletion** (Phase 4 inventory verification files
plus the Phase 6 plan Phase 5 extras):

```text
592 passed, 1 warning in 589.98s (0:09:49)
```

**GREEN** — after deletion and metadata/collection fixes:

```bash
uv run pytest tests/phase6/test_assurance_agent_deleted.py tests/phase6/test_workspace_manifest.py -q
```

```text
7 passed in 0.39s
```

`importlib.util.find_spec("assurance_agent")` is `None`. AST scan of
`production_runtime_files` (root metadata + capability/product/runtime
packages; kernel excluded until Task 9) found no production import.

## Inventory mapping

Every `assurance_agent/` path in `phase6-deletion.txt` maps through
`ownership.yaml` to a replacement test file or `obsolete`:

| Disposition | Proof |
|---|---|
| `migrate` with `verification` | that test file |
| `migrate` with owner | owner's plugin/contracts test |
| `replace_phase5` | `tests/phase5/test_full_graph_audit.py` |
| `delete_phase6` / `retain_harness` | `obsolete` |
| skill `SKILL.md` / persona `.md` | skill/persona ledger row |
| `operations_catalog.py` | `operation:run-tests` live pointer |

## Phase 4 / Phase 5 suite results

**Phase 4** (`uv run pytest tests/phase4 -q`):

```text
353 passed, 1 warning in 101.16s
```

The warning is the pre-existing `CompiledWorkflow.schema` field-name shadow.

**Phase 5** (`uv run pytest tests/phase5 -q`):

```text
7 failed, 641 passed in 1912.98s (0:31:52)
```

Combined-suite failures (all pass in isolation; same
`assurance_product_bindings_*` SourceSnapshotError family recorded by Task 4):

- `tests/phase5/test_binding_coverage.py::test_cursor_resolution_repeats_and_keeps_finalize_null`
- `tests/phase5/test_generated_declaration_mismatch.py::test_generated_provider_rejects_declaration_contribution_mismatch`
- `tests/phase5/test_graph_binding_audit.py::test_graph_bindings_are_closed_and_inventoried[cursor]`
- `tests/phase5/test_graph_binding_audit.py::test_graph_has_no_runtime_or_phase4_agent_targets[cursor]`
- `tests/phase5/test_graph_intake_and_triplets.py::test_workflow_compiles_under_both_product_providers[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_has_exact_provider_and_binding_closure[cursor]`
- `tests/phase5/test_product_composition.py::test_composition_selects_exact_plugin_and_product_identity[cursor]`

Isolated rerun of those seven nodes: `7 passed in 38.05s`.

## Files changed

Deleted:

- `assurance_agent/` (entire tree)
- `packages/assurance-execution/tests/test_execution_characterization.py`
- `packages/assurance-healing/tests/test_healing_characterization.py`
- `packages/assurance-improvement/tests/test_improvement_characterization.py`
- untracked `tests/phase6/test_conftest_product_guard.py`

Modified:

- `pyproject.toml`
- `.importlinter`
- `tests/conftest.py`
- `tests/phase6/conformance.py`
- `tests/phase6/test_workspace_manifest.py`
- `tests/phase4/ownership.py`
- `tests/phase4/test_ownership_ledger.py`
- `tests/phase4/test_product_hooks_parity.py`
- `tests/phase4/test_six_wheel_composition.py`
- `packages/assurance-quality/assurance_quality/contracts/sufficiency.py` (comment only)
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/residual-disposition.json`

Created:

- `tests/phase6/test_assurance_agent_deleted.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-8-report.md`

Not modified: `uv.lock` (no `assurance-agent` package after Task 7).
Not deleted: `packages/assurance-kernel/`, capability wheels, `assurance-product`.

## Self-review

- No production import resolves `assurance_agent`.
- `tests/conftest.py` does not import the deleted package.
- Kernel remains a workspace member for Task 9.
- No live OpenCode/Cursor was run. No Task 3 admission artifact was written.

## Concerns

- Phase 5 combined-suite cursor isolation still fails 7 nodes; they pass
  isolated. Task 4 already recorded this family. Not patched here.
- `uv.lock` is unchanged. The Files list named it; there was nothing to edit.
- Unit/integration tests under `tests/unit` and `tests/integration` still
  import the deleted package. They were not proven replacement tests and
  were left unstaged. Task 10/11 can retire them.
- Kernel production sources still mention `assurance_agent` (Task 9).
- `retain_harness` writing-skills resources lived under `assurance_agent/`
  and were deleted with the tree; mapped as `obsolete`.
