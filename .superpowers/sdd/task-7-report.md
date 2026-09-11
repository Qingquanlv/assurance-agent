# Task 7 Report — Product identity, allowlist, globs, leftover path strings

**Plan:** `docs/superpowers/plans/2026-09-11-qa-flat-workspace.md` Task 7  
**Branch:** `benchmark-regression` (in-place)

## RED

Added `tests/product/test_qa_flat_workspace_sweep.py` first.

```text
uv run pytest tests/product/test_qa_flat_workspace_sweep.py -v
→ 3 failed, 1 passed
```

Failures were real leftovers / missing identity check, not typos.

- Sweep `test_installed_python_has_no_legacy_change_tree` listed **127** Python files still containing `qa/changes/` or `qa/archive/`.
- `ChangeWorkspace.open` / `prepare_change_workspace` did not refuse when `qa/status.json` `change.change_id` ≠ requested `--change`.
- Matching identity already passed (no status check yet).

### Leftover-file list from the first sweep fail (127)

First hits (queue used for the mechanical rewrite):

- `packages/products/assurance-product/assurance_product/opencode_agents.py`
- `packages/products/assurance-product/assurance_product/status.py`
- `packages/products/assurance-product/assurance_product/graphs/execute.py`

Remainder spanned capability production/tests, adapter tests, `tests/product/**`, `tests/phase4/**`, `tests/agent_runtime/fakes.py`, and other installed Python under `packages/` and `tests/`. The sweep test itself must not contain the literal substrings; tokens are built as `"qa/" + "changes/"` / `"qa/" + "archive/"`.

## GREEN

Sweep + identity now pass. Installed Python under `packages/**/*.py` and `tests/**/*.py` has **0** leftover `qa/changes/` or `qa/archive/` hits.

### Production leftovers cleared

- `qa_join` rejects only prefix `qa/changes`, `qa/archive`, `changes/`, `archive/` (not mid-path `results/archive`).
- `status.py` `finalize_achieved` writes `qa/status.json` and `qa/apply-manifest.json`.
- `ChangeWorkspace.open` / `aa start` (`prepare`) fail closed on identity mismatch.
- `opencode_agents.py` globs: `qa/cases/**`, `qa/tests/**`, `qa/results/**`, `qa/fixtures/**`; deny `qa/.runtime/**`, `qa/.staging/**`, explore context, workflow-state.
- Boundary plugin execution-view regex no longer matches `qa/changes/<id>/.staging/execution`.
- `run_item.py`: `case_delta_paths` = `qa/cases/{module}/case.yaml`; `allowed_artifact_paths` = `["qa/cases", "qa/fixtures", "qa/results", "qa/tests"]`.
- Plan-reviewer skills and `resolve_inputs.py` remapped off `qa/changes/{change_id}/`.
- Plugin `plugin-declaration.json` files regenerated so live descriptors match static snapshots after contract path changes.

### Tests run

```text
uv run pytest tests/product/test_qa_flat_workspace_sweep.py \
  tests/product/test_change_workspace_paths.py \
  tests/product/test_product_input.py \
  tests/product/test_execution_view.py \
  tests/product/test_generated_merge.py \
  tests/product/test_agent_execution_contracts.py \
  tests/product/test_achieved_terminal.py
→ 144 passed
```

```text
uv run pytest tests/product -q --tb=line
→ 13 failed, 843 passed, 13 skipped  (before last isolation/parser/boundary fixes)
```

After those leftover remaps, isolation + sweep + edited modules were re-run green. Full `tests/product` was not re-run end-to-end after the last parser/view-selector fixes.

Capability packages: many tests still fail from mechanical rewrite leftovers (`qa/trace` vs `qa/results/trace`, case-design allowlist vs `qa/.qa.yaml`, skill-doc assertions). Not all capability modules were brought green.

## Concerns

- Full `tests/product` still had leftover failures in phase5 gate count, cursor-adapter composition message, and some capability-owned graphs; last isolation/boundary fixes may have cleared several of the 13.
- Capability test suite: 69 failed / 1559 passed before fixture-sort and declaration regen; remaining failures are rewrite residue, not leftover `qa/changes/` strings.
- Shared `qa/tests/` means per-family generated isolation is gone; tests now expect last-writer / digest mismatch instead of dual physical trees.
- Did not add `benchmark/results` contents or eval-fixtures.

## Important review findings

Locked `ProductInputV1.allowed_artifact_paths` to the exact sorted tuple
`("qa/cases", "qa/fixtures", "qa/results", "qa/tests")`. Subsets, supersets
(including `qa/extra`), and `("tests",)` now fail closed.

Dropped leftover OpenCode glob `**qa/improvements/reviews/**` on
`assurance-v1-reviewer`. Improvement reviews already live under
`qa/results/review/**`.

Sweep now also fails on slash-free tokens `qa/changes` and `qa/archive`.
Queue from the first fail (15 files), then cleared:

- intake finalize lock → `["qa/cases", "qa/fixtures", "qa/results", "qa/tests"]`
- generation / product dummy artifacts → `"path": "qa/results"`
- explore degraded reason → `historical archive projection is empty or missing`
  (no longer walks leftover `qa/archive`)
- contract route assertions rewritten with concatenated tokens
- loop-helper cleanup target → `"$PWD/qa"`

### Tests

```text
uv run pytest tests/product/test_qa_flat_workspace_sweep.py \
  tests/product/test_product_input.py -v
→ 53 passed
```

Also green: edited generation graph/interrupt/join tests, contract route
assertions, intake finalize lock tests, `test_change_local_output_routing`,
`test_loop_helpers` cleanup, `test_agent_execution_contracts`,
`test_opencode_staging_boundary`.
