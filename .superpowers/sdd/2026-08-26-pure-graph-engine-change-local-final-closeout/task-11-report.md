# Task 11 Report — Consolidate Final Product Tests and Benchmark Names

## Status: DONE_WITH_CONCERNS

Product tests and the live benchmark were renamed to their final names, the
legacy-vs-current comparison surface was deleted after a mapping test, and the
final OpenCode-only manifest contract is asserted. Task 3 live OpenCode
admission was not fabricated. Cursor live stays absent.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Baseline HEAD: `39143602eb8b2ee4b296b87ed2bb7f7bc9613f7c`

## What I implemented

- Renamed `tests/phase5/` → `tests/product/` and
  `benchmark/assurance-product-phase5/` → `benchmark/assurance-product/` with
  `git mv`.
- Wrote a mapping test that pins retained comparison assertions onto existing
  product tests for STOP, interrupt, replay, coverage, healing, report, retro,
  and improvement.
- After that mapping was green, deleted comparison-only tests, both sides of
  `fixtures/comparison/`, and comparison executables (`compare.py`,
  `comparison-manifest.json`, `generate_comparison_fixtures.py`,
  `run-comparison.sh`, `run_comparison.py`).
- Updated live imports, CI/smoke fixture paths, capability-package imports, and
  constructed runner paths (`tests/product/fixtures/project-config`).
- Kept frozen Phase 5 SDD bytes: `_criterion_evidence` still cites
  `tests/phase5/...` when rebuilding `acceptance.json`. Live deletion proof
  now points at `tests/product/test_full_graph_audit.py`.
- Final live `benchmark/assurance-product/manifest.json` still has exactly one
  item (`RET-dept-management`, `opencode-http-v1`). Cursor live entries stay
  absent. `run-cursor.sh` and Cursor adapter packaging tests remain.
- Moved `COMPARED_FIELDS` / `SET_FIELDS` into `eval.py` so eval/projection keep
  working without `compare.py`. Did not reimplement comparison.

## What I tested and test results

Focused mapping/manifest (after implementation):

```text
uv run pytest tests/phase6/test_final_benchmark_manifest.py -q
5 passed in 0.04s
```

Focused gates/projection/layout after path updates:

```text
26 passed in 47.04s
tests/product/test_phase5_benchmark_change_layout.py: 8 passed in 2.20s
```

Required Task 11 suite (once, before the one-line `run_item.py` fixture-path
fix):

```text
uv run pytest tests/product tests/phase6/test_final_benchmark_manifest.py -q
11 failed, 548 passed in 1761.16s
```

Three of those eleven were this task: change-layout tests still copied
`tests/phase5/fixtures/project-config` via path parts. Fixed and re-verified
(8 passed). The other eight are leftover Cursor/composition and the Task 10
deleted packaging-smoke path inside the repository-gate nested suite. They
were not re-owned.

## TDD Evidence

**RED** — final names and comparison deletion not present yet:

```bash
uv run pytest tests/phase6/test_final_benchmark_manifest.py -q
```

```text
5 failed in 0.06s
```

Expected RED causes: `tests/product/test_stop_and_interrupts.py` missing;
`tests/phase5` still existed; `benchmark/assurance-product/manifest.json`
missing; runner files still under `assurance-product-phase5`.

**GREEN** — after `git mv`, import/path updates, comparison deletion, and
eval field-constant move:

```bash
uv run pytest tests/phase6/test_final_benchmark_manifest.py -q
```

```text
5 passed in 0.04s
```

## Files changed

Created:

- `tests/phase6/test_final_benchmark_manifest.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-11-report.md`

Renamed:

- `tests/phase5/` → `tests/product/`
- `benchmark/assurance-product-phase5/` → `benchmark/assurance-product/`

Deleted:

- `tests/product/test_comparison_{dispositions,isolation,matrix}.py`
- `tests/product/fixtures/comparison/` (legacy and current)
- `benchmark/assurance-product/{compare.py,comparison-manifest.json,generate_comparison_fixtures.py,run-comparison.sh,run_comparison.py}`

Modified (live path/import or naming only, plus the eval constant move):

- `tests/phase6/{conformance.py,test_cli_cutover.py,test_final_wheel_metadata.py}`
- `scripts/assurance_product_wheel_smoke_test.sh`
- capability tests importing `tests.product.test_change_local_output_routing`
- `benchmark/assurance-product/{run_item.py,run-opencode.sh,run-cursor.sh,eval.py}`
- product tests/helpers that referenced `tests.phase5` / `assurance-product-phase5`

Not staged: leftover dirty hunks already present under the old `tests/phase5/`
files, untracked `benchmark/assurance-product/results/`, untracked
`benchmark/assurance-product/tests/`, and
`tests/product/fixtures/project-config/.aa/capability-catalog.json`.

## Self-review findings

- Mapping test names the eight retained categories and checks real product
  test functions exist.
- Manifest test requires exactly one OpenCode item and no Cursor live ids.
- Frozen `phase6-handoff.json` / `acceptance.json` were not rewritten.
- `git mv` recorded the committed-content rename. Dirty files were staged with
  mechanical replacements only (`hash-object` / `update-index`). No
  directory-wide `git add`, no `git reset` / `git clean`.
- No live OpenCode/Cursor. No fabricated Task 3 admission.

## Issues or concerns

- The required combined suite is not fully green in this dirty worktree.
  After the layout-path fix the remaining failures are Cursor composition /
  binding isolation, one generated-declaration mismatch, and the repository
  gate nested suite still reading the Task 10-deleted
  `scripts/packaging_smoke_test.sh`. Those match previously recorded leftover
  work, not this rename.
- `eval.py` now carries the field lists it used to import from `compare.py`.
  Comparison behavior was not reintroduced.
- Full suite was run once as required; the one-line runner fixture-path fix
  was checked with the layout file only.
