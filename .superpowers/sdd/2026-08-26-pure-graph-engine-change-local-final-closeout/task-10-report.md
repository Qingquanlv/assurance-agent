# Task 10 Report — Enforce No-legacy Architecture and Final Wheel Isolation

## Status: DONE_WITH_CONCERNS

The repository/wheel no-legacy gate is committed and green. The 11-wheel
committed-HEAD smoke builds all closed wheels and installs `aa` (never the
retired console script or deleted distributions), then fails at
`aa bindings build` on the already-recorded intake import miss. Task 3 live
OpenCode admission was not fabricated. Pre-existing dirty Phase 4/5/package
files were left unstaged.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Baseline HEAD: `f17054159135cdb8f5f59a95bdc92b6fcff59f1c`
- Gate commit: `f0b0e5a test(cutover): add no-legacy repository gate`

## What I implemented

- `scripts/check_no_legacy.py` with `scan_repository(..., scope=runtime|repository)`,
  exact-path `load_allowlist`, AST import scan, metadata/token scan, leftover
  `tests/unit` + `tests/integration` import scan, and current-doc scan.
- `scripts/no_legacy_allowlist.txt`: 306 exact historical spec/evidence paths.
  No wildcards or directory-wide entries.
- Final smoke now requires installed `aa`, rejects the retired console script,
  runs `--scope runtime` on the archived tree, and keeps the scripts-package
  leak check from the deleted packaging smoke.
- Deleted `scripts/packaging_smoke_test.sh` and leftover live tests/helpers that
  imported the deleted packages. Rewrote `examples/minimal-product` so it no
  longer loads deleted modules or the old product entry group.
- CI runs `uv run python scripts/check_no_legacy.py --scope repository` and no
  longer calls the old packaging smoke.

## TDD Evidence (RED then GREEN)

**RED** — scanner missing; packaging smoke still present; smoke still invoked
the retired CLI:

```bash
uv run pytest tests/phase6/test_no_legacy_gate.py tests/phase6/test_final_wheel_metadata.py -q
```

```text
14 failed, 1 passed
```

Expected RED causes: `scripts.check_no_legacy` missing;
`scripts/packaging_smoke_test.sh` existed; CI still called it; smoke still ran
`aa-next compile` / `aa-next bindings`. The one passing test is product-wheel
metadata already owning `aa`.

**GREEN** — after the scanner, allowlist, leftover deletion, current-doc
scrub, smoke flip, and CI update:

```bash
uv run pytest tests/phase6/test_no_legacy_gate.py tests/phase6/test_final_wheel_metadata.py -q
```

```text
15 passed in 1.53s
```

Mutation tests reject production imports, retired console metadata, default
graph, `AA_RUNTIME=legacy`, leftover unit imports, wildcards, and
directory-wide allowlist entries. Exact allowlist exempts only the listed file.

## Step 4 — smoke from committed HEAD

Command (after `f0b0e5a`):

```bash
bash scripts/assurance_product_wheel_smoke_test.sh
```

```text
no-legacy runtime: OK
PRODUCT_WHEEL_FILES=graph_engine-0.1.0-py3-none-any.whl agent_runtime_contracts-0.1.0-py3-none-any.whl assurance_intake-0.1.0-py3-none-any.whl assurance_generation-0.1.0-py3-none-any.whl assurance_execution-0.1.0-py3-none-any.whl assurance_healing-0.1.0-py3-none-any.whl assurance_quality-0.1.0-py3-none-any.whl assurance_improvement-0.1.0-py3-none-any.whl assurance_product-0.1.0-py3-none-any.whl agent_runtime_opencode-0.1.0-py3-none-any.whl agent_runtime_cursor-0.1.0-py3-none-any.whl
WHEEL_ARCHIVES_OK
PREFIX=base-no-adapter ENTRY_POINTS=execution,generation,healing,improvement,intake,quality
ImportError: cannot import name 'MinimumCoverageMatrixAuthoring' from 'assurance_intake.contracts'
SMOKE_EXIT:1
```

11 wheels built from `git archive HEAD`. Base env installed `aa`. Failure is
the Task 4 recorded committed-HEAD intake import miss. The dirty worktree has
the type and re-export, but those files are pre-existing other-work and were
not staged.

## Step 5 — repository no-legacy

```bash
uv run python scripts/check_no_legacy.py --scope repository
```

```text
no-legacy repository: OK
```

Exit code: `0`.

## Files changed

Created:

- `scripts/check_no_legacy.py`
- `scripts/no_legacy_allowlist.txt`
- `tests/phase6/test_no_legacy_gate.py`
- `tests/phase6/test_final_wheel_metadata.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-10-report.md`

Modified:

- `scripts/assurance_product_wheel_smoke_test.sh`
- `.github/workflows/ci.yml`
- `README.md`, `AGENTS.md`, `docs/eval.md`, `docs/schemas.md`
- `packages/assurance-product/README.md`
- `examples/README.md`
- `examples/minimal-product/aa_sample/product.py`
- `examples/minimal-product/pyproject.toml`

Deleted:

- `scripts/packaging_smoke_test.sh`
- `scripts/capture_eval_fixture.py`
- leftover `tests/unit/**`, `tests/integration/**`, and helper modules that
  imported the deleted packages (451-file gate commit, mostly deletions)

## Self-review

- Installed smoke environments require `aa` and reject the retired console
  script plus deleted import/distribution names.
- Allowlist is exact files only.
- Gate committed before smoke. Smoke was run from that HEAD.
- No live OpenCode/Cursor. No fabricated Task 3 admission.
- Did not `git reset` / `git clean` or skip hooks.

## Concerns

- Committed-HEAD product smoke exits 1 on `MinimumCoverageMatrixAuthoring`.
  This is the same unresolved Task 4 gate item; fixing it requires dirty
  intake contract hunks that are not Task 10 files.
- README / AGENTS / eval / schemas were reduced to current-state text so the
  repository gate could pass. Task 12 still owns the full documentation rewrite.
- Combined-suite Cursor isolation failures recorded by Tasks 4/8/9 are
  unchanged.
