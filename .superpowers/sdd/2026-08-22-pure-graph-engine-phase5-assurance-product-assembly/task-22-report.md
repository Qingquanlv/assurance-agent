# Task 22 Report — Implement the Deterministic 25-Case Comparison Matrix

**Status:** DONE  
**Worktree:** `/Users/lvqingquan/agent/assurance-agent/.worktrees/pure-graph-engine-phase3-spec`  
**Branch:** `codex/pure-graph-engine-phase3-spec`  
**Does not amend:** `e474cee`

## Summary

The Phase 5 comparison matrix is the exact 25 IDs in `EXPECTED_25_CASE_IDS`, that order, unique. `ComparisonManifestV1` has `schema_version` `"1"`. Each case is a committed synthetic export pair under `tests/phase5/fixtures/comparison/{legacy,current}/<case-id>/`. Digests are `sha256:` plus 64 lowercase hex from harness `digest_directory` (export-root walk only). Legacy and current trees were generated in separate subprocesses; only immutable exports were copied into the matrix tree.

`run-comparison.sh` calls the isolated harness runner, not `assurance_product` / `graph_engine` / `assurance_agent`. It opens export roots and the disposition ledger only. Result: 25 executed, 25 governed passes, 0 missing, 0 extra, 0 undisposed mismatches. The only governed difference is `runtime_identity` (`legacy-aa` vs `assurance-product`), disposed with an exact case/field row. Stale and wildcard dispositions fail tests. Changing any governed projection field fails unless that field stays disposed. No pytest fixture named `request`. `aa` is not cut over. No live OpenCode/Cursor.

## TDD evidence

### RED (Step 2)

```bash
uv run pytest tests/phase5/test_comparison_matrix.py -q
```

```text
ERROR tests/phase5/test_comparison_matrix.py
AttributeError: module 'compare' has no attribute 'ComparisonManifestV1'
```

Failure reason: the exact matrix API and authenticated fixtures were absent (feature missing), not a typo.

### GREEN (Step 4)

```bash
bash benchmark/assurance-product-phase5/run-comparison.sh
```

```text
executed=25
governed_passes=25
missing=0
extra=0
undisposed_mismatches=0
schema_version=1
```

Stdout is one machine-readable JSON aggregate. Case IDs match `EXPECTED_25_CASE_IDS`.

### GREEN (Step 5)

```bash
uv run pytest tests/phase5/test_comparison_matrix.py tests/phase5/test_comparison_dispositions.py tests/phase5/test_comparison_isolation.py -q
```

```text
50 passed, 1 warning in 1.60s
```

The warning is the pre-existing kernel `CompiledWorkflow.schema` shadow. Focused `ruff check`, `ruff format --check`, and `pyright` on touched files: clean.

Related suite after format (`test_behavioral_projection.py` + ledgers + matrix + dispositions + isolation): `75 passed, 1 warning`.

Covered: exact 25-ID order from `EXPECTED_25_CASE_IDS` (not derived from the manifest under test); string schema `"1"`; authenticated `digest_directory` match; required arrays present in every fixture (`[]` allowed, omitted keys refused); 25 governed passes; all-four-family plan/review/codegen/execution evidence; report artifacts in the result tree; one mutation per `COMPARED_FIELDS` member; stale and wildcard disposition rejection; isolation AST scan; runner reads only export roots.

## Files changed

### Created

- `benchmark/assurance-product-phase5/comparison-manifest.json`
- `benchmark/assurance-product-phase5/run-comparison.sh`
- `benchmark/assurance-product-phase5/run_comparison.py`
- `benchmark/assurance-product-phase5/generate_comparison_fixtures.py`
- `tests/phase5/fixtures/comparison/legacy/<25 case ids>/`
- `tests/phase5/fixtures/comparison/current/<25 case ids>/`
- `tests/phase5/test_comparison_matrix.py`
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-22-report.md`

### Modified

- `benchmark/assurance-product-phase5/compare.py` — `ComparisonCaseV1` / `ComparisonManifestV1`
- `benchmark/assurance-product-phase5/projection.py` — `digest_directory` alias of `digest_export`
- `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml` — 25 exact `runtime_identity` rows, `status: compared`

## Self-review

- Public IDs are the `EXPECTED_25_CASE_IDS` literal. Manifest validation also requires `compare.CASE_IDS` in that order.
- Fixtures are synthetic Task 21 export trees (`manifest.json` + `result-tree/`). Every required array is written; omitted keys cannot project.
- Dispositions are exact `(case_id, field)` rows. Wildcards and stale rows fail `validate_disposition_ledger`.
- Isolation: new harness modules import stdlib, `pydantic`, `yaml` (runner), and sibling harness modules only.

## Residuals

1. **Synthetic comparison-shaped exports, not live `ResultExportV1`.** Controller allowed scripted fixtures that project through Task 21. No OpenCode/Cursor/`aa-next export` run.
2. **`digest_directory` is harness-local.** It aliases `digest_export` (sorted relative-path hash map). Isolation forbids importing Task 20 `workspace_tree_id`.
3. **`CASE_IDS` remains a second copy of `EXPECTED_25_CASE_IDS`.** Tests lock the committed manifest against the conformance literal.
4. **Only `runtime_identity` differs between sides.** Case semantics are identical on legacy and current so the matrix stays deterministic; each identity mismatch has an exact disposition.

## Commit

```bash
git add benchmark/assurance-product-phase5/comparison-manifest.json benchmark/assurance-product-phase5/run-comparison.sh tests/phase5/fixtures/comparison tests/phase5/test_comparison_matrix.py
git add benchmark/assurance-product-phase5/compare.py benchmark/assurance-product-phase5/projection.py benchmark/assurance-product-phase5/run_comparison.py benchmark/assurance-product-phase5/generate_comparison_fixtures.py
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/comparison-dispositions.yaml
git add -f .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/task-22-report.md
git commit -m "test(phase5): cover the 25-case comparison matrix"
```
